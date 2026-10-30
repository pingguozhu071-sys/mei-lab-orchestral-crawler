"""网络守卫:拦截并掐断一切**非目标域**的请求。

浏览器通道里最常见的两种"意外暴露":
1. 页面里的第三方埋点/广告/字体 CDN 请求泄露真实 IP 与访问行为;
2. 页面内嵌的第三方 iframe 悄悄把当前 URL 带出去。

网络守卫在 Playwright 的路由层拦截所有请求,只放行目标域及其子域,
其余一律 abort——既减少噪声,也让浏览器只与目标站交互。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


class NetGuard:
    """按域名白名单放行请求的网络守卫。"""

    def __init__(
        self,
        allowed_hosts: set[str] | None = None,
        *,
        allow_subdomains: bool = True,
        allow_data_and_blob: bool = True,
    ) -> None:
        """初始化守卫。

        :param allowed_hosts: 允许的域名集合(小写,不含端口);为空表示不限制。
        :param allow_subdomains: 是否放行白名单域名的子域。
        :param allow_data_and_blob: 是否放行 ``data:`` / ``blob:`` 这类本地伪协议。
        """
        self.allowed_hosts = {h.lower().strip() for h in (allowed_hosts or set())}
        self.allow_subdomains = allow_subdomains
        self.allow_data_and_blob = allow_data_and_blob
        self.blocked: list[str] = []

    # ------------------------------------------------------------------ 判定

    def is_allowed(self, url: str) -> bool:
        """判断某个 URL 是否放行。

        :param url: 待判定 URL。
        :return: 是否放行。
        """
        try:
            parts = urlsplit(url)
        except ValueError:
            return False

        scheme = (parts.scheme or "").lower()
        if scheme in ("data", "blob", "about"):
            return self.allow_data_and_blob
        if scheme not in ("http", "https"):
            return False
        if not self.allowed_hosts:
            return True  # 未配置白名单 = 不限制

        host = (parts.hostname or "").lower()
        if not host:
            return False
        if host in self.allowed_hosts:
            return True
        if self.allow_subdomains:
            for allowed in self.allowed_hosts:
                if host.endswith("." + allowed):
                    return True
        return False

    # ------------------------------------------------------------------ 挂载

    def install(self, context: Any) -> None:
        """把守卫挂到 BrowserContext 上。

        :param context: Playwright 的 ``BrowserContext``。
        """
        context.route("**/*", self._handle)

    def _handle(self, route: Any) -> None:
        """路由回调:放行则 continue,否则 abort。"""
        try:
            url = route.request.url
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass
            return

        if self.is_allowed(url):
            try:
                route.continue_()
            except Exception:
                pass
            return

        self.blocked.append(url)
        try:
            route.abort()
        except Exception:
            pass

    # ------------------------------------------------------------------ 报告

    def summary(self) -> dict[str, Any]:
        """返回拦截统计。"""
        unique = sorted(set(self.blocked))
        return {
            "allowed_hosts": sorted(self.allowed_hosts),
            "blocked_total": len(self.blocked),
            "blocked_unique": len(unique),
            "blocked_sample": unique[:20],
        }


def hosts_of(urls: list[str]) -> set[str]:
    """从一组 URL 提取域名集合(用于生成白名单)。

    :param urls: URL 列表。
    :return: 小写域名集合。
    """
    out: set[str] = set()
    for url in urls:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if host:
            out.add(host)
    return out
