"""router 子包:静态嗅探与分层路由。"""

from .layer import ChannelDecision, LayeredRouter, WorkerKind
from .sniff import (
    MEDIA_SUFFIXES,
    PLATFORM_HOSTS,
    Channel,
    ContentType,
    SniffResult,
    detect_anti_bot_signals,
    detect_media,
    refine_with_content_type,
    sniff_url,
)

__all__ = [
    "MEDIA_SUFFIXES",
    "PLATFORM_HOSTS",
    "Channel",
    "ChannelDecision",
    "ContentType",
    "LayeredRouter",
    "SniffResult",
    "WorkerKind",
    "detect_anti_bot_signals",
    "detect_media",
    "refine_with_content_type",
    "sniff_url",
]
