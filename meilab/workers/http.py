"""HTTP 轻量通道 Worker。

优先使用 ``curl_cffi``(浏览器级 TLS/JA3 指纹),不可用时自动回退到标准库
``urllib.request``。遵守 robots.txt,全局限速 ≥1.5s + 随机抖动,失败自动重试。

安全要点:
* **SSRF 防护**:每个请求(含每一跳重定向)都要先过
  :func:`meilab.workers.base.check_url_safe` —— 协议白名单 + DNS 解析后 IP 段校验,
  环回/私网/链路本地/云元数据地址一律拒绝。重定向**手动**处理,不交给客户端自动跟随。
* **CA 证书路径**:Windows 版 libcurl 无法从含非 ASCII 字符的路径加载信任锚
  (报 ``curl: (77) error adding trust anchors``),因此启动时把 certifi 的 CA bundle
  复制到纯 ASCII 路径并通过 ``CURL_CA_BUNDLE`` 告知 libcurl。
"""

from __future__ import annotations

import os
import secrets
import shutil
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..core.types import Task
from ..router.sniff import detect_anti_bot_signals
from .base import BaseWorker, WorkerResult, check_url_safe

#: libcurl 读取 CA bundle 的环境变量名
CA_BUNDLE_ENV = "CURL_CA_BUNDLE"

#: 默认 UA(轻量通道冒充常见桌面浏览器)
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

#: 单次响应体最大保留字节数,避免把大文件塞进进程队列
MAX_BODY_BYTES = 4 * 1024 * 1024

#: 手动跟随重定向的最大跳数
MAX_REDIRECTS = 3

#: 需要手动处理的重定向状态码
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def ensure_ascii_ca_bundle() -> str | None:
    """确保 libcurl 能加载 CA 证书,返回可用的 CA 文件路径。

    * 若 certifi 的 CA 文件路径本身是纯 ASCII,直接使用;
    * 否则复制到系统临时目录下的纯 ASCII 路径再使用。

    :return: CA 文件路径;无法定位时返回 ``None``。
    """
    try:
        import certifi  # 第三方库:提供可信根证书包
    except Exception:
        return None

    src = certifi.where()
    if not os.path.exists(src):
        return None

    target = src
    try:
        is_ascii = src.encode("ascii", errors="ignore").decode("ascii") == src
    except Exception:
        is_ascii = True
    if not is_ascii:
        target = os.path.join(tempfile.gettempdir(), "melab_cacert.pem")
        try:
            if not os.path.exists(target) or os.path.getmtime(target) < os.path.getmtime(src):
                shutil.copyfile(src, target)
        except OSError:
            return None
    os.environ[CA_BUNDLE_ENV] = target
    return target


@dataclass
class FetchResult:
    """一次 HTTP 抓取的结果。"""

    url: str = ""
    final_url: str = ""
    status: int | None = None
    content_type: str = ""
    body: bytes = b""
    elapsed: float = 0.0
    error: str = ""
    signals: list[str] = field(default_factory=list)
    engine: str = "none"
    ok: bool = False

    @property
    def text(self) -> str:
        """按 UTF-8 宽容解码正文。"""
        return self.body.decode("utf-8", errors="replace")

    @property
    def blocked(self) -> bool:
        """是否被反爬拦截。"""
        return bool(self.signals)


class RobotsChecker:
    """按域名缓存 robots.txt 判定结果。"""

    def __init__(self, user_agent: str = DEFAULT_UA) -> None:
        """初始化检查器。

        :param user_agent: 用于声明身份并被 robots.txt 匹配的 UA。
        """
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        """判断某 URL 是否被 robots.txt 允许。

        robots.txt 取不到或解析失败时按"允许"处理(与主流爬虫一致)。

        :param url: 待判定 URL。
        :return: 是否允许抓取。
        """
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return False
        host_key = f"{parts.scheme}://{parts.netloc}"
        if host_key not in self._cache:
            self._cache[host_key] = self._load(host_key)
        parser = self._cache[host_key]
        if parser is None:
            return True
        try:
            return bool(parser.can_fetch(self.user_agent, url))
        except Exception:
            return True

    def _load(self, base: str) -> urllib.robotparser.RobotFileParser | None:
        """下载并解析某个站点的 robots.txt,失败返回 ``None``。"""
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{base}/robots.txt")
        try:
            parser.read()
        except Exception:
            return None
        return parser


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止 urllib 自动跟随重定向(改为手动逐跳校验)。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        """返回 ``None`` 让 urllib 抛出 HTTPError,由调用方处理跳转。"""
        return None


class HttpWorker(BaseWorker):
    """轻量 HTTP Worker:urllib / curl_cffi 抓取 + 限速 + 重试 + robots 遵守。"""

    channel_name = "http"

    def __init__(
        self,
        inbox: Any,
        outbox: Any,
        *,
        delay: float = 1.5,
        jitter: float = 0.5,
        timeout: float = 20.0,
        retries: int = 2,
        user_agent: str = DEFAULT_UA,
        obey_robots: bool = True,
        use_curl: bool = True,
        allow_private: bool = False,
    ) -> None:
        """初始化 HTTP Worker。

        :param inbox: 任务队列。
        :param outbox: 结果队列。
        :param delay: 两次请求之间的最小间隔(秒),不得低于 1.5。
        :param jitter: 随机抖动上界(秒)。
        :param timeout: 单次请求超时(秒)。
        :param retries: 网络失败时的重试次数。
        :param user_agent: 请求 UA。
        :param obey_robots: 是否遵守 robots.txt(默认遵守)。
        :param use_curl: 是否优先使用 curl_cffi。
        :param allow_private: 是否允许访问内网地址(默认禁止,SSRF 防护)。
        """
        super().__init__(inbox, outbox)
        self.delay = max(1.5, float(delay))
        self.jitter = max(0.0, float(jitter))
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self.obey_robots = obey_robots
        self.use_curl = use_curl
        self.allow_private = allow_private
        self.robots = RobotsChecker(user_agent)
        self._last_request = 0.0
        self._curl = None
        self._curl_failed = False
        self._opener = None

    def _get_opener(self) -> Any:
        """惰性构造 urllib opener(内含 lambda,不可 pickle,故不入 __init__)。"""
        if self._opener is None:
            self._opener = urllib.request.build_opener(_NoRedirectHandler())
        return self._opener

    # ------------------------------------------------------------------ 处理

    def handle(self, task: Task) -> dict[str, Any]:
        """抓取一个任务并返回结果字典。

        :param task: 待抓取任务。
        :return: 含 ``status`` / ``content_type`` / ``body`` / ``signals`` 等键的字典。
        """
        result = self.fetch(task.url)
        expect = task.payload.get("expect_content_type")
        if expect and result.content_type and expect.lower() not in result.content_type.lower():
            result.error = f"内容类型不符合预期: 期望含 {expect!r},实际 {result.content_type!r}"
            result.ok = False
        return {
            "url": result.url,
            "final_url": result.final_url,
            "status": result.status,
            "content_type": result.content_type,
            "body": result.body,
            "elapsed": result.elapsed,
            "error": result.error,
            "signals": result.signals,
            "engine": result.engine,
            "ok": result.ok,
        }

    def fetch(self, url: str) -> FetchResult:
        """抓取一个 URL(限速 + 重试 + robots + SSRF 校验)。

        :param url: 目标 URL。
        :return: 抓取结果。
        """
        result = FetchResult(url=url, final_url=url)
        if self.obey_robots and not self.robots.allowed(url):
            result.error = "robots.txt 不允许抓取该 URL"
            return result

        safe, why = check_url_safe(url, allow_private=self.allow_private)
        if not safe:
            result.error = f"SSRF 防护拒绝该 URL: {why}"
            return result

        started = time.time()
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                self._fetch_follow(url, result)
            except Exception as exc:
                result.error = f"{exc!r}"
                result.ok = False
                result.engine = "none"
                if attempt < self.retries:
                    time.sleep(min(10.0, self.delay * (2**attempt)))
                    continue
                break

            if result.status is not None and result.status >= 500 and attempt < self.retries:
                time.sleep(min(10.0, self.delay * (2**attempt)))
                continue
            break

        result.elapsed = time.time() - started
        if result.status is not None:
            result.signals = detect_anti_bot_signals(result.text if result.ok else "", result.status)
        return result

    # ------------------------------------------------------------------ 内部

    def _throttle(self) -> None:
        """全局限速:相邻两次请求间隔 ≥ delay,再加密码学安全随机抖动。"""
        wait = self.delay + secrets.SystemRandom().uniform(0.0, self.jitter)
        elapsed = time.time() - self._last_request
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request = time.time()

    def _fetch_follow(self, url: str, result: FetchResult) -> None:
        """逐跳跟随重定向(每一跳都做 SSRF 校验)。"""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            safe, why = check_url_safe(current, allow_private=self.allow_private)
            if not safe:
                result.ok = False
                result.error = f"SSRF 防护拒绝该重定向目标: {why}"
                return
            status, headers, body, engine = self._request(current)
            result.engine = engine
            if status in _REDIRECT_CODES and headers.get("location"):
                current = urljoin(current, headers["location"])
                continue
            result.status = status
            result.final_url = current
            result.content_type = headers.get("content-type", "")
            result.body = body[:MAX_BODY_BYTES]
            result.ok = 200 <= status < 400
            if not result.ok and not result.error:
                result.error = f"HTTP {status}"
            return
        result.ok = False
        result.error = f"重定向超过 {MAX_REDIRECTS} 跳"

    def _request(self, url: str) -> tuple[int, dict[str, str], bytes, str]:
        """发一次请求(不跟随重定向),返回 ``(状态码, 头, 正文, 引擎名)``。"""
        if self.use_curl and not self._curl_failed:
            try:
                return self._request_curl(url)
            except ImportError:
                self._curl_failed = True
        return self._request_urllib(url)

    def _request_curl(self, url: str) -> tuple[int, dict[str, str], bytes, str]:
        """用 curl_cffi 发一次请求(带浏览器 TLS 指纹)。"""
        if self._curl is None:
            from curl_cffi import requests as curl_requests  # 第三方库:TLS 指纹模拟

            ensure_ascii_ca_bundle()
            self._curl = curl_requests
        response = self._curl.get(
            url,
            timeout=self.timeout,
            impersonate="chrome",
            allow_redirects=False,
            headers={"User-Agent": self.user_agent, "Accept-Language": "en-US,en;q=0.9"},
        )
        headers = {str(k).lower(): str(v) for k, v in dict(response.headers).items()}
        return int(response.status_code), headers, bytes(response.content or b""), "curl_cffi"

    def _request_urllib(self, url: str) -> tuple[int, dict[str, str], bytes, str]:
        """用标准库 urllib 发一次请求(curl_cffi 不可用时的回退)。"""
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        ca_path = ensure_ascii_ca_bundle()
        context = ssl.create_default_context(cafile=ca_path) if ca_path else None
        try:
            with self._get_opener().open(request, timeout=self.timeout) as resp:
                headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                return int(resp.status), headers, resp.read(MAX_BODY_BYTES), "urllib"
        except urllib.error.HTTPError as exc:
            headers = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
            try:
                body = exc.read(MAX_BODY_BYTES)
            except Exception:
                body = b""
            return int(exc.code), headers, body, "urllib"
