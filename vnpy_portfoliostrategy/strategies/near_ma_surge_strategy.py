from collections import deque
from datetime import datetime, time, timedelta

from vnpy.trader.constant import Exchange, Direction, Status
from vnpy.trader.object import TickData, BarData, TradeData, OrderData

from xtquant import xtdata

from vnpy_sqlapp import APP_NAME as SQLAPP_NAME

from vnpy_portfoliostrategy import StrategyTemplate, StrategyEngine


# xtquant 代码后缀 -> vnpy 交易所映射
CODE_SUFFIX_EXCHANGE: dict[str, Exchange] = {
    ".SH": Exchange.SSE,
    ".SZ": Exchange.SZSE,
    ".BJ": Exchange.BSE,
}


class NearMaSurgeStrategy(StrategyTemplate):
    """Near-Ma 快速拉升策略

    每日盘前从 SqlApp 的 ``stock_near_ma`` 表按行业名取当日成分股，订阅行情；
    退订不在当日池中且无持仓的旧标的；在无持仓标的上用 tick 实时检测「快速拉升」
    （窗口内涨幅或相对昨收涨幅 >= surge_pct），命中即开 fixed_size 股固定仓位并持有。

    卖出信号（参数固定）：相对开仓价亏损超过 2% 止损；记录开仓后最大收益率，
    回撤超过 10% 卖出；最大收益 >= 10% 时最低保底 5%、>= 5% 时最低保底 2%、
    >= 3% 时不能赔钱（跌破成本即卖）。
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

    # 快速拉升检测参数
    surge_pct: float = 0.03
    surge_window: int = 300        # 拉升检测时间窗口（秒），默认 5 分钟
    MIN_SURGE_SPAN: int = 180      # 窗口至少覆盖 3 分钟才判定拉升

    # 每日盘前刷新订阅标的池的固定时刻（09:15）
    REFRESH_TIME: time = time(9, 15)

    # 止损 / 移动止盈参数（固定，不暴露为策略参数）
    STOP_LOSS_PCT: float = 0.02          # 固定止损：相对开仓价亏损 2% 即卖
    MAX_DRAWDOWN_PCT: float = 0.10       # 最大回撤上限：相对最大收益回撤 10% 即卖
    TIER_HIGH_PCT: float = 0.10          # 最大收益 >= 10% 时，最低保底 5%
    TIER_HIGH_FLOOR: float = 0.05
    TIER_LOW_PCT: float = 0.05           # 最大收益 >= 5% 时，最低保底 2%
    TIER_LOW_FLOOR: float = 0.02
    TIER_BREAKEVEN_PCT: float = 0.03     # 最大收益 >= 3% 时，不能赔钱（保底 0%）
    TIER_BREAKEVEN_FLOOR: float = 0.0

    parameters: list = [
        "industry_name",
        "fixed_size",
        "price_add",
        "surge_pct",
        "surge_window",
    ]

    variables: list = [
        "target_symbols",
        "last_refresh_date",
        "surge_count",
        "entry_prices",
        "max_profit_pct",
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

        # 每标的价格窗口：vt_symbol -> deque[(datetime, price)]，仅保留窗口内样本
        self.price_windows: dict[str, deque] = {}
        # 本轮已触发买入的标的，避免同一波拉升重复下单
        self.entered: set[str] = set()

        # 持仓追踪：开仓价与开仓后最大收益率，用于移动止盈
        self.entry_prices: dict[str, float] = {}
        self.max_profit_pct: dict[str, float] = {}

    def on_init(self) -> None:
        """策略初始化回调

        引擎在 ``on_init`` 返回后即按 ``vt_symbols`` 订阅并把 ``inited`` 置 True，
        此后就会收到 ``on_tick``。此处仅还原订阅集合；当日标的池刷新由
        ``on_tick`` 日切逻辑触发。
        """
        self.write_log("策略初始化")
        self._restore_subscribed_symbols()
        self.load_bars(1)

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

        to_subscribe: list[str] = [
            vt_symbol for vt_symbol in restored
            if vt_symbol not in self.subscribed_symbols
        ]
        if to_subscribe:
            self.strategy_engine.subscribe_symbols(self, to_subscribe)
        self.subscribed_symbols = restored
        if restored:
            self.write_log(f"订阅集合已还原：{len(restored)} 只")

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
        """同步 T+1 持仓，并用经纪商持仓均价恢复开仓成本"""
        super().sync_t1_position(vt_symbol, volume, yd_volume, price)

        broker_price: float = self.get_pos_price(vt_symbol)
        if int(volume) > 0:
            changed: bool = False
            if broker_price > 0 and self.entry_prices.get(vt_symbol) != broker_price:
                self.entry_prices[vt_symbol] = broker_price
                changed = True
            self.entered.add(vt_symbol)
            if changed:
                self._sync_tracking_state()
        else:
            if vt_symbol in self.entry_prices or vt_symbol in self.max_profit_pct:
                self.entry_prices.pop(vt_symbol, None)
                self.max_profit_pct.pop(vt_symbol, None)
                self._sync_tracking_state()
            self.entered.discard(vt_symbol)

    def _sync_tracking_state(self) -> None:
        """持久化 entry_prices / max_profit_pct 到 portfolio_strategy_data.json"""
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

    def on_tick(self, tick: TickData) -> None:
        """行情推送回调：日切刷新 + 拉升检测 + 买入/卖出"""
        # 1. 日切刷新：日期变更且到盘前刷新时刻（09:15），重新拉取当日池
        tick_date: str = tick.datetime.strftime("%Y%m%d")
        if tick_date != self.last_refresh_date:
            if tick.datetime.time() >= self.REFRESH_TIME:
                self.refresh_universe()

        vt_symbol: str = tick.vt_symbol
        pos: int = self.get_pos(vt_symbol)

        # 2. 有持仓且可卖量 > 0 才走卖出分支（止损 / 移动止盈）
        if pos > 0:
            sellable: int = self.get_sellable(vt_symbol)
            if sellable > 0:
                if vt_symbol not in self.max_profit_pct:
                    self._init_max_profit_from_tick(vt_symbol, tick)
                sell_msg: str = self.check_sell_signal(vt_symbol, tick)
                if sell_msg:
                    self.sell(vt_symbol, tick.last_price - self.price_add, sellable, mark=sell_msg)
                    self.send_wecom(sell_msg)
            return

        # 3. 仅对当日池内、无持仓的标的做拉升检测
        if vt_symbol not in self.target_symbols:
            return
        if vt_symbol in self.entered:
            return
        if not tick.last_price or tick.last_price <= 0:
            return

        # 维护窗口内样本，弹出早于 surge_window 秒前的数据
        window: deque = self.price_windows.setdefault(vt_symbol, deque())
        window.append((tick.datetime, tick.last_price))

        cutoff: datetime = tick.datetime
        while window and (cutoff - window[0][0]).total_seconds() > self.surge_window:
            window.popleft()

        # 4. 拉升判定：窗口内相对最低价涨幅 或 相对昨收涨幅 >= surge_pct（满足其一即可）
        window_gain: float | None = None
        if (cutoff - window[0][0]).total_seconds() >= self.MIN_SURGE_SPAN:
            low_price: float = min(price for _, price in window if price > 0)
            if low_price > 0:
                window_gain = (tick.last_price - low_price) / low_price

        day_gain: float | None = None
        if tick.pre_close > 0:
            day_gain = (tick.last_price - tick.pre_close) / tick.pre_close

        gain: float | None = None
        reason: str = ""
        if window_gain is not None and window_gain >= self.surge_pct:
            gain = window_gain
            reason = "窗口"
        elif day_gain is not None and day_gain >= self.surge_pct:
            gain = day_gain
            reason = "昨收"

        if gain is None:
            return

        mark: str = f"{reason}涨幅 {gain * 100:.2f}% 价格 {tick.last_price} 数量 {self.fixed_size}"
        self.buy(vt_symbol, tick.last_price + self.price_add, self.fixed_size, mark=mark)
        self.entered.add(vt_symbol)
        self.surge_count += 1

        # 记录开仓价（用触发买入的现价近似）与初始最大收益
        self.entry_prices[vt_symbol] = tick.last_price
        self.max_profit_pct[vt_symbol] = 0.0
        self._sync_tracking_state()

        name: str = self.target_symbols.get(vt_symbol, "")
        msg: str = f"快速拉升买入 {vt_symbol}({name}) {mark}"
        self.write_log(msg)
        self.send_wecom(msg)
        self.put_event()

    def on_bars(self, bars: dict[str, BarData]) -> None:
        """K线切片回调（本策略拉升检测走 tick，此处留空）"""
        return

    def update_trade(self, trade: TradeData) -> None:
        """成交数据更新：维护开仓价，平仓后清理止盈追踪，推送企业微信通知"""
        super().update_trade(trade)

        vt_symbol: str = trade.vt_symbol
        tracking_changed: bool = False
        if trade.direction == Direction.LONG:
            # 开仓：用实际成交价修正开仓价（买入信号时用的是触发价近似）
            if vt_symbol not in self.entry_prices:
                self.entry_prices[vt_symbol] = trade.price
                self.max_profit_pct[vt_symbol] = 0.0
                tracking_changed = True
            elif self.entry_prices.get(vt_symbol) != trade.price:
                self.entry_prices[vt_symbol] = trade.price
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

        name: str = self.target_symbols.get(vt_symbol, "")
        direction: str = trade.direction.value if trade.direction else ""
        offset: str = trade.offset.value if trade.offset else ""
        self.send_wecom(
            f"成交通知 {vt_symbol}({name}) 方向 {direction} 开平 {offset} "
            f"价格 {trade.price:.2f} 数量 {trade.volume}"
        )

    def update_order(self, order: OrderData) -> None:
        """委托数据更新：仅在关键状态变化时推送企业微信通知"""
        super().update_order(order)

        # 过滤 SUBMITTING/NOTTRADED 等中间状态噪声
        if order.status not in {
            Status.ALLTRADED, Status.PARTTRADED,
            Status.CANCELLED, Status.REJECTED
        }:
            return
        name: str = self.target_symbols.get(order.vt_symbol, "")
        direction: str = order.direction.value if order.direction else ""
        offset: str = order.offset.value if order.offset else ""
        status: str = order.status.value if order.status else ""
        self.send_wecom(
            f"委托通知 {order.vt_symbol}({name}) 状态 {status} 方向 {direction} "
            f"开平 {offset} 价格 {order.price:.2f} 委托 {order.volume} 成交 {order.traded}"
        )

    def check_sell_signal(self, vt_symbol: str, tick: TickData) -> str:
        """卖出信号判定：固定止损 + 移动止盈

        规则（参数固定，不可配置）：
        1. 相对开仓价亏损 >= STOP_LOSS_PCT(2%) → 止损卖出；
        2. 记录开仓后最大收益率 ``max_profit``；
        3. 当前收益相对最大收益回撤 >= MAX_DRAWDOWN_PCT(10%) → 卖出；
        4. 保底收益线（取最高适用档）：
           - 最大收益 >= 10% → 最低保证 5%；
           - 最大收益 >= 5%  → 最低保证 2%；
           - 最大收益 >= 3%  → 不能赔钱（保底 0%）；
           当前收益跌破保底线 → 卖出。

        命中则返回卖出说明字符串，否则返回空串。
        """
        entry_price: float | None = self.entry_prices.get(vt_symbol, None)
        if not entry_price or entry_price <= 0 or not tick.last_price:
            return ""

        profit_pct: float = (tick.last_price - entry_price) / entry_price

        # 更新历史最大收益率
        max_profit: float = self.max_profit_pct.get(vt_symbol, 0.0)
        if profit_pct > max_profit:
            max_profit = profit_pct
            self.max_profit_pct[vt_symbol] = max_profit
            self._sync_tracking_state()

        # 1) 固定止损：相对开仓价亏损超 STOP_LOSS_PCT
        if profit_pct <= -self.STOP_LOSS_PCT:
            return self._log_sell(
                vt_symbol, tick, profit_pct, max_profit,
                f"止损 亏损 {abs(profit_pct) * 100:.2f}% 超过 {self.STOP_LOSS_PCT * 100:.0f}%",
            )

        # 2) 回撤止盈：相对最大收益回撤超 MAX_DRAWDOWN_PCT
        drawdown: float = max_profit - profit_pct
        if drawdown >= self.MAX_DRAWDOWN_PCT:
            return self._log_sell(
                vt_symbol, tick, profit_pct, max_profit,
                f"回撤 {drawdown * 100:.2f}% 超过 {self.MAX_DRAWDOWN_PCT * 100:.0f}%",
            )

        # 3) 保底收益线
        floor: float | None = None
        if max_profit >= self.TIER_HIGH_PCT:
            floor = self.TIER_HIGH_FLOOR
        elif max_profit >= self.TIER_LOW_PCT:
            floor = self.TIER_LOW_FLOOR
        elif max_profit >= self.TIER_BREAKEVEN_PCT:
            floor = self.TIER_BREAKEVEN_FLOOR

        if floor is not None and profit_pct < floor:
            return self._log_sell(
                vt_symbol, tick, profit_pct, max_profit,
                f"跌破保底 {floor * 100:.0f}%",
            )

        return ""

    def _log_sell(
        self,
        vt_symbol: str,
        tick: TickData,
        profit_pct: float,
        max_profit: float,
        reason: str,
    ) -> str:
        """记录卖出信号日志，并返回同一条说明供 mark / 企微复用"""
        name: str = self.target_symbols.get(vt_symbol, "")
        msg: str = (
            f"卖出信号 {vt_symbol}({name}) {reason} "
            f"现价 {tick.last_price} 当前收益 {profit_pct * 100:.2f}% "
            f"最大收益 {max_profit * 100:.2f}%"
        )
        self.write_log(msg)
        return msg

    def refresh_universe(self) -> None:
        """每日刷新标的池：按前一交易日查库取当日成分 → 订阅新标的 → 退订不在池中且无持仓的旧标的

        DB 数据按前一交易日生成（screening 基于前一交易日收盘价），因此取数日期
        取前一交易日（由 xtquant 交易日历给出，自动跨越周末/节假日）。
        若前一交易日查不到数据，不做任何订阅/退订处理，维持昨天的订阅数据（现有订阅保持不变）。
        """
        sql_engine = self._get_sql_engine()
        if sql_engine is None:
            return

        # 取数日期：DB 数据按前一交易日生成，直接查前一交易日的行
        today: str = datetime.now().strftime("%Y%m%d")
        trade_date: str = self._previous_trade_date()
        sector: str = self.industry_name.replace("'", "''")
        # 显式列出全部列，按 stock_near_ma 表结构
        sql: str = (
            f"SELECT trade_date, sector_name, code, name, close, high52w, "
            f"score, near_dates, near_days, near_values "
            f"FROM {self.TABLE_NAME} "
            f"WHERE sector_name = '{sector}' AND trade_date = '{trade_date}'"
        )
        try:
            rows: list[dict] = sql_engine.query_all(sql)
        except Exception as exc:  # noqa: BLE001 - 查询失败写日志，不影响策略运行
            self.write_log(f"查询 {self.TABLE_NAME} 失败：{exc}")
            return

        # 标记今日已尝试刷新（用日历当天，供 on_tick 日切检测），避免每 tick 重复查库
        self.last_refresh_date = today
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
            # 清理已退订标的的窗口/已触发缓存
            for vt_symbol in to_unsubscribe:
                self.price_windows.pop(vt_symbol, None)
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

        优先用 xtquant 交易日历（``xtdata.get_trading_dates``，市场取 SH）；
        若 xtdata 未连接或取数失败，回退为按自然日往回跳过周末的近似值并写日志。
        """
        today: datetime = datetime.now()
        today_str: str = today.strftime("%Y%m%d")
        try:
            # 往前看 15 天，足够跨越周末和长假（春节/国庆最长 7 天）
            start: str = (today - timedelta(days=15)).strftime("%Y%m%d")
            dates = xtdata.get_trading_dates("SH", start_time=start, end_time=today_str, count=-1)
            trade_days: list[str] = [
                datetime.fromtimestamp(ts / 1000).strftime("%Y%m%d") for ts in dates
            ]
            prev_days: list[str] = [d for d in trade_days if d < today_str]
            if prev_days:
                return max(prev_days)
            self.write_log("xtquant 未返回小于今天的交易日，回退近似前一交易日")
        except Exception as exc:  # noqa: BLE001 - 回退近似值，不影响策略运行
            self.write_log(f"xtquant 取前一交易日失败，回退近似：{exc}")
        # 回退：往回跳过周末（不考虑节假日）
        d: datetime = today - timedelta(days=1)
        while d.weekday() >= 5:  # 5=周六, 6=周日
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")

    @staticmethod
    def _code_to_vt_symbol(code: str) -> str:
        """xtquant 代码（如 000001.SZ）转 vnpy vt_symbol（如 000001.SZSE）"""
        for suffix, exchange in CODE_SUFFIX_EXCHANGE.items():
            if code.endswith(suffix):
                symbol: str = code[:-len(suffix)]
                return f"{symbol}.{exchange.value}"
        return ""
