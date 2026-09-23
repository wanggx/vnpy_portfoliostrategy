from datetime import datetime, time, timedelta

from vnpy.trader.constant import Exchange, Direction, Status
from vnpy.trader.object import TickData, BarData, TradeData, OrderData

from bigqmt_signal_trader.xtquant_compat import xtdata

from vnpy_sqlapp import APP_NAME as SQLAPP_NAME

from vnpy_portfoliostrategy import OrderMonitor, StrategyTemplate, StrategyEngine
from vnpy_portfoliostrategy.signals import (
    SignalResult,
    SignalType,
    SellAggregator,
    BuyAggregator,
)


# xtquant 代码后缀 -> vnpy 交易所映射
CODE_SUFFIX_EXCHANGE: dict[str, Exchange] = {
    ".SH": Exchange.SSE,
    ".SZ": Exchange.SZSE,
    ".BJ": Exchange.BSE,
}


class NearMaSurgeStrategy(StrategyTemplate):
    """Near-Ma 快速拉升策略

    每日盘前从 SqlApp 的 ``stock_near_ma`` 表按行业名取当日成分股，订阅行情；
    09:15 刷新标的池，09:30 连续竞价开始后才开始买卖判定与拉升检测；
    退订不在当日池中且无持仓的旧标的；在无持仓标的上用 tick 实时检测「快速拉升」
    （窗口内涨幅或相对昨收涨幅 >= surge_pct），命中即开 fixed_size 股固定仓位并持有。

    卖出信号由两类信号器组合，情绪卖出优先：行业情绪卖出信号在收益低于 3%（含亏损）
    且所属行业评级转为中性以下（偏弱/极弱）时主动离场全清，不等价格止损（弱市里亏
    0.5% 也走）；价格卖出信号负责分档止损（10:00 前相对开仓价 4%、10:00 后 2%，情绪档
    不在岗时全天 2% 不放宽）、收益达 clear_profit_pct(默认 20%) 清仓、达
    half_profit_pct(默认 10%) 仓位减半（剩余可卖量不足 fixed_size 后不再减半）、
    回撤超过 10% 卖出，以及最大收益 >= 10% 保底 5%、>= 5% 保底 2%、>= 3% 不赔钱的
    保底收益线。亏损离场（止损 / 情绪离场）后记录止损日，该标的在止损日起
    ``COOLDOWN_DAYS`` 个自然日的冷却期内不再买入，避免"清仓→再买→再清"的反复止损。

    信号架构分两层：买入/卖出各一个总信号（``BuyAggregator`` / ``SellAggregator``），
    内部按 ``vt_symbol`` 维护子信号映射并分发行情；子信号经 ``signal_result()`` 返回
    ``SignalResult``（type=买入/卖出/清仓 + 数量 + 价格 + 原因），策略据此下单。
    需持久化的全局状态（开仓价/最大收益/标的池等）仍由策略持有，子信号经引用访问。

    卖单发出后会纳入**卖出委托监控**（通用件 ``OrderMonitor``，见 ``order_monitor``
    模块）：全部成交/撤单/拒单即去掉监控；挂满 ``SELL_ORDER_TIMEOUT``（默认 5 分钟）
    仍未全部成交则企微告警并撤单，**不重发**（撤单释放的 T+1 冻结量会让可卖量恢复，
    若卖出条件仍成立，卖出信号会在后续 tick 自然重新下单）。监控状态仅存活在进程内，
    不持久化。
    """

    author: str = "用Python的交易员"

    # 行业过滤与数据源
    industry_name: str = ""
    TABLE_NAME: str = "stock_near_ma"

    # 交易参数
    fixed_size: int = 500
    price_add: float = 0.0
    # T+1 开关（A 股现货 True：仅可卖昨仓；期货 False：T+0）
    t1: bool = True

    # 卖出委托监控：卖单发出后跟踪成交；全部成交/撤单/拒单即去掉监控，
    # 挂满 SELL_ORDER_TIMEOUT 仍未全部成交则企微告警并撤单，**不重发**
    SELL_ORDER_TIMEOUT: float = 300.0    # 秒（默认 5 分钟），作为 OrderMonitor 的 timeout

    # 快速拉升检测参数
    surge_pct: float = 0.03
    surge_window: int = 300        # 拉升检测时间窗口（秒），默认 5 分钟
    MIN_SURGE_SPAN: int = 180      # 窗口至少覆盖 3 分钟才判定拉升

    # 每日盘前刷新订阅标的池的固定时刻（09:15）
    REFRESH_TIME: time = time(9, 15)
    # 连续竞价开始时刻（09:30）；此前为集合竞价，波动大，不做买卖判定
    MARKET_OPEN_TIME: time = time(9, 30)

    # 止损 / 移动止盈参数（固定，不暴露为策略参数）
    # 止损分档：开盘 10:00 前用宽档（容忍低开与早盘噪声），10:00 后回到常规档；
    # 情绪档不在岗（大盘快照过期/未加载，或该标的取不到行业评级）时全天按常规档，不放宽
    STOP_LOSS_PCT: float = 0.02             # 常规档止损：相对开仓价亏损 2% 即卖
    WIDE_STOP_LOSS_PCT: float = 0.04        # 开盘宽档止损：相对开仓价亏损 4% 即卖
    WIDE_STOP_END_TIME: time = time(10, 0)  # 宽档截止时刻，之后回到常规档
    MAX_DRAWDOWN_PCT: float = 0.10       # 最大回撤上限：相对最大收益回撤 10% 即卖
    TIER_HIGH_PCT: float = 0.10          # 最大收益 >= 10% 时，最低保底 5%
    TIER_HIGH_FLOOR: float = 0.05
    TIER_LOW_PCT: float = 0.05           # 最大收益 >= 5% 时，最低保底 2%
    TIER_LOW_FLOOR: float = 0.02
    # 最大收益 >= 3% 时，不能赔钱（保底 0%）
    # 亦作情绪卖出阈值：收益低于此值（含亏损）且行业评级中性以下时主动离场，不等价格止损
    TIER_BREAKEVEN_PCT: float = 0.03
    TIER_BREAKEVEN_FLOOR: float = 0.0

    # 亏损离场（止损 / 情绪离场）后记录**止损日**，止损日起 COOLDOWN_DAYS 个自然日内
    # 不再买入该标的（含止损当日：D 日止损 → D、D+1、D+2 三天不买，D+3 起释放）。
    # 用自然日近似交易日，规避交易日历依赖；改本值对已有记录立即生效
    COOLDOWN_DAYS: int = 2

    # 分档止盈参数（可配置）：收益达 half_profit_pct 仓位减半，达 clear_profit_pct 清仓
    half_profit_pct: float = 0.10        # 收益率达此值时仓位减半（默认 10%）
    clear_profit_pct: float = 0.20       # 收益率达此值时清仓（默认 20%）

    parameters: list = [
        "industry_name",
        "fixed_size",
        "price_add",
        "surge_pct",
        "surge_window",
        "half_profit_pct",
        "clear_profit_pct",
    ]

    variables: list = [
        "target_symbols",
        "last_refresh_date",
        "surge_count",
        "entry_prices",
        "max_profit_pct",
        "cooldown_dates",
    ]

    def __init__(
        self,
        strategy_engine: StrategyEngine,
        strategy_name: str,
        vt_symbols: list[str],
        setting: dict
    ) -> None:
        """构造函数"""
        super().__init__(strategy_engine, strategy_name, vt_symbols, setting)

        # 当前已订阅的标的集合
        self.subscribed_symbols: set[str] = set(vt_symbols)
        # 当日标的池：vt_symbol -> name（用于日志展示）
        self.target_symbols: dict[str, str] = {}
        # 当日标的池全行数据：vt_symbol -> row(dict)，保留库表所有列
        self.universe_data: dict[str, dict] = {}
        # 最近一次刷新日期（YYYYMMDD），用于日切判断
        self.last_refresh_date: str = ""
        # 累计触发拉升买入次数
        self.surge_count: int = 0

        # 本轮已触发买入的标的，避免同一波拉升重复下单（含行业拦截也标记）
        self.entered: set[str] = set()

        # 卖出委托监控（通用件，仅运行时状态，不持久化）：全部成交/撤单/拒单即摘除，
        # 超时未全部成交则告警并撤单（不重发）。label="卖出" 让告警文案为
        # "卖出委托超时告警 …"
        self.sell_monitor: OrderMonitor = OrderMonitor(
            self, timeout=self.SELL_ORDER_TIMEOUT, label="卖出"
        )

        # 亏损离场后的买入冷却：vt_symbol -> 止损离场日（YYYYMMDD，持久化，重启不丢）
        self.cooldown_dates: dict[str, str] = {}

        # 持仓追踪：开仓价与开仓后最大收益率，用于移动止盈。
        # 开仓价在一轮持仓内不可变：只在建仓成交时确定一次，之后的涨跌比例、止损、
        # 清仓/减半止盈、回撤与保底全部以它为准；分笔成交、加仓、经纪商持仓回报都不得改动
        self.entry_prices: dict[str, float] = {}
        self.max_profit_pct: dict[str, float] = {}

    def on_init(self) -> None:
        """策略初始化回调

        引擎在 ``on_init`` 返回后即按 ``vt_symbols`` 订阅并把 ``inited`` 置 True，
        此后就会收到 ``on_tick``。此处仅还原订阅集合；当日标的池刷新由
        ``on_tick`` 日切逻辑触发。
        """
        self.write_log("策略初始化")
        t = datetime.now()
        self._restore_subscribed_symbols()
        self.write_log(f"还原订阅总耗时 {(datetime.now() - t).total_seconds():.2f}s")
        # 买入总信号：按 vt_symbol 聚合拉升 + 行业过滤子信号链，分发 tick、取 SignalResult
        self.buy_signal: BuyAggregator = BuyAggregator(self)
        # 卖出总信号：按 vt_symbol 聚合卖出子信号，情绪卖出优先，其次价格卖出
        self.sell_signal: SellAggregator = SellAggregator(self)
        t = datetime.now()
        self.load_bars(1)
        self.write_log(f"load_bars 耗时 {(datetime.now() - t).total_seconds():.2f}s")

    def on_start(self) -> None:
        """策略启动回调"""
        self.write_log("策略启动")

    def _restore_subscribed_symbols(self) -> None:
        """还原订阅集合并补订行情

        ``on_init`` 时引擎尚未把 data JSON 写回实例，因此同时读取内存字段和
        ``strategy_data``。``vt_symbols`` 已由 setting 载入。
        """
        restored: set[str] = set(self.vt_symbols)
        restored.update(self.target_symbols.keys())
        restored.update(
            vt_symbol for vt_symbol, position in self.pos_data.items()
            if position != 0
        )

        # 读取持久化数据（strategy_data 已在引擎启动时 load_json 进内存，此处为字典查找）
        t_read: datetime = datetime.now()
        data: dict = self.strategy_engine.strategy_data.get(self.strategy_name, {}) or {}
        saved_targets: dict = data.get("target_symbols") or {}
        if isinstance(saved_targets, dict):
            restored.update(saved_targets.keys())
        saved_pos: dict = data.get("pos_data") or {}
        if isinstance(saved_pos, dict):
            restored.update(
                vt_symbol for vt_symbol, position in saved_pos.items()
                if position
            )
        self.write_log(
            f"还原-读取持久化数据耗时 {(datetime.now() - t_read).total_seconds():.4f}s"
        )

        to_subscribe: list[str] = [
            vt_symbol for vt_symbol in restored
            if vt_symbol not in self.subscribed_symbols
        ]
        t_sub: datetime = datetime.now()
        if to_subscribe:
            self.strategy_engine.subscribe_symbols(self, to_subscribe)
        self.write_log(
            f"还原-订阅行情耗时 {(datetime.now() - t_sub).total_seconds():.2f}s "
            f"补订 {len(to_subscribe)} 只"
        )
        self.subscribed_symbols = restored
        if restored:
            self.write_log(f"订阅集合已还原：{len(restored)} 只")

    def _get_symbol_name(self, vt_symbol: str) -> str:
        """取标的名称：直接查 MainEngine 的合约缓存。

        vnpy_xt 网关连上时已通过 on_contract 把全市场合约灌入 MainEngine，
        ``get_contract(vt_symbol).name`` 即标的名称，无需策略自行维护映射。
        取不到（合约未就绪）时返回空串。
        """
        contract = self.strategy_engine.main_engine.get_contract(vt_symbol)
        if contract:
            return contract.name or ""
        return ""

    def on_stop(self) -> None:
        """策略停止回调"""
        self.write_log("策略停止")

    def sync_t1_position(
        self,
        vt_symbol: str,
        volume: float,
        yd_volume: float,
        price: float = 0,
    ) -> None:
        """同步 T+1 持仓，并（仅在本地缺失时）用经纪商持仓均价兜底开仓成本"""
        super().sync_t1_position(vt_symbol, volume, yd_volume, price)

        broker_price: float = self.get_pos_price(vt_symbol)
        if int(volume) > 0:
            # 经纪商成本价只在本地没有开仓价时兜底（重启后手工建仓、JSON 丢失）；
            # 不能用它覆盖已有开仓价：A 股券商/QMT 返回的多为「摊薄成本价」，部分卖出
            # 后已实现盈利会摊低成本价（如 10 元买 500 股、11 元卖 300 股后成本变 8.5 元），
            # 覆盖会让剩余仓位的收益率从 10% 瞬间跳到 29%，立刻误触 clear_profit_pct 清仓。
            if broker_price > 0 and vt_symbol not in self.entry_prices:
                self.entry_prices[vt_symbol] = broker_price
                self._sync_tracking_state()
            self.entered.add(vt_symbol)
        else:
            if vt_symbol in self.entry_prices or vt_symbol in self.max_profit_pct:
                self.entry_prices.pop(vt_symbol, None)
                self.max_profit_pct.pop(vt_symbol, None)
                self._sync_tracking_state()
            self.entered.discard(vt_symbol)

    def _sync_tracking_state(self) -> None:
        """持久化策略变量（开仓价 / 最大收益 / 止损冷却日）到 portfolio_strategy_data.json"""
        self.sync_data()

    def _init_max_profit_from_tick(self, vt_symbol: str, tick: TickData) -> None:
        """JSON 无历史峰值时，用首个 tick 相对开仓价初始化 max_profit_pct"""
        if not tick.last_price or tick.last_price <= 0:
            return

        entry_price: float = self.entry_prices.get(vt_symbol) or self.get_pos_price(vt_symbol)
        if entry_price <= 0:
            return

        if vt_symbol not in self.entry_prices:
            self.entry_prices[vt_symbol] = entry_price

        profit_pct: float = (tick.last_price - entry_price) / entry_price
        self.max_profit_pct[vt_symbol] = profit_pct
        self._sync_tracking_state()

    def _is_trading_session(self, tick: TickData) -> bool:
        """是否处于连续竞价时段（09:30 起）

        09:15~09:30 为集合竞价，价格波动大且不可连续成交，跳过买卖与窗口采样。
        """
        return tick.datetime.time() >= self.MARKET_OPEN_TIME

    def on_tick(self, tick: TickData) -> None:
        """行情推送回调：日切刷新 + 信号分发 + 下单

        策略为协调器：日切刷新标的池、转发行情给信号器、按 ``SignalResult`` 的
        type 下单；拉升检测/行业过滤/卖出判定均下沉到买入/卖出子信号。
        """
        # 1. 日切刷新：日期变更且到盘前刷新时刻（09:15），重新拉取当日池
        tick_date: str = tick.datetime.strftime("%Y%m%d")
        if tick_date != self.last_refresh_date:
            if tick.datetime.time() >= self.REFRESH_TIME:
                self.refresh_universe()

        # 集合竞价时段仅刷新标的池，不做买卖判定与窗口采样
        if not self._is_trading_session(tick):
            return

        # 2. 卖出委托监控：挂满 SELL_ORDER_TIMEOUT 仍未全部成交则告警并撤单
        self.sell_monitor.check()

        vt_symbol: str = tick.vt_symbol
        pos: int = self.get_pos(vt_symbol)

        # 3. 有持仓且可卖量 > 0 才走卖出分支（情绪卖出 / 止损 / 移动止盈 / 分档止盈）
        if pos > 0:
            sellable: int = self.get_sellable(vt_symbol)
            if sellable > 0:
                if vt_symbol not in self.max_profit_pct:
                    self._init_max_profit_from_tick(vt_symbol, tick)
                self.sell_signal.on_tick(tick)
                result: SignalResult = self.sell_signal.signal_result(vt_symbol)
                if result.type in (SignalType.SELL, SignalType.CLEAR) and result.volume > 0:
                    vt_orderids: list[str] = self.sell(
                        vt_symbol, result.price, result.volume, mark=result.reason
                    )
                    self.sell_monitor.track(
                        vt_orderids, vt_symbol, mark=result.reason
                    )
                    msg: str = self._fmt_sell_msg(vt_symbol, tick, result)
                    self.write_log(msg)
                    self.send_wecom(msg)
                    # 亏损离场（止损 / 情绪离场）后给该标的加买入冷却，避免"清仓→再买→
                    # 再清"反复止损；止盈离场不加冷却，允许新一轮拉升再进
                    if result.type == SignalType.CLEAR and self._profit_pct(vt_symbol, tick) < 0:
                        end: str = self._set_cooldown(vt_symbol, tick)
                        self.write_log(
                            f"{vt_symbol} 亏损离场，买入冷却至 {end}（含）"
                        )
            return

        # 4. 无持仓标的走买入分支：仅当日池内标的才做拉升检测（总信号维护窗口 +
        #    拉升判定 + 行业过滤）；非池内标的不创建子信号，避免无谓采样与残留
        if vt_symbol not in self.target_symbols:
            return
        self.buy_signal.on_tick(tick)
        result = self.buy_signal.signal_result(vt_symbol)
        if result.type == SignalType.BUY and result.volume > 0:
            name: str = self._get_symbol_name(vt_symbol)
            self.buy(vt_symbol, result.price, result.volume, mark=result.reason)
            self.entered.add(vt_symbol)
            self.surge_count += 1

            # 记录开仓价（用触发买入的现价近似）与初始最大收益。是否已减半由可卖量
            # 与 fixed_size 比较推断，无需额外的减半标记或复位
            self.entry_prices[vt_symbol] = tick.last_price
            self.max_profit_pct[vt_symbol] = 0.0
            self._sync_tracking_state()

            msg = self._fmt_buy_msg(vt_symbol, name, result)
            self.write_log(msg)
            self.send_wecom(msg)
            self.put_event()

    def _fmt_buy_msg(self, vt_symbol: str, name: str, result: SignalResult) -> str:
        """拼接买入信号消息（日志/企微复用）"""
        return f"快速拉升买入 {vt_symbol}({name}) {result.reason}"

    def _profit_pct(self, vt_symbol: str, tick: TickData) -> float:
        """当前持仓收益率（相对开仓价）；无开仓价记录或价格无效时返回 0。"""
        entry_price: float = self.entry_prices.get(vt_symbol, 0.0)
        if entry_price <= 0 or not tick.last_price:
            return 0.0
        return (tick.last_price - entry_price) / entry_price

    def _cooldown_end(self, stop_date: str) -> str:
        """冷却窗口最后一天（止损日 + COOLDOWN_DAYS 天，YYYYMMDD）；日期非法返回空串。"""
        try:
            stop: datetime = datetime.strptime(stop_date, "%Y%m%d")
        except ValueError:
            return ""
        return (stop + timedelta(days=self.COOLDOWN_DAYS)).strftime("%Y%m%d")

    def is_buy_cooldown(self, vt_symbol: str, today: str) -> bool:
        """该标的今天是否仍在亏损离场冷却期内（含止损当日与窗口最后一天）。

        ``today`` 为 YYYYMMDD；固定宽度下字符串比较即时间先后，只需对止损日做一次
        日期运算，供每 tick 买入前置低开销调用。
        """
        stop_date: str = self.cooldown_dates.get(vt_symbol, "")
        if not stop_date:
            return False
        end: str = self._cooldown_end(stop_date)
        return bool(end) and stop_date <= today <= end

    def _set_cooldown(self, vt_symbol: str, tick: TickData) -> str:
        """登记亏损离场日，返回冷却窗口最后一天（YYYYMMDD）。

        记录的是**止损日**本身（便于排查，且改 ``COOLDOWN_DAYS`` 对已有记录立即生效），
        是否仍在冷却期内由 ``is_buy_cooldown`` 现算。登记后立即落盘：若卖单被拒/撤单
        就不会有成交回调触发保存，重启会丢掉冷却记录。
        """
        stop_date: str = tick.datetime.strftime("%Y%m%d")
        self.cooldown_dates[vt_symbol] = stop_date
        self._sync_tracking_state()
        return self._cooldown_end(stop_date)

    def _fmt_sell_msg(self, vt_symbol: str, tick: TickData, result: SignalResult) -> str:
        """拼接卖出信号消息（日志/企微复用）"""
        name: str = self._get_symbol_name(vt_symbol)
        profit_pct: float = self._profit_pct(vt_symbol, tick)
        max_profit: float = self.max_profit_pct.get(vt_symbol, 0.0)
        return (
            f"卖出信号 {vt_symbol}({name}) {result.reason} "
            f"卖出价格 {result.price:.2f} 当前收益 {profit_pct * 100:.2f}% "
            f"最大收益 {max_profit * 100:.2f}%"
        )

    def on_bars(self, bars: dict[str, BarData]) -> None:
        """K线切片回调（本策略拉升检测走 tick，此处留空）"""
        return

    def update_trade(self, trade: TradeData) -> None:
        """成交数据更新：维护开仓价，平仓后清理止盈追踪，推送企业微信通知"""
        super().update_trade(trade)

        vt_symbol: str = trade.vt_symbol
        tracking_changed: bool = False
        if trade.direction == Direction.LONG:
            # 建仓成交（成交前仓位为 0）：用实际成交价确定开仓价，替代下单时的触发价近似。
            # 建仓之后开仓价不再被任何事件改动（分笔成交、加仓、经纪商持仓回报都不动）：
            # 涨跌比例全部以这一次确定的开仓价为准，这是减半/清仓/止损判定的唯一基准。
            pos_before: int = self.get_pos(vt_symbol) - int(trade.volume)
            if pos_before <= 0:
                self.entry_prices[vt_symbol] = trade.price
                self.max_profit_pct[vt_symbol] = 0.0
                tracking_changed = True
        else:
            # 平仓后仓位归零则清理追踪，允许后续新一轮拉升再进
            if self.get_pos(vt_symbol) <= 0:
                if vt_symbol in self.entry_prices or vt_symbol in self.max_profit_pct:
                    self.entry_prices.pop(vt_symbol, None)
                    self.max_profit_pct.pop(vt_symbol, None)
                    tracking_changed = True
                self.entered.discard(vt_symbol)

        if tracking_changed:
            self._sync_tracking_state()

        name: str = self._get_symbol_name(vt_symbol)
        direction: str = trade.direction.value if trade.direction else ""
        offset: str = trade.offset.value if trade.offset else ""
        self.send_wecom(
            f"成交通知 {vt_symbol}({name}) 方向 {direction} 开平 {offset} "
            f"价格 {trade.price:.2f} 数量 {trade.volume}"
        )

    def update_order(self, order: OrderData) -> None:
        """委托数据更新：维护卖出委托监控，并推送关键状态通知"""
        super().update_order(order)

        # 委托终结（全部成交/撤单/拒单）即去掉卖出委托监控
        self.sell_monitor.update_order(order)

        # 过滤 SUBMITTING/NOTTRADED 等中间状态噪声
        if order.status not in {
            Status.ALLTRADED, Status.PARTTRADED,
            Status.CANCELLED, Status.REJECTED
        }:
            return
        name: str = self._get_symbol_name(order.vt_symbol)
        direction: str = order.direction.value if order.direction else ""
        offset: str = order.offset.value if order.offset else ""
        status: str = order.status.value if order.status else ""
        self.send_wecom(
            f"委托通知 {order.vt_symbol}({name}) 状态 {status} 方向 {direction} "
            f"开平 {offset} 价格 {order.price:.2f} 委托 {order.volume} 成交 {order.traded}"
        )

    def refresh_universe(self) -> None:
        """每日刷新标的池：按前一交易日查库取当日成分 → 订阅新标的 → 退订不在池中且无持仓的旧标的

        DB 数据按前一交易日生成（screening 基于前一交易日收盘价），因此取数日期
        取前一交易日（由大 QMT 交易日历给出，自动跨越周末/节假日）。
        若前一交易日查不到数据，不做任何订阅/退订处理，维持昨天的订阅数据（现有订阅保持不变）。
        """
        sql_engine = self._get_sql_engine()
        if sql_engine is None:
            return

        # 取数日期：DB 数据按前一交易日生成，直接查前一交易日的行
        today: str = datetime.now().strftime("%Y%m%d")
        t_trade_date: datetime = datetime.now()
        trade_date: str = self._previous_trade_date()
        self.write_log(
            f"刷新-取前一交易日耗时 {(datetime.now() - t_trade_date).total_seconds():.2f}s "
            f"trade_date={trade_date}"
        )
        sector: str = self.industry_name.replace("'", "''")
        # 显式列出全部列，按 stock_near_ma 表结构
        sql: str = (
            f"SELECT trade_date, sector_name, code, name, close, high52w, "
            f"score, near_dates, near_days, near_values "
            f"FROM {self.TABLE_NAME} "
            f"WHERE sector_name = '{sector}' AND trade_date = '{trade_date}'"
        )
        t_query: datetime = datetime.now()
        try:
            rows: list[dict] = sql_engine.query_all(sql)
            self.write_log(
                f"刷新-数据库查询耗时 {(datetime.now() - t_query).total_seconds():.2f}s "
                f"返回 {len(rows)} 行"
            )
        except Exception as exc:  # noqa: BLE001 - 查询失败写日志，不影响策略运行
            self.write_log(
                f"查询 {self.TABLE_NAME} 失败（耗时 "
                f"{(datetime.now() - t_query).total_seconds():.2f}s）：{exc}"
            )
            return

        # 标记今日已尝试刷新（用日历当天，供 on_tick 日切检测），避免每 tick 重复查库
        self.last_refresh_date = today
        # 清理已出冷却窗口的记录，避免持久化数据无限增长
        self.cooldown_dates = {
            symbol: stop_date
            for symbol, stop_date in self.cooldown_dates.items()
            if self._cooldown_end(stop_date) >= today
        }
        if not rows:
            self.write_log(
                f"前一交易日(trade_date={trade_date})行业“{self.industry_name}”无数据，维持现有订阅"
            )
            self.strategy_engine.sync_strategy_data(self)
            self.put_event()
            return

        # code -> vt_symbol 转换，过滤无法识别的；同时保留库表全行数据
        new_targets: dict[str, str] = {}
        new_universe: dict[str, dict] = {}
        for row in rows:
            code: str = str(row.get("code", "")).strip()
            name: str = str(row.get("name", "")).strip()
            vt_symbol: str = self._code_to_vt_symbol(code)
            if vt_symbol:
                new_targets[vt_symbol] = name
                new_universe[vt_symbol] = row

        if not new_targets:
            self.write_log("查询结果中没有有效标的，维持现有订阅")
            self.put_event()
            return

        new_set: set[str] = set(new_targets.keys())
        held_symbols: set[str] = {
            vt_symbol for vt_symbol, position in self.pos_data.items()
            if position != 0
        }
        required_subscriptions: set[str] = new_set | held_symbols

        # 新订阅：当日池以及策略已有持仓中尚未订阅的
        to_subscribe: list[str] = list(
            required_subscriptions - self.subscribed_symbols
        )
        if to_subscribe:
            self.strategy_engine.subscribe_symbols(self, to_subscribe)

        # 退订：已订阅但不在当日池、且无持仓的（有持仓的不退）
        to_unsubscribe: list[str] = [
            vt_symbol for vt_symbol in (self.subscribed_symbols - new_set)
            if self.get_pos(vt_symbol) == 0
        ]
        if to_unsubscribe:
            self.strategy_engine.unsubscribe_symbols(self, to_unsubscribe)
            # 清理已退订标的的买入/卖出子信号实例与已触发缓存
            for vt_symbol in to_unsubscribe:
                self.buy_signal.remove(vt_symbol)
                self.sell_signal.remove(vt_symbol)
                self.entered.discard(vt_symbol)

        # 更新状态
        self.subscribed_symbols = self.subscribed_symbols | required_subscriptions
        # 退订的从已订阅集合移除
        self.subscribed_symbols -= set(to_unsubscribe)
        self.target_symbols = new_targets
        self.universe_data = new_universe

        # 动态标的池变化后重新读取经纪商持仓，确保每个标的独立完成 T+1 同步。
        self.strategy_engine.init_t1_position(self)

        self.write_log(
            f"标的池刷新完成：trade_date={trade_date} 行业“{self.industry_name}” "
            f"池内 {len(new_targets)} 只，"
            f"新订阅 {len(to_subscribe)} 只，退订 {len(to_unsubscribe)} 只"
        )
        self.strategy_engine.sync_strategy_data(self)
        self.put_event()

    def _get_sql_engine(self) -> any:
        """获取 SqlApp 引擎，取不到时写日志返回 None"""
        sql_engine = self.strategy_engine.main_engine.get_engine(SQLAPP_NAME)
        if sql_engine is None:
            self.write_log(f"未加载 SqlApp（{SQLAPP_NAME}），无法刷新标的池")
        return sql_engine

    def _previous_trade_date(self) -> str:
        """前一交易日（严格小于今天的最大交易日，A 股自动跨越周末/节假日）

        经大 QMT RPC 桥 ``xtdata.get_trading_dates`` 取 SH 日历。
        RPC 失败时回退为跳过周末的近似值。
        """
        today: datetime = datetime.now()
        today_str: str = today.strftime("%Y%m%d")
        try:
            # 往前看 15 天，足够跨越周末和长假（春节/国庆最长 7 天）
            start: str = (today - timedelta(days=15)).strftime("%Y%m%d")
            dates = xtdata.get_trading_dates(
                "SH", start_time=start, end_time=today_str, count=-1
            )
            trade_days: list[str] = [
                day for day in (self._as_yyyymmdd(item) for item in (dates or []))
                if day
            ]
            prev_days: list[str] = [d for d in trade_days if d < today_str]
            if prev_days:
                return max(prev_days)
            self.write_log("交易日历未返回小于今天的交易日，回退近似前一交易日")
        except Exception as exc:  # noqa: BLE001 - 回退近似值，不影响策略运行
            self.write_log(f"交易日历取前一交易日失败，回退近似：{exc}")
        # 回退：往回跳过周末（不考虑节假日）
        d: datetime = today - timedelta(days=1)
        while d.weekday() >= 5:  # 5=周六, 6=周日
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")

    @staticmethod
    def _as_yyyymmdd(value) -> str:
        """兼容 MiniQMT 毫秒时间戳和大 QMT 的 YYYYMMDD 字符串。"""
        if isinstance(value, datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, str):
            digits: str = "".join(ch for ch in value if ch.isdigit())
            return digits[:8] if len(digits) >= 8 else ""
        number: int = int(value)
        if number >= 10**12:
            return datetime.fromtimestamp(number / 1000.0).strftime("%Y%m%d")
        if number >= 10**9:
            return datetime.fromtimestamp(float(number)).strftime("%Y%m%d")
        if 19_000_000 <= number <= 21_000_000:
            return f"{number:08d}"
        return ""

    @staticmethod
    def _code_to_vt_symbol(code: str) -> str:
        """xtquant 代码（如 000001.SZ）转 vnpy vt_symbol（如 000001.SZSE）"""
        for suffix, exchange in CODE_SUFFIX_EXCHANGE.items():
            if code.endswith(suffix):
                symbol: str = code[:-len(suffix)]
                return f"{symbol}.{exchange.value}"
        return ""
