"""统一情绪上下文：实盘与回测共用一个读取接口，策略源文件零修改。

情绪模块里只有两个引擎：

- ``MarketSentimentEngine``（实盘）：有线程、连 QMT、算当前情绪；
- ``ReplaySentimentEngine``（回测）：无线程、按回放时钟查历史库。

两者都满足 ``vnpy_marketsentiment.context.SentimentEngine`` 契约
（``get_latest / get_at / get_sector_sentiment / get_stock_sector / set_datetime``），
所以这一层只是"类型说明 + 兼容包装"：

- ``SentimentContext``：兼容旧名，等价于 ``SentimentEngine``；
- ``LiveSentimentContext``：包实盘引擎，``set_datetime`` no-op；
- ``ReplaySentimentContext``：包回测引擎（推荐）或 ``ReplaySentimentReader``（兼容），
  ``set_datetime(dt)`` 后所有读取都基于同一回放时刻。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from vnpy_marketsentiment.context import SentimentEngine
from vnpy_marketsentiment.models import (
    MarketBreadth,
    MarketSentimentSnapshot,
    SectorState,
    SentimentLevel,
)

# 兼容旧名：本模块原来自带一份结构相同的 Protocol。
SentimentContext = SentimentEngine

__all__ = [
    "LiveSentimentContext",
    "ReplaySentimentContext",
    "SentimentContext",
    "SentimentEngine",
]


def _unavailable(when: datetime, error: str) -> MarketSentimentSnapshot:
    """构造不可用快照（与 marketsentiment 的降级语义一致）。"""
    return MarketSentimentSnapshot(
        datetime=when,
        score=0.0,
        level=SentimentLevel.UNAVAILABLE,
        breadth=MarketBreadth(),
        stale=False,
        error=error,
    )


class LiveSentimentContext:
    """实盘情绪上下文：包装当前 MarketSentimentEngine。

    ``get_latest`` / ``get_stock_sector`` / ``get_sector_sentiment`` 都转发给
    实时引擎；``set_datetime`` 在实盘无意义，no-op。
    """

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def get_latest(self) -> MarketSentimentSnapshot:
        if self.engine is None:
            return _unavailable(datetime.now().astimezone(), "无市场情绪引擎")
        return self.engine.get_latest()

    def get_at(self, when: datetime) -> MarketSentimentSnapshot:
        # 实盘只有一份当前缓存，忽略 when，等同 get_latest
        return self.get_latest()

    def get_sector_sentiment(self, sector_name: str) -> SectorState | None:
        if self.engine is None:
            return None
        direct = getattr(self.engine, "get_sector_sentiment", None)
        if callable(direct):
            return direct(sector_name)
        snapshot = self.engine.get_latest()
        return snapshot.sectors.get(sector_name) if snapshot else None

    def get_stock_sector(self, symbol: str) -> SectorState | None:
        if self.engine is None:
            return None
        return self.engine.get_stock_sector(symbol)

    def set_datetime(self, when: datetime) -> None:
        """实盘无需设置回测时钟。"""
        return


class ReplaySentimentContext:
    """回测情绪上下文：包回测引擎（推荐）或 reader（兼容）。

    ``set_datetime(dt)`` 后，``get_latest()`` 调 ``get_at(dt)``，
    ``get_sector_sentiment()`` / ``get_stock_sector()`` 都基于同一回测时刻。
    未设置时钟返回 UNAVAILABLE。不启动实时线程、不连 XtQuant。
    """

    def __init__(self, source: Any) -> None:
        """``source`` 可以是 ``ReplaySentimentEngine``（推荐）或
        ``ReplaySentimentReader``（兼容旧调用）。"""
        self.source = source
        self._clock: datetime | None = None
        # 引擎自带时钟：set_datetime 要同步给它，保证行业读取同一时刻
        self._engine: Any = (
            source
            if callable(getattr(source, "set_datetime", None))
            and callable(getattr(source, "get_latest", None))
            else None
        )
        self._reader: Any = None if self._engine is not None else source

    def set_datetime(self, when: datetime) -> None:
        """由回测引擎在每个 Bar 前调用。"""
        self._clock = when
        if self._engine is not None:
            self._engine.set_datetime(when)

    def get_latest(self) -> MarketSentimentSnapshot:
        if self._clock is None:
            return _unavailable(datetime.now().astimezone(), "回测情绪时钟尚未设置")
        return self.get_at(self._clock)

    def get_at(self, when: datetime) -> MarketSentimentSnapshot:
        if self.source is None:
            return _unavailable(when, "无回放情绪 reader")
        snap: MarketSentimentSnapshot | None = self.source.get_at(when)
        if snap is None:
            return _unavailable(when, "该时刻没有预计算的市场情绪")
        return snap

    def get_sector_sentiment(self, sector_name: str) -> SectorState | None:
        if self._clock is None:
            return None
        if self._engine is not None:
            return self._engine.get_sector_sentiment(sector_name)
        if self._reader is None:
            return None
        return self._reader.get_sector_sentiment(self._clock, sector_name)

    def get_stock_sector(self, symbol: str) -> SectorState | None:
        """按当前回测时钟取该标的所属行业情绪。"""
        if self._clock is None:
            return None
        if self._engine is not None:
            return self._engine.get_stock_sector(symbol)
        snap = self.get_at(self._clock)
        if not snap.available:
            return None
        return snap.get_stock_sector(symbol)