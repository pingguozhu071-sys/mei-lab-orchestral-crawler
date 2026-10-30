"""嗅探器单测:已知 URL 模式 → 正确的内容类型与通道映射。"""

from __future__ import annotations

import pytest

from meilab.router.sniff import (
    Channel,
    ContentType,
    detect_anti_bot_signals,
    refine_with_content_type,
    sniff_url,
)


@pytest.mark.parametrize(
    "url,ctype,channel",
    [
        ("https://example.com/paper.pdf", ContentType.PDF, Channel.PDF),
        ("https://example.com/data.json", ContentType.JSON, Channel.DIRECT_JSON),
        ("https://example.com/a/b/c.xml", ContentType.XML, Channel.LIGHTWEIGHT),
        ("https://example.com/logo.png", ContentType.IMAGE, Channel.LIGHTWEIGHT),
        ("https://example.com/page.html", ContentType.HTML, Channel.LIGHTWEIGHT),
        ("https://example.com/api/v1/items", ContentType.JSON, Channel.DIRECT_JSON),
        ("https://example.com/graphql", ContentType.JSON, Channel.DIRECT_JSON),
        ("https://example.com/dashboard/overview", ContentType.HTML, Channel.BROWSER),
        ("https://example.com/captcha/challenge", ContentType.HTML, Channel.BROWSER),
    ],
)
def test_known_url_patterns(url: str, ctype: ContentType, channel: Channel) -> None:
    """已知 URL 模式映射到正确的内容类型与通道。"""
    r = sniff_url(url)
    assert r.content_type is ctype
    assert r.channel is channel
    assert r.reasons, "必须给出判定依据"


def test_json_query_hint() -> None:
    """查询参数 format=json 应命中 JSON 直取。"""
    r = sniff_url("https://example.com/list?format=json&page=2")
    assert r.channel is Channel.DIRECT_JSON
    assert r.content_type is ContentType.JSON


def test_default_is_lightweight_html() -> None:
    """无特征的 URL 走轻量 HTML 通道。"""
    r = sniff_url("https://news.ycombinator.com/")
    assert r.channel is Channel.LIGHTWEIGHT
    assert r.content_type is ContentType.HTML
    assert 0 < r.confidence <= 1


def test_empty_url_is_conservative() -> None:
    """空 URL 保守走浏览器通道,不猜。"""
    r = sniff_url("")
    assert r.channel is Channel.BROWSER
    assert r.confidence == 0.0


def test_refine_with_content_type_header() -> None:
    """真实响应头优先于 URL 猜测。"""
    base = sniff_url("https://example.com/thing")
    r = refine_with_content_type(base, "application/json; charset=utf-8")
    assert r.channel is Channel.DIRECT_JSON
    assert r.content_type is ContentType.JSON


def test_refine_detects_empty_shell_html() -> None:
    """HTML 空壳(正文极少)判定为需要 JS 渲染。"""
    base = sniff_url("https://example.com/page")
    body = b"<!DOCTYPE html><html><head><title>x</title></head><body><div id=\"root\"></div></body></html>"
    r = refine_with_content_type(base, "text/html", body)
    assert r.channel is Channel.BROWSER
    assert r.requires_js is True


def test_detect_anti_bot_signals() -> None:
    """反爬信号检测。"""
    assert "cloudflare_challenge" in detect_anti_bot_signals(
        "<html><title>Just a moment...</title></html>", 200
    )
    assert "captcha" in detect_anti_bot_signals("<div class='g-recaptcha'></div>")
    assert "status_403" in detect_anti_bot_signals("", 403)
    assert detect_anti_bot_signals("<p>hello</p>", 200) == []
