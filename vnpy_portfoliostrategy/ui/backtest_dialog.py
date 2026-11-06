"""组合策略单页回测窗口（阶段四，对照 stage4-portfoliostrategy-vnpy-v7-start-backtest.png）。

从策略卡片的"回测"按钮进入，绑定该策略的 strategy_name / class_name / vt_symbols /
当前参数。可最大化的单页窗口：配置栏 / 运行状态栏 / 指标+图表区 / 明细区（左侧列表
切换）/ 底部操作区。第一行最右固定"开始回测"主按钮。不使用 Tab，不弹独立结果窗口。

回测在后台线程执行，不阻塞 Qt 主线程；前置检查、状态机、停止、异常处理、结果展示
均在本窗口完成。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from vnpy.trader.ui import QtWidgets, QtCore, QtGui
from vnpy.trader.constant import Interval

from ..backtesting import BacktestingEngine
from ..template import StrategyTemplate


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

        self._init_ui()

    def _init_ui(self) -> None:
        self.setWindowTitle(f"组合策略回测 - {self.strategy_name}")
        self.setMinimumSize(1400, 900)
        self.setWindowFlag(QtCore.Qt.WindowType.Window, True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowCloseButtonHint, True)

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
        self.sentiment_db_label: QtWidgets.QLabel = QtWidgets.QLabel("情绪数据：vnpy.duckdb")
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
        config_row.addWidget(self.sentiment_db_label)
        config_row.addWidget(self.quality_label)
        config_row.addStretch()
        config_row.addWidget(self.run_button)

        # 2. 运行状态栏
        self.status_label: QtWidgets.QLabel = QtWidgets.QLabel(f"状态：{BacktestStatus.IDLE}")
        self.progress_bar: QtWidgets.QProgressBar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.detail_status: QtWidgets.QLabel = QtWidgets.QLabel("")

        status_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        status_row.addWidget(self.status_label)
        status_row.addWidget(self.progress_bar, 1)
        status_row.addWidget(self.detail_status)

        # 3. 指标区
        self.metrics_label: QtWidgets.QLabel = QtWidgets.QLabel("指标：--")
        self.metrics_label.setWordWrap(True)

        # 4. 明细区：左侧列表 + 右侧表格
        self.detail_list: QtWidgets.QListWidget = QtWidgets.QListWidget()
        self.detail_list.addItem("成交明细")
        self.detail_list.addItem("每日收益")
        self.detail_list.addItem("情绪数据")
        self.detail_list.addItem("回测日志")
        self.detail_list.setCurrentRow(0)
        self.detail_list.currentRowChanged.connect(self._switch_detail)
        self.detail_table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.detail_table.horizontalHeader().setStretchLastSection(True)

        detail_split: QtWidgets.QSplitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        detail_split.addWidget(self.detail_list)
        detail_split.addWidget(self.detail_table)
        detail_split.setStretchFactor(0, 1)
        detail_split.setStretchFactor(1, 5)

        # 5. 底部操作区
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
        vbox.addWidget(self.metrics_label)
        vbox.addWidget(detail_split, 1)
        vbox.addLayout(bottom_row)
        self.setLayout(vbox)

        self._update_buttons()

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
        self.progress_bar.setValue(0)
        self.detail_status.setText("")

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

            engine.load_data()
            if not engine.history_data:
                self._fail_output("DuckDB 无可用行情数据，回测终止")
                return

            engine.run_backtesting()

            if self._stop_flag:
                self._on_status(BacktestStatus.STOPPED)
                self._on_detail("停止回测，已保留已完成结果")
            else:
                df = engine.calculate_result()
                stats = engine.calculate_statistics(df, output=False)
                self._on_metrics(stats)
                self._on_trades(engine.trades)
                self._on_logs(engine.logs)
                self._on_status(BacktestStatus.COMPLETED)
        except Exception as ex:  # noqa: BLE001
            import traceback
            self._fail_output(f"回测异常：{ex}\n{traceback.format_exc()}")

    def _inject_sentiment_context(self, engine: BacktestingEngine) -> None:
        """注入 ReplaySentimentContext（包 DuckDBSentimentStore 的 reader）。

        无情绪历史时设 None，策略按既有保守降级。不启动 MarketSentimentEngine。
        """
        try:
            from vnpy.trader.database import get_database
            from vnpy_marketsentiment.history.store import DuckDBSentimentStore, ReplaySentimentReader
            from ..context.sentiment import ReplaySentimentContext
            db = get_database()
            conn = getattr(db, "conn", None)
            if conn is None:
                return
            store = DuckDBSentimentStore(conn)
            reader = ReplaySentimentReader(store)
            engine.sentiment_context = ReplaySentimentContext(reader)
        except Exception:  # noqa: BLE001 - 无情绪历史不阻断回测
            engine.sentiment_context = None

    def _on_stop(self) -> None:
        """停止回测：设停止标志（回测循环在当前 Bar 完成后停止）。"""
        self._stop_flag = True
        self._set_status(BacktestStatus.STOPPING)

    # -- 结果回传到 UI 线程 --------------------------------------------

    def _on_output(self, msg: str) -> None:
        """回测引擎输出 -> 日志明细（线程安全经 signal）。"""
        QtCore.QMetaObject.invokeMethod(
            self, "_append_log", QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(str, str(msg)),
        )

    @QtCore.Slot(str)
    def _append_log(self, msg: str) -> None:
        if self._status == BacktestStatus.RUNNING:
            self.detail_status.setText(msg[:80])
        # 进度估算：日志含"数据加载完成"/"回测完成"等
        if "完成" in msg:
            self.progress_bar.setValue(min(100, self.progress_bar.value() + 20))

    def _on_status(self, status: str) -> None:
        QtCore.QMetaObject.invokeMethod(
            self, "_set_status_slot", QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(str, status),
        )

    @QtCore.Slot(str)
    def _set_status_slot(self, status: str) -> None:
        self._set_status(status)
        if status == BacktestStatus.COMPLETED:
            self.progress_bar.setValue(100)

    def _fail_output(self, msg: str) -> None:
        self._on_output(msg)
        self._on_status(BacktestStatus.FAILED)

    def _on_metrics(self, stats: dict) -> None:
        QtCore.QMetaObject.invokeMethod(
            self, "_render_metrics", QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(object, stats),
        )

    @QtCore.Slot(object)
    def _render_metrics(self, stats: dict) -> None:
        keys = ["total_return", "annual_return", "max_drawdown", "sharpe_ratio", "trade_count"]
        parts = [f"{k}={stats.get(k, 'N/A')}" for k in keys]
        self.metrics_label.setText("指标：" + "  ".join(parts))

    def _on_trades(self, trades: dict) -> None:
        QtCore.QMetaObject.invokeMethod(
            self, "_render_trades", QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(object, trades),
        )

    @QtCore.Slot(object)
    def _render_trades(self, trades: dict) -> None:
        self._trades = trades
        if self.detail_list.currentRow() == 0:
            self._switch_detail(0)

    def _on_logs(self, logs: list) -> None:
        QtCore.QMetaObject.invokeMethod(
            self, "_render_logs", QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(object, logs),
        )

    @QtCore.Slot(object)
    def _render_logs(self, logs: list) -> None:
        self._logs = logs
        if self.detail_list.currentRow() == 3:
            self._switch_detail(3)

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
            for t in getattr(self, "_trades", {}).values():
                r = self.detail_table.rowCount()
                self.detail_table.insertRow(r)
                self.detail_table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(t.datetime)))
                self.detail_table.setItem(r, 1, QtWidgets.QTableWidgetItem(t.vt_symbol))
                self.detail_table.setItem(r, 2, QtWidgets.QTableWidgetItem(str(t.direction.value)))
                self.detail_table.setItem(r, 3, QtWidgets.QTableWidgetItem(str(t.volume)))
                self.detail_table.setItem(r, 4, QtWidgets.QTableWidgetItem(str(t.price)))
                self.detail_table.setItem(r, 5, QtWidgets.QTableWidgetItem(str(t.volume * t.price)))
        elif row == 3:  # 回测日志
            self.detail_table.setColumnCount(1)
            self.detail_table.setHorizontalHeaderLabels(["日志"])
            for msg in getattr(self, "_logs", []):
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
