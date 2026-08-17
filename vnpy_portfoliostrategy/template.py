from abc import ABC, abstractmethod
from copy import copy
from collections import defaultdict
from typing import Any, cast

from vnpy.trader.constant import Interval, Direction, Offset
from vnpy.trader.object import BarData, TickData, OrderData, TradeData

from .base import EngineType


class StrategyTemplate(ABC):
    """组合策略模板"""

    author: str = ""
    # T+1 开关：True=A 股 T+1（仅可卖昨仓），False=期货 T+0（可卖全部持仓）。
    # 由策略子类在类属性上设定（参考 vnpy_ctastrategy）；T+1 持仓与可卖量
    # 由引擎从经纪商持仓回报同步，并在下单/撤单/成交时维护冻结量。
    t1: bool = False
    parameters: list = ["t1"]
    variables: list = []

    def __init__(
        self,
        strategy_engine: Any,
        strategy_name: str,
        vt_symbols: list[str],
        setting: dict
    ) -> None:
        """构造函数"""
        self.strategy_engine: Any = strategy_engine
        self.strategy_name: str = strategy_name
        self.vt_symbols: list[str] = vt_symbols

        # 状态控制变量
        self.inited: bool = False
        self.trading: bool = False

        # 持仓数据字典
        self.pos_data: dict[str, int] = defaultdict(int)        # 实际持仓
        self.pos_price_data: dict[str, float] = {}              # 经纪商持仓均价
        self.target_data: dict[str, int] = defaultdict(int)     # 目标持仓

        # T+1 持仓管理（参考 vnpy_ctastrategy）：
        # yd_pos_data 昨仓（T+1 可卖）、td_pos_data 今仓（T+1 不可卖）、
        # sell_frozen_data 已发起卖出委托的冻结量；position_synced 是否已从
        # 经纪商持仓回报同步。T+0 策略这些字段保持 0/False，可卖量等于持仓。
        self.yd_pos_data: dict[str, int] = defaultdict(int)
        self.td_pos_data: dict[str, int] = defaultdict(int)
        self.sell_frozen_data: dict[str, int] = defaultdict(int)
        self.position_synced: bool = False
        self.position_synced_symbols: set[str] = set()

        # 委托缓存容器
        self.orders: dict[str, OrderData] = {}
        self.active_orderids: set[str] = set()

        # 复制变量名列表，插入默认变量内容
        self.variables: list = copy(self.variables)
        self.variables.insert(0, "inited")
        self.variables.insert(1, "trading")
        self.variables.insert(2, "pos_data")
        self.variables.insert(3, "pos_price_data")
        self.variables.insert(4, "target_data")
        self.variables.insert(5, "yd_pos_data")
        self.variables.insert(6, "td_pos_data")
        self.variables.insert(7, "sell_frozen_data")
        self.variables.insert(8, "position_synced")

        # 设置策略参数
        self.update_setting(setting)

    def update_setting(self, setting: dict) -> None:
        """设置策略参数"""
        for name in self.get_class_parameter_names():
            if name in setting:
                setattr(self, name, setting[name])

    @classmethod
    def get_class_parameter_names(cls) -> list:
        """查取参数名列表（含基类 t1 等公共参数）"""
        parameters: list = copy(cls.parameters)
        if "t1" not in parameters:
            parameters.insert(0, "t1")
        return parameters

    @classmethod
    def get_class_parameters(cls) -> dict:
        """查取策略默认参数"""
        class_parameters: dict = {}
        for name in cls.get_class_parameter_names():
            class_parameters[name] = getattr(cls, name)
        return class_parameters

    def get_parameters(self) -> dict:
        """查询策略参数"""
        strategy_parameters: dict = {}
        for name in self.get_class_parameter_names():
            strategy_parameters[name] = getattr(self, name)
        return strategy_parameters

    def get_variables(self) -> dict:
        """查询策略变量"""
        strategy_variables: dict = {}
        for name in self.variables:
            strategy_variables[name] = getattr(self, name)
        return strategy_variables

    def get_data(self) -> dict:
        """查询策略状态数据"""
        strategy_data: dict = {
            "strategy_name": self.strategy_name,
            "vt_symbols": self.vt_symbols,
            "class_name": self.__class__.__name__,
            "author": self.author,
            "parameters": self.get_parameters(),
            "variables": self.get_variables(),
        }
        return strategy_data

    @abstractmethod
    def on_init(self) -> None:
        """策略初始化回调"""
        return

    def on_start(self) -> None:
        """策略启动回调"""
        return

    def on_stop(self) -> None:
        """策略停止回调"""
        return

    def on_tick(self, tick: TickData) -> None:
        """行情推送回调"""
        return

    @abstractmethod
    def on_bars(self, bars: dict[str, BarData]) -> None:
        """K线切片回调"""
        return

    def update_trade(self, trade: TradeData) -> None:
        """成交数据更新"""
        if trade.direction == Direction.LONG:
            self.pos_data[trade.vt_symbol] += trade.volume
            if self.t1:
                # 开仓计入今仓（T+1 当日不可卖）
                self.td_pos_data[trade.vt_symbol] += trade.volume
        else:
            self.pos_data[trade.vt_symbol] -= trade.volume
            if self.t1:
                # 平仓消耗昨仓；冻结量由引擎在成交回报时释放
                self.yd_pos_data[trade.vt_symbol] = max(
                    self.yd_pos_data[trade.vt_symbol] - trade.volume, 0
                )

    def get_sellable(self, vt_symbol: str) -> int:
        """查询可卖量

        T+1：昨仓减去卖出冻结；T+0：当前持仓。卖出冻结由引擎在发起卖出委托时
        增加、在撤单/成交回报时释放，避免在成交回报到达前重复下单超卖。
        """
        if self.t1:
            return max(
                self.yd_pos_data.get(vt_symbol, 0)
                - self.sell_frozen_data.get(vt_symbol, 0),
                0
            )
        return self.pos_data.get(vt_symbol, 0)

    def sync_t1_position(
        self,
        vt_symbol: str,
        volume: float,
        yd_volume: float,
        price: float = 0,
    ) -> None:
        """从经纪商持仓回报同步 T+1 昨/今仓

        ``yd_volume`` 取经纪商返回的当前可卖量。加回本策略已冻结的卖单量后作为
        昨仓基数，避免经纪商回报已扣冻结量、策略本地又重复扣减。引擎在持仓事件
        与初始化查询时调用。

        ``price`` 为经纪商持仓均价，写入 ``pos_price_data``；空仓时清除对应记录。
        """
        position: int = int(volume)
        local_frozen: int = int(self.sell_frozen_data.get(vt_symbol, 0))
        yesterday_position: int = min(int(yd_volume) + local_frozen, position)

        self.pos_data[vt_symbol] = position
        self.yd_pos_data[vt_symbol] = yesterday_position
        self.td_pos_data[vt_symbol] = max(position - yesterday_position, 0)
        if position > 0:
            if price > 0:
                self.pos_price_data[vt_symbol] = price
        else:
            self.pos_price_data.pop(vt_symbol, None)
        self.position_synced_symbols.add(vt_symbol)
        self.position_synced = True

    def get_pos_price(self, vt_symbol: str) -> float:
        """查询经纪商同步的持仓均价"""
        return self.pos_price_data.get(vt_symbol, 0.0)

    def is_position_synced(self, vt_symbol: str) -> bool:
        """查询指定标的的 T+1 持仓是否已同步"""
        return vt_symbol in self.position_synced_symbols

    def update_order(self, order: OrderData) -> None:
        """委托数据更新"""
        self.orders[order.vt_orderid] = order

        if not order.is_active() and order.vt_orderid in self.active_orderids:
            self.active_orderids.remove(order.vt_orderid)

    def buy(self, vt_symbol: str, price: float, volume: float, lock: bool = False, net: bool = False, mark: str = "") -> list[str]:
        """买入开仓"""
        return self.send_order(vt_symbol, Direction.LONG, Offset.OPEN, price, volume, lock, net, mark)

    def sell(self, vt_symbol: str, price: float, volume: float, lock: bool = False, net: bool = False, mark: str = "") -> list[str]:
        """卖出平仓"""
        return self.send_order(vt_symbol, Direction.SHORT, Offset.CLOSE, price, volume, lock, net, mark)

    def short(self, vt_symbol: str, price: float, volume: float, lock: bool = False, net: bool = False, mark: str = "") -> list[str]:
        """卖出开仓"""
        return self.send_order(vt_symbol, Direction.SHORT, Offset.OPEN, price, volume, lock, net, mark)

    def cover(self, vt_symbol: str, price: float, volume: float, lock: bool = False, net: bool = False, mark: str = "") -> list[str]:
        """买入平仓"""
        return self.send_order(vt_symbol, Direction.LONG, Offset.CLOSE, price, volume, lock, net, mark)

    def send_order(
        self,
        vt_symbol: str,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        lock: bool = False,
        net: bool = False,
        mark: str = "",
    ) -> list[str]:
        """发送委托"""
        if self.trading:
            vt_orderids: list = self.strategy_engine.send_order(
                self, vt_symbol, direction, offset, price, volume, lock, net, mark
            )

            for vt_orderid in vt_orderids:
                self.active_orderids.add(vt_orderid)

            return vt_orderids
        else:
            return []

    @staticmethod
    def get_order_mark(order: OrderData) -> str:
        """从委托 reference 中提取触发标记（mark）"""
        _, separator, mark = order.reference.partition(":")
        return mark if separator else ""

    def cancel_order(self, vt_orderid: str) -> None:
        """撤销委托"""
        if self.trading:
            self.strategy_engine.cancel_order(self, vt_orderid)

    def cancel_all(self) -> None:
        """全撤活动委托"""
        for vt_orderid in list(self.active_orderids):
            self.cancel_order(vt_orderid)

    def get_pos(self, vt_symbol: str) -> int:
        """查询当前持仓"""
        return self.pos_data.get(vt_symbol, 0)

    def get_target(self, vt_symbol: str) -> int:
        """查询目标仓位"""
        return self.target_data[vt_symbol]

    def set_target(self, vt_symbol: str, target: int) -> None:
        """设置目标仓位"""
        self.target_data[vt_symbol] = target

    def rebalance_portfolio(self, bars: dict[str, BarData]) -> None:
        """基于目标执行调仓交易"""
        self.cancel_all()

        # 只发出当前K线切片有行情的合约的委托
        for vt_symbol, bar in bars.items():
            # 计算仓差
            target: int = self.get_target(vt_symbol)
            pos: int = self.get_pos(vt_symbol)
            diff: int = target - pos

            # 多头
            if diff > 0:
                # 计算多头委托价
                order_price: float = self.calculate_price(
                    vt_symbol,
                    Direction.LONG,
                    bar.close_price
                )

                # 计算买平和买开数量
                cover_volume: int = 0
                buy_volume: int = 0

                if pos < 0:
                    cover_volume = min(diff, abs(pos))
                    buy_volume = diff - cover_volume
                else:
                    buy_volume = diff

                # 发出对应委托
                if cover_volume:
                    self.cover(vt_symbol, order_price, cover_volume)

                if buy_volume:
                    self.buy(vt_symbol, order_price, buy_volume)
            # 空头
            elif diff < 0:
                # 计算空头委托价
                order_price = self.calculate_price(
                    vt_symbol,
                    Direction.SHORT,
                    bar.close_price
                )

                # 计算卖平和卖开数量
                sell_volume: int = 0
                short_volume: int = 0

                if pos > 0:
                    sell_volume = min(abs(diff), pos)
                    short_volume = abs(diff) - sell_volume
                else:
                    short_volume = abs(diff)

                # 发出对应委托
                if sell_volume:
                    self.sell(vt_symbol, order_price, sell_volume)

                if short_volume:
                    self.short(vt_symbol, order_price, short_volume)

    def calculate_price(
        self,
        vt_symbol: str,
        direction: Direction,
        reference: float
    ) -> float:
        """计算调仓委托价格（支持按需重载实现）"""
        return reference

    def get_order(self, vt_orderid: str) -> OrderData | None:
        """查询委托数据"""
        return self.orders.get(vt_orderid, None)

    def get_all_active_orderids(self) -> list[OrderData]:
        """获取全部活动状态的委托号"""
        return list(self.active_orderids)

    def write_log(self, msg: str) -> None:
        """记录日志"""
        self.strategy_engine.write_log(msg, self)

    def get_engine_type(self) -> EngineType:
        """查询引擎类型"""
        return cast(EngineType, self.strategy_engine.get_engine_type())

    def get_pricetick(self, vt_symbol: str) -> float:
        """查询合约最小价格跳动"""
        return cast(float, self.strategy_engine.get_pricetick(self, vt_symbol))

    def get_size(self, vt_symbol: str) -> int:
        """查询合约乘数"""
        return cast(int, self.strategy_engine.get_size(self, vt_symbol))

    def load_bars(self, days: int, interval: Interval = Interval.MINUTE) -> None:
        """加载历史K线数据来执行初始化"""
        self.strategy_engine.load_bars(self, days, interval)

    def put_event(self) -> None:
        """推送策略数据更新事件"""
        if self.inited:
            self.strategy_engine.put_strategy_event(self)

    def send_notification(self, msg: str) -> None:
        """通过已配置渠道推送通知"""
        if self.inited:
            self.strategy_engine.send_notification(msg, self)

    send_email = send_notification

    def send_wecom(self, msg: str) -> None:
        """通过企业微信推送消息"""
        if self.inited:
            self.strategy_engine.send_wecom(msg, self)

    def sync_data(self) -> None:
        """同步策略状态数据到文件"""
        if self.trading:
            self.strategy_engine.sync_strategy_data(self)
