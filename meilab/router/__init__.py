"""router 子包:静态嗅探与分层路由。"""

from .layer import ChannelDecision, LayeredRouter, WorkerKind
from .sniff import (
    Channel,
    ContentType,
    SniffResult,
    detect_anti_bot_signals,
    refine_with_content_type,
    sniff_url,
)

__all__ = [
    "Channel",
    "ChannelDecision",
    "ContentType",
    "LayeredRouter",
    "SniffResult",
    "WorkerKind",
    "detect_anti_bot_signals",
    "refine_with_content_type",
    "sniff_url",
]
