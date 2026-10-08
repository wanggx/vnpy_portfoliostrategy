"""组合策略买入信号器：拉升检测（窗口/昨收，OR）+ 行业情绪过滤（子信号链）。

买入总信号 ``BuyAggregator`` 内部按序聚合子信号，链式传递结果：

1. ``OrCompositeSubSignal``（链首，OR 组合）：内含两条独立的拉升检测子信号，
   任一命中即产出 BUY 意向（不含行业评级）：
   - ``WindowSurgeSubSignal``：维护价格窗口，窗口内相对最低价涨幅 >= surge_pct 命中。
   - ``DayGainSubSignal``：相对昨收涨幅 >= surge_pct 命中。
2. ``SectorBuySubSignal``（链次）：前序有 BUY 意向才查大盘 + 行业情绪。大盘评级在中性
   以下（偏弱 / 极弱）→ **大盘一票否决，任何标的都不买入**（只按日记一次拦截日志 / 企微，
   不写 ``entered``，大盘当天转好后标的再起拉升仍可买入）。其余大盘评级下按档看行业：
   大盘评级中性以上（快照可用）时行业需中性及以上，大盘评级中性（含大盘快照不可用）时
   行业需中性以上（不含中性）。达标则保留 BUY 并把大盘 / 行业评级补进 reason；行业不达标
   则记拦截日志 / 企微 + ``entered.add``，否定为 NONE（短路后续子信号）。

需要持久化的全局状态（``entered`` 去重集合、标的池）经 ``self.strategy`` 访问；
子信号只持有价格窗口、行业判定器实例等运行时状态。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING

from vnpy.trader.object import BarData, TickData

from .base import SignalAggregator, SignalResult, SignalType, SubSignal
from .sentiment_signals import (
    SectorBuyResult,
    SectorBuySignal,
    format_market_context,
    format_sentiment_context,
)

if TYPE_CHECKING:
    from vnpy_portfoliostrategy.template import StrategyTemplate


def _check_buyable_context(s, vt_symbol: str, tick: TickData) -> bool:
    """买入检测公共前置：当日池内、未 entered、价格有效、不在冷却期才允许检测。

    返回 True 表示可继续检测；False 表示不满足前置（子信号应返回 NONE）。
    抽出为公共函数，供窗口拉升与昨收涨幅两个子信号复用，避免重复。
    """
    if vt_symbol not in s.target_symbols:
        return False
    if vt_symbol in s.entered:
        return False
    if not tick.last_price or tick.last_price <= 0:
        return False
    # 亏损离场冷却期内不再买入（策略持久化的是止损日，窗口按 COOLDOWN_DAYS 现算），
    # 避免"清仓→再买→再清"反复止损
    if s.is_buy_cooldown(vt_symbol, tick.datetime.strftime("%Y%m%d")):
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
    """单标的情绪过滤子信号（链次）：前序有 BUY 意向才查大盘 + 行业。

    ``on_tick`` 见 ``prev`` 为 BUY 才查大盘与所属行业情绪：

    - 大盘评级在中性以下（偏弱 / 极弱）：大盘一票否决，**任何标的都不买入**。此时不查行业，
      按日去重记一次"大盘情绪拦截"日志 / 企微，否定为 NONE；**不写 ``entered``**，大盘当天
      转好后该标的再起拉升仍可买入。
    - 大盘其余评级：大盘评级中性以上（快照可用）时行业需中性及以上、大盘评级中性（含大盘
      快照不可用）时行业需中性以上，达标则保留 BUY 并把大盘 / 行业评级补进 reason；行业不
      达标则记拦截日志 / 企微 + ``entered.add``，否定为 NONE（短路后续子信号）。

    ``prev`` 非 BUY 时直接返回 NONE（不查情绪，避免每 tick 刷屏）。复用 ``SectorBuySignal``
    做大盘 + 行业评级判定。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：持有行业情绪判定器、本次判定结果与大盘拦截的按日去重标记。"""
        super().__init__(vt_symbol, strategy)
        self._sector: SectorBuySignal = SectorBuySignal(self.strategy)
        self._result: SignalResult = SignalResult()
        # 大盘拦截消息已推送的日期（YYYYMMDD）：大盘拦截可能持续整日、每 tick 都命中，
        # 按日去重避免同一标的反复刷日志 / 企微
        self._market_block_date: str = ""

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """前序有 BUY 意向才查大盘+行业；大盘/行业不可买则拦截+否定，可买则保留并补评级。"""
        # 前序无买入意向，不查大盘/行业（短路：避免每 tick 刷屏）
        if prev.type != SignalType.BUY:
            self._result = SignalResult()
            return self._result

        s = self.strategy
        sector_result = self._sector.is_buyable(self.vt_symbol)

        # 大盘拦截：大盘情绪中性以下时不买入任何标的（不影响 entered，当日转好仍可买入）
        if sector_result.market_blocked:
            self._log_market_block(tick, sector_result)
            self._result = SignalResult()
            return self._result

        # 行业拦截：记日志/企微 + 标记本轮已处理，否定买入
        if not sector_result.buyable:
            name: str = s._get_symbol_name(self.vt_symbol)
            buy_price: float = tick.last_price + s.price_add
            context: str = format_sentiment_context(
                sector_result.market_level,
                sector_result.market_score,
                sector_result.sector_name,
                sector_result.sector_level,
                sector_result.sector_score,
            )
            skip_msg: str = (
                f"行业情绪拦截 {self.vt_symbol}({name}) "
                f"买入价格 {buy_price:.2f} {context}，未达准入，不买入"
            )
            s.write_log(skip_msg)
            s.send_wecom(skip_msg)
            # 标记本轮已处理，避免同一波拉升反复拦截刷屏
            s.entered.add(self.vt_symbol)
            s.put_event()
            self._result = SignalResult()
            return self._result

        # 可买：保留 prev 的 BUY，并补大盘/行业情绪上下文进 reason
        context: str = format_sentiment_context(
            sector_result.market_level,
            sector_result.market_score,
            sector_result.sector_name,
            sector_result.sector_level,
            sector_result.sector_score,
        )
        self._result = SignalResult(
            type=prev.type,
            volume=prev.volume,
            price=prev.price,
            reason=f"{prev.reason} {context}",
        )
        return self._result

    def _log_market_block(self, tick: TickData, sector_result: SectorBuyResult) -> None:
        """大盘中性以下拦截：按日去重记一次日志 + 企微（同一标的当日只推一条）。"""
        today: str = tick.datetime.strftime("%Y%m%d")
        if self._market_block_date == today:
            return
        self._market_block_date = today

        s = self.strategy
        name: str = s._get_symbol_name(self.vt_symbol)
        buy_price: float = tick.last_price + s.price_add
        context: str = format_market_context(
            sector_result.market_level, sector_result.market_score
        )
        skip_msg: str = (
            f"大盘情绪拦截 {self.vt_symbol}({name}) "
            f"买入价格 {buy_price:.2f} {context}，大盘中性以下不买入任何标的"
        )
        s.write_log(skip_msg)
        s.send_wecom(skip_msg)

    def on_bar(self, bar: BarData, prev: SignalResult) -> SignalResult:
        """K线推送回调：本信号走 tick，此处不处理，原样返回 prev。"""
        self._result = prev
        return prev

    def signal_result(self) -> SignalResult:
        """返回 ``on_tick`` 已缓存的判定结果（零计算）。"""
        return self._result


class CashGateSubSignal(SubSignal):
    """单标的资金上限子信号（链尾）：前序有 BUY 意向才做资金校验。

    ``on_tick`` 见 ``prev`` 为 BUY 才校验本策略总占用资金（``context.deployed_cash`` =
    持仓成本 + 在途买单）加上本笔买入金额是否超过 ``max_cash``：

    - 超限 → 否定为 NONE（不发买单），计数 + 写日志 + 推企微提示（**同一标的当日只
      告警一次**，按 ``_alert_date`` 去重）；
    - 未超限 → 原样放行 ``prev`` 的 BUY（不补 reason，资金是总量闸门而非准入条件）。

    **不写 ``entered``**：资金不足 ≠ 已买入，不能因此认为该标的建仓完成。否则一旦
    后续资金释放（卖出回款 / 容量回收），该标的当天再也买不进。重复触发由 ``_alert_date``
    按日去重兜底：只是不再推送消息，每 tick 仍会重新校验资金，够钱就立即放行。

    ``prev`` 非 BUY 时直接返回 NONE（短路，不查资金）。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：初始化本次判定结果与当日告警去重标记。"""
        super().__init__(vt_symbol, strategy)
        self._result: SignalResult = SignalResult()
        # 最近一次资金不足告警的日期（YYYYMMDD）：同一标的当日只告警一次
        self._alert_date: str = ""

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """前序有 BUY 意向才校验资金上限；超限则否定+告警，否则放行。"""
        if prev.type != SignalType.BUY:
            self._result = SignalResult()
            return self._result

        s = self.strategy
        s.context.refresh()
        deployed: float = s.context.deployed_cash
        needed: float = prev.price * prev.volume

        if deployed + needed > s.max_cash:
            # 只告警不建仓：资金不足不算已买入，不写 entered，资金释放后同一标的仍可买入
            self._alert_cash_short(tick, prev)
            self._result = SignalResult()
            return self._result

        self._result = prev
        return self._result

    def _alert_cash_short(self, tick: TickData, prev: SignalResult) -> None:
        """资金不足告警：同一标的当日只告警一次（计数 + 写日志 + 推企微）。"""
        today: str = tick.datetime.strftime("%Y%m%d")
        if self._alert_date == today:
            return
        self._alert_date = today

        s = self.strategy
        s.cash_short_count += 1
        name: str = s._get_symbol_name(self.vt_symbol)
        msg: str = (
            f"资金不足，跳过买入 {self.vt_symbol}({name}) "
            f"下单价 {prev.price:.2f} 已占用资金 {s.context.deployed_cash:.2f} "
            f"策略资金上限 {s.max_cash:.0f}（当日已跳过 {s.cash_short_count} 笔）"
        )
        s.write_log(msg)
        s.send_wecom(msg)

    def on_bar(self, bar: BarData, prev: SignalResult) -> SignalResult:
        """K线推送回调：本信号走 tick，此处不处理，原样返回 prev。"""
        self._result = prev
        return prev

    def signal_result(self) -> SignalResult:
        """返回 ``on_tick`` 已缓存的判定结果（零计算）。"""
        return self._result


class BuyAggregator(SignalAggregator):
    """买入总信号：按 vt_symbol 聚合拉升检测 + 行业过滤 + 资金上限子信号链。

    链序：``SurgeBuyOrSignal``（拉升检测 OR 组合）→ ``SectorBuySubSignal``（行业过滤）
    → ``CashGateSubSignal``（资金上限）。
    """

    sub_factories = [SurgeBuyOrSignal, SectorBuySubSignal, CashGateSubSignal]
