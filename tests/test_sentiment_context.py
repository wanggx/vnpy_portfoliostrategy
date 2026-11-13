"""阶段四 SentimentContext 测试。

覆盖 LiveSentimentContext / ReplaySentimentContext、get_latest/get_at/
get_sector_sentiment/get_stock_sector/set_datetime、未设时钟不可用、
回测不启后台线程、策略零修改（信号器经上下文读取）。
"""
from __future__ import annotations

import gc
from datetime import datetime, date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("duckdb")

from vnpy_marketsentiment.models import (
    MarketBreadth, MarketSentimentSnapshot, SectorState, SentimentLevel,
)
from vnpy_marketsentiment.scoring.calculator import SentimentCalculator
from vnpy_marketsentiment.history.store import DuckDBSentimentStore, ReplaySentimentReader

from vnpy_portfoliostrategy.context.sentiment import (
    SentimentContext, LiveSentimentContext, ReplaySentimentContext,
)


def _snapshot(moment, *, score=60.0, sector_name="SW2城商行", sector_score=70.0,
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
def store(tmp_path: Path):
    import duckdb
    conn = duckdb.connect(str(tmp_path / "ctx.duckdb"))
    s = DuckDBSentimentStore(conn)
    yield s
    conn.close()
    del conn
    gc.collect()


# -- LiveSentimentContext ----------------------------------------------

def test_live_context_wraps_engine() -> None:
    """LiveSentimentContext 转发给实时引擎。"""
    engine = MagicMock()
    snap = _snapshot(datetime(2024, 1, 2, 9, 31), score=55.0)
    engine.get_latest.return_value = snap
    engine.get_stock_sector.return_value = snap.sectors["SW2城商行"]

    ctx = LiveSentimentContext(engine)
    assert ctx.get_latest() is snap
    assert ctx.get_stock_sector("600000.SH") is snap.sectors["SW2城商行"]
    # set_datetime 在实盘 no-op，不报错
    ctx.set_datetime(datetime(2024, 1, 2, 10, 0))
    engine.set_datetime.assert_not_called()


def test_live_context_no_engine_returns_unavailable() -> None:
    """无引擎时返回 UNAVAILABLE。"""
    ctx = LiveSentimentContext(None)
    snap = ctx.get_latest()
    assert snap.level == SentimentLevel.UNAVAILABLE
    assert ctx.get_stock_sector("600000.SH") is None


# -- ReplaySentimentContext -------------------------------------------

def test_replay_context_no_clock_returns_unavailable(store) -> None:
    """未设时钟时 get_latest 返回不可用。"""
    reader = ReplaySentimentReader(store)
    ctx = ReplaySentimentContext(reader)
    snap = ctx.get_latest()
    assert snap.level == SentimentLevel.UNAVAILABLE


def test_replay_context_get_latest_follows_clock(store) -> None:
    """set_datetime 后 get_latest 按回测时钟取。"""
    store.append(_snapshot(datetime(2024, 1, 2, 9, 31), score=55.0))
    store.append(_snapshot(datetime(2024, 1, 2, 14, 0), score=82.0))
    reader = ReplaySentimentReader(store)
    ctx = ReplaySentimentContext(reader)

    ctx.set_datetime(datetime(2024, 1, 2, 10, 0))
    assert ctx.get_latest().score == 55.0

    ctx.set_datetime(datetime(2024, 1, 2, 14, 30))
    assert ctx.get_latest().score == 82.0


def test_replay_context_does_not_read_future(store) -> None:
    """回测上下文不读未来快照。"""
    store.append(_snapshot(datetime(2024, 1, 2, 9, 31), score=50.0))
    store.append(_snapshot(datetime(2024, 1, 2, 14, 0), score=80.0))
    reader = ReplaySentimentReader(store)
    ctx = ReplaySentimentContext(reader)
    ctx.set_datetime(datetime(2024, 1, 2, 10, 0))
    # 10:00 不应读到 14:00 的 80.0
    assert ctx.get_latest().score == 50.0


def test_replay_context_get_stock_sector_by_clock(store) -> None:
    """get_stock_sector 按回测时钟取历史行业。"""
    store.append(_snapshot(
        datetime(2024, 1, 2, 9, 31), sector_name="SW2城商行", sector_score=72.0,
        stock_sectors={"600000.SH": ("SW2城商行",)},
    ))
    reader = ReplaySentimentReader(store)
    ctx = ReplaySentimentContext(reader)
    ctx.set_datetime(datetime(2024, 1, 2, 10, 0))
    sec = ctx.get_stock_sector("600000.SH")
    assert sec is not None
    assert sec.score == 72.0


def test_replay_context_no_clock_get_stock_sector_none(store) -> None:
    """未设时钟 get_stock_sector 返回 None。"""
    reader = ReplaySentimentReader(store)
    ctx = ReplaySentimentContext(reader)
    assert ctx.get_stock_sector("600000.SH") is None


def test_replay_context_no_thread_no_provider(store) -> None:
    """回放上下文不启后台线程、无 provider。"""
    reader = ReplaySentimentReader(store)
    ctx = ReplaySentimentContext(reader)
    assert not hasattr(ctx, "_thread")
    assert not hasattr(ctx, "_provider")


def test_sentiment_context_protocol_satisfied(store) -> None:
    """两个实现都满足 SentimentContext Protocol 接口。"""
    reader = ReplaySentimentReader(store)
    replay = ReplaySentimentContext(reader)
    live = LiveSentimentContext(MagicMock())
    for ctx in (replay, live):
        assert hasattr(ctx, "get_latest")
        assert hasattr(ctx, "get_at")
        assert hasattr(ctx, "get_sector_sentiment")
        assert hasattr(ctx, "get_stock_sector")
        assert hasattr(ctx, "set_datetime")


def test_live_context_uses_engine_sector_api() -> None:
    """LiveSentimentContext 优先走引擎新增的 get_sector_sentiment(name)。"""
    engine = MagicMock()
    sector = _snapshot(datetime(2024, 1, 2, 9, 31)).sectors["SW2城商行"]
    engine.get_sector_sentiment.return_value = sector

    ctx = LiveSentimentContext(engine)

    assert ctx.get_sector_sentiment("SW2城商行") is sector
    engine.get_sector_sentiment.assert_called_once_with("SW2城商行")
    engine.get_latest.assert_not_called()


def test_replay_context_accepts_engine_source(store) -> None:
    """ReplaySentimentContext 可直接包 ReplaySentimentEngine（推荐用法）。"""
    from vnpy_marketsentiment import ReplaySentimentEngine

    store.append(_snapshot(
        datetime(2024, 1, 2, 9, 31), score=55.0, sector_score=72.0,
        stock_sectors={"600000.SH": ("SW2城商行",)},
    ))
    store.append(_snapshot(
        datetime(2024, 1, 2, 14, 0), score=82.0, sector_score=88.0,
        stock_sectors={"600000.SH": ("SW2城商行",)},
    ))
    engine = ReplaySentimentEngine.from_store(ReplaySentimentReader(store))
    ctx = ReplaySentimentContext(engine)

    assert ctx.get_latest().level == SentimentLevel.UNAVAILABLE  # 未拨钟

    ctx.set_datetime(datetime(2024, 1, 2, 10, 0))
    # 时钟同时拨给了引擎本身，两条读取路径一致
    assert ctx.get_latest().score == 55.0
    assert engine.get_latest().score == 55.0
    assert ctx.get_sector_sentiment("SW2城商行").score == 72.0
    assert ctx.get_stock_sector("600000.SH").score == 72.0

    ctx.set_datetime(datetime(2024, 1, 2, 14, 30))
    assert ctx.get_latest().score == 82.0
    assert ctx.get_sector_sentiment("SW2城商行").score == 88.0


def test_replay_context_engine_sector_missing_returns_none(store) -> None:
    """行业不存在时返回 None，不抛异常。"""
    from vnpy_marketsentiment import ReplaySentimentEngine

    store.append(_snapshot(datetime(2024, 1, 2, 9, 31)))
    engine = ReplaySentimentEngine.from_store(ReplaySentimentReader(store))
    ctx = ReplaySentimentContext(engine)
    ctx.set_datetime(datetime(2024, 1, 2, 9, 45))
    assert ctx.get_sector_sentiment("不存在") is None
