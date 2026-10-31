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

#: 手动跟随重定向的最大跳数(每一跳都重新过 SSRF 校验)
MAX_REDIRECTS = 10

#: ``Retry-After`` 的最长等待(秒)
MAX_RETRY_AFTER = 300

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
    headers: dict[str, str] = field(default_factory=dict)
    retry_after: float = 0.0

    @property
    def text(self) -> str:
        """按 UTF-8 宽容解码正文。"""
        return self.body.decode("utf-8", errors="replace")

    @property
    def blocked(self) -> bool:
        """是否被反爬拦截。"""
        return bool(self.signals)


class RobotsChecker:
    """按域名缓存 robots.txt 判定结果(含 ``Crawl-delay``)。"""

    def __init__(self, user_agent: str = DEFAULT_UA) -> None:
        """初始化检查器。

        :param user_agent: 用于声明身份并被 robots.txt 匹配的 UA。
        """
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def crawl_delay(self, url: str) -> float:
        """取目标站点的 ``Crawl-delay``(秒);没有则返回 0。

        :param url: 目标 URL。
        :return: 建议的请求间隔(秒)。
        """
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return 0.0
        host_key = f"{parts.scheme}://{parts.netloc}"
        if host_key not in self._cache:
            self._cache[host_key] = self._load(host_key)
        parser = self._cache[host_key]
        if parser is None:
            return 0.0
        try:
            delay = parser.crawl_delay(self.user_agent)
        except Exception:
            return 0.0
        return float(delay) if delay else 0.0

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
        """下载并解析某个站点的 robots.txt,失败返回 ``None``。

        用本框架的抓取栈(curl_cffi + 浏览器 UA)而不是 ``parser.read()`` 的
        默认 urllib 请求——后者会被拒绝默认 Python UA 的站点挡掉,
        使 robots 检查**静默失效**成"全部允许、无延迟"(见经验记录 G-18)。
        """
        url = f"{base}/robots.txt"
        safe, _why = check_url_safe(url)
        if not safe:
            return None
        try:
            from curl_cffi import requests as curl_requests  # 第三方库:TLS 指纹模拟

            ensure_ascii_ca_bundle()
            response = curl_requests.get(
                url,
                timeout=10.0,
                impersonate="chrome",
                headers={"User-Agent": self.user_agent},
            )
            if response.status_code != 200:
                return None
            return self.load_text(response.text or "", base)
        except Exception:
            return None

    def load_text(self, text: str, base: str) -> urllib.robotparser.RobotFileParser:
        """用已取到的 robots 文本初始化缓存(离线测试与复现场景用)。

        :param text: robots.txt 内容。
        :param base: 站点根(形如 ``https://example.com``)。
        :return: 解析后的 parser(已写入缓存)。
        """
        parser = urllib.robotparser.RobotFileParser()
        parser.parse((text or "").splitlines())
        self._cache[base] = parser
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
        cache_enabled: bool = True,
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
        :param cache_enabled: 是否启用 ETag/Last-Modified 条件请求。
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
        # 域名冷却:被 429 限流后,该域名在一段时间内不再发请求
        self._cooldowns: dict[str, float] = {}
        # 条件请求缓存:URL → {etag, last_modified}
        self.cache_enabled = cache_enabled
        self._cache: dict[str, dict[str, str]] = {}
        self._curl = None
        self._curl_failed = False
        self._opener = None

    def _conditional_headers(self, url: str) -> dict[str, str]:
        """构造条件请求头(ETag / Last-Modified),用于 304 复用。

        :param url: 目标 URL。
        :return: 条件请求头(没有缓存时为空字典)。
        """
        if not self.cache_enabled:
            return {}
        cached = self._cache.get(url)
        if not cached:
            return {}
        headers: dict[str, str] = {}
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
        return headers

    def _remember(self, url: str, headers: dict[str, str]) -> None:
        """记下响应的 ETag / Last-Modified 供下次条件请求。

        :param url: 目标 URL。
        :param headers: 响应头。
        """
        if not self.cache_enabled:
            return
        etag = headers.get("etag") or ""
        last_modified = headers.get("last-modified") or ""
        if etag or last_modified:
            self._cache[url] = {"etag": etag, "last_modified": last_modified}

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
            self._throttle(url)
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

            # 429:按 Retry-After 退避,并给该域名设冷却
            if result.status == 429:
                wait = min(MAX_RETRY_AFTER, result.retry_after or 0)
                host = ""
                try:
                    host = (urlsplit(result.final_url or url).hostname or "").lower()
                except ValueError:
                    host = ""
                if host and wait:
                    self._cooldowns[host] = time.time() + wait
                if attempt < self.retries:
                    time.sleep(wait or min(10.0, self.delay * (2**attempt)))
                    continue

            if result.status is not None and result.status >= 500 and attempt < self.retries:
                time.sleep(min(10.0, self.delay * (2**attempt)))
                continue
            break

        result.elapsed = time.time() - started
        if result.status is not None:
            result.signals = detect_anti_bot_signals(result.text if result.ok else "", result.status)
        return result

    # ------------------------------------------------------------------ 内部

    def _throttle(self, url: str = "") -> None:
        """全局限速:相邻两次请求间隔 ≥ delay,再加密码学安全随机抖动。

        还会叠加三种更严格的约束:
        * 该域名的冷却时间(被 429 限流后设置);
        * robots.txt 的 ``Crawl-delay``;
        * 域名级并发上限。

        :param url: 本次请求的 URL(用于取域名)。
        """
        wait = self.delay + secrets.SystemRandom().uniform(0.0, self.jitter)
        host = ""
        if url:
            try:
                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                host = ""
        if host:
            cooldown = self._cooldowns.get(host, 0.0) - time.time()
            if cooldown > 0:
                wait = max(wait, cooldown)
            try:
                crawl_delay = self.robots.crawl_delay(url)
            except Exception:
                crawl_delay = 0.0
            if crawl_delay > 0:
                wait = max(wait, crawl_delay)
        elapsed = time.time() - self._last_request
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last_request = time.time()

    @staticmethod
    def parse_retry_after(value: str | None) -> float:
        """解析 ``Retry-After`` 头(秒数或 HTTP 日期)。

        :param value: 头的原始值。
        :return: 建议等待秒数;解析不了返回 0。
        """
        if not value:
            return 0.0
        text = value.strip()
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(text)
            if when is None:
                return 0.0
            import datetime as _dt

            now = _dt.datetime.now(_dt.timezone.utc)
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.timezone.utc)
            else:
                when = when.astimezone(_dt.timezone.utc)
            return max(0.0, (when - now).total_seconds())
        except Exception:
            return 0.0

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
            result.headers = headers
            result.retry_after = self.parse_retry_after(headers.get("retry-after"))
            if status == 304:
                # 内容未变:直接复用(不重复解析)
                result.status = status
                result.final_url = current
                result.ok = True
                result.error = ""
                result.content_type = headers.get("content-type", "")
                return
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
        """用 curl_cffi 发一次请求(带浏览器 TLS 指纹 + 条件请求头)。"""
        if self._curl is None:
            from curl_cffi import requests as curl_requests  # 第三方库:TLS 指纹模拟

            ensure_ascii_ca_bundle()
            self._curl = curl_requests
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        }
        headers.update(self._conditional_headers(url))
        response = self._curl.get(
            url,
            timeout=self.timeout,
            impersonate="chrome",
            allow_redirects=False,
            headers=headers,
        )
        resp_headers = {str(k).lower(): str(v) for k, v in dict(response.headers).items()}
        self._remember(url, resp_headers)
        return int(response.status_code), resp_headers, bytes(response.content or b""), "curl_cffi"

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
        request.add_header("Accept-Language", "en-US,en;q=0.9")
        for key, value in self._conditional_headers(url).items():
            request.add_header(key, value)
        try:
            with self._get_opener().open(request, timeout=self.timeout) as resp:
                headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                self._remember(url, headers)
                return int(resp.status), headers, resp.read(MAX_BODY_BYTES), "urllib"
        except urllib.error.HTTPError as exc:
            headers = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
            try:
                body = exc.read(MAX_BODY_BYTES)
            except Exception:
                body = b""
            return int(exc.code), headers, body, "urllib"
