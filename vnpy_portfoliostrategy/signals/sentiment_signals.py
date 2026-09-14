"""基于市场情绪（vnpy_marketsentiment）的组合策略信号器。

借鉴 vnpy_ctastrategy 的 ``CtaSignal`` 思路：信号器只负责表态（输出布尔判断），
不下单、不持仓、不订阅行情，由组合策略自行取用并组合。本模块的信号器统一从
``MarketSentimentEngine`` 的只读快照取数，与 CTA 引擎解耦。

两个信号器：
- ``SectorBuySignal``：根据标的所属申万二级行业的情绪分，判断是否可买入。
- ``MarketRiskOffSignal``：根据全市场综合情绪分，判断是否需要全部清仓。

降级原则（缺数据时偏保守）：
- 买入信号：情绪 App 未加载 / 标的未映射到行业 / 快照不可用 → 不买入。
- 清仓信号：情绪 App 未加载 / 快照不可用或过期 → 不触发清仓。

注意：``_evaluate`` 内的准确判断逻辑暂为占位实现（无干预），待业务规则确定后填入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vnpy.trader.constant import Exchange
from vnpy.trader.object import BarData, TickData

try:
    from vnpy_marketsentiment import SentimentLevel, get_sentiment_engine
except ImportError:  # noqa: BLE001 - 情绪 App 未安装时降级为不可用
    get_sentiment_engine = None  # type: ignore[assignment]
    SentimentLevel = None  # type: ignore[assignment]


if TYPE_CHECKING:
    # 仅供类型标注用（from __future__ import annotations 使标注不求值），
    # 运行时不需要，因此放在 TYPE_CHECKING 下，避免情绪 App 未装时 NameError。
    from vnpy_marketsentiment import MarketSentimentSnapshot, SectorState
    from vnpy_portfoliostrategy.template import StrategyTemplate


# vnpy 交易所枚举值 -> xtquant/QMT 代码后缀，用于 vt_symbol 反向映射回 QMT 代码。
# 情绪引擎按 QMT 代码（如 600000.SH）查行业，portfolio 内部用 vnpy vt_symbol（如 600000.SSE）。
_QMT_SUFFIX_BY_EXCHANGE_VALUE: dict[str, str] = {
    Exchange.SSE.value: ".SH",
    Exchange.SZSE.value: ".SZ",
    Exchange.BSE.value: ".BJ",
}


@dataclass(frozen=True, slots=True)
class SectorBuyResult:
    """行业可买信号判定结果。

    ``buyable`` 为是否允许买入；``sector_name`` / ``sector_score`` /
    ``sector_level`` 供策略展示与消息推送。降级或未映射行业时字段为空，
    ``sector_level`` 标注降级原因（如 "不可用" / "未分行业"）。
    """

    buyable: bool
    sector_name: str = ""
    sector_score: float = 0.0
    sector_level: str = ""


class SentimentSignal:
    """市场情绪信号器基类。

    子类只需实现各自的判断入口（``is_buyable`` / ``is_risk_off``）与 ``_evaluate``。
    所有子类共享：情绪引擎获取、vt_symbol → QMT 代码转换、引擎缺失一次性告警。
    日志统一走策略引擎的 ``write_log``（带策略名前缀，进主窗口日志栏）。
    """

    def __init__(self, strategy: StrategyTemplate) -> None:
        """构造函数

        传入所属策略 ``strategy``，从中取 ``strategy_engine``（用于写日志）与
        ``main_engine``（用于取情绪引擎）。日志经 ``strategy_engine.write_log``
        输出，自动带上策略名前缀。
        """
        self.strategy: StrategyTemplate | None = strategy
        self.strategy_engine = (
            strategy.strategy_engine if strategy is not None else None
        )
        self.main_engine = (
            strategy.strategy_engine.main_engine
            if self.strategy_engine is not None
            else None
        )
        self._engine_missing_logged: bool = False
        # 最近行情缓存，由 on_tick / on_bar 更新，供子类按需取用
        self.last_tick: TickData | None = None
        self.last_bar: BarData | None = None

    def on_tick(self, tick: TickData) -> None:
        """行情推送回调：缓存最近 tick。子类可按需重载做更多处理。"""
        self.last_tick = tick

    def on_bar(self, bar: BarData) -> None:
        """K线推送回调：缓存最近 bar。子类可按需重载做更多处理。"""
        self.last_bar = bar

    def _log(self, msg: str) -> None:
        """写日志：走策略引擎 write_log，带策略名前缀。"""
        self.strategy_engine.write_log(msg, self.strategy)

    def _get_engine(self):
        """取市场情绪引擎；App 未加载时告警一次并返回 None。"""
        if self.main_engine is None:
            if not self._engine_missing_logged:
                self._log("未提供 main_engine，情绪信号器降级为不可用")
                self._engine_missing_logged = True
            return None
        if get_sentiment_engine is None:
            if not self._engine_missing_logged:
                self._log("未安装 vnpy_marketsentiment，情绪信号器降级为不可用")
                self._engine_missing_logged = True
            return None
        engine = get_sentiment_engine(self.main_engine)
        if engine is None:
            if not self._engine_missing_logged:
                self._log("未加载市场情绪App，情绪信号器降级为不可用")
                self._engine_missing_logged = True
            return None
        return engine

    @staticmethod
    def _vt_to_qmt(vt_symbol: str) -> str:
        """vnpy vt_symbol（如 600000.SSE）转 QMT 代码（如 600000.SH）。

        无法识别的交易所返回空串。
        """
        symbol: str
        dot: str
        exchange_value: str
        symbol, dot, exchange_value = vt_symbol.rpartition(".")
        if not dot:
            return ""
        suffix: str | None = _QMT_SUFFIX_BY_EXCHANGE_VALUE.get(exchange_value)
        if suffix is None:
            return ""
        return f"{symbol}{suffix}"


class SectorBuySignal(SentimentSignal):
    """行业可买信号器：判断标的所属申万二级行业情绪是否允许买入。

    规则：行业评级在中性（NEUTRAL）及以上（偏强 / 极强）时允许买入，
    其余（偏弱 / 极弱 / 不可用 / 未分行业）一律不买。阈值写死，不暴露为策略参数。
    """

    # 允许买入的评级集合：中性及以上
    BUYABLE_LEVELS: set = {
        SentimentLevel.NEUTRAL,
        SentimentLevel.BULLISH,
        SentimentLevel.EXTREME_BULLISH,
    } if SentimentLevel is not None else set()

    def __init__(self, strategy: StrategyTemplate) -> None:
        super().__init__(strategy)
        # 可观测变量，供策略展示/日志
        self.sector_name: str = ""
        self.sector_score: float = 0.0
        self.sector_level: str = ""
        # 当前判定的标的，由 on_tick / on_bar 维护，is_buyable 不传参时取它
        self.vt_symbol: str = ""

    def on_tick(self, tick: TickData) -> None:
        """行情推送回调：缓存 tick 并记录当前标的。"""
        super().on_tick(tick)
        self.vt_symbol = tick.vt_symbol

    def on_bar(self, bar: BarData) -> None:
        """K线推送回调：缓存 bar 并记录当前标的。"""
        super().on_bar(bar)
        self.vt_symbol = bar.vt_symbol

    def is_buyable(self, vt_symbol: str | None = None) -> SectorBuyResult:
        """行业情绪是否允许买入该标的。

        ``vt_symbol`` 未传时，取最近一次 ``on_tick`` / ``on_bar`` 推送的标的。
        返回 ``SectorBuyResult``，含 buyable / 行业名 / 得分 / 评级。
        降级原则：引擎不可用、标的未映射到行业、行业有效家数不足 → buyable=False。
        """
        if vt_symbol is None:
            vt_symbol = self.vt_symbol
        if not vt_symbol:
            self.sector_name = ""
            self.sector_score = 0.0
            self.sector_level = ""
            return SectorBuyResult(False, "", 0.0, "")
        engine = self._get_engine()
        if engine is None:
            return SectorBuyResult(False, "", 0.0, "不可用")
        qmt_symbol: str = self._vt_to_qmt(vt_symbol)
        if not qmt_symbol:
            self.sector_name = ""
            self.sector_score = 0.0
            self.sector_level = ""
            return SectorBuyResult(False, "", 0.0, "")
        state = engine.get_stock_sector(qmt_symbol)
        if state is None:
            # 未分到行业或该行业有效家数不够，保守不买
            self.sector_name = ""
            self.sector_score = 0.0
            self.sector_level = "未分行业"
            return SectorBuyResult(False, "", 0.0, "未分行业")
        # 更新可观测变量
        self.sector_name = state.name
        self.sector_score = state.score
        self.sector_level = state.level.value
        buyable: bool = self._evaluate(state)
        return SectorBuyResult(
            buyable,
            state.name,
            state.score,
            state.level.value,
        )

    def _evaluate(self, state: SectorState) -> bool:
        """行业情绪是否允许买入：评级在中性及以上放行。"""
        return state.level in self.BUYABLE_LEVELS


class MarketRiskOffSignal(SentimentSignal):
    """全市场清仓信号器：判断市场综合情绪是否差到需要全部清仓。

    写死阈值，不暴露为策略参数。``_evaluate`` 待业务规则确定后填入。
    """

    # 全市场情绪分清仓阈值（0~100），仅占位，待准确逻辑替换后可能不再单一使用。
    RISKOFF_SCORE_THRESHOLD: float = 20.0

    def __init__(self, strategy: StrategyTemplate) -> None:
        super().__init__(strategy)
        # 可观测变量，供策略展示/日志
        self.market_score: float = 0.0
        self.market_level: str = ""
        self.market_stale: bool = False

    def is_risk_off(self) -> bool:
        """市场情绪是否差到需要全部清仓。

        降级原则：引擎不可用、快照不可用或过期 → 返回 False（不触发清仓）。
        """
        engine = self._get_engine()
        if engine is None:
            return False
        snapshot: MarketSentimentSnapshot = engine.get_latest()
        if not snapshot.available:
            # 快照不可用/过期/有效标的不足，不贸然清仓
            self.market_score = snapshot.score
            self.market_level = snapshot.level.value
            self.market_stale = snapshot.stale
            return False
        # 更新可观测变量
        self.market_score = snapshot.score
        self.market_level = snapshot.level.value
        self.market_stale = snapshot.stale
        return self._evaluate(snapshot)

    def _evaluate(self, snapshot: MarketSentimentSnapshot) -> bool:
        """【待填】市场情绪是否需要全部清仓的准确判断逻辑。

        目前为占位实现：综合分 < RISKOFF_SCORE_THRESHOLD 或处于 risk_off（偏弱/极弱）即触发。
        待业务规则确定后替换为完整逻辑（可结合 score / level / breadth / 跌停家数等）。
        """
        return snapshot.risk_off or snapshot.score < self.RISKOFF_SCORE_THRESHOLD
