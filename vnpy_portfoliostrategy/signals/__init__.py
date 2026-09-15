"""组合策略信号器子包：可被任意组合策略复用的信号组件。

统一两层信号架构：
- ``base``：``SignalType`` / ``SignalResult`` / ``SubSignal`` / ``SignalAggregator``
- ``buy_signals``：``WindowSurgeSubSignal`` / ``DayGainSubSignal`` / ``SurgeBuyOrSignal`` /
  ``SectorBuySubSignal`` / ``BuyAggregator``（拉升检测 OR + 行业情绪过滤）
- ``sell_signals``：``SectorSellSubSignal`` / ``PriceSellSubSignal`` /
  ``PriorityCompositeSubSignal`` / ``SellSubSignal`` / ``SellAggregator``
  （情绪卖出优先 + 价格卖出，优先级短路）
- ``sentiment_signals``：``SectorBuySignal`` / ``SectorSellSignal`` / ``MarketRiskOffSignal``
  （行业可买 / 行业应卖 / 市场情绪共享依赖）
"""

from .base import (
    SignalAggregator,
    SignalResult,
    SignalType,
    SubSignal,
)
from .buy_signals import (
    SectorBuySubSignal,
    BuyAggregator,
    DayGainSubSignal,
    OrCompositeSubSignal,
    SurgeBuyOrSignal,
    WindowSurgeSubSignal,
)
from .sell_signals import (
    PriceSellSubSignal,
    PriorityCompositeSubSignal,
    SectorSellSubSignal,
    SellAggregator,
    SellSubSignal,
)
from .sentiment_signals import (
    MarketRiskOffSignal,
    SectorBuyResult,
    SectorBuySignal,
    SectorSellResult,
    SectorSellSignal,
    SentimentSignal,
)

__all__ = [
    # base
    "SignalType",
    "SignalResult",
    "SubSignal",
    "SignalAggregator",
    # buy
    "WindowSurgeSubSignal",
    "DayGainSubSignal",
    "OrCompositeSubSignal",
    "SurgeBuyOrSignal",
    "SectorBuySubSignal",
    "BuyAggregator",
    # sell
    "SectorSellSubSignal",
    "PriceSellSubSignal",
    "PriorityCompositeSubSignal",
    "SellSubSignal",
    "SellAggregator",
    # sentiment
    "SentimentSignal",
    "SectorBuySignal",
    "SectorBuyResult",
    "SectorSellSignal",
    "SectorSellResult",
    "MarketRiskOffSignal",
]
