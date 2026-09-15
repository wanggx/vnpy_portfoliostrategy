"""组合策略统一信号基类与结果类型。

定义两层信号架构的公共骨架，供买入/卖出信号复用：

- ``SubSignal``：单标的子信号基类。每标的一组实例（由总信号按序聚合），状态隔离；
  ``on_tick(tick, prev) -> SignalResult`` / ``on_bar(bar, prev) -> SignalResult`` 接收
  链上前序子信号的累计结果 ``prev``，返回自己的产出（可基于 ``prev`` 决定保留/否定/改写）。
  返回值的 ``type`` 为 NONE 表示该子信号无产出或已否定。是否触发交易最终由总信号取链终态
  ``signal_result()`` 表达。
- ``SignalAggregator``：总信号基类。内部按 ``vt_symbol`` 维护一组有序子信号实例，
  ``on_tick`` 按序运行并把累计结果逐个传递给下一个子信号（链式），``signal_result(vt)``
  返回链终态缓存（零计算）；``sub_factories`` 由子类提供有序工厂列表。

需要跨日持久化的全局状态仍由策略实例持有并注册到 ``variables``，子信号持有策略引用，
经 ``self.strategy.xxx`` 访问；子信号只持有无需持久化的运行时状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

from vnpy.trader.object import BarData, TickData

if TYPE_CHECKING:
    # 仅供类型标注用（from __future__ import annotations 使标注不求值），
    # 运行时不需要，避免循环导入。
    from vnpy_portfoliostrategy.template import StrategyTemplate


class SignalType(Enum):
    """信号操作类型。"""

    NONE = "无"        # 无操作
    BUY = "买入"       # 买入开仓
    SELL = "卖出"      # 部分卖出（如减半）
    CLEAR = "清仓"     # 全清


@dataclass(frozen=True, slots=True)
class SignalResult:
    """单标的信号判定结果（当前快照，可重复读）。

    ``type`` 为 NONE 表示无操作；策略按 ``type`` + ``volume`` + ``price`` 决定下单，
    ``reason`` 供日志/企微复用。链式聚合中，子信号返回的结果作为下一个子信号的 ``prev``。
    """

    type: SignalType = SignalType.NONE
    volume: int = 0
    price: float = 0.0
    reason: str = ""


class SubSignal:
    """单标的子信号基类：持有该标的的隔离运行时状态。

    子类在 ``__init__`` 中初始化各自的状态（如价格窗口），由总信号按需创建。
    ``on_tick`` / ``on_bar`` 接收前序累计结果 ``prev``，返回自己的产出（可基于 ``prev``
    决定保留/否定/改写）；所有计算与副作用都在此完成，``signal_result`` 仅返回缓存结果。
    """

    def __init__(self, vt_symbol: str, strategy: StrategyTemplate) -> None:
        """构造函数：绑定标的与所属策略。"""
        self.vt_symbol: str = vt_symbol
        self.strategy: StrategyTemplate = strategy

    def on_tick(self, tick: TickData, prev: SignalResult) -> SignalResult:
        """处理 tick，更新内部状态；返回本子信号产出（可基于 prev）。"""
        raise NotImplementedError

    def on_bar(self, bar: BarData, prev: SignalResult) -> SignalResult:
        """处理 bar，更新内部状态；返回本子信号产出（可基于 prev）。"""
        raise NotImplementedError

    def signal_result(self) -> SignalResult:
        """返回 ``on_tick`` / ``on_bar`` 已缓存的判定结果（零计算）。"""
        raise NotImplementedError


class SignalAggregator:
    """总信号基类：按 vt_symbol 维护一组有序子信号，链式分发行情。

    子类通过类属性 ``sub_factories`` 指定有序子信号工厂列表
    （``Callable[[str, StrategyTemplate], SubSignal]``）。``on_tick`` / ``on_bar`` 按序
    运行子信号，把累计结果逐个传递给下一个子信号（链式）；``signal_result(vt)`` 返回
    链终态缓存。子信号在首次见到 vt_symbol 时按需创建，退订时由 ``remove`` 清理。
    """

    # 子类覆盖：有序子信号工厂列表，按序判定（前序结果传入后序）
    sub_factories: list[Callable[[str, StrategyTemplate], SubSignal]] = []

    def __init__(self, strategy: StrategyTemplate) -> None:
        """构造函数：持有策略引用并初始化子信号映射与结果缓存。"""
        self.strategy: StrategyTemplate = strategy
        # vt_symbol -> 有序子信号实例列表
        self._subs: dict[str, list[SubSignal]] = {}
        # vt_symbol -> 链终态结果缓存（signal_result 零计算取此）
        self._results: dict[str, SignalResult] = {}

    def on_tick(self, tick: TickData) -> bool:
        """按序分发 tick 到该标的的子信号链，累计结果逐个传递，缓存链终态。"""
        subs = self._get_or_create(tick.vt_symbol)
        result: SignalResult = SignalResult()
        for sub in subs:
            result = sub.on_tick(tick, result)
        self._results[tick.vt_symbol] = result
        return True

    def on_bar(self, bar: BarData) -> bool:
        """按序分发 bar 到该标的的子信号链，累计结果逐个传递，缓存链终态。"""
        subs = self._get_or_create(bar.vt_symbol)
        result: SignalResult = SignalResult()
        for sub in subs:
            result = sub.on_bar(bar, result)
        self._results[bar.vt_symbol] = result
        return True

    def signal_result(self, vt_symbol: str) -> SignalResult:
        """取该标的的链终态结果；无子信号时返回空结果（无操作）。"""
        return self._results.get(vt_symbol, SignalResult())

    def remove(self, vt_symbol: str) -> None:
        """退订时清理子信号及其持有的 per-symbol 运行时状态与结果缓存。"""
        self._subs.pop(vt_symbol, None)
        self._results.pop(vt_symbol, None)

    def _get_or_create(self, vt_symbol: str) -> list[SubSignal]:
        """取子信号列表，不存在则按 sub_factories 逐个创建并登记。"""
        subs = self._subs.get(vt_symbol)
        if subs is None:
            subs = [factory(vt_symbol, self.strategy) for factory in self.sub_factories]
            self._subs[vt_symbol] = subs
        return subs
