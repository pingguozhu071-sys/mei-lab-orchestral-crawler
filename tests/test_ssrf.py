"""SSRF 防护回归:对抗性私网样本必须全部被拒绝。

样本覆盖环回 / 私网 / 链路本地 / CGNAT / 云元数据 / 数字字面量 / IPv6 zone id
/ 协议白名单等经典绕过手法。
"""

from __future__ import annotations

import pytest

from meilab.workers.base import (
    check_url_safe,
    is_public_ip,
    normalize_host,
    numeric_host_to_ip,
)

#: 必须被拒绝的 URL(不发起任何真实请求,只看校验结果)
BLOCKED_URLS = [
    "http://127.0.0.1/",
    "http://127.0.0.1:8080/admin",
    "http://127.1/",
    "https://127.0.0.1/",
    "http://localhost/",
    "http://localhost:3000/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://10.0.0.1/",
    "http://10.255.255.255/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",   # 云元数据
    "http://100.64.0.1/",                        # CGNAT
    "http://192.88.99.1/",                       # 6to4 继任段
    "http://198.18.0.1/",                        # 基准测试保留段
    "http://0.0.0.0/",
    "http://2130706433/",                        # 十进制 127.0.0.1
    "http://0x7f000001/",                        # 十六进制 127.0.0.1
    "http://017700000001/",                      # 八进制 127.0.0.1
    "http://127.1/",                             # 省略段写法
    "http://[fe80::1%25eth0]/",                  # IPv6 zone id
    "file:///C:/Windows/win.ini",                 # 协议白名单
    "gopher://127.0.0.1:11211/",
    "ftp://example.com/x",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_ssrf_blocks_dangerous_urls(url: str) -> None:
    """对抗性 URL 一律拒绝。"""
    safe, why = check_url_safe(url)
    assert safe is False, f"未拦截危险 URL: {url}"
    assert why  # 必须给出原因


@pytest.mark.parametrize(
    "host,expected",
    [
        ("2130706433", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("017700000001", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("10.0.0.1", "10.0.0.1"),
    ],
)
def test_numeric_host_forms(host: str, expected: str) -> None:
    """数字字面量主机名要还原成真实 IP。"""
    assert numeric_host_to_ip(host) == expected


def test_numeric_host_for_non_numeric() -> None:
    """普通域名不是数字字面量。"""
    assert numeric_host_to_ip("example.com") is None
    assert numeric_host_to_ip("") is None


def test_normalize_host_strips_zone_and_dot() -> None:
    """主机名规范化:去 IPv6 zone id 与 FQDN 尾点。"""
    assert normalize_host("FE80::1%25eth0") == "fe80::1"
    assert normalize_host("Example.COM.") == "example.com"


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.1.2.3", "192.168.0.1", "172.16.5.5", "169.254.169.254",
     "100.64.0.1", "192.88.99.1", "198.18.0.1", "0.0.0.0", "::1"],
)
def test_non_public_ips_are_rejected(ip: str) -> None:
    """非公网 IP 一律判为不安全。"""
    assert is_public_ip(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_public_ips_are_accepted(ip: str) -> None:
    """公网 IP 判为安全。"""
    assert is_public_ip(ip) is True


def test_non_http_scheme_is_rejected() -> None:
    """非 http/https 协议一律拒绝(即使主机是公网)。"""
    safe, why = check_url_safe("ftp://example.com/file")
    assert safe is False
    assert "http" in why


def test_unresolvable_host_is_rejected() -> None:
    """解析不出来的域名按不安全处理(不放行)。"""
    safe, why = check_url_safe("http://this-host-does-not-exist.invalid/")
    assert safe is False
    assert why
