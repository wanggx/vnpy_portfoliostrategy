"""组合策略信号器子包：可被任意组合策略复用的只读信号组件。"""

from .sentiment_signals import (
    MarketRiskOffSignal,
    SectorBuyResult,
    SectorBuySignal,
    SentimentSignal,
)

__all__ = [
    "SentimentSignal",
    "SectorBuySignal",
    "SectorBuyResult",
    "MarketRiskOffSignal",
]
