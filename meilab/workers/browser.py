"""浏览器重量通道 Worker。

**每个 BrowserWorker 进程拥有自己完全独立的 Chromium 实例**,不与任何其他 Worker
共享浏览器、Profile 或 Context——这是本批最重要的约束:多进程共享一个浏览器会在
指纹层面暴露"同源并发",是 bot 检测的高危信号。

每个任务 → 新建 BrowserContext(注入设备档案指纹);**同域连续任务**可复用同一个
Context(可配置),**跨域必销毁**,避免 cookie/指纹串站。

引擎优先用 ``patchright``(Playwright API 兼容的隐身版),缺失时回退 ``playwright``。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

from ..core.types import DeviceProfile, Task
from ..router.sniff import detect_anti_bot_signals
from ..stealth.inject import apply_profile, harden_page
from ..stealth.netguard import NetGuard
from .base import BaseWorker, check_url_safe

#: 没有设备档案库时使用的保守默认档案(Windows 桌面 Chrome)
DEFAULT_DEVICE: dict[str, Any] = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "viewport": {"width": 1920, "height": 1080},
    "timezone_id": "America/New_York",
    "locale": "en-US",
    "has_touch": False,
    "is_mobile": False,
    "device_scale_factor": 1.0,
}

#: Chromium 启动参数:抹掉最容易被检测的自动化特征
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
]


class BrowserWorker(BaseWorker):
    """重量通道 Worker:独立 Chromium + 独立 Context。"""

    channel_name = "browser"

    def __init__(
        self,
        inbox: Any,
        outbox: Any,
        *,
        headless: bool = True,
        timeout: float = 45.0,
        reuse_same_domain: bool = True,
        context_ttl: float = 120.0,
        device_library: Any | None = None,
        allow_private: bool = False,
        wait_until: str = "domcontentloaded",
        net_guard: bool = True,
    ) -> None:
        """初始化浏览器 Worker。

        :param inbox: 任务队列。
        :param outbox: 结果队列。
        :param headless: 是否无头运行。
        :param timeout: 导航超时(秒)。
        :param reuse_same_domain: 同域连续任务是否复用 Context。
        :param context_ttl: Context 复用有效期(秒)。
        :param device_library: 设备档案库(批次 8 的 ``DeviceLibrary``);为空时用内置默认档案。
        :param allow_private: 是否允许访问内网地址(默认禁止,SSRF 防护)。
        :param wait_until: 导航完成条件。
        :param net_guard: 是否启用网络守卫(拦截非目标域请求)。
        """
        super().__init__(inbox, outbox)
        self.headless = headless
        self.timeout = timeout
        self.reuse_same_domain = reuse_same_domain
        self.context_ttl = context_ttl
        self.device_library = device_library
        self.allow_private = allow_private
        self.wait_until = wait_until
        self.net_guard = net_guard

        self._pw = None
        self._browser = None
        self._engine = "none"
        self._context = None
        self._context_host: str | None = None
        self._context_time = 0.0
        self._profile: DeviceProfile | None = None
        self._guard: NetGuard | None = None
        self._init_script_supported = False

    # ------------------------------------------------------------------ 生命周期

    def setup(self) -> None:
        """启动本进程专属的 Playwright 与 Chromium。"""
        try:
            from patchright.sync_api import sync_playwright  # 隐身版 Playwright
        except Exception:
            from playwright.sync_api import sync_playwright  # 回退:官方 Playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless, args=CHROMIUM_ARGS)
        self._engine = "patchright" if "patchright" in str(type(self._pw)).lower() else "playwright"
        self._init_script_supported = self._probe_init_script()

    def _probe_init_script(self) -> bool:
        """探测当前引擎是否真的执行 init script。

        Patchright 1.61.2 实测**不执行** init script(其驱动会忽略注入内容),
        官方 Playwright 则正常。不靠假设,靠一次 about:blank 实测。

        :return: 引擎是否支持 init script。
        """
        try:
            ctx = self._browser.new_context()
            page = ctx.new_page()
            page.add_init_script("window.__MELAB_PROBE__ = 1;")
            page.goto("about:blank", timeout=15000)
            ok = bool(page.evaluate("window.__MELAB_PROBE__ === 1"))
            page.close()
            ctx.close()
            return ok
        except Exception:
            return False

    def teardown(self) -> None:
        """关闭 Context 与浏览器,释放进程资源。"""
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------ 处理

    def handle(self, task: Task) -> dict[str, Any]:
        """渲染一个任务并返回页面结果。

        :param task: 待渲染任务。
        :return: 含 ``html`` / ``title`` / ``url`` / ``status`` / ``signals`` 等键的字典。
        """
        started = time.time()
        safe, why = check_url_safe(task.url, allow_private=self.allow_private)
        if not safe:
            return {
                "url": task.url,
                "final_url": task.url,
                "status": None,
                "title": "",
                "html": "",
                "signals": [],
                "ok": False,
                "error": f"SSRF 防护拒绝该 URL: {why}",
                "engine": self._engine,
                "elapsed": 0.0,
            }

        host = urlsplit(task.url).hostname or ""
        context = self._get_context(host)
        page = None
        status: int | None = None
        html = ""
        title = ""
        final_url = task.url
        try:
            page = context.new_page()
            response = page.goto(task.url, timeout=int(self.timeout * 1000), wait_until=self.wait_until)
            status = int(response.status) if response is not None else None
            html = page.content()
            title = page.title()
            final_url = page.url
            if self._profile is not None:
                harden_page(page, self._profile)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

        signals = detect_anti_bot_signals(html, status)
        ok = bool(html) and (status is None or status < 400) and not signals
        return {
            "url": task.url,
            "final_url": final_url,
            "status": status,
            "title": title,
            "html": html,
            "signals": signals,
            "ok": ok,
            "error": "" if ok else (f"HTTP {status}" if status else "页面内容为空或被反爬拦截"),
            "engine": self._engine,
            "elapsed": time.time() - started,
            "device_profile": self._profile.name if self._profile else None,
            "blocked_requests": self._guard.summary() if self._guard else {},
        }

    # ------------------------------------------------------------------ 内部

    def _get_context(self, host: str) -> Any:
        """获取(或新建)BrowserContext。

        同域且未过期则复用;跨域或过期则销毁重建。
        """
        now = time.time()
        if (
            self.reuse_same_domain
            and self._context is not None
            and self._context_host == host
            and (now - self._context_time) <= self.context_ttl
        ):
            self._context_time = now
            return self._context

        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
            self._context_host = None

        profile = self._pick_profile()
        options = profile.as_context_options() if profile is not None else dict(DEFAULT_DEVICE)
        self._context = self._browser.new_context(**options)
        self._profile = profile
        if profile is not None:
            apply_profile(self._context, profile, inject_init_script=self._init_script_supported)
        if self.net_guard and host:
            self._guard = NetGuard(allowed_hosts={host})
            self._guard.install(self._context)
        self._context_host = host
        self._context_time = now
        return self._context

    def _pick_profile(self) -> DeviceProfile | None:
        """挑一台设备档案;档案库不可用时返回 ``None``(用内置默认档案)。"""
        if self.device_library is None:
            return None
        try:
            return self.device_library.pick()
        except Exception:
            return None

    def _context_options(self) -> dict[str, Any]:
        """生成 new_context 的参数(保留给外部调用/调试)。"""
        profile = self._pick_profile()
        if profile is not None:
            return profile.as_context_options()
        return dict(DEFAULT_DEVICE)
