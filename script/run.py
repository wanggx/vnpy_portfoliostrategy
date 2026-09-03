import os
import sys

# 优先使用旁边 vnpy_xt 仓库根下的 bigqmt_signal_trader / xtquant shim，
# 避免 site-packages 里官方 MiniQMT xtquant 抢 import。
_VNPY_XT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "vnpy_xt")
)
if os.path.isdir(_VNPY_XT_ROOT) and _VNPY_XT_ROOT not in sys.path:
    sys.path.insert(0, _VNPY_XT_ROOT)

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_xt import XtGateway
from vnpy_sqlapp import SqlApp
from vnpy_scripttrader import ScriptTraderApp
from vnpy_portfoliostrategy import PortfolioStrategyApp


def main() -> None:
    """Start Trader"""
    qapp = create_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(XtGateway)

    main_engine.add_app(PortfolioStrategyApp)
    main_engine.add_app(SqlApp)
    main_engine.add_app(ScriptTraderApp)

    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
