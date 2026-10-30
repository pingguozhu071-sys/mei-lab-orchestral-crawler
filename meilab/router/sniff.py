"""URL / 内容类型嗅探:纯静态启发,不发任何网络请求。

设计目标是让"最快路径优先"落地:在真正抓取之前,先用 URL 的后缀、路径模式、
查询参数判断这个 URL 大概率是什么内容类型、该走哪条通道,避免无谓地开浏览器。

嗅探结果 :class:`SniffResult` 带有 ``reasons``(判定依据)与 ``confidence``(置信度),
便于排障时解释"为什么这个 URL 被派给了浏览器通道"。
"""

from __future__ import annotations

import re
from enum import Enum
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field


class ContentType(str, Enum):
    """推测的内容类型。"""

    HTML = "html"
    JSON = "json"
    XML = "xml"
    PDF = "pdf"
    IMAGE = "image"
    TEXT = "text"
    UNKNOWN = "unknown"


class Channel(str, Enum):
    """建议使用的抓取通道。"""

    LIGHTWEIGHT = "lightweight"  # urllib / curl_cffi 直接抓
    DIRECT_JSON = "direct_json"  # JSON API,直接解析
    BROWSER = "browser"          # 需要 JS 渲染或强反爬
    PDF = "pdf"                  # PDF 提取管线


class SniffResult(BaseModel):
    """一次嗅探的结论。"""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="被嗅探的 URL")
    content_type: ContentType = Field(default=ContentType.UNKNOWN, description="推测的内容类型")
    channel: Channel = Field(default=Channel.LIGHTWEIGHT, description="建议通道")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="置信度 0~1")
    reasons: list[str] = Field(default_factory=list, description="判定依据")
    requires_js: bool = Field(default=False, description="是否可能需要 JS 渲染")
    anti_bot_hint: bool = Field(default=False, description="是否命中反爬特征")
    suggested_type: str = Field(default="crawl", description="建议的任务类型:crawl / json_api / pdf / image")

    @property
    def is_json(self) -> bool:
        """是否走 JSON 直取通道。"""
        return self.channel is Channel.DIRECT_JSON

    @property
    def needs_browser(self) -> bool:
        """是否需要浏览器通道。"""
        return self.channel is Channel.BROWSER


#: 文件后缀 → 内容类型
_SUFFIX_TYPES: dict[str, ContentType] = {
    ".pdf": ContentType.PDF,
    ".json": ContentType.JSON,
    ".jsonl": ContentType.JSON,
    ".ndjson": ContentType.JSON,
    ".xml": ContentType.XML,
    ".rss": ContentType.XML,
    ".atom": ContentType.XML,
    ".txt": ContentType.TEXT,
    ".csv": ContentType.TEXT,
    ".md": ContentType.TEXT,
    ".html": ContentType.HTML,
    ".htm": ContentType.HTML,
    ".png": ContentType.IMAGE,
    ".jpg": ContentType.IMAGE,
    ".jpeg": ContentType.IMAGE,
    ".gif": ContentType.IMAGE,
    ".webp": ContentType.IMAGE,
    ".svg": ContentType.IMAGE,
    ".ico": ContentType.IMAGE,
    ".bmp": ContentType.IMAGE,
}

#: 路径中出现即视为 API 的关键词
_API_PATH_RE = re.compile(
    r"(^|/)(api|apis|v\d+|rest|graphql|gql|json|ajax|service|services|endpoint|data)(/|$|\.)",
    re.IGNORECASE,
)

#: 路径中出现即视为 SPA / 需要 JS 渲染的关键词
_JS_PATH_RE = re.compile(
    r"(^|/)(app|spa|web|dashboard|console|portal|frontend|client)(/|$)", re.IGNORECASE
)

#: 反爬特征关键词(挑战页 / 验证码 / 风控)
_ANTIBOT_RE = re.compile(
    r"(captcha|challenge|cf-challenge|__cf_chl|verify_you_are|bot-check|denied|blocked|security-check)",
    re.IGNORECASE,
)

#: 查询参数里表示"我要 JSON"的键
_JSON_QUERY_KEYS = ("format", "output", "callback", "content-type", "type", "mime")
_JSON_QUERY_VALUES = ("json", "application/json", "jsonp")


def _host_of(url: str) -> str:
    """取 URL 的主机名(小写,不含端口)。非法 URL 返回空串。"""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _path_of(url: str) -> str:
    """取 URL 的路径部分。非法 URL 返回空串。"""
    try:
        return urlsplit(url).path or ""
    except ValueError:
        return ""


def _query_of(url: str) -> dict[str, list[str]]:
    """取 URL 的查询参数字典。非法 URL 返回空字典。"""
    try:
        return parse_qs(urlsplit(url).query, keep_blank_values=False)
    except ValueError:
        return {}


def sniff_url(url: str) -> SniffResult:
    """对 URL 做静态嗅探。

    判定顺序(先命中先返回):
    1. 反爬特征 → 浏览器通道;
    2. 文件后缀 → 对应类型;
    3. API 路径模式 → JSON 直取;
    4. 查询参数要 JSON → JSON 直取;
    5. SPA 路径模式 → 浏览器通道;
    6. 兜底 → HTML + 轻量通道。

    :param url: 待嗅探的 URL。
    :return: 嗅探结论。
    """
    if not isinstance(url, str) or not url.strip():
        return SniffResult(url=str(url), content_type=ContentType.UNKNOWN, channel=Channel.BROWSER,
                           confidence=0.0, reasons=["空 URL,保守走浏览器通道"])

    url = url.strip()
    reasons: list[str] = []
    path = _path_of(url)
    host = _host_of(url)
    query = _query_of(url)
    lower_path = path.lower()

    # 1) 反爬特征
    if _ANTIBOT_RE.search(url):
        return SniffResult(
            url=url,
            content_type=ContentType.HTML,
            channel=Channel.BROWSER,
            confidence=0.6,
            reasons=["URL 含反爬/挑战特征关键词"],
            anti_bot_hint=True,
            requires_js=True,
        )

    # 2) 文件后缀
    dot = lower_path.rfind(".")
    if dot != -1:
        suffix = lower_path[dot:]
        if suffix in _SUFFIX_TYPES:
            ctype = _SUFFIX_TYPES[suffix]
            reasons.append(f"路径后缀 {suffix} → {ctype.value}")
            if ctype is ContentType.PDF:
                return SniffResult(url=url, content_type=ctype, channel=Channel.PDF,
                                   confidence=0.95, reasons=reasons, suggested_type="pdf")
            if ctype is ContentType.JSON:
                return SniffResult(url=url, content_type=ctype, channel=Channel.DIRECT_JSON,
                                   confidence=0.9, reasons=reasons, suggested_type="json_api")
            if ctype is ContentType.IMAGE:
                return SniffResult(url=url, content_type=ctype, channel=Channel.LIGHTWEIGHT,
                                   confidence=0.9, reasons=reasons, suggested_type="image")
            return SniffResult(url=url, content_type=ctype, channel=Channel.LIGHTWEIGHT,
                               confidence=0.85, reasons=reasons)

    # 3) API 路径模式
    if _API_PATH_RE.search(lower_path):
        return SniffResult(url=url, content_type=ContentType.JSON, channel=Channel.DIRECT_JSON,
                           confidence=0.7, reasons=reasons + ["路径匹配 API 模式"],
                           suggested_type="json_api")

    # 4) 查询参数要 JSON
    for key in _JSON_QUERY_KEYS:
        values = query.get(key) or []
        for value in values:
            if value.lower() in _JSON_QUERY_VALUES:
                return SniffResult(url=url, content_type=ContentType.JSON,
                                   channel=Channel.DIRECT_JSON, confidence=0.65,
                                   reasons=reasons + [f"查询参数 {key}={value}"],
                                   suggested_type="json_api")

    # 5) SPA / 需要 JS 渲染
    if _JS_PATH_RE.search(lower_path):
        return SniffResult(url=url, content_type=ContentType.HTML, channel=Channel.BROWSER,
                           confidence=0.5, reasons=reasons + ["路径匹配 SPA/前端应用模式"],
                           requires_js=True)

    # 6) 兜底
    reasons.append("无明确特征,按静态 HTML 处理")
    conf = 0.4 if host else 0.2
    return SniffResult(url=url, content_type=ContentType.HTML, channel=Channel.LIGHTWEIGHT,
                       confidence=conf, reasons=reasons)


def refine_with_content_type(
    result: SniffResult,
    content_type_header: str | None,
    body_prefix: bytes | None = None,
) -> SniffResult:
    """用真实的响应头(可选正文前若干字节)修正嗅探结果。

    :param result: 原始嗅探结果。
    :param content_type_header: 响应的 ``Content-Type`` 头,形如 ``text/html; charset=utf-8``。
    :param body_prefix: 正文前若干字节,用于判断"HTML 但是空壳(需要 JS)"。
    :return: 修正后的新结果(不修改入参)。
    """
    header = (content_type_header or "").split(";")[0].strip().lower()
    data = result.model_copy(deep=True)
    reasons = list(data.reasons)

    if header:
        if "json" in header:
            data.content_type = ContentType.JSON
            data.channel = Channel.DIRECT_JSON
            data.suggested_type = "json_api"
            data.confidence = max(data.confidence, 0.9)
            reasons.append(f"响应头 Content-Type={header} → JSON 直取")
        elif "html" in header:
            data.content_type = ContentType.HTML
            if data.channel is not Channel.BROWSER:
                data.channel = Channel.LIGHTWEIGHT
            reasons.append(f"响应头 Content-Type={header} → HTML")
        elif "pdf" in header:
            data.content_type = ContentType.PDF
            data.channel = Channel.PDF
            data.suggested_type = "pdf"
            reasons.append(f"响应头 Content-Type={header} → PDF")
        elif "xml" in header:
            data.content_type = ContentType.XML
            reasons.append(f"响应头 Content-Type={header} → XML")
        elif "image/" in header:
            data.content_type = ContentType.IMAGE
            data.suggested_type = "image"
            reasons.append(f"响应头 Content-Type={header} → 图片")
        elif header.startswith("text/"):
            data.content_type = ContentType.TEXT
            reasons.append(f"响应头 Content-Type={header} → 纯文本")

    # 空壳 HTML:正文短且以 <html/<!doctype 开头,判定为需要 JS 渲染
    if body_prefix is not None and data.content_type is ContentType.HTML:
        prefix = body_prefix[:4096].lstrip().lower()
        texty = len(re.sub(rb"<[^>]+>", b"", body_prefix[:4096]).strip())
        if prefix.startswith((b"<!doctype html", b"<html")) and texty < 200:
            data.channel = Channel.BROWSER
            data.requires_js = True
            data.confidence = max(data.confidence, 0.7)
            reasons.append("HTML 正文近乎为空,疑似需要 JS 渲染")

    data.reasons = reasons
    return data


def detect_anti_bot_signals(html: str | None, status_code: int | None = None) -> list[str]:
    """从响应正文里检测常见的反爬信号,返回命中的信号名列表。

    :param html: 响应正文(可为空)。
    :param status_code: HTTP 状态码,可空。
    :return: 命中的信号名,例如 ``["cloudflare_challenge", "status_403"]``。
    """
    signals: list[str] = []
    body = (html or "").lower()

    if status_code in (403, 429, 503):
        signals.append(f"status_{status_code}")
    if "cf-challenge" in body or "cf_chl_opt" in body or "just a moment" in body:
        signals.append("cloudflare_challenge")
    if "captcha" in body or "recaptcha" in body or "hcaptcha" in body:
        signals.append("captcha")
    if "attention required" in body and "cloudflare" in body:
        signals.append("cloudflare_block")
    if "access denied" in body or "请开启 javascript" in body or "enable javascript" in body:
        signals.append("js_required")
    if "too many requests" in body or "rate limit" in body:
        signals.append("rate_limited")
    return signals
