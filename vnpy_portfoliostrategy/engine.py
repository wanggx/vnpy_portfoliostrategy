import importlib
import glob
import traceback
from typing import Any
from collections import defaultdict
from pathlib import Path
from types import ModuleType
from collections.abc import Callable
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import BaseEngine, MainEngine, LogEngine
from vnpy.trader.object import (
    OrderRequest,
    CancelRequest,
    SubscribeRequest,
    HistoryRequest,
    LogData,
    TickData,
    OrderData,
    TradeData,
    BarData,
    ContractData,
    PositionData
)
from vnpy.trader.event import (
    EVENT_TICK,
    EVENT_ORDER,
    EVENT_TRADE,
    EVENT_POSITION
)
from vnpy.trader.constant import (
    Direction,
    OrderType,
    Interval,
    Exchange,
    Offset,
    Status
)
from vnpy.trader.utility import load_json, save_json, extract_vt_symbol, round_to
from vnpy.trader.datafeed import BaseDatafeed, get_datafeed
from vnpy.trader.database import BaseDatabase, get_database, DB_TZ

from .base import (
    APP_NAME,
    EVENT_PORTFOLIO_LOG,
    EVENT_PORTFOLIO_STRATEGY,
    EngineType
)
from .locale import _
from .template import StrategyTemplate


class StrategyEngine(BaseEngine):
    """组合策略引擎"""

    engine_type: EngineType = EngineType.LIVE

    setting_filename: str = "portfolio_strategy_setting.json"
    data_filename: str = "portfolio_strategy_data.json"

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__(main_engine, event_engine, APP_NAME)

        self.strategy_data: dict[str, dict] = {}

        self.classes: dict[str, type[StrategyTemplate]] = {}
        self.strategies: dict[str, StrategyTemplate] = {}

        self.symbol_strategy_map: dict[str, list[StrategyTemplate]] = defaultdict(list)
        self.orderid_strategy_map: dict[str, StrategyTemplate] = {}
        self.orderid_reference_map: dict[str, str] = {}                 # vt_orderid: reference

        self.init_executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)

        self.vt_tradeids: set[str] = set()

        # T+1 卖出冻结量（参考 vnpy_ctastrategy）：vt_orderid -> volume，
        # 在发起卖出委托时冻结、撤单/成交回报时释放。
        self.sell_frozen_by_orderid: dict[str, tuple[str, float]] = {}

        # 数据库和数据服务
        self.database: BaseDatabase = get_database()
        self.datafeed: BaseDatafeed = get_datafeed()

    def init_engine(self) -> None:
        """初始化引擎"""
        self.init_datafeed()
        self.load_strategy_class()
        self.load_strategy_setting()
        self.load_strategy_data()
        self.register_event()
        self.write_log(_("组合策略引擎初始化成功"))

    def close(self) -> None:
        """关闭"""
        self.stop_all_strategies()

    def register_event(self) -> None:
        """注册事件引擎"""
        self.event_engine.register(EVENT_TICK, self.process_tick_event)
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)
        self.event_engine.register(EVENT_POSITION, self.process_position_event)

        log_engine: LogEngine = self.main_engine.get_engine("log")
        log_engine.register_log(EVENT_PORTFOLIO_LOG)

    def init_datafeed(self) -> None:
        """初始化数据服务"""
        result: bool = self.datafeed.init(self.write_log)
        if result:
            self.write_log(_("数据服务初始化成功"))

    def query_bar_from_datafeed(
        self, symbol: str, exchange: Exchange, interval: Interval, start: datetime, end: datetime
    ) -> list[BarData]:
        """通过数据服务获取历史数据"""
        req: HistoryRequest = HistoryRequest(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            start=start,
            end=end
        )
        data: list[BarData] = self.datafeed.query_bar_history(req, self.write_log)
        return data

    def process_tick_event(self, event: Event) -> None:
        """行情数据推送"""
        tick: TickData = event.data

        strategies: list = self.symbol_strategy_map[tick.vt_symbol]
        if not strategies:
            return

        for strategy in strategies:
            if strategy.inited:
                self.call_strategy_func(strategy, strategy.on_tick, tick)

    def process_order_event(self, event: Event) -> None:
        """委托数据推送"""
        order: OrderData = event.data

        # 部分网关在后续委托推送中不再携带 reference，用本地缓存补回
        order.reference = self.orderid_reference_map.get(
            order.vt_orderid,
            order.reference
        )

        strategy: StrategyTemplate | None = self.orderid_strategy_map.get(order.vt_orderid, None)
        if not strategy:
            return

        # 撤单/拒单：释放该委托冻结的可卖量
        if strategy.t1 and order.status in {Status.CANCELLED, Status.REJECTED}:
            frozen = self.sell_frozen_by_orderid.get(order.vt_orderid)
            if frozen:
                self.release_t1_sell_frozen(strategy, order.vt_orderid, frozen[1])

        self.call_strategy_func(strategy, strategy.update_order, order)

        # 委托终结后清理本地缓存
        if not order.is_active():
            self.orderid_reference_map.pop(order.vt_orderid, None)

    def process_trade_event(self, event: Event) -> None:
        """成交数据推送"""
        trade: TradeData = event.data

        # 过滤重复的成交推送
        if trade.vt_tradeid in self.vt_tradeids:
            return
        self.vt_tradeids.add(trade.vt_tradeid)

        # 推送给策略
        strategy: StrategyTemplate | None = self.orderid_strategy_map.get(trade.vt_orderid, None)
        if not strategy:
            return

        # T+1 卖出成交：释放对应冻结量（先于 update_trade，使可卖量一致）
        if strategy.t1 and trade.direction == Direction.SHORT:
            self.release_t1_sell_frozen(strategy, trade.vt_orderid, trade.volume)

        self.call_strategy_func(strategy, strategy.update_trade, trade)

    def process_position_event(self, event: Event) -> None:
        """持仓数据推送：同步 T+1 策略的昨/今仓"""
        position: PositionData = event.data

        # 仅多头/净持仓有意义（A 股现货为 NET）
        if position.direction not in {Direction.LONG, Direction.NET}:
            return

        strategies: list = self.symbol_strategy_map.get(position.vt_symbol, [])
        for strategy in strategies:
            if not strategy.t1:
                continue
            strategy.sync_t1_position(
                position.vt_symbol, position.volume, position.yd_volume, position.price
            )
            self.put_strategy_event(strategy)

    def send_order(
        self,
        strategy: StrategyTemplate,
        vt_symbol: str,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        lock: bool,
        net: bool,
        mark: str = "",
    ) -> list:
        """发送委托"""
        contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
        if not contract:
            self.write_log(_("委托失败，找不到合约：{}").format(vt_symbol), strategy)
            return []

        price = round_to(price, contract.pricetick)
        volume = round_to(volume, contract.min_volume)

        # T+1 下单校验：禁止开空/买平、校验可卖量、校验整手
        if not self.check_t1_order(strategy, vt_symbol, direction, offset, volume):
            return []

        original_req: OrderRequest = OrderRequest(
            symbol=contract.symbol,
            exchange=contract.exchange,
            direction=direction,
            offset=offset,
            type=OrderType.LIMIT,
            price=price,
            volume=volume,
            reference=self.create_order_reference(strategy, mark)
        )

        req_list: list[OrderRequest] = self.main_engine.convert_order_request(
            original_req,
            contract.gateway_name,
            lock,
            net
        )

        vt_orderids: list = []

        for req in req_list:
            vt_orderid: str = self.main_engine.send_order(
                req, contract.gateway_name)

            if not vt_orderid:
                continue

            vt_orderids.append(vt_orderid)

            self.orderid_reference_map[vt_orderid] = req.reference

            self.main_engine.update_order_request(req, vt_orderid, contract.gateway_name)

            self.orderid_strategy_map[vt_orderid] = strategy

            # T+1 卖出委托：冻结可卖量
            if self.is_t1_sell_order(strategy, direction, offset):
                self.freeze_t1_sell(strategy, vt_symbol, vt_orderid, req.volume)

        return vt_orderids

    @staticmethod
    def create_order_reference(strategy: StrategyTemplate, mark: str) -> str:
        """构造委托 reference：APP_NAME_策略名[:mark]"""
        reference: str = f"{APP_NAME}_{strategy.strategy_name}"
        if mark:
            reference = f"{reference}:{mark}"
        return reference

    @staticmethod
    def get_order_mark(reference: str) -> str:
        """从 reference 中提取触发标记（mark）"""
        _, separator, mark = reference.partition(":")
        return mark if separator else ""

    def cancel_order(self, strategy: StrategyTemplate, vt_orderid: str) -> None:
        """委托撤单"""
        order: OrderData | None = self.main_engine.get_order(vt_orderid)
        if not order:
            self.write_log(f"撤单失败，找不到委托{vt_orderid}", strategy)
            return

        req: CancelRequest = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)

    def cancel_all(self, strategy: StrategyTemplate) -> None:
        """委托撤单"""
        for vt_orderid in list(strategy.active_orderids):
            self.cancel_order(strategy, vt_orderid)

    def is_t1_sell_order(
        self, strategy: StrategyTemplate, direction: Direction, offset: Offset
    ) -> bool:
        """是否为 T+1 模式下的卖出平仓委托"""
        return strategy.t1 and direction == Direction.SHORT and offset == Offset.CLOSE

    def check_t1_order(
        self,
        strategy: StrategyTemplate,
        vt_symbol: str,
        direction: Direction,
        offset: Offset,
        volume: float
    ) -> bool:
        """T+1 下单前校验（参考 vnpy_ctastrategy）

        - 禁止开空（SHORT + OPEN）与买平（LONG + CLOSE）；
        - 卖出平仓需已同步持仓，且数量不超过可卖量（昨仓减冻结）；
        - 买入数量须为 100 的整数倍（A 股最小买入手数）。
        """
        if not strategy.t1:
            return True

        if direction == Direction.SHORT and offset != Offset.CLOSE:
            self.write_log("T+1 模式禁止开空仓", strategy)
            return False

        if direction == Direction.LONG and offset == Offset.CLOSE:
            self.write_log("T+1 模式禁止买平", strategy)
            return False

        if direction == Direction.LONG and offset == Offset.OPEN and volume % 100:
            self.write_log(
                f"T+1 模式要求买入数量为 100 的整数倍：{volume}", strategy
            )
            return False

        if self.is_t1_sell_order(strategy, direction, offset):
            if not strategy.is_position_synced(vt_symbol):
                self.write_log(
                    f"T+1 模式持仓未同步，禁止卖出（vt_symbol={vt_symbol}）",
                    strategy
                )
                return False

            available: int = strategy.get_sellable(vt_symbol)
            if volume > available:
                self.write_log(
                    f"T+1 可卖量不足：委托 {volume} 可卖 {available}（vt_symbol={vt_symbol}）",
                    strategy
                )
                return False

        return True

    def freeze_t1_sell(
        self,
        strategy: StrategyTemplate,
        vt_symbol: str,
        vt_orderid: str,
        volume: float,
    ) -> None:
        """冻结 T+1 卖出可卖量"""
        if not strategy.t1 or not volume:
            return

        strategy.sell_frozen_data[vt_symbol] += volume
        frozen = self.sell_frozen_by_orderid.get(vt_orderid)
        frozen_volume: float = frozen[1] if frozen else 0
        self.sell_frozen_by_orderid[vt_orderid] = (
            vt_symbol, frozen_volume + volume
        )

    def release_t1_sell_frozen(
        self, strategy: StrategyTemplate, vt_orderid: str, volume: float
    ) -> None:
        """释放 T+1 卖出冻结量（撤单/拒单/成交回报时）"""
        if not strategy.t1 or volume <= 0:
            return

        frozen = self.sell_frozen_by_orderid.get(vt_orderid)
        if not frozen:
            return

        vt_symbol, frozen_volume = frozen
        release_volume: float = min(volume, frozen_volume)
        if release_volume <= 0:
            return

        strategy.sell_frozen_data[vt_symbol] = max(
            strategy.sell_frozen_data.get(vt_symbol, 0) - release_volume, 0
        )

        frozen_volume -= release_volume
        if frozen_volume > 0:
            self.sell_frozen_by_orderid[vt_orderid] = (vt_symbol, frozen_volume)
        else:
            self.sell_frozen_by_orderid.pop(vt_orderid, None)

    def init_t1_position(self, strategy: StrategyTemplate) -> None:
        """初始化时从缓存持仓同步 T+1 昨/今仓，并向网关请求最新持仓"""
        if not strategy.t1:
            return

        strategy.position_synced = False
        strategy.position_synced_symbols.clear()

        for position in self.main_engine.get_all_positions():
            if position.direction not in {Direction.LONG, Direction.NET}:
                continue
            if position.vt_symbol not in strategy.vt_symbols:
                continue
            strategy.sync_t1_position(
                position.vt_symbol, position.volume, position.yd_volume, position.price
            )

        # 请求各网关刷新持仓
        queried_gateways: set[str] = set()
        for vt_symbol in strategy.vt_symbols:
            contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
            if not contract:
                continue
            gateway_name: str = contract.gateway_name
            if gateway_name in queried_gateways:
                continue
            gateway = self.main_engine.get_gateway(gateway_name)
            if gateway and hasattr(gateway, "query_position"):
                gateway.query_position()
                queried_gateways.add(gateway_name)

    def subscribe_symbols(
        self, strategy: StrategyTemplate, vt_symbols: list[str]
    ) -> None:
        """运行期为策略动态订阅行情

        与 ``_init_strategy`` 中的订阅逻辑一致：取合约、构造带订阅者标识的
        ``SubscribeRequest`` 调 ``MainEngine.subscribe``，并同步更新
        ``strategy.vt_symbols`` 与 ``symbol_strategy_map``（行情路由依赖它，
        见 ``process_tick_event``）。已订阅的标的会跳过，避免重复订阅。
        找不到合约时写日志不抛异常，保证批量订阅不被单只异常中断。

        仅在 ``inited`` 后写入 setting JSON；``on_init`` 阶段配置已读入且尚未完成初始化，不落盘。
        """
        changed: bool = False
        for vt_symbol in vt_symbols:
            if not vt_symbol:
                continue

            # 已在该标的的策略列表中则跳过，避免重复订阅
            strategies: list = self.symbol_strategy_map[vt_symbol]
            if strategy in strategies:
                continue

            contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
            if not contract:
                self.write_log(_("行情订阅失败，找不到合约{}").format(vt_symbol), strategy)
                continue

            req: SubscribeRequest = SubscribeRequest(
                symbol=contract.symbol,
                exchange=contract.exchange,
                app_name=APP_NAME,
                subscriber_name=strategy.strategy_name,
            )
            self.main_engine.subscribe(req, contract.gateway_name)

            strategies.append(strategy)
            if vt_symbol not in strategy.vt_symbols:
                strategy.vt_symbols.append(vt_symbol)
            changed = True

        if changed and strategy.inited:
            self.save_strategy_setting()

    def unsubscribe_symbols(
        self, strategy: StrategyTemplate, vt_symbols: list[str]
    ) -> None:
        """运行期为策略动态退订行情

        与 ``remove_strategy`` 中的退订逻辑一致：构造带订阅者标识的
        ``SubscribeRequest`` 调 ``MainEngine.unsubscribe``（按订阅者退订，
        不影响其他策略对该标的的订阅），并从 ``symbol_strategy_map`` 与
        ``strategy.vt_symbols`` 移除该项。合约不存在时仅清理本地登记。
        仅在 ``inited`` 后写入 setting JSON。
        """
        changed: bool = False
        for vt_symbol in vt_symbols:
            if not vt_symbol:
                continue

            contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
            if contract:
                req: SubscribeRequest = SubscribeRequest(
                    symbol=contract.symbol,
                    exchange=contract.exchange,
                    app_name=APP_NAME,
                    subscriber_name=strategy.strategy_name,
                )
                self.main_engine.unsubscribe(req, contract.gateway_name)

            strategies: list = self.symbol_strategy_map.get(vt_symbol, [])
            if strategy in strategies:
                strategies.remove(strategy)

            if vt_symbol in strategy.vt_symbols:
                strategy.vt_symbols.remove(vt_symbol)
            changed = True

        if changed and strategy.inited:
            self.save_strategy_setting()

    def get_engine_type(self) -> EngineType:
        """获取引擎类型"""
        return self.engine_type

    def get_pricetick(self, strategy: StrategyTemplate, vt_symbol: str) -> float | None:
        """获取合约价格跳动"""
        contract: ContractData | None = self.main_engine.get_contract(vt_symbol)

        if contract:
            pricetick: float = contract.pricetick
            return pricetick
        else:
            return None

    def get_size(self, strategy: StrategyTemplate, vt_symbol: str) -> int | None:
        """获取合约乘数"""
        contract: ContractData | None = self.main_engine.get_contract(vt_symbol)

        if contract:
            size: int = contract.size
            return size
        else:
            return None

    def load_bars(self, strategy: StrategyTemplate, days: int, interval: Interval) -> None:
        """加载历史数据"""
        vt_symbols: list = strategy.vt_symbols
        dts_set: set[datetime] = set()
        history_data: dict[tuple, BarData] = {}

        # 通过接口、数据服务、数据库获取历史数据
        for vt_symbol in vt_symbols:
            data: list[BarData] = self.load_bar(vt_symbol, days, interval)

            for bar in data:
                dts_set.add(bar.datetime)
                history_data[(bar.datetime, vt_symbol)] = bar

        dts: list[datetime] = list(dts_set)
        dts.sort()

        bars: dict = {}

        for dt in dts:
            for vt_symbol in vt_symbols:
                bar = history_data.get((dt, vt_symbol), None)

                # 如果获取到合约指定时间的历史数据，缓存进bars字典
                if bar:
                    bars[vt_symbol] = bar
                # 如果获取不到，但bars字典中已有合约数据缓存, 使用之前的数据填充
                elif vt_symbol in bars:
                    old_bar: BarData = bars[vt_symbol]

                    bar = BarData(
                        symbol=old_bar.symbol,
                        exchange=old_bar.exchange,
                        datetime=dt,
                        open_price=old_bar.close_price,
                        high_price=old_bar.close_price,
                        low_price=old_bar.close_price,
                        close_price=old_bar.close_price,
                        gateway_name=old_bar.gateway_name
                    )
                    bars[vt_symbol] = bar

            self.call_strategy_func(strategy, strategy.on_bars, bars)

    def load_bar(self, vt_symbol: str, days: int, interval: Interval) -> list[BarData]:
        """加载单个合约历史数据"""
        symbol, exchange = extract_vt_symbol(vt_symbol)
        end: datetime = datetime.now(DB_TZ)
        start: datetime = end - timedelta(days)
        contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
        data: list[BarData]

        # 通过接口获取历史数据
        if contract and contract.history_data:
            req: HistoryRequest = HistoryRequest(
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                start=start,
                end=end
            )
            data = self.main_engine.query_history(req, contract.gateway_name)

        # 通过数据服务获取历史数据
        else:
            data = self.query_bar_from_datafeed(symbol, exchange, interval, start, end)

        # 通过数据库获取数据
        if not data:
            data = self.database.load_bar_data(
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                start=start,
                end=end,
            )

        return data

    def call_strategy_func(self, strategy: StrategyTemplate, func: Callable, params: object = None) -> None:
        """安全调用策略函数"""
        try:
            if params:
                func(params)
            else:
                func()
        except Exception:
            strategy.trading = False
            strategy.inited = False

            msg: str = _("触发异常已停止\n{}").format(traceback.format_exc())
            self.write_log(msg, strategy)

    def add_strategy(
        self, class_name: str, strategy_name: str, vt_symbols: list, setting: dict
    ) -> None:
        """添加策略实例"""
        if strategy_name in self.strategies:
            self.write_log(_("创建策略失败，存在重名{}").format(strategy_name))
            return

        strategy_class: type[StrategyTemplate] | None = self.classes.get(class_name, None)
        if not strategy_class:
            self.write_log(_("创建策略失败，找不到策略类{}").format(class_name))
            return

        strategy: StrategyTemplate = strategy_class(self, strategy_name, vt_symbols, setting)
        self.strategies[strategy_name] = strategy

        for vt_symbol in vt_symbols:
            strategies: list = self.symbol_strategy_map[vt_symbol]
            strategies.append(strategy)

        self.save_strategy_setting()
        self.put_strategy_event(strategy)

    def init_strategy(self, strategy_name: str) -> None:
        """初始化策略"""
        self.init_executor.submit(self._init_strategy, strategy_name)

    def _init_strategy(self, strategy_name: str) -> None:
        """初始化策略"""
        strategy: StrategyTemplate = self.strategies[strategy_name]

        if strategy.inited:
            self.write_log(_("{}已经完成初始化，禁止重复操作").format(strategy_name))
            return

        self.write_log(_("{}开始执行初始化").format(strategy_name))

        # 调用策略on_init函数
        self.call_strategy_func(strategy, strategy.on_init)

        # 恢复策略状态
        data: dict | None = self.strategy_data.get(strategy_name, None)
        if data:
            for name in strategy.variables:
                value: object | None = data.get(name, None)
                if value is None:
                    continue

                # 对于持仓和目标数据字典，需要使用dict.update更新defaultdict
                if name in {"yd_pos_data", "td_pos_data",
                            "sell_frozen_data", "position_synced"}:
                    continue

                if name in {"pos_data", "target_data", "pos_price_data"}:
                    strategy_data = getattr(strategy, name)
                    strategy_data.update(value)
                # 对于其他int/float/str/bool字段则可以直接赋值
                else:
                    setattr(strategy, name, value)

        # 订阅行情
        for vt_symbol in strategy.vt_symbols:
            contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
            if contract:
                req: SubscribeRequest = SubscribeRequest(
                    symbol=contract.symbol,
                    exchange=contract.exchange,
                    app_name=APP_NAME,
                    subscriber_name=strategy.strategy_name
                )
                self.main_engine.subscribe(req, contract.gateway_name)
            else:
                self.write_log(_("行情订阅失败，找不到合约{}").format(vt_symbol), strategy)

        # T+1 模式：同步经纪商持仓的昨/今仓
        self.init_t1_position(strategy)

        # 推送策略事件通知初始化完成状态
        strategy.inited = True
        self.put_strategy_event(strategy)
        self.write_log(_("{}初始化完成").format(strategy_name))

    def start_strategy(self, strategy_name: str) -> None:
        """启动策略"""
        strategy: StrategyTemplate = self.strategies[strategy_name]
        if not strategy.inited:
            self.write_log(_("策略{}启动失败，请先初始化").format(strategy.strategy_name))
            return

        if strategy.trading:
            self.write_log(_("{}已经启动，请勿重复操作").format(strategy_name))
            return

        # 调用策略on_start函数
        self.call_strategy_func(strategy, strategy.on_start)

        # 推送策略事件通知启动完成状态
        strategy.trading = True
        self.put_strategy_event(strategy)

    def stop_strategy(self, strategy_name: str) -> None:
        """停止策略"""
        strategy: StrategyTemplate = self.strategies[strategy_name]
        if not strategy.trading:
            return

        # 调用策略on_stop函数
        self.call_strategy_func(strategy, strategy.on_stop)

        # 将交易状态设为False
        strategy.trading = False

        # 撤销全部委托
        self.cancel_all(strategy)

        # 同步数据状态
        self.sync_strategy_data(strategy)

        # 推送策略事件通知停止完成状态
        self.put_strategy_event(strategy)

    def edit_strategy(self, strategy_name: str, setting: dict) -> None:
        """编辑策略参数"""
        strategy: StrategyTemplate = self.strategies[strategy_name]
        strategy.update_setting(setting)

        self.save_strategy_setting()
        self.put_strategy_event(strategy)

    def remove_strategy(self, strategy_name: str) -> bool:
        """移除策略实例"""
        strategy: StrategyTemplate = self.strategies[strategy_name]
        if strategy.trading:
            self.write_log(_("策略{}移除失败，请先停止").format(strategy.strategy_name))
            return False

        for vt_symbol in strategy.vt_symbols:
            contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
            if contract:
                req: SubscribeRequest = SubscribeRequest(
                    symbol=contract.symbol,
                    exchange=contract.exchange,
                    app_name=APP_NAME,
                    subscriber_name=strategy.strategy_name
                )
                self.main_engine.unsubscribe(req, contract.gateway_name)

            strategies: list = self.symbol_strategy_map[vt_symbol]
            strategies.remove(strategy)

        for vt_orderid in strategy.active_orderids:
            if vt_orderid in self.orderid_strategy_map:
                self.orderid_strategy_map.pop(vt_orderid)

        self.strategies.pop(strategy_name)
        self.save_strategy_setting()

        self.strategy_data.pop(strategy_name, None)
        save_json(self.data_filename, self.strategy_data)

        return True

    def load_strategy_class(self) -> None:
        """加载策略类"""
        path1: Path = Path(__file__).parent.joinpath("strategies")
        self.load_strategy_class_from_folder(path1, "vnpy_portfoliostrategy.strategies")

        path2: Path = Path.cwd().joinpath("strategies")
        self.load_strategy_class_from_folder(path2, "strategies")

    def load_strategy_class_from_folder(self, path: Path, module_name: str = "") -> None:
        """通过指定文件夹加载策略类"""
        for suffix in ["py", "pyd", "so"]:
            pathname: str = str(path.joinpath(f"*.{suffix}"))
            for filepath in glob.glob(pathname):
                stem: str = Path(filepath).stem
                strategy_module_name: str = f"{module_name}.{stem}"
                self.load_strategy_class_from_module(strategy_module_name)

    def load_strategy_class_from_module(self, module_name: str) -> None:
        """通过策略文件加载策略类"""
        try:
            module: ModuleType = importlib.import_module(module_name)

            for name in dir(module):
                value = getattr(module, name)
                if (isinstance(value, type) and issubclass(value, StrategyTemplate) and value is not StrategyTemplate):
                    self.classes[value.__name__] = value
        except:  # noqa
            msg: str = _("策略文件{}加载失败，触发异常：\n{}").format(module_name, traceback.format_exc())
            self.write_log(msg)

    def load_strategy_data(self) -> None:
        """加载策略数据"""
        self.strategy_data = load_json(self.data_filename)

    def sync_strategy_data(self, strategy: StrategyTemplate) -> None:
        """保存策略数据到文件"""
        data: dict = strategy.get_variables()
        data.pop("inited")      # 不保存策略状态信息
        data.pop("trading")
        for name in {
            "yd_pos_data", "td_pos_data", "sell_frozen_data", "position_synced"
        }:
            data.pop(name, None)

        self.strategy_data[strategy.strategy_name] = data
        save_json(self.data_filename, self.strategy_data)

    def get_all_strategy_class_names(self) -> list:
        """获取所有加载策略类名"""
        return list(self.classes.keys())

    def get_strategy_class_parameters(self, class_name: str) -> dict:
        """获取策略类参数"""
        strategy_class: type[StrategyTemplate] = self.classes[class_name]

        return strategy_class.get_class_parameters()

    def get_strategy_parameters(self, strategy_name: str) -> dict:
        """获取策略参数"""
        strategy: StrategyTemplate = self.strategies[strategy_name]
        return strategy.get_parameters()

    def init_all_strategies(self) -> None:
        """初始化所有策略"""
        for strategy_name in self.strategies.keys():
            self.init_strategy(strategy_name)

    def start_all_strategies(self) -> None:
        """启动所有策略"""
        for strategy_name in self.strategies.keys():
            self.start_strategy(strategy_name)

    def stop_all_strategies(self) -> None:
        """停止所有策略"""
        for strategy_name in self.strategies.keys():
            self.stop_strategy(strategy_name)

    def load_strategy_setting(self) -> None:
        """加载策略配置"""
        strategy_setting: dict = load_json(self.setting_filename)

        for strategy_name, strategy_config in strategy_setting.items():
            self.add_strategy(
                strategy_config["class_name"],
                strategy_name,
                strategy_config["vt_symbols"],
                strategy_config["setting"]
            )

    def save_strategy_setting(self) -> None:
        """保存策略配置"""
        strategy_setting: dict = {}

        for name, strategy in self.strategies.items():
            strategy_setting[name] = {
                "class_name": strategy.__class__.__name__,
                "vt_symbols": strategy.vt_symbols,
                "setting": strategy.get_parameters()
            }

        save_json(self.setting_filename, strategy_setting)

    def put_strategy_event(self, strategy: StrategyTemplate) -> None:
        """推送事件更新策略界面"""
        data: dict = strategy.get_data()
        event: Event = Event(EVENT_PORTFOLIO_STRATEGY, data)
        self.event_engine.put(event)

    def write_log(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """输出日志"""
        if strategy:
            msg = f"{strategy.strategy_name}: {msg}"

        log: LogData = LogData(msg=msg, gateway_name=APP_NAME)
        event: Event = Event(type=EVENT_PORTFOLIO_LOG, data=log)
        self.event_engine.put(event)

    def send_notification(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """通过已配置渠道推送通知"""
        if strategy:
            subject: str = f"{strategy.strategy_name}"
        else:
            subject = _("组合策略引擎")

        self.main_engine.send_notification(msg, subject)

    def send_wecom(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """通过企业微信推送消息"""
        if strategy:
            subject: str = f"{strategy.strategy_name}"
        else:
            subject = _("组合策略引擎")

        wecom_engine: Any = self.main_engine.get_engine("wecom")
        if wecom_engine:
            wecom_engine.send_wecom(f"{subject}\n{msg}")
