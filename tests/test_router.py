"""分层路由器单测:URL 模式 / 任务类型 → 正确的 Worker 与通道。"""

from __future__ import annotations

import pytest

from meilab.core.types import Task, TaskType
from meilab.router.layer import ChannelDecision, LayeredRouter, WorkerKind
from meilab.router.sniff import Channel, ContentType, sniff_url


@pytest.fixture()
def router() -> LayeredRouter:
    """构造一个默认路由器。"""
    return LayeredRouter()


def test_static_html_goes_http(router: LayeredRouter) -> None:
    """静态 HTML 走轻量 HTTP 通道。"""
    d = router.route(Task(url="https://news.ycombinator.com/"))
    assert d.worker is WorkerKind.HTTP
    assert d.channel is Channel.LIGHTWEIGHT
    assert d.expected_content_type is ContentType.HTML
    assert d.max_rounds == 1
    assert d.requires_js is False


def test_json_api_goes_direct_json(router: LayeredRouter) -> None:
    """API 路径走 JSON 直取通道。"""
    d = router.route(Task(url="https://example.com/api/v1/items"))
    assert d.worker is WorkerKind.HTTP
    assert d.channel is Channel.DIRECT_JSON


def test_pdf_goes_pdf_channel(router: LayeredRouter) -> None:
    """PDF URL 走 PDF 通道与 PDF 提取。"""
    d = router.route(Task(url="https://example.com/paper.pdf"))
    assert d.worker is WorkerKind.HTTP
    assert d.channel is Channel.PDF
    assert d.expected_content_type is ContentType.PDF


def test_js_site_goes_browser(router: LayeredRouter) -> None:
    """SPA 路径升级到浏览器通道。"""
    d = router.route(Task(url="https://example.com/dashboard/home"))
    assert d.worker is WorkerKind.BROWSER
    assert d.is_browser is True
    assert d.requires_js is True
    assert d.max_rounds >= 2
    assert d.timeout > 20


def test_antibot_marks_stealth(router: LayeredRouter) -> None:
    """命中反爬特征时开启强反检测档案。"""
    d = router.route(Task(url="https://example.com/captcha/challenge"))
    assert d.worker is WorkerKind.BROWSER
    assert d.stealth is True
    assert d.retry_hint >= 1


def test_task_type_overrides_url_guess(router: LayeredRouter) -> None:
    """显式任务类型优先于 URL 猜测。"""
    # URL 看起来像 SPA,但任务类型声明为 JSON API → 走 JSON 直取
    d = router.route(Task(url="https://example.com/dashboard/data", task_type=TaskType.JSON_API))
    assert d.channel is Channel.DIRECT_JSON
    assert d.worker is WorkerKind.HTTP

    d2 = router.route(Task(url="https://example.com/app/x", task_type=TaskType.PDF))
    assert d2.channel is Channel.PDF


def test_explicit_sniff_result_is_used(router: LayeredRouter) -> None:
    """传入已有嗅探结果时不再重新嗅探。"""
    sniff = sniff_url("https://example.com/dashboard/x")
    d = router.route(Task(url="https://example.com/dashboard/x"), sniff_result=sniff)
    assert d.worker is WorkerKind.BROWSER
    assert "嗅探判定" in d.reason


def test_decision_is_serialisable(router: LayeredRouter) -> None:
    """决策可序列化,便于跨进程传递。"""
    d = router.route(Task(url="https://example.com/"))
    data = d.model_dump(mode="json")
    assert data["worker"] == "http"
    assert ChannelDecision.model_validate(data).task_id == d.task_id


def test_route_url_helper(router: LayeredRouter) -> None:
    """URL 快捷入口可用。"""
    d = router.route_url("https://example.com/a.pdf")
    assert d.channel is Channel.PDF
