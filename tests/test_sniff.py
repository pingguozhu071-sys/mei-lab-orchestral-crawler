"""嗅探器单测:已知 URL 模式 → 正确的内容类型与通道映射。"""

from __future__ import annotations

import pytest

from meilab.router.sniff import (
    Channel,
    ContentType,
    detect_anti_bot_signals,
    detect_media,
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


def test_captcha_sdk_reference_is_not_a_challenge() -> None:
    """预加载的验证码 SDK 引用不算拦截(真实 B 站页面即如此)。"""
    html = '<script src="https://x.com/risk-captcha-sdk/CaptchaLoader.js"></script><p>正文</p>'
    assert "captcha" not in detect_anti_bot_signals(html, 200)
    assert "captcha" not in detect_anti_bot_signals("<script>var captcha=1</script>", 200)


@pytest.mark.parametrize(
    "url,platform,kind",
    [
        ("https://www.bilibili.com/video/BV1Dvpg6BEt5/", "bilibili", "video"),
        ("https://b23.tv/abcdef", "bilibili", "video"),
        ("https://v.douyin.com/iRNBho6/", "douyin", "video"),
        ("https://www.xiaohongshu.com/explore/123", "xiaohongshu", "video"),
        ("https://www.kuaishou.com/short-video/xyz", "kuaishou", "video"),
        ("https://music.163.com/song?id=123", "netease_music", "audio"),
        ("https://cdn.example.com/a/b.mp4", "generic", "video"),
        ("https://cdn.example.com/a/b.m3u8", "generic", "video"),
        ("https://cdn.example.com/a/b.mp3", "generic", "audio"),
    ],
)
def test_detect_media(url: str, platform: str, kind: str) -> None:
    """媒体识别:平台页面与媒体直链。"""
    assert detect_media(url) == (platform, kind)


def test_detect_media_returns_none_for_normal_pages() -> None:
    """普通网页不是媒体。"""
    assert detect_media("https://news.ycombinator.com/") is None
    assert detect_media("https://example.com/article.html") is None


def test_media_urls_route_to_media_channel() -> None:
    """媒体 URL 走媒体通道,并带上平台标识。"""
    r = sniff_url("https://www.bilibili.com/video/BV1Dvpg6BEt5/")
    assert r.channel is Channel.MEDIA
    assert r.suggested_type == "media"
    assert r.platform == "bilibili"
