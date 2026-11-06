"""组合策略无界面回测脚本（BacktestingEngine）

用法：
    python script/run_backtesting.py

说明：
- 本仓库的 UI（PortfolioStrategyManager）只有实盘面板（添加/初始化/启动/停止），
  没有回测面板，因此回测通过无界面脚本调用 ``BacktestingEngine`` 完成。
- 回测数据必须已经在 vnpy 数据库中（默认 SQLite: ~/.vntrader/database.db）。
  可先用 GUI 的「数据管理/SqlApp」下载历史数据，或自行 save_bar_data 落库。
- ``BacktestingEngine`` 只回放 K 线（只调用 ``strategy.on_bars``），**不会推送 tick**，
  因此 tick 驱动的策略（如 NearMaSurgeStrategy）无法用它回测。
"""

from __future__ import annotations

from datetime import datetime

from pandas import DataFrame

from vnpy.trader.constant import Interval
from vnpy_portfoliostrategy import BacktestingEngine
from vnpy_portfoliostrategy.strategies.pair_trading_strategy import PairTradingStrategy


# ---------------------------------------------------------------------------
# 回测配置（按需修改）
# ---------------------------------------------------------------------------
STRATEGY_CLASS = PairTradingStrategy        # 策略类

VT_SYMBOLS: list[str] = [                   # 组合标的（与数据库里的 symbol.exchange 一致）
    "rb2210.SHFE",
    "rb2301.SHFE",
]
INTERVAL: Interval = Interval.MINUTE        # 内置策略基于分钟 K 线，用 MINUTE
START: datetime = datetime(2022, 7, 1)      # 起始日期（要留出策略初始化的预热期）
END: datetime = datetime(2022, 8, 1)        # 结束日期
CAPITAL: float = 1_000_000                  # 起始资金（必填，默认 0 会让统计指标全部失真）
RISK_FREE: float = 0                        # 无风险利率（年化，Sharpe 用）
ANNUAL_DAYS: int = 240                      # 年化交易日数

# 各合约交易参数（手续费率 / 滑点 / 合约乘数 / 最小价格跳动）
#   期货：rate 按成交额比例（0.0001 = 万分之一）；slippage 是**价格**单位（如 1.0 元/吨）
#   股票：size=1，pricetick=0.01，rate 为佣金率，slippage 常取 0 或 0.01
DEFAULT_RATE: float = 0.0001
DEFAULT_SLIPPAGE: float = 0.0
DEFAULT_SIZE: float = 10
DEFAULT_PRICETICK: float = 1.0

# 需要按合约单独覆盖时填写，例如：
# SYMBOL_SETTINGS: dict[str, dict] = {"rb2210.SHFE": {"size": 10, "pricetick": 1.0}}
SYMBOL_SETTINGS: dict[str, dict] = {}

# 策略参数（覆盖策略类默认值；留空即用默认值）
STRATEGY_SETTING: dict = {}


def _build_params(key: str, default: float) -> dict[str, float]:
    """按 vt_symbol 生成 rates / slippages / sizes / priceticks 字典。"""
    return {
        vt_symbol: SYMBOL_SETTINGS.get(vt_symbol, {}).get(key, default)
        for vt_symbol in VT_SYMBOLS
    }


def run_backtesting() -> None:
    """执行一次回测。"""
    engine: BacktestingEngine = BacktestingEngine()

    engine.set_parameters(
        vt_symbols=VT_SYMBOLS,
        interval=INTERVAL,
        start=START,
        end=END,
        rates=_build_params("rate", DEFAULT_RATE),
        slippages=_build_params("slippage", DEFAULT_SLIPPAGE),
        sizes=_build_params("size", DEFAULT_SIZE),
        priceticks=_build_params("pricetick", DEFAULT_PRICETICK),
        capital=CAPITAL,
        risk_free=RISK_FREE,
        annual_days=ANNUAL_DAYS,
    )

    engine.add_strategy(STRATEGY_CLASS, STRATEGY_SETTING)

    engine.load_data()              # 1) 从数据库读历史 K 线到 engine.history_data
    engine.run_backtesting()        # 2) 预热（策略 on_init 里 load_bars(N) 的 N 天）+ 回放
    engine.calculate_result()       # 3) 逐日盯市盈亏 -> engine.daily_df
    statistics = engine.calculate_statistics()   # 4) 控制台打印 + 返回 dict
    engine.show_chart()             # 5) 资金曲线 / 回撤 / 每日盈亏 / 盈亏分布

    # ---- 查看 / 保存结果 ----
    print("\n统计指标（dict）：", statistics)
    print(f"成交笔数：{engine.trade_count}，委托笔数：{engine.limit_order_count}")
    print(f"策略日志（engine.logs）共 {len(engine.logs)} 行")

    if engine.daily_df is not None:
        engine.daily_df.to_csv("backtest_daily.csv", encoding="utf-8-sig")
        print("逐日盯市已保存：backtest_daily.csv")

    trades_df = DataFrame([
        {
            "datetime": trade.datetime,
            "vt_symbol": trade.vt_symbol,
            "direction": trade.direction.value,
            "offset": trade.offset.value,
            "price": trade.price,
            "volume": trade.volume,
        }
        for trade in engine.get_all_trades()
    ])
    trades_df.to_csv("backtest_trades.csv", index=False, encoding="utf-8-sig")
    print(f"成交明细已保存：backtest_trades.csv（{len(trades_df)} 笔）")


if __name__ == "__main__":
    run_backtesting()
