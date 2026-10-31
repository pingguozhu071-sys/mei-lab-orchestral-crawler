"""RSS / Atom / sitemap 播种:从订阅源发现入口链接。

解析用 ``defusedxml``(禁用 DTD 与实体扩展,防 billion laughs 资源耗尽);
订阅源解析失败时**降级为普通页面抓取**(返回空列表,不报错),与 kiana 一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

#: RSS 2.0 的 item 标签
_RSS_ITEM = "item"
#: Atom 的 entry 标签
_ATOM_ENTRY = "entry"

#: 订阅源的最大字节数(超过就拒绝,防资源耗尽)
MAX_FEED_BYTES = 5 * 1024 * 1024

#: 危险构造:内部 DTD 与实体声明(defusedxml 之外的第二道防线)
_DANGEROUS_XML = re.compile(r"<!(DOCTYPE|ENTITY)\b", re.IGNORECASE)


@dataclass
class FeedResult:
    """一次订阅源解析的结果。"""

    ok: bool = False
    kind: str = "none"  # rss / atom / sitemap / none
    title: str = ""
    links: list[str] = field(default_factory=list)
    error: str = ""


def _text(node: Any, tag: str) -> str:
    """取子标签的文本(找不到就递归找一次)。"""
    child = node.find(tag)
    if child is None or child.text is None:
        for candidate in node.iter(tag):
            if candidate.text:
                return candidate.text.strip()
        return ""
    return child.text.strip()


def _local(tag: str) -> str:
    """取标签的本地名(去掉 XML 命名空间前缀)。"""
    return tag.split("}")[-1].lower()


def _link_of(node: Any) -> str:
    """取条目的链接(命名空间无关)。

    Atom 的 ``<link>`` 带命名空间,``find("link")`` 找不到,故按本地名遍历。
    """
    for candidate in node.iter():
        if _local(candidate.tag) != "link":
            continue
        if candidate.text and candidate.text.strip():
            return candidate.text.strip()
        href = candidate.get("href")
        if href:
            return str(href).strip()
    return ""


def is_safe_xml(text: str) -> bool:
    """判断 XML 文本是否可以安全解析(无 DTD/实体扩展,大小合规)。

    :param text: XML 文本。
    :return: 是否安全。
    """
    if not text:
        return False
    if len(text.encode("utf-8", errors="replace")) > MAX_FEED_BYTES:
        return False
    return _DANGEROUS_XML.search(text) is None


def parse_feed(text: str, base_url: str = "") -> FeedResult:
    """解析 RSS / Atom / sitemap。

    :param text: 订阅源文本。
    :param base_url: 相对链接的基准 URL。
    :return: 解析结果。
    """
    result = FeedResult()
    if not text or not text.strip():
        result.error = "订阅源为空"
        return result
    if not is_safe_xml(text):
        result.error = "订阅源包含 DTD/实体声明或体积过大,已拒绝解析"
        return result

    try:
        from defusedxml import ElementTree as DET  # 第三方库:安全 XML 解析

        root = DET.fromstring(text.strip())
    except Exception as exc:
        result.error = f"XML 解析失败(降级为普通页面抓取): {exc!r}"
        return result

    tag = root.tag.split("}")[-1].lower()
    if tag == "rss" or tag == "rdf":
        result.kind = "rss"
        result.title = _text(root, "title")
        for item in root.iter(_RSS_ITEM):
            link = _link_of(item) or _text(item, "guid")
            if link:
                result.links.append(urljoin(base_url, link))
    elif tag == "feed":
        result.kind = "atom"
        for node in root.iter():
            if _local(node.tag) == "title" and node.text:
                result.title = node.text.strip()
                break
        # Atom 的 entry 带命名空间,按本地名遍历
        for node in root.iter():
            if _local(node.tag) != _ATOM_ENTRY:
                continue
            link = _link_of(node)
            if link:
                result.links.append(urljoin(base_url, link))
    elif tag == "urlset":
        result.kind = "sitemap"
        for node in root.iter():
            if node.tag.split("}")[-1].lower() == "url":
                for child in node:
                    if child.tag.split("}")[-1].lower() == "loc" and child.text:
                        result.links.append(urljoin(base_url, child.text.strip()))
                        break
    else:
        result.error = f"未识别的订阅源类型:<{tag}>"
        return result

    seen: list[str] = []
    for link in result.links:
        if link not in seen:
            seen.append(link)
    result.links = seen
    result.ok = bool(result.links)
    if not result.ok:
        result.error = "订阅源里没有可用链接"
    return result


def fetch_feed(url: str, *, timeout: float = 20.0) -> FeedResult:
    """抓取并解析一个订阅源。

    :param url: 订阅源 URL。
    :param timeout: 超时(秒)。
    :return: 解析结果。
    """
    from ..workers.base import check_url_safe
    from ..workers.http import ensure_ascii_ca_bundle

    safe, why = check_url_safe(url)
    if not safe:
        return FeedResult(error=f"SSRF 防护拒绝该订阅源: {why}")
    try:
        from curl_cffi import requests as curl_requests  # 第三方库:TLS 指纹模拟

        ensure_ascii_ca_bundle()
        response = curl_requests.get(
            url, timeout=timeout, impersonate="chrome",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0"},
        )
        text = response.text or ""
    except Exception as exc:
        return FeedResult(error=f"订阅源抓取失败(降级为普通页面抓取): {exc!r}")
    return parse_feed(text, url)


def looks_like_feed(url: str) -> bool:
    """判断 URL 是否可能是订阅源(按后缀启发)。

    :param url: 目标 URL。
    :return: 是否可能是订阅源。
    """
    path = (url or "").split("?")[0].lower()
    return bool(re.search(r"\.(rss|xml|atom)$|/(feed|sitemap)", path))
