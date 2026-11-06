"""组合策略情绪上下文（阶段四）。

统一实盘与回测的情绪读取入口，策略源文件零修改：
- 实盘注入 ``LiveSentimentContext``（包 MarketSentimentEngine）
- 回测注入 ``ReplaySentimentContext``（包 ReplaySentimentReader，set_datetime 后按回测时钟取历史情绪）
"""
from .sentiment import (
    SentimentContext, LiveSentimentContext, ReplaySentimentContext,
)

__all__ = [
    "SentimentContext",
    "LiveSentimentContext",
    "ReplaySentimentContext",
]
