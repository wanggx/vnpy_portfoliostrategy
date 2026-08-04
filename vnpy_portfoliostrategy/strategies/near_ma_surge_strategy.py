from collections import deque
from datetime import datetime, time

from vnpy.trader.constant import Exchange, Direction
from vnpy.trader.object import TickData, BarData, TradeData

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
    （5 分钟内涨幅 >= surge_pct），命中即开 fixed_size 股固定仓位并持有。

    卖出信号为移动止盈（参数固定）：记录开仓后最大收益率，回撤超过 10% 卖出；
    最大收益 >= 10% 时最低保底 5%、>= 5% 时最低保底 2%，跌破保底即卖。
    """

    author: str = "用Python的交易员"

    # 行业过滤与数据源
    industry_name: str = ""
    TABLE_NAME: str = "stock_near_ma"

    # 交易参数
    fixed_size: int = 500
    price_add: float = 0.0

    # 快速拉升检测参数
    surge_pct: float = 0.03
    surge_window: int = 300        # 拉升检测时间窗口（秒），默认 5 分钟

    # 每日盘前刷新订阅标的池的固定时刻（09:15）
    REFRESH_TIME: time = time(9, 15)

    # 移动止盈参数（固定，不暴露为策略参数）
    MAX_DRAWDOWN_PCT: float = 0.10       # 最大回撤上限：相对最大收益回撤 10% 即卖
    TIER_HIGH_PCT: float = 0.10          # 最大收益 >= 10% 时，最低保底 5%
    TIER_HIGH_FLOOR: float = 0.05
    TIER_LOW_PCT: float = 0.05           # 最大收益 >= 5% 时，最低保底 2%
    TIER_LOW_FLOOR: float = 0.02

    parameters: list = [
        "industry_name",
        "fixed_size",
        "price_add",
        "surge_pct",
        "surge_window",
    ]

    variables: list = [
        "subscribed_symbols",
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
        self.subscribed_symbols: set[str] = set()
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
        """策略初始化回调"""
        self.write_log("策略初始化")
        self.load_bars(1)

    def on_start(self) -> None:
        """策略启动回调"""
        self.write_log("策略启动")
        # 启动即刷新当日标的池，不等次日 refresh 时刻
        self.refresh_universe()

    def on_stop(self) -> None:
        """策略停止回调"""
        self.write_log("策略停止")

    def on_tick(self, tick: TickData) -> None:
        """行情推送回调：日切刷新 + 拉升检测 + 买入/卖出"""
        # 1. 日切刷新：日期变更且到盘前刷新时刻（09:15），重新拉取当日池
        tick_date: str = tick.datetime.strftime("%Y%m%d")
        if tick_date != self.last_refresh_date:
            if tick.datetime.time() >= self.REFRESH_TIME:
                self.refresh_universe()

        vt_symbol: str = tick.vt_symbol
        pos: int = self.get_pos(vt_symbol)

        # 2. 有持仓走卖出分支（移动止盈）
        if pos > 0:
            if self.check_sell_signal(vt_symbol, tick):
                self.sell(vt_symbol, tick.last_price - self.price_add, pos)
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

        # 窗口样本不足时不判定
        if len(window) < 2:
            return

        ref_price: float = window[0][1]
        if ref_price <= 0:
            return

        # 4. 拉升判定：窗口内涨幅 >= surge_pct
        gain: float = (tick.last_price - ref_price) / ref_price
        if gain >= self.surge_pct:
            self.buy(vt_symbol, tick.last_price + self.price_add, self.fixed_size)
            self.entered.add(vt_symbol)
            self.surge_count += 1

            # 记录开仓价（用触发买入的现价近似）与初始最大收益
            self.entry_prices[vt_symbol] = tick.last_price
            self.max_profit_pct[vt_symbol] = 0.0

            name: str = self.target_symbols.get(vt_symbol, "")
            msg: str = (
                f"快速拉升买入 {vt_symbol}({name}) "
                f"涨幅 {gain * 100:.2f}% 价格 {tick.last_price} 数量 {self.fixed_size}"
            )
            self.write_log(msg)
            self.send_notification(msg)
            self.put_event()

    def on_bars(self, bars: dict[str, BarData]) -> None:
        """K线切片回调（本策略拉升检测走 tick，此处留空）"""
        return

    def update_trade(self, trade: TradeData) -> None:
        """成交数据更新：维护开仓价，平仓后清理止盈追踪"""
        super().update_trade(trade)

        vt_symbol: str = trade.vt_symbol
        if trade.direction == Direction.LONG:
            # 开仓：用实际成交价修正开仓价（买入信号时用的是触发价近似）
            if vt_symbol not in self.entry_prices:
                self.entry_prices[vt_symbol] = trade.price
                self.max_profit_pct[vt_symbol] = 0.0
        else:
            # 平仓后仓位归零则清理追踪，允许后续新一轮拉升再进
            if self.get_pos(vt_symbol) <= 0:
                self.entry_prices.pop(vt_symbol, None)
                self.max_profit_pct.pop(vt_symbol, None)
                self.entered.discard(vt_symbol)

    def check_sell_signal(self, vt_symbol: str, tick: TickData) -> bool:
        """卖出信号判定：移动止盈

        规则（参数固定，不可配置）：
        1. 记录开仓后最大收益率 ``max_profit``；
        2. 当前收益相对最大收益回撤 >= MAX_DRAWDOWN_PCT(10%) → 卖出；
        3. 保底收益线（取两者较大者生效）：
           - 最大收益 >= 10% → 最低保证 5%；
           - 最大收益 >= 5%  → 最低保证 2%；
           当前收益跌破保底线 → 卖出。
        """
        entry_price: float | None = self.entry_prices.get(vt_symbol, None)
        if not entry_price or entry_price <= 0 or not tick.last_price:
            return False

        profit_pct: float = (tick.last_price - entry_price) / entry_price

        # 更新历史最大收益率
        max_profit: float = self.max_profit_pct.get(vt_symbol, 0.0)
        if profit_pct > max_profit:
            max_profit = profit_pct
            self.max_profit_pct[vt_symbol] = max_profit

        # 1) 回撤止盈：相对最大收益回撤超 MAX_DRAWDOWN_PCT
        drawdown: float = max_profit - profit_pct
        if drawdown >= self.MAX_DRAWDOWN_PCT:
            self._log_sell(vt_symbol, tick, profit_pct, max_profit,
                           f"回撤 {drawdown * 100:.2f}% 超过 {self.MAX_DRAWDOWN_PCT * 100:.0f}%")
            return True

        # 2) 保底收益线
        floor: float = 0.0
        if max_profit >= self.TIER_HIGH_PCT:
            floor = self.TIER_HIGH_FLOOR
        elif max_profit >= self.TIER_LOW_PCT:
            floor = self.TIER_LOW_FLOOR

        if floor > 0 and profit_pct < floor:
            self._log_sell(vt_symbol, tick, profit_pct, max_profit,
                           f"跌破保底 {floor * 100:.0f}%")
            return True

        return False

    def _log_sell(
        self,
        vt_symbol: str,
        tick: TickData,
        profit_pct: float,
        max_profit: float,
        reason: str,
    ) -> None:
        """记录卖出信号日志与通知"""
        name: str = self.target_symbols.get(vt_symbol, "")
        msg: str = (
            f"卖出信号 {vt_symbol}({name}) {reason} "
            f"现价 {tick.last_price} 当前收益 {profit_pct * 100:.2f}% "
            f"最大收益 {max_profit * 100:.2f}%"
        )
        self.write_log(msg)
        self.send_notification(msg)

    def refresh_universe(self) -> None:
        """每日刷新标的池：按当天日期查库取当日成分 → 订阅新标的 → 退订不在池中且无持仓的旧标的

        取数日期固定为当天；若当天查不到数据，不做任何订阅/退订处理，
        维持昨天的订阅数据（现有订阅保持不变）。
        """
        sql_engine = self._get_sql_engine()
        if sql_engine is None:
            return

        # 取数日期固定为当天；行业名/日期为可信策略参数，直接内联进 SQL
        today: str = datetime.now().strftime("%Y%m%d")
        sector: str = self.industry_name.replace("'", "''")
        # 显式列出全部列，按 stock_near_ma 表结构
        sql: str = (
            f"SELECT trade_date, sector_name, code, name, close, high52w, "
            f"score, near_dates, near_days, near_values "
            f"FROM {self.TABLE_NAME} "
            f"WHERE sector_name = '{sector}' AND trade_date = '{today}'"
        )
        try:
            rows: list[dict] = sql_engine.query_all(sql)
        except Exception as exc:  # noqa: BLE001 - 查询失败写日志，不影响策略运行
            self.write_log(f"查询 {self.TABLE_NAME} 失败：{exc}")
            return

        # 当天无数据：维持现有订阅，不做任何处理；仅标记今日已尝试，避免每 tick 重复查库
        self.last_refresh_date = today
        if not rows:
            self.write_log(
                f"当日(trade_date={today})行业“{self.industry_name}”无数据，维持现有订阅"
            )
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

        new_set: set[str] = set(new_targets.keys())

        # 新订阅：在当日池但尚未订阅的
        to_subscribe: list[str] = list(new_set - self.subscribed_symbols)
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
        self.subscribed_symbols = self.subscribed_symbols | new_set
        # 退订的从已订阅集合移除
        self.subscribed_symbols -= set(to_unsubscribe)
        self.target_symbols = new_targets
        self.universe_data = new_universe

        self.write_log(
            f"标的池刷新完成：池内 {len(new_targets)} 只，"
            f"新订阅 {len(to_subscribe)} 只，退订 {len(to_unsubscribe)} 只"
        )
        self.put_event()

    def _get_sql_engine(self) -> any:
        """获取 SqlApp 引擎，取不到时写日志返回 None"""
        sql_engine = self.strategy_engine.main_engine.get_engine(SQLAPP_NAME)
        if sql_engine is None:
            self.write_log(f"未加载 SqlApp（{SQLAPP_NAME}），无法刷新标的池")
        return sql_engine

    @staticmethod
    def _code_to_vt_symbol(code: str) -> str:
        """xtquant 代码（如 000001.SZ）转 vnpy vt_symbol（如 000001.SZSE）"""
        for suffix, exchange in CODE_SUFFIX_EXCHANGE.items():
            if code.endswith(suffix):
                symbol: str = code[:-len(suffix)]
                return f"{symbol}.{exchange.value}"
        return ""
