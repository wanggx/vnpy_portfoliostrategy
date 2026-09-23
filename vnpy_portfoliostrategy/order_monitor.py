"""通用委托监控：跟踪在途委托的成交情况，超时未全部成交则告警并按需撤单。

与具体策略逻辑解耦，任何 ``StrategyTemplate`` 子类都能直接复用：策略只在下单成功后
把委托号登记进来，之后由本模块负责「全部成交 / 撤单 / 拒单就摘除监控」与「挂太久
就告警（可选撤单）」。**本模块不重发任何委托**：撤销之后就不管了，要不要重新下单
由策略自己的信号决定。

接线四处（以只监控卖出委托为例）::

    self.sell_monitor = OrderMonitor(self, timeout=300, label="卖出")    # 构造一次
    self.sell_monitor.track(vt_orderids, vt_symbol, mark=result.reason)  # 下单成功后
    self.sell_monitor.check()                                           # on_tick 每 tick
    self.sell_monitor.update_order(order)                               # update_order 回调

对策略对象的依赖（鸭子类型，不绑定具体基类）：``get_order`` / ``cancel_order`` /
``write_log`` / ``send_wecom``，以及可选的 ``trading``（策略停止后不再告警/撤单）与
``strategy_engine.main_engine``（解析合约名称用）。
"""

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable

from vnpy.trader.object import OrderData


@dataclass
class OrderRecord:
    """在途委托的监控记录（仅运行时，不持久化）

    ``mark`` 为下单原因（策略传入，告警时原样带出，便于知道这笔委托为什么下）；
    ``alerted`` / ``cancel_sent`` 保证同一笔委托只告警一次、只撤单一次。
    """

    vt_orderid: str
    vt_symbol: str
    send_time: datetime              # 登记时刻（本地时钟，用于超时判定）
    mark: str = ""                   # 下单原因
    label: str = ""                  # 委托类型标签（如"卖出"），覆盖监控器默认标签
    alerted: bool = False            # 是否已告警
    cancel_sent: bool = False        # 是否已发起撤单


class OrderMonitor:
    """在途委托监控器：全部成交即摘除监控，超时未全部成交则告警（可选撤单），不重发。

    参数：

    - ``strategy``：所属策略实例（只用到上面文档列出的几个成员）；
    - ``timeout``：超时秒数；``<=0`` 表示只跟踪不告警（等价于手动查 ``is_pending``）；
    - ``cancel_on_timeout``：超时是否撤单，默认 True；
    - ``alert``：超时是否写日志 + 企微推送，默认 True（``cancel_on_timeout=False`` 可用于
      "只提醒不撤"的场景）；
    - ``label``：默认标签，用于告警措辞（``""`` → "委托超时告警"，``"卖出"`` →
      "卖出委托超时告警"），可被 ``track(..., label=...)`` 按笔覆盖；
    - ``name_func``：可选的标的名称解析函数（``vt_symbol -> name``）；不传则回落到
      ``strategy_engine.main_engine.get_contract(vt_symbol).name``，取不到时只显示代码。

    只跟踪调用方 ``track`` 过的委托：没登记的委托既不会被撤单也不会被告警。
    """

    def __init__(
        self,
        strategy,
        timeout: float = 300.0,
        cancel_on_timeout: bool = True,
        alert: bool = True,
        label: str = "",
        name_func: Callable[[str], str] | None = None,
    ) -> None:
        """构造函数：绑定策略与超时策略"""
        self.strategy = strategy
        self.timeout: float = float(timeout or 0)
        self.cancel_on_timeout: bool = cancel_on_timeout
        self.alert: bool = alert
        self.label: str = label
        self.name_func: Callable[[str], str] | None = name_func

        # 在途委托：vt_orderid -> 监控记录
        self.records: dict[str, OrderRecord] = {}

    # ------------------------------------------------------------------
    # 登记 / 摘除
    # ------------------------------------------------------------------

    def track(
        self,
        vt_orderids: list[str],
        vt_symbol: str,
        mark: str = "",
        label: str = "",
    ) -> None:
        """登记委托，纳入监控；``vt_orderids`` 直接取下单接口的返回值

        ``mark`` 建议传下单原因（策略里的 ``result.reason``）；``label`` 可不传，
        不传时用监控器默认标签。空列表调用无副作用（下单未被受理时可直接透传）。
        """
        now: datetime = datetime.now()
        for vt_orderid in vt_orderids:
            self.records[vt_orderid] = OrderRecord(
                vt_orderid=vt_orderid,
                vt_symbol=vt_symbol,
                send_time=now,
                mark=mark,
                label=label,
            )

    def untrack(self, vt_orderid: str) -> None:
        """摘除单笔委托的监控（委托已终结时调用）"""
        self.records.pop(vt_orderid, None)

    def untrack_symbol(self, vt_symbol: str) -> list[str]:
        """摘除该标的的全部在途监控，返回被摘除的委托号

        标的退出当日池、或策略不再关心该标的时用（只摘监控，不撤单；需要撤单请
        自己在调用前撤）。
        """
        removed: list[str] = [
            vt_orderid for vt_orderid, record in self.records.items()
            if record.vt_symbol == vt_symbol
        ]
        for vt_orderid in removed:
            self.records.pop(vt_orderid, None)
        return removed

    def clear(self) -> None:
        """清空全部监控记录（策略停止/日切时用）"""
        self.records.clear()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def is_pending(self, vt_symbol: str) -> bool:
        """该标的是否有在途委托（策略可用它避免重复下单）"""
        return any(
            record.vt_symbol == vt_symbol for record in self.records.values()
        )

    def pending_orderids(self, vt_symbol: str = "") -> list[str]:
        """在途委托号列表；传 ``vt_symbol`` 则只返回该标的的"""
        if not vt_symbol:
            return list(self.records)
        return [
            record.vt_orderid for record in self.records.values()
            if record.vt_symbol == vt_symbol
        ]

    def pending_symbols(self) -> set[str]:
        """有在途委托的标的集合"""
        return {record.vt_symbol for record in self.records.values()}

    def __len__(self) -> int:
        """在途委托笔数"""
        return len(self.records)

    # ------------------------------------------------------------------
    # 回报同步 / 超时检查
    # ------------------------------------------------------------------

    def update_order(self, order: OrderData) -> None:
        """委托回报同步：委托终结（全部成交/撤单/拒单）即摘除其监控

        在策略的 ``update_order`` 回调里调用一次；未登记过的委托号不会有副作用。
        """
        if not order.is_active():
            self.untrack(order.vt_orderid)

    def check(self) -> list[OrderRecord]:
        """检查全部在途委托，返回本轮因超时处理的记录（未超时返回空列表）

        每个 tick 调一次即可，且**不限当前 tick 的标的**：超时是时间维度的判定，
        任何标的的行情都能带动检查（``on_tick`` 里无脑调用，内部按标的无关处理）。

        策略 ``trading`` 为 False 时直接返回（引擎停止策略时已全撤委托，此时再告警
        或撤单没有意义，且 ``cancel_order`` 本身也是空操作）。
        """
        if not self.records or self.timeout <= 0:
            return []
        if not getattr(self.strategy, "trading", True):
            return []

        now: datetime = datetime.now()
        handled: list[OrderRecord] = []
        for record in list(self.records.values()):
            if record.alerted:
                continue

            elapsed: float = (now - record.send_time).total_seconds()
            if elapsed < self.timeout:
                continue

            order: OrderData | None = self._get_order(record.vt_orderid)
            # 已终结（全部成交/撤单/拒单）：交给 update_order 摘除，这里不重复处理
            if order is not None and not order.is_active():
                self.untrack(record.vt_orderid)
                continue

            self._on_timeout(record, order, elapsed)
            handled.append(record)
        return handled

    def _on_timeout(
        self, record: OrderRecord, order: OrderData | None, elapsed: float
    ) -> None:
        """单笔超时处理：按需撤单 + 按需告警（都只做一次）"""
        record.alerted = True

        if self.cancel_on_timeout:
            record.cancel_sent = True
            self.strategy.cancel_order(record.vt_orderid)

        if not self.alert:
            return

        msg: str = self.format_timeout_message(record, order, elapsed)
        self.strategy.write_log(msg)
        self.strategy.send_wecom(msg)

    def format_timeout_message(
        self, record: OrderRecord, order: OrderData | None, elapsed: float
    ) -> str:
        """拼装超时告警文案（子类/策略可覆写以定制格式）

        例如：``卖出委托超时告警 000001.SZSE(平安银行) 分档止损 收益-2.35% 已挂 5.0
        分钟仍未全部成交，已撤单：委托 10.50 × 500 成交 0``。
        """
        label: str = record.label or self.label
        name: str = self.symbol_name(record.vt_symbol)
        reason: str = f" {record.mark}" if record.mark else ""
        # 没收到过委托回报也照常告警（可能是网关异常，此时连成交量都无法确认，更不能静默）
        detail: str = (
            f"委托 {order.price:.2f} × {order.volume} 成交 {order.traded}"
            if order is not None else "（未收到委托回报）"
        )
        action: str = "已撤单" if self.cancel_on_timeout else "未撤单"
        return (
            f"{label}委托超时告警 {record.vt_symbol}({name}){reason} "
            f"已挂 {elapsed / 60:.1f} 分钟仍未全部成交，{action}：{detail}"
        )

    def symbol_name(self, vt_symbol: str) -> str:
        """取标的名称：优先用构造时传入的 ``name_func``，否则查 MainEngine 合约缓存

        两者都取不到（合约未就绪 / 回测引擎无 main_engine）时返回空串，告警里表现为
        ``000001.SZSE()``，不影响其它信息输出。
        """
        if self.name_func is not None:
            return self.name_func(vt_symbol) or ""

        main_engine = getattr(
            getattr(self.strategy, "strategy_engine", None), "main_engine", None
        )
        if main_engine is None:
            return ""

        contract = main_engine.get_contract(vt_symbol)
        return (contract.name or "") if contract else ""

    def _get_order(self, vt_orderid: str) -> OrderData | None:
        """从策略的委托缓存取委托数据；策略没提供 ``get_order`` 时返回 None"""
        getter = getattr(self.strategy, "get_order", None)
        return getter(vt_orderid) if getter else None
