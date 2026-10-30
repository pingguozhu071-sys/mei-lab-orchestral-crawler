"""workers 子包:Worker 抽象基类、SSRF 校验与两类通道实现。"""

from .base import (
    ALLOWED_SCHEMES,
    STOP,
    BaseWorker,
    WorkerResult,
    check_url_safe,
    is_public_ip,
    resolve_host_ips,
)
from .http import DEFAULT_UA, FetchResult, HttpWorker, RobotsChecker, ensure_ascii_ca_bundle

__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_UA",
    "STOP",
    "BaseWorker",
    "FetchResult",
    "HttpWorker",
    "RobotsChecker",
    "WorkerResult",
    "check_url_safe",
    "ensure_ascii_ca_bundle",
    "is_public_ip",
    "resolve_host_ips",
]
