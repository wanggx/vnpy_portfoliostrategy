"""组合策略买入信号器：拉升检测（窗口/昨收，OR）+ 行业情绪过滤（子信号链）。

买入总信号 ``BuyAggregator`` 内部按序聚合子信号，链式传递结果：

1. ``OrCompositeSubSignal``（链首，OR 组合）：内含两条独立的拉升检测子信号，
   任一命中即产出 BUY 意向（不含行业评级）：
   - ``WindowSurgeSubSignal``：维护价格窗口，窗口内相对最低价涨幅 >= surge_pct 命中。
   - ``DayGainSubSignal``：相对昨收涨幅 >= surge_pct 命中。
2. ``SectorBuySubSignal``（链次）：前序有 BUY 意向才查行业情绪；评级中性及以上则保留 BUY
   并补行业评级进 reason；否则记拦截日志/企微 + entered.add，否定为 NONE（短路后续子信号）。

需要持久化的全局状态（``entered`` 去重集合、标的池）经 ``self.strategy`` 访问；
子信号只持有价格窗口、行业判定器实例等运行时状态。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING

from vnpy.trader.object import BarData, TickData

from .base import SignalAggregator, SignalResult, SignalType, SubSignal
from .sentiment_signals import SectorBuySignal

if TYPE_CHECKING:
    from vnpy_portfoliostrategy.template import StrategyTemplate


def _check_buyable_context(s, vt_symbol: str, tick: TickData) -> bool:
    """买入检测公共前置：当日池内、未 entered、价格有效才允许检测。

    返回 True 表示可继续检测；False 表示不满足前置（子信号应返回 NONE）。
    抽出为公共函数，供窗口拉升与昨收涨幅两个子信号复用，避免重复。
    """
    if vt_symbol not in s.target_symbols:
        return False
    if vt_symbol in s.entered:
        return False
    if not tick.last_price or tick.last_price <= 0:
        return False
    return True


def _buy_result(s, tick: TickData, reason: str, gain: float) -> SignalResult:
    """构造买入意向结果（不含行业评级，由链次 SectorBuySubSignal 补）。"""
    buy_price: float = tick.last_price + s.price_add
    return SignalResult(
        type=SignalType.BUY,
        volume=s.fixed_size,
        price=buy_price,
        reason=(
            f"{reason}涨幅 {gain * 100:.2f}% "
            f"买入价格 {buy_price:.2f} 数量 {s.fixed_size}"
        ),
    )


class WindowSurgeSubSignal(SubSignal):
    """单标的窗口拉升子信号：窗口内相对最低价涨幅 >= surge_pct 命中。

    ``on_tick`` 维护价格窗口样本并判定窗口拉升，命中缓存 BUY 意向，未命中 NONE。
    ``prev`` 忽略（OR 组合内的独立判定支）。窗口样本是该子信号的运行时状态。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：初始化价格窗口与本次判定结果。"""
        super().__init__(vt_symbol, strategy)
        # 价格窗口：deque[(datetime, price)]，仅保留窗口内样本
        self.window: deque = deque()
        self._result: SignalResult = SignalResult()

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """维护窗口样本并判定窗口拉升，缓存结果（prev 忽略）。"""
        s = self.strategy
        if not _check_buyable_context(s, self.vt_symbol, tick):
            self._result = SignalResult()
            return self._result

        # 维护窗口内样本，弹出早于 surge_window 秒前的数据
        self.window.append((tick.datetime, tick.last_price))
        cutoff: datetime = tick.datetime
        while self.window and (cutoff - self.window[0][0]).total_seconds() > s.surge_window:
            self.window.popleft()

        # 窗口拉升：窗口内相对最低价涨幅 >= surge_pct
        window_gain: float | None = None
        if (cutoff - self.window[0][0]).total_seconds() >= s.MIN_SURGE_SPAN:
            low_price: float = min(price for _, price in self.window if price > 0)
            if low_price > 0:
                window_gain = (tick.last_price - low_price) / low_price

        if window_gain is not None and window_gain >= s.surge_pct:
            self._result = _buy_result(s, tick, "窗口", window_gain)
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


class DayGainSubSignal(SubSignal):
    """单标的昨收涨幅子信号：相对昨收涨幅 >= surge_pct 命中。

    ``on_tick`` 判定相对昨收涨幅，命中缓存 BUY 意向，未命中 NONE。
    ``prev`` 忽略（OR 组合内的独立判定支）。无运行时状态。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：初始化本次判定结果。"""
        super().__init__(vt_symbol, strategy)
        self._result: SignalResult = SignalResult()

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """判定相对昨收涨幅，缓存结果（prev 忽略）。"""
        s = self.strategy
        if not _check_buyable_context(s, self.vt_symbol, tick):
            self._result = SignalResult()
            return self._result

        # 昨收涨幅：相对昨收涨幅 >= surge_pct
        day_gain: float | None = None
        if tick.pre_close > 0:
            day_gain = (tick.last_price - tick.pre_close) / tick.pre_close

        if day_gain is not None and day_gain >= s.surge_pct:
            self._result = _buy_result(s, tick, "昨收", day_gain)
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


class OrCompositeSubSignal(SubSignal):
    """OR 组合子信号：内含一组独立判定支，任一命中即返回（短路）。

    ``on_tick`` 把 ``prev`` 传给每个子信号并按序运行，任一返回非 NONE 即作为本组合产出
    立即返回、后续子信号不跑（OR 短路）；全部 NONE 则返回 NONE。对链来说是一个子信号
    节点。子信号工厂由类属性 ``sub_factories`` 提供。
    """

    # 子类覆盖：OR 组合内的子信号工厂列表
    sub_factories: list = []

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：按 sub_factories 创建子信号实例并初始化结果缓存。"""
        super().__init__(vt_symbol, strategy)
        self._subs: list[SubSignal] = [f(vt_symbol, strategy) for f in self.sub_factories]
        self._result: SignalResult = SignalResult()

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """按序运行子信号，任一非 NONE 即返回（OR 短路），后续不跑。"""
        for sub in self._subs:
            r: SignalResult = sub.on_tick(tick, prev)
            if r.type != SignalType.NONE:
                self._result = r
                return r
        self._result = SignalResult()
        return self._result

    def on_bar(self, bar: BarData, prev: SignalResult) -> SignalResult:
        """K线推送回调：本信号走 tick，此处不处理，原样返回 prev。"""
        self._result = prev
        return prev

    def signal_result(self) -> SignalResult:
        """返回 ``on_tick`` 已缓存的判定结果（零计算）。"""
        return self._result


class SurgeBuyOrSignal(OrCompositeSubSignal):
    """拉升检测 OR 组合：窗口拉升 + 昨收涨幅，任一命中即产出 BUY 意向。"""

    sub_factories = [WindowSurgeSubSignal, DayGainSubSignal]


class SectorBuySubSignal(SubSignal):
    """单标的行业过滤子信号（链次）：前序有 BUY 意向才查行业。

    ``on_tick`` 见 ``prev`` 为 BUY 才查所属行业情绪：评级中性及以上则保留 BUY 并补行业
    评级进 reason；否则记拦截日志/企微 + ``entered.add``，否定为 NONE（短路后续子信号）。
    ``prev`` 非 BUY 时直接返回 NONE（不查行业，避免每 tick 刷屏）。复用 ``SectorBuySignal``
    做行业评级判定。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：持有行业情绪判定器与本次判定结果。"""
        super().__init__(vt_symbol, strategy)
        self._sector: SectorBuySignal = SectorBuySignal(strategy)
        self._result: SignalResult = SignalResult()

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """前序有 BUY 意向才查行业；不可买则拦截+否定，可买则保留并补评级。"""
        # 前序无买入意向，不查行业（短路：避免每 tick 刷屏）
        if prev.type != SignalType.BUY:
            self._result = SignalResult()
            return self._result

        s = self.strategy
        sector_result = self._sector.is_buyable(self.vt_symbol)

        # 行业拦截：记日志/企微 + 标记本轮已处理，否定买入
        if not sector_result.buyable:
            name: str = s._get_symbol_name(self.vt_symbol)
            buy_price: float = tick.last_price + s.price_add
            skip_msg: str = (
                f"行业情绪拦截 {self.vt_symbol}({name}) "
                f"买入价格 {buy_price:.2f} "
                f"行业 {sector_result.sector_name} 得分 {sector_result.sector_score:.1f} "
                f"评级 {sector_result.sector_level}，未达中性以上，不买入"
            )
            s.write_log(skip_msg)
            s.send_wecom(skip_msg)
            # 标记本轮已处理，避免同一波拉升反复拦截刷屏
            s.entered.add(self.vt_symbol)
            s.put_event()
            self._result = SignalResult()
            return self._result

        # 可买：保留 prev 的 BUY，并补行业评级进 reason
        self._result = SignalResult(
            type=prev.type,
            volume=prev.volume,
            price=prev.price,
            reason=(
                f"{prev.reason} "
                f"行业 {sector_result.sector_name} 得分 {sector_result.sector_score:.1f} "
                f"评级 {sector_result.sector_level}"
            ),
        )
        return self._result

    def on_bar(self, bar: BarData, prev: SignalResult) -> SignalResult:
        """K线推送回调：本信号走 tick，此处不处理，原样返回 prev。"""
        self._result = prev
        return prev

    def signal_result(self) -> SignalResult:
        """返回 ``on_tick`` 已缓存的判定结果（零计算）。"""
        return self._result


class BuyAggregator(SignalAggregator):
    """买入总信号：按 vt_symbol 聚合拉升检测 + 行业过滤子信号链，分发 tick、取链终态结果。

    链序：``SurgeBuyOrSignal``（拉升检测 OR 组合）→ ``SectorBuySubSignal``（行业过滤）。
    """

    sub_factories = [SurgeBuyOrSignal, SectorBuySubSignal]
