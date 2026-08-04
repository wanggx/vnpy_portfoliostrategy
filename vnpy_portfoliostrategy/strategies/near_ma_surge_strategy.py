from collections import deque
from datetime import datetime, time

from vnpy.trader.constant import Exchange
from vnpy.trader.object import TickData, BarData

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
    （5 分钟内涨幅 >= surge_pct），命中即开 fixed_size 股固定仓位并持有，
    卖出信号暂留空占位（``check_sell_signal`` 恒 False），待后续补全。
    """

    author: str = "用Python的交易员"

    # 行业过滤与数据源
    industry_name: str = ""
    table_name: str = "stock_near_ma"

    # 交易参数
    fixed_size: int = 500
    price_add: float = 0.0

    # 快速拉升检测参数
    surge_pct: float = 0.03
    surge_window: int = 300        # 拉升检测时间窗口（秒），默认 5 分钟

    # 每日刷新订阅时机
    refresh_hour: int = 9
    refresh_minute: int = 15

    parameters: list = [
        "industry_name",
        "table_name",
        "fixed_size",
        "price_add",
        "surge_pct",
        "surge_window",
        "refresh_hour",
        "refresh_minute",
    ]

    variables: list = [
        "subscribed_symbols",
        "target_symbols",
        "last_refresh_date",
        "surge_count",
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
        # 当日标的池：vt_symbol -> name
        self.target_symbols: dict[str, str] = {}
        # 最近一次刷新日期（YYYYMMDD），用于日切判断
        self.last_refresh_date: str = ""
        # 累计触发拉升买入次数
        self.surge_count: int = 0

        # 每标的价格窗口：vt_symbol -> deque[(datetime, price)]，仅保留窗口内样本
        self.price_windows: dict[str, deque] = {}
        # 本轮已触发买入的标的，避免同一波拉升重复下单
        self.entered: set[str] = set()

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
        # 1. 日切刷新：日期变更且到刷新时刻，重新拉取当日池
        tick_date: str = tick.datetime.strftime("%Y%m%d")
        if tick_date != self.last_refresh_date:
            now_time: time = tick.datetime.time()
            if now_time >= time(self.refresh_hour, self.refresh_minute):
                self.refresh_universe()

        vt_symbol: str = tick.vt_symbol
        pos: int = self.get_pos(vt_symbol)

        # 2. 有持仓走卖出分支（卖出信号暂留空）
        if pos > 0:
            if self.check_sell_signal(vt_symbol, tick):
                self.sell(
                    vt_symbol,
                    tick.last_price - self.price_add,
                    self.fixed_size
                )
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

    def check_sell_signal(self, vt_symbol: str, tick: TickData) -> bool:
        """卖出信号判定

        TODO: 卖出信号暂留空，待后续补全（如止盈/止损/尾盘平仓等）。
        当前恒返回 False，即只开仓不平仓。
        """
        return False

    def refresh_universe(self) -> None:
        """每日刷新标的池：查库取当日成分 → 订阅新标的 → 退订不在池中且无持仓的旧标的"""
        sql_engine = self._get_sql_engine()
        if sql_engine is None:
            return

        driver: str = getattr(sql_engine.database, "driver_name", "sqlite")
        ph: str = "?" if driver == "sqlite" else "%s"

        # 取该行业最新交易日的成分股（code/name）
        sql: str = (
            f"SELECT code, name FROM {self.table_name} "
            f"WHERE sector_name = {ph} "
            f"AND trade_date = (SELECT MAX(trade_date) FROM {self.table_name} "
            f"WHERE sector_name = {ph})"
        )
        try:
            rows: list[dict] = sql_engine.query_all(sql, (self.industry_name, self.industry_name))
        except Exception as exc:  # noqa: BLE001 - 查询失败写日志，不影响策略运行
            self.write_log(f"查询 {self.table_name} 失败：{exc}")
            return

        # code -> vt_symbol 转换，过滤无法识别的
        new_targets: dict[str, str] = {}
        for row in rows:
            code: str = str(row.get("code", "")).strip()
            name: str = str(row.get("name", "")).strip()
            vt_symbol: str = self._code_to_vt_symbol(code)
            if vt_symbol:
                new_targets[vt_symbol] = name

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
        self.last_refresh_date = datetime.now().strftime("%Y%m%d")

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
