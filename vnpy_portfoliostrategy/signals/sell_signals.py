"""组合策略卖出信号器：情绪卖出优先，其次价格卖出（优先级组合）。

卖出总信号 ``SellAggregator`` 内部聚合一个优先级组合子信号 ``SellSubSignal``，
其按优先级短路运行两条独立子信号：

1. ``SectorSellSubSignal``（优先）：情绪卖出。收益低于 ``TIER_BREAKEVEN_PCT``
   （``profit < TIER_BREAKEVEN_PCT``，**含亏损**）且所属行业评级在中性以下（偏弱/极弱）
   → 全清：行业转弱即主动离场，不等价格止损挂掉（弱市里亏 0.5% 也走）。降级（不可用/
   未分行业/未映射）不触发。命中即短路，价格卖出不跑。
2. ``PriceSellSubSignal``（其次）：价格卖出。分档止损（10:00 前相对开仓价亏损
   ``WIDE_STOP_LOSS_PCT`` 即卖、10:00 后回到 ``STOP_LOSS_PCT``；情绪档不在岗
   （大盘快照过期/未加载，或该标的取不到行业评级）时全天按常规档，不放宽）、
   收益达 clear_profit_pct 清仓、达 half_profit_pct 仓位减半
   （仓位减半后剩余不足一个标准开仓量 fixed_size，据此不再重复减半）、回撤超
   MAX_DRAWDOWN_PCT 卖出，以及保底收益线（最大收益 >= 10% 保底 5%、>= 5% 保底 2%、
   >= 3% 保底 0%）。

减半产出 ``SELL``（部分卖出），其余卖出产出 ``CLEAR``（全清）。

需要持久化的全局状态（``entry_prices`` / ``max_profit_pct``）经 ``self.strategy`` 读写；
是否已减半由可卖量是否不足 ``fixed_size`` 推断，不另存标记。行业评级由
``SectorSellSubSignal`` 自持的 ``SectorSellSignal`` 实例取；大盘快照可用性由
``PriceSellSubSignal`` 自持的 ``MarketRiskOffSignal`` 取，用于决定止损档能否放宽。
``PrioritySellSubSignal.on_tick`` 在跑子信号前统一更新历史最大收益（公共前置），供两子信号共用。
"""

from __future__ import annotations

import math
from datetime import time
from typing import TYPE_CHECKING

from vnpy.trader.object import BarData, TickData

from .base import SignalAggregator, SignalResult, SignalType, SubSignal
from .sentiment_signals import (
    MarketRiskOffSignal,
    SectorSellSignal,
    format_sentiment_context,
)

if TYPE_CHECKING:
    from vnpy_portfoliostrategy.template import StrategyTemplate


class SectorSellSubSignal(SubSignal):
    """单标的情绪卖出子信号（优先）：收益低于阈值且行业偏弱 → 全清。

    ``on_tick`` 算 profit_pct，在 ``profit_pct < TIER_BREAKEVEN_PCT``（收益低于保底阈值，
    **含亏损**）且自持 ``SectorSellSignal`` 判定行业评级偏弱/极弱（``sellable``）时产出
    CLEAR：行业转弱即主动离场，不等价格档（弱市里亏 0.5% 也走）。收益 >= 阈值时交给
    价格卖出（减半/清仓止盈档）。降级（不可用/未分行业）``sellable`` 为 False，不触发。
    ``prev`` 忽略（优先级组合内的独立判定支）。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：持有行业应卖判定器与本次判定结果。"""
        super().__init__(vt_symbol, strategy)
        # 行业应卖判定器（自持，卖出侧独立于买入侧的 SectorBuySignal）
        self._sector: SectorSellSignal = SectorSellSignal(strategy)
        # 大盘情绪判定器（自持）：仅用于消息里带上大盘评级与分数
        self._market: MarketRiskOffSignal = MarketRiskOffSignal(strategy)
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

        # 情绪卖出在收益低于保底阈值时触发（含亏损）：行业转弱即主动离场，
        # 不等价格止损（弱市里亏 0.5% 也走），避免硬扁到 4%/2% 止损线
        if profit_pct >= s.TIER_BREAKEVEN_PCT:
            self._result = SignalResult()
            return self._result

        sector_result = self._sector.is_sellable(self.vt_symbol)
        if sector_result.sellable:
            market_level, market_score = self._market.market_values()
            context: str = format_sentiment_context(
                market_level,
                market_score,
                sector_result.sector_name,
                sector_result.sector_level,
                sector_result.sector_score,
            )
            self._result = SignalResult(
                type=SignalType.CLEAR,
                volume=sellable,
                price=tick.last_price - s.price_add,
                reason=(
                    f"行业情绪离场 收益{profit_pct * 100:.2f}% "
                    f"低于{s.TIER_BREAKEVEN_PCT * 100:.0f}% {context}"
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
    """单标的价格卖出子信号（其次）：分档止损 / 分档止盈 / 回撤 / 保底。

    ``on_tick`` 按优先级判定价格维度卖出规则，命中缓存首个结果，未命中 NONE。
    ``prev`` 忽略（优先级组合内的独立判定支）。判定无副作用：是否已减半由可卖量推断。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：初始化本次判定结果与情绪档在岗判定器。"""
        super().__init__(vt_symbol, strategy)
        self._result: SignalResult = SignalResult()
        # 大盘情绪判定器（自持）：取快照可用性
        self._market: MarketRiskOffSignal = MarketRiskOffSignal(strategy)
        # 行业情绪判定器（自持）：取该标的行业评级是否可用
        self._sector: SectorSellSignal = SectorSellSignal(strategy)

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """价格卖出判定，按优先级返回首个命中结果（prev 忽略）。"""
        s = self.strategy
        entry_price: float | None = s.entry_prices.get(self.vt_symbol, None)
        if not entry_price or entry_price <= 0 or not tick.last_price:
            self._result = SignalResult()
            return self._result

        profit_pct: float = (tick.last_price - entry_price) / entry_price
        sellable: int = s.get_sellable(self.vt_symbol)
        # 是否已减半：不另存标记，用可卖量判定——满仓时为 fixed_size，减半后必然小于它。
        # 走 T+1 时 sellable 已扣除卖出冻结量，减半委托在途（未成交）也算已减半，
        # 避免委托未成交期间价格反复越过阈值而重复减半；撤单/拒单释放冻结量后恢复满仓。
        is_halved: bool = 0 < sellable < s.fixed_size
        max_profit: float = s.max_profit_pct.get(self.vt_symbol, 0.0)
        sell_price: float = tick.last_price - s.price_add

        self._result = self._decide(
            profit_pct,
            max_profit,
            sellable,
            is_halved,
            sell_price,
            tick.datetime.time(),
            self._sentiment_ok(),
        )
        # 命中才拼情绪上下文：四项（大盘评级/分、行业评级/分）必带，便于日志与推送定位
        if self._result.type != SignalType.NONE:
            self._result = SignalResult(
                type=self._result.type,
                volume=self._result.volume,
                price=self._result.price,
                reason=f"{self._result.reason} {self._sentiment_context()}",
            )
        return self._result

    def on_bar(self, bar: BarData, prev: SignalResult) -> SignalResult:
        """K线推送回调：本信号走 tick，此处不处理，原样返回 prev。"""
        self._result = prev
        return prev

    def _sentiment_context(self) -> str:
        """拼装大盘 + 行业情绪上下文（四项）。仅在真正命中卖出时调用，避免每 tick 拼串。"""
        market_level, market_score = self._market.market_values()
        sector_name, sector_level, sector_score = self._sector.sector_values(self.vt_symbol)
        return format_sentiment_context(
            market_level, market_score, sector_name, sector_level, sector_score
        )

    def _sentiment_ok(self) -> bool:
        """情绪档是否在岗（大盘快照可信 + 该标的行业评级可用）。

        不在岗时情绪档永远不会触发，价格档不得放宽：止损需全天回落到常规档，
        否则就是"没数据时风险敞口更大"。
        """
        return self._market.snapshot_available() and self._sector.is_usable(self.vt_symbol)

    def _stop_loss_tier(self, tick_time: time, sentiment_ok: bool) -> tuple[float, str]:
        """取止损档，返回 (阈值, 档名)。

        情绪档在岗时按时段分档：10:00 前开盘宽档（容忍低开与早盘噪声），之后回到
        常规档；情绪档不在岗（大盘快照过期/未加载，或该标的取不到行业评级）时价格档
        不得放宽，全天按常规档。
        """
        s = self.strategy
        if sentiment_ok and tick_time < s.WIDE_STOP_END_TIME:
            return s.WIDE_STOP_LOSS_PCT, "开盘宽档"
        return s.STOP_LOSS_PCT, "常规档"

    def _decide(
        self,
        profit_pct: float,
        max_profit: float,
        sellable: int,
        is_halved: bool,
        sell_price: float,
        tick_time: time,
        sentiment_ok: bool,
    ) -> SignalResult:
        """按优先级完成价格卖出判定（无副作用），返回首个命中结果。

        ``tick_time`` / ``sentiment_ok`` 用于取止损档（见 ``_stop_loss_tier``）。
        """
        s = self.strategy

        # 1) 分档止损：相对开仓价亏损超当前档位阈值 → 全清
        stop_pct, tier = self._stop_loss_tier(tick_time, sentiment_ok)
        if profit_pct <= -stop_pct:
            return SignalResult(
                type=SignalType.CLEAR,
                volume=sellable,
                price=sell_price,
                reason=(
                    f"止损 亏损 {abs(profit_pct) * 100:.2f}% "
                    f"超过{tier} {stop_pct * 100:.0f}%"
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

        # 3) 减半止盈：收益达 half_profit_pct 且仓位未减半
        #    卖出量按 100 股向上取整；若取整后 >= 全部可卖量则跳过（避免等于清仓）
        if profit_pct >= s.half_profit_pct and not is_halved:
            half_vol: int = math.ceil(sellable / 2 / 100) * 100
            if 0 < half_vol < sellable:
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
