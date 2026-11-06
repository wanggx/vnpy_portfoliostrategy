"""统一情绪上下文：实盘与回测共用一个读取接口，策略源文件零修改。

对照 quant-platform-agent-implementation.md 6.2 节：
- ``SentimentContext`` Protocol：get_latest / get_at / get_sector_sentiment /
  get_stock_sector / set_datetime。
- ``LiveSentimentContext``：包装当前 ``MarketSentimentEngine``，``get_latest()``
  返回实盘当前快照；``set_datetime`` 在实盘无意义（no-op）。
- ``ReplaySentimentContext``：同时实现 SentimentReader 与 ReplayClock；
  ``set_datetime(dt)`` 后，``get_latest()`` / ``get_sector_sentiment()`` /
  ``get_stock_sector()`` 都基于 ``get_at(dt)`` 返回，保证总市场情绪、行业情绪、
  股票所属行业情绪处于同一个回测时刻。不启动 MarketSentimentEngine 后台线程。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from vnpy_marketsentiment.models import (
    MarketSentimentSnapshot, SectorState, SentimentLevel, MarketBreadth,
)


class SentimentContext(Protocol):
    """策略读取情绪的统一接口（实盘/回测共用）。"""

    def get_latest(self) -> MarketSentimentSnapshot:
        """实盘取当前快照；回测取当前回测时刻对应的快照。"""
        ...

    def get_at(self, when: datetime) -> MarketSentimentSnapshot:
        """取指定时刻的快照（主要供回测引擎和测试使用）。"""
        ...

    def get_sector_sentiment(self, sector_name: str) -> SectorState | None:
        """返回当前上下文时刻某行业的情绪状态。"""
        ...

    def get_stock_sector(self, symbol: str) -> SectorState | None:
        """返回当前上下文时刻某标的所属行业的情绪状态。"""
        ...

    def set_datetime(self, when: datetime) -> None:
        """由回测引擎在每个 Bar 前设置；实盘实现 no-op。"""
        ...


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
        snap = self.engine.get_latest()
        return snap.sectors.get(sector_name) if snap else None

    def get_stock_sector(self, symbol: str) -> SectorState | None:
        if self.engine is None:
            return None
        return self.engine.get_stock_sector(symbol)

    def set_datetime(self, when: datetime) -> None:
        """实盘无需设置回测时钟。"""
        return


class ReplaySentimentContext:
    """回测情绪上下文：包 ReplaySentimentReader，按回测时钟取历史情绪。

    ``set_datetime(dt)`` 后，``get_latest()`` 调 ``get_at(dt)``，
    ``get_sector_sentiment()`` / ``get_stock_sector()`` 都基于同一回测时刻。
    未设置时钟返回 UNAVAILABLE。不启动实时线程、不连 XtQuant。
    """

    def __init__(self, reader: Any) -> None:
        """reader 为 ReplaySentimentReader（或任意实现 get_at/get_sector_sentiment/
        get_stock_sector_mapping 的对象）。"""
        self.reader = reader
        self._clock: datetime | None = None

    def set_datetime(self, when: datetime) -> None:
        """由回测引擎在每个 Bar 前调用。"""
        self._clock = when

    def get_latest(self) -> MarketSentimentSnapshot:
        if self._clock is None:
            return _unavailable(datetime.now().astimezone(), "回测情绪时钟尚未设置")
        return self.get_at(self._clock)

    def get_at(self, when: datetime) -> MarketSentimentSnapshot:
        if self.reader is None:
            return _unavailable(when, "无回放情绪 reader")
        snap = self.reader.get_at(when)
        if snap is None:
            return _unavailable(when, "该时刻没有预计算的市场情绪")
        return snap

    def get_sector_sentiment(self, sector_name: str) -> SectorState | None:
        if self._clock is None or self.reader is None:
            return None
        return self.reader.get_sector_sentiment(self._clock, sector_name)

    def get_stock_sector(self, symbol: str) -> SectorState | None:
        """按当前回测时钟取该标的所属行业情绪。"""
        if self._clock is None:
            return None
        snap = self.get_at(self._clock)
        if not snap.available:
            return None
        return snap.get_stock_sector(symbol)
