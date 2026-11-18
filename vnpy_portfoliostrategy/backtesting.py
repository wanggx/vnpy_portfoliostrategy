from collections import defaultdict
from datetime import date, datetime, time, timedelta
from functools import lru_cache, partial
from copy import copy
import traceback

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pandas import DataFrame
from collections.abc import Callable
from typing import Any

from vnpy.trader.constant import Direction, Offset, Interval, Status
from vnpy.trader.database import get_database, BaseDatabase
from vnpy.trader.object import OrderData, TradeData, BarData
from vnpy.trader.utility import round_to, extract_vt_symbol
from vnpy.trader.optimize import (
    OptimizationSetting,
    check_optimization_setting,
    run_bf_optimization,
    run_ga_optimization
)

from .base import EngineType, APP_NAME
from .locale import _
from .template import StrategyTemplate


INTERVAL_DELTA_MAP: dict[Interval, timedelta] = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}

# xtquant 代码后缀 -> vnpy Exchange.value（与 NearMaSurgeStrategy 对齐）
_XT_SUFFIX_EXCHANGE: dict[str, str] = {
    ".SH": "SSE",
    ".SZ": "SZSE",
    ".BJ": "BSE",
}


def _xt_code_to_vt_symbol(code: str) -> str:
    """000001.SZ → 000001.SZSE；无法识别返回空串。"""
    text: str = str(code).strip()
    for suffix, exchange in _XT_SUFFIX_EXCHANGE.items():
        if text.endswith(suffix):
            return f"{text[: -len(suffix)]}.{exchange}"
    return ""


class _BacktestMainEngine:
    """回测用空主引擎：满足策略侧 get_contract / get_engine 等只读调用。"""

    def __init__(self, sql_engine: Any = None) -> None:
        # 注入实盘 SqlApp，供 NearMaSurgeStrategy.refresh_universe 查 stock_near_ma
        self._sql_engine: Any = sql_engine

    def get_contract(self, vt_symbol: str) -> Any:
        return None

    def get_all_positions(self) -> list:
        return []

    def get_engine(self, name: str) -> Any:
        if self._sql_engine is None:
            return None
        if name == getattr(self._sql_engine, "engine_name", None):
            return self._sql_engine
        return None

    def get_gateway(self, name: str) -> Any:
        return None


class BacktestingEngine:
    """组合策略回测引擎"""

    engine_type: EngineType = EngineType.BACKTESTING
    gateway_name: str = "BACKTESTING"

    def __init__(self) -> None:
        """构造函数"""
        self.vt_symbols: list[str] = []
        self.start: datetime
        self.end: datetime

        self.rates: dict[str, float]
        self.slippages: dict[str, float]
        self.sizes: dict[str, float]
        self.priceticks: dict[str, float]

        self.capital: float = 1_000_000
        self.risk_free: float = 0
        self.annual_days: int = 240

        self.strategy_class: type[StrategyTemplate]
        self.strategy: StrategyTemplate
        self.bars: dict[str, BarData] = {}
        self.datetime: datetime = datetime(1970, 1, 1)

        self.interval: Interval
        self.days: int = 0
        self.history_data: dict[tuple, BarData] = {}
        self.dts: set[datetime] = set()

        self.limit_order_count: int = 0
        self.limit_orders: dict[str, OrderData] = {}
        self.active_limit_orders: dict[str, OrderData] = {}

        self.trade_count: int = 0
        self.trades: dict[str, TradeData] = {}

        self.logs: list = []

        self.daily_results: dict[date, PortfolioDailyResult] = {}
        self.daily_df: DataFrame = None

        # 阶段四：回测情绪上下文（ReplaySentimentContext），由回测入口注入。
        # 为 None 时信号器无历史情绪，按既有保守降级。不启动 MarketSentimentEngine 后台线程。
        self.sentiment_context: Any = None

        # 与实盘 StrategyEngine 对齐的只读/空操作接口：NearMaSurgeStrategy.on_init
        # 会读 strategy_data、调 subscribe_symbols；回测无持久化、无真实订阅。
        self.strategy_data: dict[str, dict] = {}
        self.main_engine: Any = _BacktestMainEngine()
        # 按自然日回放：交易日列表；当日已加载行情的标的
        self.trading_days: list[date] = []
        self._day_loaded: set[str] = set()

    def clear_data(self) -> None:
        """清理上次回测缓存数据"""
        self.limit_order_count = 0
        self.limit_orders.clear()
        self.active_limit_orders.clear()

        self.trade_count = 0
        self.trades.clear()

        self.logs.clear()
        self.daily_results.clear()
        self.daily_df = None
        self.trading_days.clear()
        self._day_loaded.clear()

    def set_parameters(
        self,
        vt_symbols: list[str],
        interval: Interval,
        start: datetime,
        rates: dict[str, float],
        slippages: dict[str, float],
        sizes: dict[str, float],
        priceticks: dict[str, float],
        capital: float = 0,
        end: datetime | None = None,
        risk_free: float = 0,
        annual_days: int = 240
    ) -> None:
        """设置参数"""
        self.vt_symbols = vt_symbols
        self.interval = interval

        self.rates = rates
        self.slippages = slippages
        self.sizes = sizes
        self.priceticks = priceticks

        self.start = start
        if not end:
            self.end = datetime.now()
        else:
            self.end = end.replace(hour=23, minute=59, second=59)

        self.capital = capital
        self.risk_free = risk_free
        self.annual_days = annual_days

    def add_strategy(self, strategy_class: type[StrategyTemplate], setting: dict) -> None:
        """增加策略"""
        self.strategy_class = strategy_class
        self.strategy = strategy_class(
            self, strategy_class.__name__, copy(self.vt_symbols), setting
        )

    def load_data(self) -> None:
        """准备回测：只生成交易日序列，不预载全区间行情（按天边取边放）。"""
        self.output(_("开始准备回测交易日"))

        if not self.end:
            self.end = datetime.now()

        if self.start >= self.end:
            self.output(_("起始日期必须小于结束日期"))
            return

        self.history_data.clear()
        self.dts.clear()
        self.trading_days.clear()
        self._day_loaded.clear()

        self.trading_days = self._build_trading_days()
        if not self.trading_days:
            self.output(_("未找到回测区间内的交易日"))
            return
        self.output(
            _("按自然日回放：共 {} 个交易日（{} ~ {}）").format(
                len(self.trading_days),
                self.trading_days[0].isoformat(),
                self.trading_days[-1].isoformat(),
            )
        )

    def _build_trading_days(self) -> list[date]:
        """用 DuckDB 日线日期并集作为交易日（轻量，不读分钟线）。"""
        database: BaseDatabase = get_database()
        if not hasattr(database, "conn") or not hasattr(database, "lock"):
            return []
        sql: str = """
            SELECT DISTINCT CAST(datetime AS DATE) AS d
            FROM dbbardata
            WHERE interval = ?
              AND exchange IN ('SSE', 'SZSE')
              AND datetime >= ?
              AND datetime <= ?
            ORDER BY d
        """
        params: tuple = (
            Interval.DAILY.value,
            self.start.replace(hour=0, minute=0, second=0),
            self.end,
        )
        try:
            with database.lock:  # type: ignore[attr-defined]
                cursor = database.conn.cursor()  # type: ignore[attr-defined]
                try:
                    rows = cursor.execute(sql, params).fetchall()
                finally:
                    cursor.close()
        except Exception as exc:  # noqa: BLE001
            self.output(_("生成交易日失败：{}").format(exc))
            return []

        days: list[date] = []
        for row in rows:
            value = row[0]
            if isinstance(value, datetime):
                days.append(value.date())
            elif isinstance(value, date):
                days.append(value)
            else:
                days.append(datetime.strptime(str(value)[:10], "%Y-%m-%d").date())
        return days

    def _register_symbol(self, vt_symbol: str) -> bool:
        """登记合约到引擎列表与默认费率；已存在返回 False。"""
        if not vt_symbol or vt_symbol in self.vt_symbols:
            return False
        self.vt_symbols.append(vt_symbol)
        self.rates.setdefault(vt_symbol, 0.0)
        self.slippages.setdefault(vt_symbol, 0.0)
        self.sizes.setdefault(vt_symbol, 1.0)
        self.priceticks.setdefault(vt_symbol, 0.01)
        return True

    def _active_symbols(self) -> set[str]:
        """当前应关注的标的：策略订阅 ∪ 有持仓。"""
        active: set[str] = set(self.strategy.vt_symbols)
        for vt_symbol, pos in self.strategy.pos_data.items():
            if pos:
                active.add(vt_symbol)
        return active

    def _near_ma_pool_for_day(self, day: date) -> set[str]:
        """会话日对应的选股池（取 stock_near_ma 中严格小于该日的最大 trade_date）。"""
        strategy: StrategyTemplate | None = getattr(self, "strategy", None)
        if strategy is None:
            return set()
        sector: str = str(getattr(strategy, "industry_name", "") or "").strip()
        table: str = str(getattr(strategy, "TABLE_NAME", "") or "").strip()
        if not sector or not table:
            return set()
        try:
            from vnpy_sqlapp import APP_NAME as _SQLAPP_NAME
        except Exception:  # noqa: BLE001
            return set()
        sql_engine = self.main_engine.get_engine(_SQLAPP_NAME)
        if sql_engine is None:
            return set()

        asof: str = day.strftime("%Y%m%d")
        try:
            trade_date = sql_engine.scalar(
                f"SELECT MAX(trade_date) FROM {table} "
                f"WHERE sector_name = ? AND trade_date < ?",
                (sector, asof),
            )
            if not trade_date:
                return set()
            rows: list[dict] = sql_engine.query_all(
                f"SELECT code FROM {table} "
                f"WHERE sector_name = ? AND trade_date = ?",
                (sector, str(trade_date)),
            )
        except Exception as exc:  # noqa: BLE001
            self.output(_("取当日选股池失败 {}：{}").format(asof, exc))
            return set()

        pool: set[str] = set()
        for row in rows:
            vt_symbol: str = _xt_code_to_vt_symbol(str(row.get("code", "")))
            if vt_symbol:
                pool.add(vt_symbol)
        return pool

    def _load_symbol_day(self, vt_symbol: str, day: date) -> int:
        """加载某标的某一个自然日的行情到 history_data，返回根数。"""
        key: str = f"{vt_symbol}:{day.isoformat()}"
        if key in self._day_loaded:
            return 0
        day_start: datetime = datetime.combine(day, time.min)
        day_end: datetime = datetime.combine(day, time(23, 59, 59))
        data: list[BarData] = load_bar_data(
            vt_symbol, self.interval, day_start, day_end
        )
        for bar in data:
            self.history_data[(bar.datetime, vt_symbol)] = bar
        self._day_loaded.add(key)
        self._register_symbol(vt_symbol)
        return len(data)

    def _prepare_day(self, day: date) -> list[datetime]:
        """取一天行情：选股池 ∪ 当前订阅 ∪ 持仓；返回当日时间戳序列。"""
        self._day_loaded.clear()
        symbols: set[str] = self._active_symbols() | self._near_ma_pool_for_day(day)
        if not symbols:
            # 无池时至少用引擎初始标的撑开当日时间轴
            symbols = set(self.vt_symbols)

        total_bars: int = 0
        for vt_symbol in sorted(symbols):
            total_bars += self._load_symbol_day(vt_symbol, day)

        day_dts: list[datetime] = sorted(
            {
                dt
                for dt, _symbol in self.history_data.keys()
                if dt.date() == day
            }
        )
        self.output(
            _("{}：加载 {} 个标的、{} 根K线、{} 个时点").format(
                day.isoformat(), len(symbols), total_bars, len(day_dts)
            )
        )
        return day_dts

    def _release_day(self, day: date) -> None:
        """释放当日 history，控制内存。"""
        stale: list[tuple] = [
            key for key in self.history_data if key[0].date() == day
        ]
        for key in stale:
            self.history_data.pop(key, None)
        # 清掉当日推送缓存；持仓标的下一交易日会重新加载
        self.bars.clear()
        self._day_loaded.clear()

    def run_backtesting(self) -> None:
        """按自然日：一天取数 → 按时间回放 → 释放，再下一天。"""
        # 阶段四：把回测情绪上下文同步到策略 context，供信号器经查找链解析。
        if self.sentiment_context is not None and hasattr(self.strategy, "context"):
            self.strategy.context.sentiment = self.sentiment_context

        if not self.trading_days:
            self.output(_("无交易日，回测终止"))
            return

        self.strategy.on_init()

        # 组合策略通常 days=0；若 >0 则前 N 个交易日只预热不交易
        warmup_days: int = max(0, int(self.days))
        if warmup_days <= 0:
            self.strategy.inited = True
            self.strategy.on_start()
            self.strategy.trading = True
            self.output(_("策略初始化完成，开始按日回放"))
        else:
            self.output(_("策略预热 {} 个交易日").format(warmup_days))

        for index, day in enumerate(self.trading_days, start=1):
            try:
                day_dts: list[datetime] = self._prepare_day(day)
                for dt in day_dts:
                    self.new_bars(dt)
            except Exception:
                self.output(_("触发异常，回测终止"))
                self.output(traceback.format_exc())
                self._release_day(day)
                return

            if not self.strategy.inited and index >= warmup_days:
                self.strategy.inited = True
                self.strategy.on_start()
                self.strategy.trading = True
                self.output(_("策略初始化完成，开始交易回放"))

            self._release_day(day)
            if index % 5 == 0 or index == len(self.trading_days):
                self.output(
                    _("回放进度：{}/{} 日（当前 {}）").format(
                        index, len(self.trading_days), day.isoformat()
                    )
                )

        self.output(_("历史数据回放结束"))

    def calculate_result(self) -> DataFrame:
        """计算逐日盯市盈亏"""
        self.output(_("开始计算逐日盯市盈亏"))

        if not self.trades:
            self.output(_("成交记录为空，无法计算"))
            return

        for trade in self.trades.values():
            d: date = trade.datetime.date()
            daily_result: PortfolioDailyResult = self.daily_results[d]
            daily_result.add_trade(trade)

        pre_closes: dict = {}
        start_poses: dict = {}

        for daily_result in self.daily_results.values():
            daily_result.calculate_pnl(
                pre_closes,
                start_poses,
                self.sizes,
                self.rates,
                self.slippages,
            )

            pre_closes = daily_result.close_prices
            start_poses = daily_result.end_poses

        results: dict = defaultdict(list)

        for daily_result in self.daily_results.values():
            fields: list = [
                "date", "trade_count", "turnover",
                "commission", "slippage", "trading_pnl",
                "holding_pnl", "total_pnl", "net_pnl"
            ]
            for key in fields:
                value = getattr(daily_result, key)
                results[key].append(value)

        if results:
            self.daily_df = DataFrame.from_dict(results).set_index("date")

        self.output(_("逐日盯市盈亏计算完成"))
        return self.daily_df

    def calculate_statistics(self, df: DataFrame = None, output: bool = True) -> dict:
        """计算策略统计指标"""
        self.output(_("开始计算策略统计指标"))

        if df is None:
            df = self.daily_df

        # 初始化统计指标
        start_date: str = ""
        end_date: str = ""
        total_days: int = 0
        profit_days: int = 0
        loss_days: int = 0
        end_balance: float = 0
        max_drawdown: float = 0
        max_ddpercent: float = 0
        max_drawdown_duration: int = 0
        total_net_pnl: float = 0
        daily_net_pnl: float = 0
        total_commission: float = 0
        daily_commission: float = 0
        total_slippage: float = 0
        daily_slippage: float = 0
        total_turnover: float = 0
        daily_turnover: float = 0
        total_trade_count: int = 0
        daily_trade_count: float = 0
        total_return: float = 0
        annual_return: float = 0
        daily_return: float = 0
        return_std: float = 0
        sharpe_ratio: float = 0
        return_drawdown_ratio: float = 0

        # 检查是否发生过爆仓
        positive_balance: bool = False

        # 计算资金相关指标
        if df is not None:
            df["balance"] = df["net_pnl"].cumsum() + self.capital
            df["return"] = np.log(df["balance"] / df["balance"].shift(1)).fillna(0)
            df["highlevel"] = df["balance"].rolling(min_periods=1, window=len(df), center=False).max()
            df["drawdown"] = df["balance"] - df["highlevel"]
            df["ddpercent"] = df["drawdown"] / df["highlevel"] * 100

            # 检查是否发生过爆仓
            positive_balance = (df["balance"] > 0).all()
            if not positive_balance:
                self.output(_("回测中出现爆仓（资金小于等于0），无法计算策略统计指标"))

        # 计算统计指标
        if positive_balance:
            start_date = df.index[0]
            end_date = df.index[-1]

            total_days = len(df)
            profit_days = len(df[df["net_pnl"] > 0])
            loss_days= len(df[df["net_pnl"] < 0])

            end_balance = df["balance"].iloc[-1]
            max_drawdown = df["drawdown"].min()
            max_ddpercent = df["ddpercent"].min()
            max_drawdown_end = df["drawdown"].idxmin()

            if isinstance(max_drawdown_end, date):
                max_drawdown_start = df["balance"][:max_drawdown_end].idxmax()          # type: ignore
                max_drawdown_duration = (max_drawdown_end - max_drawdown_start).days
            else:
                max_drawdown_duration = 0

            total_net_pnl = df["net_pnl"].sum()
            daily_net_pnl = total_net_pnl / total_days

            total_commission = df["commission"].sum()
            daily_commission = total_commission / total_days

            total_slippage = df["slippage"].sum()
            daily_slippage = total_slippage / total_days

            total_turnover = df["turnover"].sum()
            daily_turnover = total_turnover / total_days

            total_trade_count = df["trade_count"].sum()
            daily_trade_count = total_trade_count / total_days

            total_return = (end_balance / self.capital - 1) * 100
            annual_return = total_return / total_days * self.annual_days
            daily_return = df["return"].mean() * 100
            return_std = df["return"].std() * 100

            if return_std:
                daily_risk_free: float = self.risk_free / np.sqrt(self.annual_days)
                sharpe_ratio = (daily_return - daily_risk_free) / return_std * np.sqrt(self.annual_days)
            else:
                sharpe_ratio = 0

            return_drawdown_ratio = -total_net_pnl / max_drawdown

        # 输出结果
        if output:
            self.output("-" * 30)
            self.output(_("首个交易日：\t{}").format(start_date))
            self.output(_("最后交易日：\t{}").format(end_date))

            self.output(_("总交易日：\t{}").format(total_days))
            self.output(_("盈利交易日：\t{}").format(profit_days))
            self.output(_("亏损交易日：\t{}").format(loss_days))

            self.output(_("起始资金：\t{:,.2f}").format(self.capital))
            self.output(_("结束资金：\t{:,.2f}").format(end_balance))

            self.output(_("总收益率：\t{:,.2f}%").format(total_return))
            self.output(_("年化收益：\t{:,.2f}%").format(annual_return))
            self.output(_("最大回撤: \t{:,.2f}").format(max_drawdown))
            self.output(_("百分比最大回撤: {:,.2f}%").format(max_ddpercent))
            self.output(_("最长回撤天数: \t{}").format(max_drawdown_duration))

            self.output(_("总盈亏：\t{:,.2f}").format(total_net_pnl))
            self.output(_("总手续费：\t{:,.2f}").format(total_commission))
            self.output(_("总滑点：\t{:,.2f}").format(total_slippage))
            self.output(_("总成交金额：\t{:,.2f}").format(total_turnover))
            self.output(_("总成交笔数：\t{}").format(total_trade_count))

            self.output(_("日均盈亏：\t{:,.2f}").format(daily_net_pnl))
            self.output(_("日均手续费：\t{:,.2f}").format(daily_commission))
            self.output(_("日均滑点：\t{:,.2f}").format(daily_slippage))
            self.output(_("日均成交金额：\t{:,.2f}").format(daily_turnover))
            self.output(_("日均成交笔数：\t{}").format(daily_trade_count))

            self.output(_("日均收益率：\t{:,.2f}%").format(daily_return))
            self.output(_("收益标准差：\t{:,.2f}%").format(return_std))
            self.output(f"Sharpe Ratio：\t{sharpe_ratio:,.2f}")
            self.output(_("收益回撤比：\t{:,.2f}").format(return_drawdown_ratio))

        statistics: dict = {
            "start_date": start_date,
            "end_date": end_date,
            "total_days": total_days,
            "profit_days": profit_days,
            "loss_days": loss_days,
            "capital": self.capital,
            "end_balance": end_balance,
            "max_drawdown": max_drawdown,
            "max_ddpercent": max_ddpercent,
            "max_drawdown_duration": max_drawdown_duration,
            "total_net_pnl": total_net_pnl,
            "daily_net_pnl": daily_net_pnl,
            "total_commission": total_commission,
            "daily_commission": daily_commission,
            "total_slippage": total_slippage,
            "daily_slippage": daily_slippage,
            "total_turnover": total_turnover,
            "daily_turnover": daily_turnover,
            "total_trade_count": total_trade_count,
            "daily_trade_count": daily_trade_count,
            "total_return": total_return,
            "annual_return": annual_return,
            "daily_return": daily_return,
            "return_std": return_std,
            "sharpe_ratio": sharpe_ratio,
            "return_drawdown_ratio": return_drawdown_ratio,
        }

        # 过滤极值
        for key, value in statistics.items():
            if value in (np.inf, -np.inf):
                value = 0
            statistics[key] = np.nan_to_num(value)

        self.output(_("策略统计指标计算完成"))
        return statistics

    def show_chart(self, df: DataFrame = None) -> None:
        """显示图表"""
        if df is None:
            df = self.daily_df

        if df is None:
            return

        fig = make_subplots(
            rows=4,
            cols=1,
            subplot_titles=["Balance", "Drawdown", "Daily Pnl", "Pnl Distribution"],
            vertical_spacing=0.06
        )

        balance_line = go.Scatter(
            x=df.index,
            y=df["balance"],
            mode="lines",
            name="Balance"
        )
        drawdown_scatter = go.Scatter(
            x=df.index,
            y=df["drawdown"],
            fillcolor="red",
            fill='tozeroy',
            mode="lines",
            name="Drawdown"
        )
        pnl_bar = go.Bar(y=df["net_pnl"], name="Daily Pnl")
        pnl_histogram = go.Histogram(x=df["net_pnl"], nbinsx=100, name="Days")

        fig.add_trace(balance_line, row=1, col=1)
        fig.add_trace(drawdown_scatter, row=2, col=1)
        fig.add_trace(pnl_bar, row=3, col=1)
        fig.add_trace(pnl_histogram, row=4, col=1)

        fig.update_layout(height=1000, width=1000)
        fig.show()

    def run_bf_optimization(
        self,
        optimization_setting: OptimizationSetting,
        output: bool = True,
        max_workers: int | None = None
    ) -> list:
        """暴力穷举优化"""
        if not check_optimization_setting(optimization_setting):
            return []

        evaluate_func: Callable = wrap_evaluate(self, optimization_setting.target_name)
        results: list = run_bf_optimization(
            evaluate_func,
            optimization_setting,
            get_target_value,
            max_workers=max_workers,
            output=self.output,
        )

        if output:
            for result in results:
                msg: str = _("参数：{}, 目标：{}").format(result[0], result[1])
                self.output(msg)

        return results

    run_optimization = run_bf_optimization

    def run_ga_optimization(
        self,
        optimization_setting: OptimizationSetting,
        max_workers: int | None = None,
        ngen: int = 30,
        output: bool = True
    ) -> list:
        """遗传算法优化"""
        if not check_optimization_setting(optimization_setting):
            return []

        evaluate_func: Callable = wrap_evaluate(self, optimization_setting.target_name)
        results: list = run_ga_optimization(
            evaluate_func,
            optimization_setting,
            get_target_value,
            max_workers=max_workers,
            ngen=ngen,
            output=self.output
        )

        if output:
            for result in results:
                msg: str = _("参数：{}, 目标：{}").format(result[0], result[1])
                self.output(msg)

        return results

    def update_daily_close(self, bars: dict[str, BarData], dt: datetime) -> None:
        """更新每日收盘价"""
        d: date = dt.date()

        close_prices: dict = {}
        for bar in bars.values():
            close_prices[bar.vt_symbol] = bar.close_price

        daily_result: PortfolioDailyResult | None = self.daily_results.get(d, None)

        if daily_result:
            daily_result.update_close_prices(close_prices)
        else:
            self.daily_results[d] = PortfolioDailyResult(d, close_prices)

    def new_bars(self, dt: datetime) -> None:
        """历史数据推送。

        只向「当前订阅 ∪ 有持仓」推送 bars，与实盘动态订阅一致；全市场预加载的
        其它标的留在 history_data，等日切 subscribe 后再进推送集。
        """
        self.datetime = dt

        active: set[str] = set(self.strategy.vt_symbols)
        for vt_symbol, pos in self.strategy.pos_data.items():
            if pos:
                active.add(vt_symbol)

        bars: dict[str, BarData] = {}
        for vt_symbol in active:
            bar: BarData | None = self.history_data.get((dt, vt_symbol), None)

            # 判断是否获取到该合约指定时间的历史数据
            if bar:
                # 更新K线以供委托撮合
                self.bars[vt_symbol] = bar
                # 缓存K线数据以供strategy.on_bars更新
                bars[vt_symbol] = bar
            # 如果获取不到，但self.bars字典中已有合约数据缓存, 使用之前的数据填充
            elif vt_symbol in self.bars:
                old_bar: BarData = self.bars[vt_symbol]

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
                self.bars[vt_symbol] = bar

        # 阶段四：回测时间注入——在每个 Bar 前设置情绪回放时钟，
        # 使策略/信号器经统一上下文读到当前回测时刻的历史情绪（不读未来）。
        # 严格顺序：合并当前时间点 Bar → 设置情绪上下文时间 → 撮合 → on_bars。
        if self.sentiment_context is not None:
            self.sentiment_context.set_datetime(dt)

        self.cross_limit_order()
        self.strategy.on_bars(bars)

        if self.strategy.inited:
            self.update_daily_close(self.bars, dt)

    def cross_limit_order(self) -> None:
        """撮合限价委托"""
        for order in list(self.active_limit_orders.values()):
            bar: BarData = self.bars[order.vt_symbol]

            long_cross_price: float = bar.low_price
            short_cross_price: float = bar.high_price
            long_best_price: float = bar.open_price
            short_best_price: float = bar.open_price

            # 推送委托未成交状态更新
            if order.status == Status.SUBMITTING:
                order.status = Status.NOTTRADED
                self.strategy.update_order(order)

            # 检查可以被撮合的限价委托
            long_cross: bool = (
                order.direction == Direction.LONG
                and order.price >= long_cross_price
                and long_cross_price > 0
            )

            short_cross: bool = (
                order.direction == Direction.SHORT
                and order.price <= short_cross_price
                and short_cross_price > 0
            )

            if not long_cross and not short_cross:
                continue

            # 推送委托成交状态更新
            order.traded = order.volume
            order.status = Status.ALLTRADED
            self.strategy.update_order(order)

            if order.vt_orderid in self.active_limit_orders:
                self.active_limit_orders.pop(order.vt_orderid)

            # 推送成交信息
            self.trade_count += 1

            if long_cross:
                trade_price = min(order.price, long_best_price)
            else:
                trade_price = max(order.price, short_best_price)

            trade: TradeData = TradeData(
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid=str(self.trade_count),
                direction=order.direction,
                offset=order.offset,
                price=trade_price,
                volume=order.volume,
                datetime=self.datetime,
                gateway_name=self.gateway_name,
            )

            self.strategy.update_trade(trade)
            self.trades[trade.vt_tradeid] = trade

    def load_bars(
        self,
        strategy: StrategyTemplate,
        days: int,
        interval: Interval
    ) -> None:
        """加载历史数据"""
        self.days = days

    def query_bar_from_datafeed(
        self,
        symbol: str,
        exchange: Any,
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> list[BarData]:
        """回测侧对齐实盘接口：无外部 datafeed，直接读 vnpy.duckdb。"""
        from vnpy.trader.constant import Exchange as ExchangeEnum

        ex: ExchangeEnum = (
            exchange if isinstance(exchange, ExchangeEnum) else ExchangeEnum(str(exchange))
        )
        return load_bar_data(f"{symbol}.{ex.value}", interval, start, end)

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
        mark: str = ""
    ) -> list[str]:
        """发送委托"""
        price = round_to(price, self.priceticks.get(vt_symbol, 0.01))
        symbol, exchange = extract_vt_symbol(vt_symbol)

        self.limit_order_count += 1

        reference: str = f"{APP_NAME}_{strategy.strategy_name}"

        order: OrderData = OrderData(
            symbol=symbol,
            exchange=exchange,
            orderid=str(self.limit_order_count),
            direction=direction,
            offset=offset,
            price=price,
            volume=volume,
            status=Status.SUBMITTING,
            datetime=self.datetime,
            gateway_name=self.gateway_name,
            reference=reference,
            mark=mark,
        )

        self.active_limit_orders[order.vt_orderid] = order
        self.limit_orders[order.vt_orderid] = order

        return [order.vt_orderid]

    def cancel_order(self, strategy: StrategyTemplate, vt_orderid: str) -> None:
        """委托撤单"""
        if vt_orderid not in self.active_limit_orders:
            return
        order: OrderData = self.active_limit_orders.pop(vt_orderid)

        order.status = Status.CANCELLED
        self.strategy.update_order(order)

    def write_log(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """输出日志"""
        msg = f"{self.datetime}\t{msg}"
        self.logs.append(msg)

    def send_notification(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """通过已配置渠道推送通知"""
        pass

    def send_wecom(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """回测时不发送企业微信消息"""
        pass

    def sync_strategy_data(self, strategy: StrategyTemplate) -> None:
        """保存策略数据到文件（回测不落盘）。"""
        pass

    def subscribe_symbols(
        self, strategy: StrategyTemplate, vt_symbols: list[str]
    ) -> None:
        """回测动态订阅：登记列表，并补载「当前回放日」行情。"""
        day: date | None = (
            self.datetime.date()
            if isinstance(self.datetime, datetime) and self.datetime.year > 1970
            else None
        )
        for vt_symbol in vt_symbols:
            if not vt_symbol:
                continue
            if vt_symbol not in strategy.vt_symbols:
                strategy.vt_symbols.append(vt_symbol)
            self._register_symbol(vt_symbol)
            if day is not None:
                self._load_symbol_day(vt_symbol, day)

    def unsubscribe_symbols(
        self, strategy: StrategyTemplate, vt_symbols: list[str]
    ) -> None:
        """回测动态退订：从策略 vt_symbols 移除（历史与引擎列表保留供持仓用）。"""
        for vt_symbol in vt_symbols:
            if vt_symbol in strategy.vt_symbols:
                strategy.vt_symbols.remove(vt_symbol)

    def init_t1_position(self, strategy: StrategyTemplate) -> None:
        """回测无经纪商持仓，T+1 同步空操作。"""
        return

    def get_engine_type(self) -> EngineType:
        """获取引擎类型"""
        return self.engine_type

    def get_pricetick(self, strategy: StrategyTemplate, vt_symbol: str) -> float:
        """获取合约价格跳动"""
        return float(self.priceticks.get(vt_symbol, 0.01))

    def get_size(self, strategy: StrategyTemplate, vt_symbol: str) -> float:
        """获取合约乘数"""
        return float(self.sizes.get(vt_symbol, 1.0))

    def put_strategy_event(self, strategy: StrategyTemplate) -> None:
        """推送事件更新策略界面"""
        pass

    def output(self, msg: str) -> None:
        """输出回测引擎信息"""
        print(f"{datetime.now()}\t{msg}")

    def get_all_trades(self) -> list[TradeData]:
        """获取所有成交信息"""
        return list(self.trades.values())

    def get_all_orders(self) -> list[OrderData]:
        """获取所有委托信息"""
        return list(self.limit_orders.values())

    def get_all_daily_results(self) -> list["PortfolioDailyResult"]:
        """获取所有每日盈亏信息"""
        return list(self.daily_results.values())


class ContractDailyResult:
    """合约每日盈亏结果"""

    def __init__(self, result_date: date, close_price: float) -> None:
        """构造函数"""
        self.date: date = result_date
        self.close_price: float = close_price
        self.pre_close: float = 0

        self.trades: list[TradeData] = []
        self.trade_count: int = 0

        self.start_pos: float = 0
        self.end_pos: float = 0

        self.turnover: float = 0
        self.commission: float = 0
        self.slippage: float = 0

        self.trading_pnl: float = 0
        self.holding_pnl: float = 0
        self.total_pnl: float = 0
        self.net_pnl: float = 0

    def add_trade(self, trade: TradeData) -> None:
        """添加成交信息"""
        self.trades.append(trade)

    def calculate_pnl(
        self,
        pre_close: float,
        start_pos: float,
        size: float,
        rate: float,
        slippage: float
    ) -> None:
        """计算盈亏"""
        # 记录昨收盘价
        self.pre_close = pre_close

        # 计算持仓盈亏
        self.start_pos = start_pos
        self.end_pos = start_pos

        self.holding_pnl = self.start_pos * (self.close_price - self.pre_close) * size

        # 计算交易盈亏
        self.trade_count = len(self.trades)

        for trade in self.trades:
            if trade.direction == Direction.LONG:
                pos_change = trade.volume
            else:
                pos_change = -trade.volume

            self.end_pos += pos_change

            turnover: float = trade.volume * size * trade.price

            self.trading_pnl += pos_change * (self.close_price - trade.price) * size
            self.slippage += trade.volume * size * slippage
            self.turnover += turnover
            self.commission += turnover * rate

        # 计算每日盈亏
        self.total_pnl = self.trading_pnl + self.holding_pnl
        self.net_pnl = self.total_pnl - self.commission - self.slippage

    def update_close_price(self, close_price: float) -> None:
        """更新每日收盘价"""
        self.close_price = close_price


class PortfolioDailyResult:
    """组合每日盈亏结果"""

    def __init__(self, result_date: date, close_prices: dict[str, float]) -> None:
        """"""
        self.date: date = result_date
        self.close_prices: dict[str, float] = close_prices
        self.pre_closes: dict[str, float] = {}
        self.start_poses: dict[str, float] = {}
        self.end_poses: dict[str, float] = {}

        self.contract_results: dict[str, ContractDailyResult] = {}

        for vt_symbol, close_price in close_prices.items():
            self.contract_results[vt_symbol] = ContractDailyResult(result_date, close_price)

        self.trade_count: int = 0
        self.turnover: float = 0
        self.commission: float = 0
        self.slippage: float = 0
        self.trading_pnl: float = 0
        self.holding_pnl: float = 0
        self.total_pnl: float = 0
        self.net_pnl: float = 0

    def add_trade(self, trade: TradeData) -> None:
        """添加成交信息"""
        contract_result: ContractDailyResult = self.contract_results[trade.vt_symbol]
        contract_result.add_trade(trade)

    def calculate_pnl(
        self,
        pre_closes: dict[str, float],
        start_poses: dict[str, float],
        sizes: dict[str, float],
        rates: dict[str, float],
        slippages: dict[str, float],
    ) -> None:
        """计算盈亏"""
        self.pre_closes = pre_closes
        self.start_poses = start_poses

        for vt_symbol, contract_result in self.contract_results.items():
            contract_result.calculate_pnl(
                pre_closes.get(vt_symbol, 0),
                start_poses.get(vt_symbol, 0),
                sizes[vt_symbol],
                rates[vt_symbol],
                slippages[vt_symbol]
            )

            self.trade_count += contract_result.trade_count
            self.turnover += contract_result.turnover
            self.commission += contract_result.commission
            self.slippage += contract_result.slippage
            self.trading_pnl += contract_result.trading_pnl
            self.holding_pnl += contract_result.holding_pnl
            self.total_pnl += contract_result.total_pnl
            self.net_pnl += contract_result.net_pnl

            self.end_poses[vt_symbol] = contract_result.end_pos

    def update_close_prices(self, close_prices: dict[str, float]) -> None:
        """更新每日收盘价"""
        self.close_prices.update(close_prices)

        for vt_symbol, close_price in close_prices.items():
            contract_result: ContractDailyResult | None = self.contract_results.get(vt_symbol, None)
            if contract_result:
                contract_result.update_close_price(close_price)
            else:
                self.contract_results[vt_symbol] = ContractDailyResult(self.date, close_price)


@lru_cache(maxsize=999)
def load_bar_data(
    vt_symbol: str,
    interval: Interval,
    start: datetime,
    end: datetime
) -> list[BarData]:
    """通过数据库获取历史数据"""
    symbol, exchange = extract_vt_symbol(vt_symbol)

    database: BaseDatabase = get_database()

    bars: list[BarData] = database.load_bar_data(
        symbol, exchange, interval, start, end
    )

    return bars


def evaluate(
    target_name: str,
    strategy_class: type[StrategyTemplate],
    vt_symbols: list[str],
    interval: Interval,
    start: datetime,
    rates: dict[str, float],
    slippages: dict[str, float],
    sizes: dict[str, float],
    priceticks: dict[str, float],
    capital: float,
    end: datetime,
    setting: dict
) -> tuple:
    """包装回测相关函数以供进程池内运行"""
    engine: BacktestingEngine = BacktestingEngine()

    engine.set_parameters(
        vt_symbols=vt_symbols,
        interval=interval,
        start=start,
        rates=rates,
        slippages=slippages,
        sizes=sizes,
        priceticks=priceticks,
        capital=capital,
        end=end,
    )

    engine.add_strategy(strategy_class, setting)
    engine.load_data()
    engine.run_backtesting()
    engine.calculate_result()
    statistics: dict = engine.calculate_statistics(output=False)

    target_value: float = statistics[target_name]
    return (str(setting), target_value, statistics)


def wrap_evaluate(engine: BacktestingEngine, target_name: str) -> Callable:
    """包装回测配置函数以供进程池内运行"""
    func: Callable = partial(
        evaluate,
        target_name,
        engine.strategy_class,
        engine.vt_symbols,
        engine.interval,
        engine.start,
        engine.rates,
        engine.slippages,
        engine.sizes,
        engine.priceticks,
        engine.capital,
        engine.end
    )
    return func


def get_target_value(result: list) -> float:
    """获取优化目标"""
    target_value: float = result[1]
    return target_value
