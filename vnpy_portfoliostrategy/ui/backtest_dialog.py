"""组合策略单页回测窗口（阶段四，对照 stage4-portfoliostrategy-vnpy-v7-start-backtest.png）。

从策略卡片的"回测"按钮进入，绑定该策略的 strategy_name / class_name / vt_symbols /
当前参数。可最大化的单页窗口：配置栏 / 运行状态栏 / 指标+图表区 / 明细区（左侧列表
切换）/ 底部操作区。第一行最右固定"开始回测"主按钮。不使用 Tab，不弹独立结果窗口。

回测在后台线程执行，不阻塞 Qt 主线程；前置检查、状态机、停止、异常处理、结果展示
均在本窗口完成。
"""
from __future__ import annotations

from bisect import bisect_left
from datetime import date, datetime
from typing import Any, Callable

import numpy as np
import pyqtgraph as pg

from vnpy.trader.ui import QtWidgets, QtCore, QtGui
from vnpy.trader.constant import Interval

from ..backtesting import BacktestingEngine
from ..template import StrategyTemplate


# 仅装饰指标卡/曲线；其余控件跟 VeighNa 全局主题，避免整窗换皮发花
_KPI_UP = "#3ddc97"
_KPI_DOWN = "#ff6b6b"
_KPI_FLAT = "#e8eaed"
_EQUITY_COLOR = "#4fc3f7"
_DRAWDOWN_COLOR = "#ff6b6b"
_CHART_BG = "#12141a"
_CHART_BORDER = "#2a2f3a"
_CHART_AXIS = "#8b93a7"
_KPI_BOX_STYLE = (
    "QFrame#kpiCard {"
    "  background-color: #1b1e26;"
    "  border: 1px solid #2a2f3a;"
    "  border-radius: 8px;"
    "}"
)
_KPI_TITLE_STYLE = "font-size: 13px; color: #9aa3b2;"
_KPI_VALUE_STYLE = "font-size: 28px; font-weight: 700;"
_CHART_BOX_STYLE = (
    "QFrame#chartPanel {"
    f"  background-color: {_CHART_BG};"
    f"  border: 1px solid {_CHART_BORDER};"
    "  border-radius: 6px;"
    "}"
)
_SPLITTER_STYLE = """
QSplitter::handle {
  background-color: #2a2f3a;
}
QSplitter::handle:horizontal {
  width: 1px;
}
QSplitter::handle:vertical {
  height: 1px;
}
QSplitter::handle:hover {
  background-color: #3b82f6;
}
"""


class _CurveHoverHelper(QtCore.QObject):
    """曲线 hover：十字线 + 吸附点 + 日期/数值气泡。"""

    def __init__(
        self,
        plot: pg.PlotWidget,
        *,
        value_name: str,
        value_fmt: Callable[[float], str],
        color: str,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.plot = plot
        self.value_name = value_name
        self.value_fmt = value_fmt
        self.color = color
        self.xs: list[float] = []
        self.ys: list[float] = []

        pen = pg.mkPen("#e8eaed", width=1, style=QtCore.Qt.PenStyle.DashLine)
        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=pen)
        for line in (self.vline, self.hline):
            line.setZValue(20)
            line.setVisible(False)
            plot.addItem(line, ignoreBounds=True)

        self.marker = pg.ScatterPlotItem(
            size=10,
            pen=pg.mkPen("#12141a", width=1),
            brush=pg.mkBrush(color),
        )
        self.marker.setZValue(21)
        plot.addItem(self.marker)

        self.label = pg.TextItem(
            anchor=(0, 1),
            color="#12141a",
            fill=pg.mkBrush("#ffe082"),
            border=pg.mkPen("#ffe082"),
        )
        self.label.setZValue(22)
        self.label.setVisible(False)
        plot.addItem(self.label, ignoreBounds=True)

        plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        plot.viewport().installEventFilter(self)

    def set_data(self, xs: list[float], ys: list[float]) -> None:
        self.xs = list(xs)
        self.ys = list(ys)
        self.hide()

    def hide(self) -> None:
        self.vline.setVisible(False)
        self.hline.setVisible(False)
        self.label.setVisible(False)
        self.marker.setData([], [])

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802
        if event.type() == QtCore.QEvent.Type.Leave:
            self.hide()
        return super().eventFilter(obj, event)

    def _on_mouse_moved(self, pos: QtCore.QPointF) -> None:
        if not self.xs or not self.ys:
            self.hide()
            return
        if not self.plot.sceneBoundingRect().contains(pos):
            self.hide()
            return

        point = self.plot.getViewBox().mapSceneToView(pos)
        idx = self._nearest_index(point.x())
        x = self.xs[idx]
        y = self.ys[idx]

        self.vline.setPos(x)
        self.hline.setPos(y)
        self.vline.setVisible(True)
        self.hline.setVisible(True)
        self.marker.setData([x], [y])

        try:
            date_text = datetime.fromtimestamp(x).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            date_text = str(x)

        self.label.setHtml(
            f"<div style='padding:2px 4px;'>"
            f"<b>{date_text}</b><br>{self.value_name}  {self.value_fmt(y)}"
            f"</div>"
        )
        self.label.setPos(x, y)
        self.label.setVisible(True)

    def _nearest_index(self, x: float) -> int:
        i = bisect_left(self.xs, x)
        if i <= 0:
            return 0
        if i >= len(self.xs):
            return len(self.xs) - 1
        before = self.xs[i - 1]
        after = self.xs[i]
        return i if (after - x) < (x - before) else i - 1


# 回测任务状态机
class BacktestStatus:
    IDLE = "待运行"
    VALIDATING = "检查中"
    READY = "就绪"
    RUNNING = "运行中"
    STOPPING = "停止中"
    STOPPED = "已停止"
    COMPLETED = "已完成"
    FAILED = "失败"


class PortfolioBacktestDialog(QtWidgets.QDialog):
    """组合策略单页回测工作区。"""

    INTERVAL_OPTIONS: list[tuple[str, Interval]] = [
        ("1m", Interval.MINUTE),
        ("1h", Interval.HOUR),
        ("1d", Interval.DAILY),
    ]

    # 后台线程 -> UI：复杂结果不走 Signal(object)（Queued 常丢），用无参 finished + 成员暂存
    signal_log: QtCore.Signal = QtCore.Signal(str)
    signal_status: QtCore.Signal = QtCore.Signal(str)
    signal_progress: QtCore.Signal = QtCore.Signal(int)
    signal_finished: QtCore.Signal = QtCore.Signal()

    def __init__(
        self,
        strategy_engine: Any,
        *,
        strategy_name: str,
        strategy_class_name: str,
        vt_symbols: list[str],
        strategy_setting: dict,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.strategy_engine = strategy_engine
        self.strategy_name: str = strategy_name
        self.strategy_class_name: str = strategy_class_name
        self.vt_symbols: list[str] = vt_symbols
        self.strategy_setting: dict = strategy_setting

        self._status: str = BacktestStatus.IDLE
        self._engine: BacktestingEngine | None = None
        self._stop_flag: bool = False
        self._progress_phase: str = "idle"
        self._symbols_done: int = 0
        self._symbols_total: int = 1
        self._progress_value: int = 0
        self._pending_stats: dict = {}
        self._pending_trades: dict = {}
        self._pending_logs: list = []
        self._pending_daily: Any = None
        self._output_logs: list[str] = []
        self._trades: dict = {}
        self._logs: list = []
        self._daily_df: Any = None
        self._did_layout_splitters: bool = False

        self._init_ui()
        self.signal_log.connect(self._append_log)
        self.signal_status.connect(self._set_status_slot)
        self.signal_progress.connect(self._set_progress)
        self.signal_finished.connect(self._on_finished)

    def _init_ui(self) -> None:
        self.setWindowTitle(f"组合策略回测 - {self.strategy_name}")
        self.setMinimumSize(1100, 700)
        # 独立顶层窗口，才能稳定最大化（挂在策略卡片 parent 上时常失败）
        self.setWindowFlags(
            QtCore.Qt.WindowType.Window
            | QtCore.Qt.WindowType.WindowMinimizeButtonHint
            | QtCore.Qt.WindowType.WindowMaximizeButtonHint
            | QtCore.Qt.WindowType.WindowCloseButtonHint
        )

        # 1. 配置栏
        self.strategy_label: QtWidgets.QLabel = QtWidgets.QLabel(
            f"策略：{self.strategy_name}（{self.strategy_class_name}）"
        )
        self.interval_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for name, _iv in self.INTERVAL_OPTIONS:
            self.interval_combo.addItem(name)
        self.start_date: QtWidgets.QDateEdit = QtWidgets.QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(QtCore.QDate.currentDate().addMonths(-12))
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date: QtWidgets.QDateEdit = QtWidgets.QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(QtCore.QDate.currentDate())
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.market_db_label: QtWidgets.QLabel = QtWidgets.QLabel("行情数据：vnpy.duckdb")
        self.quality_label: QtWidgets.QLabel = QtWidgets.QLabel("数据质量：--")

        self.run_button: QtWidgets.QPushButton = QtWidgets.QPushButton("开始回测")
        self.run_button.clicked.connect(self._on_run)

        config_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        config_row.addWidget(self.strategy_label)
        config_row.addWidget(QtWidgets.QLabel("周期"))
        config_row.addWidget(self.interval_combo)
        config_row.addWidget(QtWidgets.QLabel("开始"))
        config_row.addWidget(self.start_date)
        config_row.addWidget(QtWidgets.QLabel("结束"))
        config_row.addWidget(self.end_date)
        config_row.addWidget(self.market_db_label)
        config_row.addWidget(self.quality_label)
        config_row.addStretch()
        config_row.addWidget(self.run_button)

        # 2. 运行状态栏：进度一行，提示单独下一行，避免右侧文案刷新挤闪进度条
        self.status_label: QtWidgets.QLabel = QtWidgets.QLabel(f"状态：{BacktestStatus.IDLE}")
        self.progress_bar: QtWidgets.QProgressBar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.detail_status: QtWidgets.QLabel = QtWidgets.QLabel("")
        self.detail_status.setWordWrap(True)
        self.detail_status.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft
        )

        # 左半：状态+进度条；右半：提示文案
        status_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addWidget(self.status_label)
        status_row.addWidget(self.progress_bar, 1)
        status_row.addWidget(self.detail_status, 1)

        # 3. 指标区：5 个大卡片
        self._kpi_values: dict[str, QtWidgets.QLabel] = {}
        kpi_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        kpi_row.setSpacing(12)
        for key, title in (
            ("total_return", "总收益"),
            ("max_ddpercent", "最大回撤"),
            ("annual_return", "年化收益"),
            ("sharpe_ratio", "夏普比率"),
            ("total_trade_count", "成交次数"),
        ):
            card, value_label = self._make_kpi_card(title)
            self._kpi_values[key] = value_label
            kpi_row.addWidget(card, 1)

        # 4. 曲线区：资金曲线 | 回撤曲线
        self.equity_plot: pg.PlotWidget = self._make_curve_plot()
        self.drawdown_plot: pg.PlotWidget = self._make_curve_plot()
        self._equity_curve: pg.PlotDataItem = self.equity_plot.plot(
            pen=pg.mkPen(_EQUITY_COLOR, width=2),
            fillLevel=1.0,
            brush=pg.mkBrush(79, 195, 247, 55),
            name="策略净值",
        )
        self._drawdown_curve: pg.PlotDataItem = self.drawdown_plot.plot(
            pen=pg.mkPen(_DRAWDOWN_COLOR, width=2),
            fillLevel=0,
            brush=pg.mkBrush(255, 107, 107, 70),
            name="回撤",
        )
        self._equity_hover = _CurveHoverHelper(
            self.equity_plot,
            value_name="净值",
            value_fmt=lambda v: f"{v:.4f}",
            color=_EQUITY_COLOR,
            parent=self,
        )
        self._drawdown_hover = _CurveHoverHelper(
            self.drawdown_plot,
            value_name="回撤",
            value_fmt=lambda v: f"{v:.2f}%",
            color=_DRAWDOWN_COLOR,
            parent=self,
        )
        chart_split: QtWidgets.QSplitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Horizontal
        )
        chart_split.setHandleWidth(1)
        chart_split.setStyleSheet(_SPLITTER_STYLE)
        chart_split.addWidget(self._wrap_plot("资金曲线", self.equity_plot))
        chart_split.addWidget(self._wrap_plot("回撤曲线", self.drawdown_plot))
        chart_split.setChildrenCollapsible(False)
        chart_split.setStretchFactor(0, 1)
        chart_split.setStretchFactor(1, 1)
        chart_split.setMinimumHeight(160)

        # 5. 明细区：左侧列表 + 细 splitter + 右侧表格（列表跟全局主题，不单独换皮）
        self.detail_list: QtWidgets.QListWidget = QtWidgets.QListWidget()
        self.detail_list.addItem("成交明细")
        self.detail_list.addItem("每日收益")
        self.detail_list.addItem("回测日志")
        self.detail_list.setCurrentRow(0)
        self.detail_list.setMaximumWidth(140)
        self.detail_list.currentRowChanged.connect(self._switch_detail)

        self.detail_table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.detail_table.horizontalHeader().setStretchLastSection(True)
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )

        self._detail_split: QtWidgets.QSplitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Horizontal
        )
        self._detail_split.setHandleWidth(1)
        self._detail_split.setStyleSheet(_SPLITTER_STYLE)
        self._detail_split.addWidget(self.detail_list)
        self._detail_split.addWidget(self.detail_table)
        self._detail_split.setChildrenCollapsible(False)
        self._detail_split.setStretchFactor(0, 0)
        self._detail_split.setStretchFactor(1, 1)

        self._mid_split: QtWidgets.QSplitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Vertical
        )
        self._mid_split.setHandleWidth(1)
        self._mid_split.setStyleSheet(_SPLITTER_STYLE)
        self._mid_split.addWidget(chart_split)
        self._mid_split.addWidget(self._detail_split)
        self._mid_split.setChildrenCollapsible(False)
        self._mid_split.setStretchFactor(0, 2)
        self._mid_split.setStretchFactor(1, 3)

        # 6. 底部操作区
        self.stop_button: QtWidgets.QPushButton = QtWidgets.QPushButton("停止回测")
        self.stop_button.clicked.connect(self._on_stop)
        self.export_button: QtWidgets.QPushButton = QtWidgets.QPushButton("导出报告")
        self.export_button.clicked.connect(self._export_report)
        self.close_button: QtWidgets.QPushButton = QtWidgets.QPushButton("关闭")
        self.close_button.clicked.connect(self.reject)

        bottom_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        bottom_row.addStretch()
        bottom_row.addWidget(self.stop_button)
        bottom_row.addWidget(self.export_button)
        bottom_row.addWidget(self.close_button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(config_row)
        vbox.addLayout(status_row)
        vbox.addLayout(kpi_row)
        vbox.addWidget(self._mid_split, 1)
        vbox.addLayout(bottom_row)
        self.setLayout(vbox)

        self._clear_metrics_and_charts()
        self._update_buttons()
        self._switch_detail(0)

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # type: ignore[override]
        """首次显示后按比例压低曲线区，给表格留空。"""
        super().showEvent(event)
        if self._did_layout_splitters:
            return
        self._did_layout_splitters = True
        QtCore.QTimer.singleShot(0, self._apply_splitter_sizes)

    def _apply_splitter_sizes(self) -> None:
        """曲线约占 32%，下方表格约占 68%；明细区表格占满右侧。"""
        total = max(400, self._mid_split.height())
        chart_h = min(280, max(160, int(total * 0.32)))
        self._mid_split.setSizes([chart_h, max(240, total - chart_h)])
        detail_w = max(500, self._detail_split.width())
        self._detail_split.setSizes([120, max(380, detail_w - 120)])

    @staticmethod
    def _make_kpi_card(title: str) -> tuple[QtWidgets.QFrame, QtWidgets.QLabel]:
        """单个大指标卡：标题 + 大号数值。"""
        card = QtWidgets.QFrame()
        card.setObjectName("kpiCard")
        card.setStyleSheet(_KPI_BOX_STYLE)
        card.setMinimumHeight(92)
        card.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        title_label = QtWidgets.QLabel(title)
        title_label.setStyleSheet(_KPI_TITLE_STYLE)
        value_label = QtWidgets.QLabel("--")
        value_label.setStyleSheet(f"{_KPI_VALUE_STYLE} color: {_KPI_FLAT};")
        value_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addStretch(1)
        card.setLayout(layout)
        return card, value_label

    @staticmethod
    def _make_curve_plot() -> pg.PlotWidget:
        """暗底坐标轴图，供资金/回撤曲线复用。"""
        plot = pg.PlotWidget(
            background=_CHART_BG,
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")},
        )
        # 去掉 PlotWidget 默认浅色边框/凸起框
        plot.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        plot.setStyleSheet(
            f"PlotWidget {{ background-color: {_CHART_BG}; border: none; }}"
        )
        plot.setMinimumHeight(120)
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.setMenuEnabled(False)
        plot.hideButtons()
        plot.addLegend(offset=(10, 8))
        plot.getPlotItem().getViewBox().setBackgroundColor(_CHART_BG)
        for name in ("left", "bottom"):
            axis = plot.getAxis(name)
            axis.setPen(pg.mkPen(_CHART_AXIS, width=1))
            axis.setTextPen(pg.mkPen(_CHART_AXIS))
        plot.setLabel("bottom", "日期")
        return plot

    @staticmethod
    def _wrap_plot(title: str, plot: pg.PlotWidget) -> QtWidgets.QWidget:
        panel = QtWidgets.QFrame()
        panel.setObjectName("chartPanel")
        panel.setStyleSheet(_CHART_BOX_STYLE)
        title_label = QtWidgets.QLabel(title)
        title_label.setStyleSheet("color: #c5cad3; font-size: 13px;")
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        layout.addWidget(title_label)
        layout.addWidget(plot, 1)
        panel.setLayout(layout)
        return panel

    def _update_buttons(self) -> None:
        """根据状态切换底部按钮。"""
        self.run_button.setEnabled(self._status in (BacktestStatus.IDLE, BacktestStatus.COMPLETED,
                                                    BacktestStatus.STOPPED, BacktestStatus.FAILED))
        self.stop_button.setEnabled(self._status == BacktestStatus.RUNNING)
        self.export_button.setEnabled(self._status == BacktestStatus.COMPLETED)

    def _set_status(self, status: str) -> None:
        self._status = status
        self.status_label.setText(f"状态：{status}")
        self._update_buttons()

    # -- 前置检查 -------------------------------------------------------

    def _validate(self) -> bool:
        """开始回测前的检查（文档 6.5.1 前置检查）。"""
        self._set_status(BacktestStatus.VALIDATING)
        sd = self.start_date.date()
        ed = self.end_date.date()
        if sd > ed:
            self._fail("开始日期不能晚于结束日期")
            return False
        if not self.vt_symbols:
            self._fail("策略未绑定标的（vt_symbols 为空）")
            return False
        # 检查 vnpy.duckdb 存在可打开 + 行情覆盖（简化：只查库可连）
        try:
            from vnpy.trader.database import get_database
            db = get_database()
            overviews = db.get_bar_overview()
            interval: Interval = self.INTERVAL_OPTIONS[self.interval_combo.currentIndex()][1]
            matched = [o for o in overviews
                       if o.interval == interval
                       and (o.symbol, o.exchange) in
                       [(s.split(".")[0], __import__(
                           "vnpy.trader.constant", fromlist=["Exchange"]).Exchange(s.split(".")[1]))
                        for s in self.vt_symbols if "." in s]]
            if not matched:
                self.quality_label.setText("数据质量：WARN（DuckDB 无匹配行情，回测可能无数据）")
            else:
                self.quality_label.setText("数据质量：PASS")
        except Exception as ex:  # noqa: BLE001
            self.quality_label.setText(f"数据质量：FAIL（{ex}）")
        self._set_status(BacktestStatus.READY)
        return True

    def _fail(self, msg: str) -> None:
        self._set_status(BacktestStatus.FAILED)
        self.detail_status.setText(msg)

    # -- 执行 -----------------------------------------------------------

    def _on_run(self) -> None:
        """点击开始回测：先检查，通过后在后台线程跑回测。"""
        if not self._validate():
            return
        self._stop_flag = False
        self._set_status(BacktestStatus.RUNNING)
        self._progress_phase = "load"
        self._symbols_done = 0
        self._symbols_total = max(1, len(self.vt_symbols))
        self._progress_value = 0
        self._output_logs = []
        self._pending_stats = {}
        self._pending_trades = {}
        self._pending_logs = []
        self._pending_daily = None
        self.progress_bar.setValue(0)
        self._clear_metrics_and_charts()
        self.detail_status.setText("")
        self.detail_table.setRowCount(0)

        # 后台线程执行回测
        import threading
        self._thread = threading.Thread(target=self._run_backtest, daemon=True)
        self._thread.start()

    def _run_backtest(self) -> None:
        """后台执行回测（不阻塞 Qt 主线程）。"""
        try:
            engine = BacktestingEngine()
            self._engine = engine
            engine.output = self._on_output
            # 注入实盘 SqlApp，供策略 refresh_universe 读 stock_near_ma
            try:
                from vnpy_sqlapp import APP_NAME as _SQLAPP_NAME
                from ..backtesting import _BacktestMainEngine
                sql_engine = self.strategy_engine.main_engine.get_engine(_SQLAPP_NAME)
                engine.main_engine = _BacktestMainEngine(sql_engine=sql_engine)
            except Exception:  # noqa: BLE001 - 无 SqlApp 时选股池刷新会跳过
                pass

            interval: Interval = self.INTERVAL_OPTIONS[self.interval_combo.currentIndex()][1]
            sd = self.start_date.date()
            ed = self.end_date.date()
            start: datetime = datetime(sd.year(), sd.month(), sd.day())
            end: datetime = datetime(ed.year(), ed.month(), ed.day(), 23, 59, 59)

            # 简化参数：rate/slippage/size/pricetick 用默认 0（回测不撮合细节）
            rates = {s: 0.0 for s in self.vt_symbols}
            slippages = {s: 0.0 for s in self.vt_symbols}
            sizes = {s: 1.0 for s in self.vt_symbols}
            priceticks = {s: 0.01 for s in self.vt_symbols}

            engine.set_parameters(
                self.vt_symbols, interval, start, rates, slippages,
                sizes, priceticks, capital=1_000_000, end=end,
            )

            # 注入 ReplaySentimentContext（阶段四核心）
            self._inject_sentiment_context(engine)

            # 策略类
            strategy_class = self.strategy_engine.classes.get(self.strategy_class_name)
            if strategy_class is None:
                self._fail_output(f"找不到策略类 {self.strategy_class_name}")
                return
            engine.add_strategy(strategy_class, self.strategy_setting)

            self._progress_phase = "load"
            engine.load_data()
            if not engine.trading_days:
                self._fail_output("DuckDB 无可用交易日，回测终止")
                return

            self._progress_phase = "replay"
            self.signal_progress.emit(60)
            engine.run_backtesting()

            if self._stop_flag:
                self.signal_status.emit(BacktestStatus.STOPPED)
                self.signal_log.emit("停止回测，已保留已完成结果")
            else:
                self._progress_phase = "stats"
                self.signal_progress.emit(90)
                df = engine.calculate_result()
                stats = engine.calculate_statistics(df, output=False) or {}
                # 结果先落到成员，再无参通知主线程渲染（避免 Signal(object) 排队丢失）
                self._pending_stats = {
                    k: (v.item() if hasattr(v, "item") else v)
                    for k, v in dict(stats).items()
                }
                self._pending_trades = dict(engine.trades)
                # 引擎 output + 策略 write_log 合并，避免策略无日志时明细页空白
                merged_logs = list(self._output_logs)
                for msg in engine.logs:
                    merged_logs.append(str(msg))
                self._pending_logs = merged_logs
                self._pending_daily = df
                self.signal_progress.emit(100)
                self.signal_finished.emit()
                self.signal_status.emit(BacktestStatus.COMPLETED)
        except Exception as ex:  # noqa: BLE001
            import traceback
            self._fail_output(f"回测异常：{ex}\n{traceback.format_exc()}")

    def _inject_sentiment_context(self, engine: BacktestingEngine) -> None:
        """注入情绪上下文：回测引擎走 make_replay_engine() 统一构造。

        无情绪历史时设 None，策略按既有保守降级。不启动 MarketSentimentEngine。
        """
        try:
            from vnpy_marketsentiment import make_replay_engine
            from ..context.sentiment import ReplaySentimentContext
            replay_engine = make_replay_engine()
            if replay_engine is None:
                engine.sentiment_context = None
                return
            engine.sentiment_context = ReplaySentimentContext(replay_engine)
        except Exception:  # noqa: BLE001 - 无情绪历史不阻断回测
            engine.sentiment_context = None

    def _on_stop(self) -> None:
        """停止回测：设停止标志（回测循环在当前 Bar 完成后停止）。"""
        self._stop_flag = True
        self._set_status(BacktestStatus.STOPPING)

    # -- 结果回传到 UI 线程 --------------------------------------------

    def _on_output(self, msg: str) -> None:
        """回测引擎输出 -> 日志 + 单调进度（线程安全经 Signal）。"""
        text = str(msg)
        self._output_logs.append(text)
        self.signal_log.emit(text)
        self._update_progress_from_log(text)

    def _update_progress_from_log(self, msg: str) -> None:
        """一次回测只走 0→100：按自然日回放进度映射。"""
        if "按自然日回放" in msg or "准备回测交易日" in msg:
            self.signal_progress.emit(max(self._progress_value, 5))
            return
        # 「回放进度：12/38 日」
        if "回放进度：" in msg and "/" in msg:
            self._progress_phase = "replay"
            try:
                part: str = msg.split("回放进度：", 1)[1]
                frac: str = part.split("日", 1)[0].strip()
                done_s, total_s = frac.split("/", 1)
                done: int = int(done_s)
                total: int = max(1, int(total_s))
                overall: int = 10 + int(done / total * 78)
                self.signal_progress.emit(min(88, max(10, overall)))
            except Exception:  # noqa: BLE001
                pass
            return
        if "策略初始化完成" in msg:
            self.signal_progress.emit(max(self._progress_value, 12))
        elif "历史数据回放结束" in msg:
            self.signal_progress.emit(88)

    @QtCore.Slot(int)
    def _set_progress(self, value: int) -> None:
        """进度只增不减；归零只在主线程点「开始回测」时做。"""
        value = max(0, min(100, int(value)))
        if value <= self._progress_value:
            return
        self._progress_value = value
        self.progress_bar.setValue(value)

    @QtCore.Slot(str)
    def _append_log(self, msg: str) -> None:
        if self._status == BacktestStatus.RUNNING:
            self.detail_status.setText(str(msg)[:80])

    def _on_status(self, status: str) -> None:
        self.signal_status.emit(status)

    @QtCore.Slot(str)
    def _set_status_slot(self, status: str) -> None:
        self._set_status(status)
        if status == BacktestStatus.COMPLETED:
            self._progress_value = 100
            self.progress_bar.setValue(100)

    def _fail_output(self, msg: str) -> None:
        self.signal_log.emit(msg)
        self.signal_status.emit(BacktestStatus.FAILED)

    @QtCore.Slot()
    def _on_finished(self) -> None:
        """主线程取回测结果并刷新指标卡、曲线与明细。"""
        self._trades = dict(self._pending_trades or {})
        self._logs = list(self._pending_logs or [])
        self._daily_df = self._pending_daily
        self._render_metrics(dict(self._pending_stats or {}))
        self._render_charts(self._daily_df)
        self._switch_detail(self.detail_list.currentRow())
        self._progress_value = 100
        self.progress_bar.setValue(100)
        trade_count = int((self._pending_stats or {}).get("total_trade_count", 0) or 0)
        if not trade_count:
            self.detail_status.setText(
                "无成交：可能未触发信号或标的/区间无匹配行情"
            )

    def _clear_metrics_and_charts(self) -> None:
        """清空 5 指标卡与双曲线。"""
        for label in self._kpi_values.values():
            label.setText("--")
            label.setStyleSheet(f"{_KPI_VALUE_STYLE} color: {_KPI_FLAT};")
        self._equity_curve.setData([], [])
        self._drawdown_curve.setData([], [])
        self._equity_hover.set_data([], [])
        self._drawdown_hover.set_data([], [])

    def _render_metrics(self, stats: dict) -> None:
        """刷新 5 个大指标卡。"""
        self._set_kpi_percent("total_return", stats.get("total_return"), signed=True)
        self._set_kpi_percent("max_ddpercent", stats.get("max_ddpercent"), signed=True)
        self._set_kpi_percent("annual_return", stats.get("annual_return"), signed=True)
        self._set_kpi_number("sharpe_ratio", stats.get("sharpe_ratio"), digits=2)
        self._set_kpi_count("total_trade_count", stats.get("total_trade_count"))

    def _set_kpi_percent(
        self, key: str, value: Any, *, signed: bool = False
    ) -> None:
        label = self._kpi_values[key]
        if value is None or value == "" or (
            isinstance(value, float) and (np.isnan(value) or np.isinf(value))
        ):
            label.setText("--")
            label.setStyleSheet(f"{_KPI_VALUE_STYLE} color: {_KPI_FLAT};")
            return
        num = float(value)
        text = f"{num:+.2f}%" if signed else f"{num:.2f}%"
        color = _KPI_UP if num > 0 else (_KPI_DOWN if num < 0 else _KPI_FLAT)
        # 最大回撤本身为负，固定红色更贴近 mock
        if key == "max_ddpercent" and num < 0:
            color = _KPI_DOWN
        label.setText(text)
        label.setStyleSheet(f"{_KPI_VALUE_STYLE} color: {color};")

    def _set_kpi_number(self, key: str, value: Any, *, digits: int = 2) -> None:
        label = self._kpi_values[key]
        if value is None or value == "" or (
            isinstance(value, float) and (np.isnan(value) or np.isinf(value))
        ):
            label.setText("--")
            label.setStyleSheet(f"{_KPI_VALUE_STYLE} color: {_KPI_FLAT};")
            return
        label.setText(f"{float(value):.{digits}f}")
        label.setStyleSheet(f"{_KPI_VALUE_STYLE} color: {_KPI_FLAT};")

    def _set_kpi_count(self, key: str, value: Any) -> None:
        label = self._kpi_values[key]
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            label.setText("--")
            label.setStyleSheet(f"{_KPI_VALUE_STYLE} color: {_KPI_FLAT};")
            return
        label.setText(f"{count:,}")
        label.setStyleSheet(f"{_KPI_VALUE_STYLE} color: {_KPI_FLAT};")

    def _render_charts(self, df: Any) -> None:
        """刷新资金曲线（净值）与回撤曲线（ddpercent）。"""
        if df is None or getattr(df, "empty", True):
            self._equity_curve.setData([], [])
            self._drawdown_curve.setData([], [])
            self._equity_hover.set_data([], [])
            self._drawdown_hover.set_data([], [])
            return

        xs: list[float] = []
        for idx in df.index:
            if isinstance(idx, datetime):
                xs.append(idx.timestamp())
            elif isinstance(idx, date):
                xs.append(datetime(idx.year, idx.month, idx.day).timestamp())
            else:
                try:
                    xs.append(datetime.fromisoformat(str(idx)[:10]).timestamp())
                except ValueError:
                    xs.append(float(len(xs)))

        capital = 1.0
        if self._engine is not None and getattr(self._engine, "capital", 0):
            capital = float(self._engine.capital) or 1.0

        if "balance" in df.columns:
            equity = (df["balance"].astype(float) / capital).tolist()
        else:
            equity = []

        if "ddpercent" in df.columns:
            drawdown = df["ddpercent"].astype(float).tolist()
        elif "drawdown" in df.columns and capital:
            drawdown = (df["drawdown"].astype(float) / capital * 100.0).tolist()
        else:
            drawdown = []

        if equity and len(equity) == len(xs):
            # 填充到序列最低点，避免净值从 0 起填整幅
            self._equity_curve.setFillLevel(min(equity))
            self._equity_curve.setData(xs, equity)
            self.equity_plot.setLabel("left", "净值")
            self._equity_hover.set_data(xs, equity)
        else:
            self._equity_curve.setData([], [])
            self._equity_hover.set_data([], [])

        if drawdown and len(drawdown) == len(xs):
            self._drawdown_curve.setData(xs, drawdown)
            self.drawdown_plot.setLabel("left", "回撤 %")
            self._drawdown_hover.set_data(xs, drawdown)
        else:
            self._drawdown_curve.setData([], [])
            self._drawdown_hover.set_data([], [])

    @QtCore.Slot(int)
    def _switch_detail(self, row: int) -> None:
        """左侧列表切换明细视图。"""
        self.detail_table.setRowCount(0)
        self.detail_table.setColumnCount(0)
        if row == 0:  # 成交明细
            self.detail_table.setColumnCount(6)
            self.detail_table.setHorizontalHeaderLabels(
                ["成交时间", "标的", "方向", "数量", "价格", "成交金额"]
            )
            trades = getattr(self, "_trades", {}) or {}
            if not trades:
                self.detail_table.setRowCount(1)
                self.detail_table.setItem(
                    0, 0, QtWidgets.QTableWidgetItem("本次回测无成交记录")
                )
                return
            for t in trades.values():
                r = self.detail_table.rowCount()
                self.detail_table.insertRow(r)
                dt = t.datetime
                if isinstance(dt, datetime):
                    dt_text = dt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    dt_text = str(dt)[:19]
                amount = float(t.volume) * float(t.price)
                self.detail_table.setItem(r, 0, QtWidgets.QTableWidgetItem(dt_text))
                self.detail_table.setItem(r, 1, QtWidgets.QTableWidgetItem(t.vt_symbol))
                self.detail_table.setItem(r, 2, QtWidgets.QTableWidgetItem(str(t.direction.value)))
                self.detail_table.setItem(r, 3, QtWidgets.QTableWidgetItem(str(t.volume)))
                self.detail_table.setItem(r, 4, QtWidgets.QTableWidgetItem(f"{float(t.price):.2f}"))
                self.detail_table.setItem(r, 5, QtWidgets.QTableWidgetItem(f"{amount:.2f}"))
            self.detail_table.resizeColumnToContents(0)
            self.detail_table.resizeColumnToContents(1)
            # 标的列默认再放宽一点，避免 600259.SSE 被截断
            self.detail_table.setColumnWidth(
                1, max(140, self.detail_table.columnWidth(1) + 24)
            )
        elif row == 1:  # 每日收益
            self.detail_table.setColumnCount(4)
            self.detail_table.setHorizontalHeaderLabels(
                ["日期", "成交笔数", "净盈亏", "日收益率"]
            )
            df = getattr(self, "_daily_df", None)
            if df is None or getattr(df, "empty", True):
                self.detail_table.setRowCount(1)
                self.detail_table.setItem(
                    0, 0, QtWidgets.QTableWidgetItem("无每日收益（通常因无成交）")
                )
                return
            for idx, row_data in df.iterrows():
                r = self.detail_table.rowCount()
                self.detail_table.insertRow(r)
                self.detail_table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(idx)))
                self.detail_table.setItem(
                    r, 1, QtWidgets.QTableWidgetItem(str(row_data.get("trade_count", "")))
                )
                self.detail_table.setItem(
                    r, 2, QtWidgets.QTableWidgetItem(f"{float(row_data.get('net_pnl', 0)):.2f}")
                )
                ret = row_data.get("return", None)
                self.detail_table.setItem(
                    r, 3,
                    QtWidgets.QTableWidgetItem(
                        f"{float(ret):.6f}" if ret is not None else ""
                    ),
                )
        elif row == 2:  # 回测日志
            self.detail_table.setColumnCount(1)
            self.detail_table.setHorizontalHeaderLabels(["日志"])
            logs = getattr(self, "_logs", []) or []
            if not logs:
                self.detail_table.setRowCount(1)
                self.detail_table.setItem(
                    0, 0, QtWidgets.QTableWidgetItem("无策略日志")
                )
                return
            for msg in logs:
                r = self.detail_table.rowCount()
                self.detail_table.insertRow(r)
                self.detail_table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(msg)))

    def _export_report(self) -> None:
        """导出回测报告（配置/质量/绩效/成交/情绪/版本）。"""
        if self._engine is None:
            return
        from vnpy.trader.utility import get_file_path
        path = get_file_path("backtest_report.txt")
        stats = self._engine.calculate_statistics(output=False) if self._status == BacktestStatus.COMPLETED else {}
        with open(str(path), "w", encoding="utf-8") as f:
            f.write(f"策略：{self.strategy_name}（{self.strategy_class_name}）\n")
            f.write(f"周期：{self.interval_combo.currentText()}\n")
            f.write(f"区间：{self.start_date.date().toString() } ~ {self.end_date.date().toString()}\n")
            f.write(f"行情数据库：vnpy.duckdb\n情绪数据库：vnpy.duckdb\n")
            f.write(f"数据质量：{self.quality_label.text()}\n")
            f.write("\n=== 绩效 ===\n")
            for k, v in stats.items():
                f.write(f"{k}: {v}\n")
            f.write("\n=== 成交 ===\n")
            for t in self._engine.trades.values():
                f.write(f"{t.datetime} {t.vt_symbol} {t.direction.value} {t.volume}@{t.price}\n")
        QtWidgets.QMessageBox.information(self, "导出", f"报告已导出到 {path}")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore[override]
        """关闭窗口：运行中先停止。"""
        if self._status in (BacktestStatus.RUNNING, BacktestStatus.VALIDATING, BacktestStatus.STOPPING):
            reply = QtWidgets.QMessageBox.question(
                self, "确认", "回测进行中，确认关闭？",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            )
            if reply == QtWidgets.QMessageBox.StandardButton.No:
                event.ignore()
                return
            self._on_stop()
        event.accept()
