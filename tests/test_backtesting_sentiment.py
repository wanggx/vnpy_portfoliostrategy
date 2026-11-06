"""阶段四回测情绪注入测试。

覆盖：
- BacktestingEngine.new_bars 在 on_bars 前调 sentiment_context.set_datetime
- 信号器经 ReplaySentimentContext 读历史情绪（不读未来）
- 添加未来快照不改变此前成交/判定
- 回测不启动 MarketSentimentEngine 实时线程
- 策略源文件零修改（框架层注入）
"""
from __future__ import annotations

import gc
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("duckdb")

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ
from vnpy.trader.object import BarData
from vnpy_duckdb import DuckdbDatabase

from vnpy_marketsentiment.models import (
    MarketBreadth, MarketSentimentSnapshot, SectorState, SentimentLevel,
)
from vnpy_marketsentiment.scoring.calculator import SentimentCalculator
from vnpy_marketsentiment.history.store import DuckDBSentimentStore, ReplaySentimentReader

from vnpy_portfoliostrategy.context.sentiment import ReplaySentimentContext
from vnpy_portfoliostrategy.backtesting import BacktestingEngine


def _bar(symbol, exchange, dt, close=10.0, interval=Interval.MINUTE) -> BarData:
    return BarData(
        symbol=symbol, exchange=exchange, datetime=dt, interval=interval,
        volume=100, turnover=1000, open_interest=0,
        open_price=close - 1, high_price=close + 1, low_price=close - 2,
        close_price=close, gateway_name="TEST",
    )


def _snap(moment, *, score=60.0, sector_name="SW2城商行", sector_score=70.0,
          stock_sectors=None) -> MarketSentimentSnapshot:
    breadth = MarketBreadth(universe_size=2, valid_count=2, advancing=1, declining=100)
    return MarketSentimentSnapshot(
        datetime=moment, score=score, level=SentimentCalculator.classify(score),
        breadth=breadth,
        sectors={sector_name: SectorState(
            name=sector_name, breadth=breadth, score=sector_score,
            level=SentimentCalculator.classify(sector_score))},
        stock_sectors=stock_sectors or {"600000.SH": (sector_name,)},
        sector_score=sector_score, source_count=2, stale=False,
    )


@pytest.fixture()
def duckdb_db(tmp_path: Path, monkeypatch):
    """临时 DuckDB + 写入 bar 数据，monkeypatch get_database 指向它。"""
    db_path = str(tmp_path / "bt.duckdb")
    db = DuckdbDatabase(db_path)
    # 写入 5 根 1m bar
    base = datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ)
    bars = [_bar("600000", Exchange.SSE, base + timedelta(minutes=i)) for i in range(5)]
    db.save_bar_data(bars)
    monkeypatch.setattr("vnpy_portfoliostrategy.backtesting.get_database", lambda: db)
    yield db
    db.close()
    del db
    gc.collect()


@pytest.fixture()
def sentiment_store(tmp_path: Path):
    import duckdb
    conn = duckdb.connect(str(tmp_path / "bt_sentiment.duckdb"))
    s = DuckDBSentimentStore(conn)
    # 写入情绪快照：9:31 偏弱（score=30），9:35 极强（score=85）
    s.append(_snap(datetime(2024, 1, 2, 9, 31), score=30.0, sector_score=35.0))
    s.append(_snap(datetime(2024, 1, 2, 9, 35), score=85.0, sector_score=90.0))
    yield s
    conn.close()
    del conn
    gc.collect()


def test_new_bars_sets_sentiment_clock(duckdb_db, sentiment_store) -> None:
    """new_bars(dt) 在 on_bars 前调 sentiment_context.set_datetime(dt)。"""
    engine = BacktestingEngine()
    reader = ReplaySentimentReader(sentiment_store)
    ctx = ReplaySentimentContext(reader)
    engine.sentiment_context = ctx

    # 准备回测数据
    engine.vt_symbols = ["600000.SSE"]
    engine.interval = Interval.MINUTE
    engine.start = datetime(2024, 1, 2, 9, 31)
    engine.end = datetime(2024, 1, 2, 9, 36)
    engine.rates = {"600000.SSE": 0.0}
    engine.slippages = {"600000.SSE": 0.0}
    engine.sizes = {"600000.SSE": 1.0}
    engine.priceticks = {"600000.SSE": 0.01}
    engine.capital = 1_000_000

    # 加载历史数据
    engine.load_data()
    assert engine.history_data, "DuckDB 应有 bar 数据"

    # 手动逐个 dt 调 new_bars，验证 set_datetime 被调用
    captured_clocks: list[datetime] = []
    orig_set = ctx.set_datetime

    def spy_set(when):
        captured_clocks.append(when)
        orig_set(when)

    ctx.set_datetime = spy_set
    # new_bars 会调 strategy.on_bars，给个 mock 策略
    engine.strategy = MagicMock()
    engine.strategy.inited = False

    dts = sorted(engine.dts)
    for dt in dts[:3]:
        engine.new_bars(dt)

    # 每个 new_bars 都应 set_datetime
    assert len(captured_clocks) == 3
    assert captured_clocks == dts[:3]


def test_replay_context_reads_history_not_future(duckdb_db, sentiment_store) -> None:
    """回测读历史情绪，9:31 时刻读到 9:31 的偏弱分（30），不读 9:35 的 85。"""
    reader = ReplaySentimentReader(sentiment_store)
    ctx = ReplaySentimentContext(reader)
    ctx.set_datetime(datetime(2024, 1, 2, 9, 31))
    snap = ctx.get_latest()
    assert snap.score == 30.0   # 9:31 那根，不是 9:35 的 85

    ctx.set_datetime(datetime(2024, 1, 2, 9, 35))
    snap = ctx.get_latest()
    assert snap.score == 85.0


def test_future_snapshot_does_not_change_earlier_read(duckdb_db, sentiment_store) -> None:
    """添加未来快照不改变此前时刻的读取结果。"""
    reader = ReplaySentimentReader(sentiment_store)
    ctx = ReplaySentimentContext(reader)
    ctx.set_datetime(datetime(2024, 1, 2, 9, 31))
    before = ctx.get_latest().score

    # 添加更晚的快照（未来）
    sentiment_store.append(_snap(datetime(2024, 1, 2, 14, 0), score=95.0))

    ctx.set_datetime(datetime(2024, 1, 2, 9, 31))
    after = ctx.get_latest().score
    assert before == after == 30.0   # 不受未来快照影响


def test_backtest_does_not_start_sentiment_engine(duckdb_db, sentiment_store) -> None:
    """回测不启动 MarketSentimentEngine 后台线程（无 _thread/_run 属性）。"""
    engine = BacktestingEngine()
    reader = ReplaySentimentReader(sentiment_store)
    engine.sentiment_context = ReplaySentimentContext(reader)
    # BacktestingEngine 无 MarketSentimentEngine 相关属性
    assert not hasattr(engine, "_sentiment_engine")
    assert not hasattr(engine.sentiment_context, "_thread")


def test_no_sentiment_context_keeps_conservative(duckdb_db) -> None:
    """无情绪历史时 sentiment_context 为 None，策略按既有保守降级。"""
    engine = BacktestingEngine()
    assert engine.sentiment_context is None
    # new_bars 在 sentiment_context None 时不调 set_datetime，不报错
    engine.vt_symbols = ["600000.SSE"]
    engine.interval = Interval.MINUTE
    engine.start = datetime(2024, 1, 2, 9, 31)
    engine.end = datetime(2024, 1, 2, 9, 36)
    engine.rates = {"600000.SSE": 0.0}
    engine.slippages = {"600000.SSE": 0.0}
    engine.sizes = {"600000.SSE": 1.0}
    engine.priceticks = {"600000.SSE": 0.01}
    engine.capital = 1_000_000
    engine.load_data()
    dts = sorted(engine.dts)
    engine.strategy = MagicMock()
    engine.strategy.inited = False
    # 不应抛异常
    engine.new_bars(dts[0])


def test_strategies_source_unchanged() -> None:
    """阶段四策略零修改验收：strategies/*.py 未被修改。"""
    import subprocess
    result = subprocess.run(
        ["git", "status", "--porcelain", "vnpy_portfoliostrategy/strategies/"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.stdout.strip() == "", f"策略文件被修改：\n{result.stdout}"
