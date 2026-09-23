"""基于市场情绪（vnpy_marketsentiment）的组合策略信号器。

借鉴 vnpy_ctastrategy 的 ``CtaSignal`` 思路：信号器只负责表态（输出布尔判断），
不下单、不持仓、不订阅行情，由组合策略自行取用并组合。本模块的信号器统一从
``MarketSentimentEngine`` 的只读快照取数，与 CTA 引擎解耦。

两个信号器：
- ``SectorBuySignal``：结合大盘（全市场综合）情绪与标的所属申万二级行业情绪分，判断
  是否可买入。大盘评级中性以上时行业需中性及以上；大盘评级中性及以下时行业需中性以上
  （不含中性）。
- ``MarketRiskOffSignal``：根据全市场综合情绪分，判断是否需要全部清仓。

另含卖出侧的行业判定器：
- ``SectorSellSignal``：根据标的所属申万二级行业的情绪分，判断是否应卖出离场
  （评级中性以下）。与 ``SectorBuySignal`` 对称，各自独立的结果类型与判定器。

降级原则（缺数据时偏保守）：
- 买入信号：情绪 App 未加载 / 标的未映射到行业 / 行业有效家数不足 → 不买入；
  大盘快照不可用（未加载 / 过期 / 有效标的不足）→ 按中性及以下处理，行业需中性以上。
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
    ``below_neutral`` 表示行业评级是否严格在中性以下（偏弱 / 极弱），
    降级（不可用 / 未分行业 / 未映射）时为 False，供策略据此离场而非误判。
    ``market_level`` / ``market_score`` 为大盘情绪快照的评级与得分，用于确定行业
    准入档位并供策略展示；大盘快照不可用（过期 / 有效标的不足）时仍带上快照自带
    评级，但判定已按中性及以下的严格档处理。
    """

    buyable: bool
    sector_name: str = ""
    sector_score: float = 0.0
    sector_level: str = ""
    below_neutral: bool = False
    market_level: str = ""
    market_score: float = 0.0


@dataclass(frozen=True, slots=True)
class SectorSellResult:
    """行业应卖信号判定结果。

    ``sellable`` 为是否应因行业情绪离场（评级在中性以下，偏弱 / 极弱）；
    ``sector_name`` / ``sector_score`` / ``sector_level`` 供策略展示与消息推送。
    降级或未映射行业时字段为空，``sector_level`` 标注降级原因（如 "不可用" /
    "未分行业"）。降级（不可用 / 未分行业 / 未映射）时 ``sellable`` 为 False，
    不触发离场，交价格卖出处理。
    """

    sellable: bool
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
    """行业可买信号器：结合大盘情绪判断标的所属申万二级行业是否允许买入。

    行业准入档位随大盘（全市场综合）情绪评级分档：
    - 大盘评级在中性以上（偏强 / 极强）：行业评级中性及以上（中性 / 偏强 / 极强）即可买入；
    - 大盘评级在中性及以下（中性 / 偏弱 / 极弱）：行业评级需中性以上（不含中性）；
    - 大盘快照不可用（未加载 / 过期 / 有效标的不足）：按中性及以下处理，走严格档。

    其余（行业偏弱 / 极弱 / 不可用 / 未分行业）一律不买。阈值写死，不暴露为策略参数。
    """

    # 中性及以上的评级集合：中性 / 偏强 / 极强（大盘中性以上时的行业准入档）
    BUYABLE_LEVELS: set = {
        SentimentLevel.NEUTRAL,
        SentimentLevel.BULLISH,
        SentimentLevel.EXTREME_BULLISH,
    } if SentimentLevel is not None else set()

    # 中性以上（不包含中性）的评级集合：偏强 / 极强
    # （大盘中性及以下时的行业准入档，也是判断大盘强弱的分界）
    ABOVE_NEUTRAL_LEVELS: set = {
        SentimentLevel.BULLISH,
        SentimentLevel.EXTREME_BULLISH,
    } if SentimentLevel is not None else set()

    # 中性以下（不包含中性）的评级集合：偏弱 / 极弱
    BELOW_NEUTRAL_LEVELS: set = {
        SentimentLevel.BEARISH,
        SentimentLevel.EXTREME_BEARISH,
    } if SentimentLevel is not None else set()

    def __init__(self, strategy: StrategyTemplate) -> None:
        super().__init__(strategy)
        # 可观测变量，供策略展示/日志
        self.sector_name: str = ""
        self.sector_score: float = 0.0
        self.sector_level: str = ""
        # 大盘情绪快照可观测变量，供策略展示/日志
        self.market_level: str = ""
        self.market_score: float = 0.0
        self.market_stale: bool = False
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
        返回 ``SectorBuyResult``，含 buyable / 行业名 / 得分 / 评级 / 大盘评级与得分。
        降级原则：引擎不可用、标的未映射到行业、行业有效家数不足 → buyable=False；
        大盘快照不可用（过期 / 有效标的不足）→ 按中性及以下收紧为严格档（行业需中性以上）。
        """
        if vt_symbol is None:
            vt_symbol = self.vt_symbol
        if not vt_symbol:
            self.sector_name = ""
            self.sector_score = 0.0
            self.sector_level = ""
            return SectorBuyResult(False, "", 0.0, "", False)
        engine = self._get_engine()
        if engine is None:
            return SectorBuyResult(False, "", 0.0, "不可用", False)
        qmt_symbol: str = self._vt_to_qmt(vt_symbol)
        if not qmt_symbol:
            self.sector_name = ""
            self.sector_score = 0.0
            self.sector_level = ""
            return SectorBuyResult(False, "", 0.0, "", False)
        state = engine.get_stock_sector(qmt_symbol)
        if state is None:
            # 未分到行业或该行业有效家数不够，保守不买
            self.sector_name = ""
            self.sector_score = 0.0
            self.sector_level = "未分行业"
            return SectorBuyResult(False, "", 0.0, "未分行业", False)
        # 大盘情绪快照：决定行业准入档位。不可用（过期 / 有效标的不足）时按中性及以下
        # 处理，即走严格档（行业需中性以上），符合缺数据偏保守的降级原则
        snapshot: MarketSentimentSnapshot = engine.get_latest()
        self.market_level = snapshot.level.value
        self.market_score = snapshot.score
        self.market_stale = snapshot.stale
        # 更新可观测变量
        self.sector_name = state.name
        self.sector_score = state.score
        self.sector_level = state.level.value
        buyable: bool = self._evaluate(state, snapshot)
        below_neutral: bool = state.level in self.BELOW_NEUTRAL_LEVELS
        return SectorBuyResult(
            buyable,
            state.name,
            state.score,
            state.level.value,
            below_neutral,
            self.market_level,
            self.market_score,
        )

    def _evaluate(self, state: SectorState, snapshot: MarketSentimentSnapshot) -> bool:
        """行业情绪是否允许买入：按大盘情绪分档收紧行业准入。

        大盘快照可用且评级在中性以上（偏强 / 极强）：行业中性及以上即放行；
        大盘评级在中性及以下（中性 / 偏弱 / 极弱）或快照不可用：行业需中性以上
        （不含中性，仅偏强 / 极强）。
        """
        if snapshot.available and snapshot.level in self.ABOVE_NEUTRAL_LEVELS:
            return state.level in self.BUYABLE_LEVELS
        return state.level in self.ABOVE_NEUTRAL_LEVELS


class SectorSellSignal(SentimentSignal):
    """行业应卖信号器：判断标的所属申万二级行业情绪是否需要离场。

    规则：行业评级在中性以下（偏弱 / 极弱）时应卖出离场，其余（中性 / 偏强 / 极强 /
    不可用 / 未分行业）不触发。阈值写死，不暴露为策略参数。与 ``SectorBuySignal`` 对称：
    买入侧判定"中性以上可买"，卖出侧判定"中性以下应卖"，各自独立的判定器与结果类型。
    """

    # 中性以下（不包含中性）的评级集合：偏弱 / 极弱
    SELLABLE_LEVELS: set = {
        SentimentLevel.BEARISH,
        SentimentLevel.EXTREME_BEARISH,
    } if SentimentLevel is not None else set()

    def __init__(self, strategy: StrategyTemplate) -> None:
        super().__init__(strategy)
        # 可观测变量，供策略展示/日志
        self.sector_name: str = ""
        self.sector_score: float = 0.0
        self.sector_level: str = ""
        # 当前判定的标的，由 on_tick / on_bar 维护，is_sellable 不传参时取它
        self.vt_symbol: str = ""

    def on_tick(self, tick: TickData) -> None:
        """行情推送回调：缓存 tick 并记录当前标的。"""
        super().on_tick(tick)
        self.vt_symbol = tick.vt_symbol

    def on_bar(self, bar: BarData) -> None:
        """K线推送回调：缓存 bar 并记录当前标的。"""
        super().on_bar(bar)
        self.vt_symbol = bar.vt_symbol

    def is_sellable(self, vt_symbol: str | None = None) -> SectorSellResult:
        """行业情绪是否应卖出离场该标的。

        ``vt_symbol`` 未传时，取最近一次 ``on_tick`` / ``on_bar`` 推送的标的。
        返回 ``SectorSellResult``，含 sellable / 行业名 / 得分 / 评级。
        降级原则：引擎不可用、标的未映射到行业、行业有效家数不足 → sellable=False，
        不触发离场，交价格卖出处理。
        """
        if vt_symbol is None:
            vt_symbol = self.vt_symbol
        if not vt_symbol:
            self.sector_name = ""
            self.sector_score = 0.0
            self.sector_level = ""
            return SectorSellResult(False, "", 0.0, "")
        engine = self._get_engine()
        if engine is None:
            return SectorSellResult(False, "", 0.0, "不可用")
        qmt_symbol: str = self._vt_to_qmt(vt_symbol)
        if not qmt_symbol:
            self.sector_name = ""
            self.sector_score = 0.0
            self.sector_level = ""
            return SectorSellResult(False, "", 0.0, "")
        state = engine.get_stock_sector(qmt_symbol)
        if state is None:
            # 未分到行业或该行业有效家数不够，不触发离场
            self.sector_name = ""
            self.sector_score = 0.0
            self.sector_level = "未分行业"
            return SectorSellResult(False, "", 0.0, "未分行业")
        # 更新可观测变量
        self.sector_name = state.name
        self.sector_score = state.score
        self.sector_level = state.level.value
        sellable: bool = self._evaluate(state)
        return SectorSellResult(
            sellable,
            state.name,
            state.score,
            state.level.value,
        )

    def _evaluate(self, state: SectorState) -> bool:
        """行业情绪是否应卖出离场：评级在中性以下（偏弱 / 极弱）触发。"""
        return state.level in self.SELLABLE_LEVELS


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
