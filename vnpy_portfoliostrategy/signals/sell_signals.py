"""组合策略卖出信号器：情绪卖出优先，其次价格卖出（优先级组合）。

卖出总信号 ``SellAggregator`` 内部聚合一个优先级组合子信号 ``SellSubSignal``，
其按优先级短路运行两条独立子信号：

1. ``SectorSellSubSignal``（优先）：情绪卖出。还有收益但低于 ``TIER_BREAKEVEN_PCT``
   （``0 <= profit < TIER_BREAKEVEN_PCT``）且所属行业评级在中性以下（偏弱/极弱）→ 全清；
   亏损交给价格止损，避免抢先绕过 2% 止损线。降级（不可用/未分行业/未映射）不触发。
   命中即短路，价格卖出不跑。
2. ``PriceSellSubSignal``（其次）：价格卖出。相对开仓价亏损 2% 止损、收益达
   clear_profit_pct 清仓、达 half_profit_pct 仓位减半（每标的仅一次）、回撤超
   MAX_DRAWDOWN_PCT 卖出，以及保底收益线（最大收益 >= 10% 保底 5%、>= 5% 保底 2%、
   >= 3% 保底 0%）。

减半产出 ``SELL``（部分卖出），其余卖出产出 ``CLEAR``（全清）。

需要持久化的全局状态（``entry_prices`` / ``max_profit_pct`` / ``halved``）经
``self.strategy`` 读写；行业评级由 ``SectorSellSubSignal`` 自持的 ``SectorSellSignal`` 实例取。
``PrioritySellSubSignal.on_tick`` 在跑子信号前统一更新历史最大收益（公共前置），供两子信号共用。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from vnpy.trader.object import BarData, TickData

from .base import SignalAggregator, SignalResult, SignalType, SubSignal
from .sentiment_signals import SectorSellSignal

if TYPE_CHECKING:
    from vnpy_portfoliostrategy.template import StrategyTemplate


class SectorSellSubSignal(SubSignal):
    """单标的情绪卖出子信号（优先）：还有收益但低于阈值且行业偏弱 → 全清。

    ``on_tick`` 算 profit_pct，仅在 ``0 <= profit_pct < TIER_BREAKEVEN_PCT``（有收益但
    低于保底阈值）且自持 ``SectorSellSignal`` 判定行业评级偏弱/极弱（``sellable``）时产出
    CLEAR；亏损（profit < 0）交给价格止损处理，避免抢先绕过 2% 止损线。其余 NONE
    （交价格卖出）。降级（不可用/未分行业）``sellable`` 为 False，不触发。``prev`` 忽略
    （优先级组合内的独立判定支）。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：持有行业应卖判定器与本次判定结果。"""
        super().__init__(vt_symbol, strategy)
        # 行业应卖判定器（自持，卖出侧独立于买入侧的 SectorBuySignal）
        self._sector: SectorSellSignal = SectorSellSignal(strategy)
        self._result: SignalResult = SignalResult()

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """情绪卖出判定：收益低于阈值且行业偏弱 → CLEAR，否则 NONE（prev 忽略）。"""
        s = self.strategy
        entry_price: float | None = s.entry_prices.get(self.vt_symbol, None)
        if not entry_price or entry_price <= 0 or not tick.last_price:
            self._result = SignalResult()
            return self._result

        profit_pct: float = (tick.last_price - entry_price) / entry_price
        sellable: int = s.get_sellable(self.vt_symbol)

        # 情绪卖出仅在"还有收益但低于保底阈值"时触发（0 <= profit < TIER_BREAKEVEN_PCT）；
        # 亏损时交给价格止损处理，避免情绪卖出抢先绕过 2% 止损线
        if profit_pct < 0 or profit_pct >= s.TIER_BREAKEVEN_PCT:
            self._result = SignalResult()
            return self._result

        sector_result = self._sector.is_sellable(self.vt_symbol)
        if sector_result.sellable:
            self._result = SignalResult(
                type=SignalType.CLEAR,
                volume=sellable,
                price=tick.last_price - s.price_add,
                reason=(
                    f"行业情绪{sector_result.sector_level} "
                    f"收益{profit_pct * 100:.2f}% "
                    f"低于{s.TIER_BREAKEVEN_PCT * 100:.0f}% 中性以下离场"
                ),
            )
        else:
            self._result = SignalResult()
        return self._result

    def on_bar(self, bar: BarData, prev: SignalResult) -> SignalResult:
        """K线推送回调：本信号走 tick，此处不处理，原样返回 prev。"""
        self._result = prev
        return prev

    def signal_result(self) -> SignalResult:
        """返回 ``on_tick`` 已缓存的判定结果（零计算）。"""
        return self._result


class PriceSellSubSignal(SubSignal):
    """单标的价格卖出子信号（其次）：止损 / 分档止盈 / 回撤 / 保底。

    ``on_tick`` 按优先级判定价格维度卖出规则，命中缓存首个结果（含 ``halved.add`` 副作用），
    未命中 NONE。``prev`` 忽略（优先级组合内的独立判定支）。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：初始化本次判定结果。"""
        super().__init__(vt_symbol, strategy)
        self._result: SignalResult = SignalResult()

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """价格卖出判定，按优先级返回首个命中结果（prev 忽略）。"""
        s = self.strategy
        entry_price: float | None = s.entry_prices.get(self.vt_symbol, None)
        if not entry_price or entry_price <= 0 or not tick.last_price:
            self._result = SignalResult()
            return self._result

        profit_pct: float = (tick.last_price - entry_price) / entry_price
        sellable: int = s.get_sellable(self.vt_symbol)
        is_halved: bool = self.vt_symbol in s.halved
        max_profit: float = s.max_profit_pct.get(self.vt_symbol, 0.0)
        sell_price: float = tick.last_price - s.price_add

        self._result = self._decide(profit_pct, max_profit, sellable, is_halved, sell_price)
        return self._result

    def on_bar(self, bar: BarData, prev: SignalResult) -> SignalResult:
        """K线推送回调：本信号走 tick，此处不处理，原样返回 prev。"""
        self._result = prev
        return prev

    def _decide(
        self,
        profit_pct: float,
        max_profit: float,
        sellable: int,
        is_halved: bool,
        sell_price: float,
    ) -> SignalResult:
        """按优先级完成价格卖出判定（含副作用），返回首个命中结果。"""
        s = self.strategy

        # 1) 固定止损：相对开仓价亏损超 STOP_LOSS_PCT → 全清
        if profit_pct <= -s.STOP_LOSS_PCT:
            return SignalResult(
                type=SignalType.CLEAR,
                volume=sellable,
                price=sell_price,
                reason=(
                    f"止损 亏损 {abs(profit_pct) * 100:.2f}% "
                    f"超过 {s.STOP_LOSS_PCT * 100:.0f}%"
                ),
            )

        # 2) 清仓止盈：收益达 clear_profit_pct → 全清
        if profit_pct >= s.clear_profit_pct:
            return SignalResult(
                type=SignalType.CLEAR,
                volume=sellable,
                price=sell_price,
                reason=(
                    f"清仓 收益 {profit_pct * 100:.2f}% "
                    f"达到 {s.clear_profit_pct * 100:.0f}%"
                ),
            )

        # 3) 减半止盈：收益达 half_profit_pct 且未减半（每标的仅触发一次）
        #    卖出量按 100 股向上取整；若取整后 >= 全部可卖量则跳过（避免等于清仓）
        if profit_pct >= s.half_profit_pct and not is_halved:
            half_vol: int = math.ceil(sellable / 2 / 100) * 100
            if 0 < half_vol < sellable:
                s.halved.add(self.vt_symbol)
                return SignalResult(
                    type=SignalType.SELL,
                    volume=half_vol,
                    price=sell_price,
                    reason=(
                        f"减半 收益 {profit_pct * 100:.2f}% "
                        f"达到 {s.half_profit_pct * 100:.0f}% 卖 {half_vol}"
                    ),
                )

        # 4) 回撤止盈：相对最大收益回撤超 MAX_DRAWDOWN_PCT → 全清
        drawdown: float = max_profit - profit_pct
        if drawdown >= s.MAX_DRAWDOWN_PCT:
            return SignalResult(
                type=SignalType.CLEAR,
                volume=sellable,
                price=sell_price,
                reason=(
                    f"回撤 {drawdown * 100:.2f}% "
                    f"超过 {s.MAX_DRAWDOWN_PCT * 100:.0f}%"
                ),
            )

        # 5) 保底收益线（取最高适用档）
        floor: float | None = None
        if max_profit >= s.TIER_HIGH_PCT:
            floor = s.TIER_HIGH_FLOOR
        elif max_profit >= s.TIER_LOW_PCT:
            floor = s.TIER_LOW_FLOOR
        elif max_profit >= s.TIER_BREAKEVEN_PCT:
            floor = s.TIER_BREAKEVEN_FLOOR

        if floor is not None and profit_pct < floor:
            return SignalResult(
                type=SignalType.CLEAR,
                volume=sellable,
                price=sell_price,
                reason=f"跌破保底 {floor * 100:.0f}%",
            )

        return SignalResult()

    def signal_result(self) -> SignalResult:
        """返回 ``on_tick`` 已缓存的判定结果（零计算）。"""
        return self._result


class PriorityCompositeSubSignal(SubSignal):
    """优先级组合子信号：按序短路运行一组子信号，首个非 NONE 即返回。

    ``on_tick`` 把 ``prev`` 传给每个子信号并按序运行；首个返回非 NONE 的子信号结果即作为
    本组合产出，后续子信号不跑（短路）。全部 NONE 则返回 NONE。对链来说是一个子信号节点。
    在跑子信号前统一更新历史最大收益（公共前置），供各子信号共用。子信号工厂由类属性
    ``sub_factories`` 提供。
    """

    # 子类覆盖：按优先级排列的子信号工厂列表
    sub_factories: list = []

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：按 sub_factories 创建子信号实例并初始化结果缓存。"""
        super().__init__(vt_symbol, strategy)
        self._subs: list[SubSignal] = [f(vt_symbol, strategy) for f in self.sub_factories]
        self._result: SignalResult = SignalResult()

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """公共前置更新最大收益后，按序短路运行子信号，取首个非 NONE 结果。"""
        self._update_max_profit(tick)
        result: SignalResult = SignalResult()
        for sub in self._subs:
            r: SignalResult = sub.on_tick(tick, prev)
            if r.type != SignalType.NONE:
                result = r
                break
        self._result = result
        return result

    def _update_max_profit(self, tick: TickData) -> None:
        """公共前置：更新历史最大收益率（写策略 max_profit_pct + 持久化）。"""
        s = self.strategy
        entry_price: float | None = s.entry_prices.get(self.vt_symbol, None)
        if not entry_price or entry_price <= 0 or not tick.last_price:
            return
        profit_pct: float = (tick.last_price - entry_price) / entry_price
        max_profit: float = s.max_profit_pct.get(self.vt_symbol, 0.0)
        if profit_pct > max_profit:
            s.max_profit_pct[self.vt_symbol] = profit_pct
            s._sync_tracking_state()

    def on_bar(self, bar: BarData, prev: SignalResult) -> SignalResult:
        """K线推送回调：本信号走 tick，此处不处理，原样返回 prev。"""
        self._result = prev
        return prev

    def signal_result(self) -> SignalResult:
        """返回 ``on_tick`` 已缓存的判定结果（零计算）。"""
        return self._result


class SellSubSignal(PriorityCompositeSubSignal):
    """卖出子信号：情绪卖出优先，其次价格卖出（优先级短路）。

    优先级序：``SectorSellSubSignal``（情绪卖出）→ ``PriceSellSubSignal``（价格卖出）。
    情绪命中即短路，价格卖出不跑。
    """

    sub_factories = [SectorSellSubSignal, PriceSellSubSignal]


class SellAggregator(SignalAggregator):
    """卖出总信号：按 vt_symbol 聚合卖出子信号链，分发 tick、取链终态结果。"""

    sub_factories = [SellSubSignal]
