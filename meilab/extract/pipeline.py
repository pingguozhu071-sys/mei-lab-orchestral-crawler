"""提取管线:按内容类型把响应分发给对应的提取器。

输入是 Worker 拿到的原始响应(``Content-Type`` + 字节流),
输出是统一的 :class:`ExtractResult`:标题 + 正文 + 内容哈希 + 元数据。

分派依据的优先级:
1. 响应头 ``Content-Type``;
2. 响应头缺失或不可信时,**嗅探字节流**(PDF 魔数 / JSON 首字符 / HTML 标签)。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .html import extract_html
from .jsondata import extract_from_html, parse_json_response, to_markdown
from .pdf import extract_pdf, looks_like_pdf


@dataclass
class ExtractResult:
    """一次内容提取的统一结果。"""

    kind: str = "none"  # html / json / pdf / unknown
    title: str = ""
    content: str = ""
    content_hash: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    strategy: str = ""

    @property
    def ok(self) -> bool:
        """是否提取到了内容。"""
        return bool(self.content.strip()) and not self.error


def sha256_of(text: str) -> str:
    """计算文本的 SHA256(十六进制)。

    :param text: 文本。
    :return: 哈希值。
    """
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def sniff_body(body: bytes | str | None) -> str:
    """嗅探字节流的类型(不联网)。

    :param body: 字节流或文本。
    :return: ``"pdf"`` / ``"json"`` / ``"html"`` / ``"text"`` / ``"unknown"``。
    """
    if not body:
        return "unknown"
    if isinstance(body, (bytes, bytearray)):
        if looks_like_pdf(bytes(body)):
            return "pdf"
        head = bytes(body[:512]).lstrip()
        text_head = head.decode("utf-8", errors="replace")
    else:
        text_head = body[:512].lstrip()
        if text_head.startswith("%PDF-"):
            return "pdf"
    if text_head[:1] in ("{", "["):
        return "json"
    lowered = text_head.lower()
    if "<html" in lowered or "<!doctype html" in lowered or "<body" in lowered:
        return "html"
    if lowered.startswith("<"):
        return "html"
    return "text"


def _decode(body: bytes | str | None) -> str:
    """把字节流解码为文本。"""
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    try:
        return bytes(body).decode("utf-8", errors="replace")
    except Exception:
        return ""


class ExtractionPipeline:
    """按内容类型编排提取器。"""

    def __init__(self, *, use_fallback: bool = True, max_pdf_pages: int = 100) -> None:
        """初始化管线。

        :param use_fallback: HTML 密度算法失效时是否启用 trafilatura 兜底。
        :param max_pdf_pages: PDF 最多提取页数。
        """
        self.use_fallback = use_fallback
        self.max_pdf_pages = max_pdf_pages

    def extract(
        self,
        body: bytes | str | None,
        content_type: str | None = None,
        *,
        url: str = "",
    ) -> ExtractResult:
        """提取内容。

        :param body: 响应体。
        :param content_type: 响应头 ``Content-Type``。
        :param url: 原始 URL(仅记录用)。
        :return: 统一提取结果。
        """
        result = ExtractResult()
        header = (content_type or "").split(";")[0].strip().lower()

        kind = ""
        if header:
            if "json" in header:
                kind = "json"
            elif "html" in header or "xhtml" in header:
                kind = "html"
            elif "pdf" in header:
                kind = "pdf"
            elif header.startswith("text/"):
                kind = "text"
        if not kind:
            kind = sniff_body(body)
        result.kind = kind if kind != "unknown" else "unknown"

        if kind == "json":
            return self._extract_json(result, body, url)
        if kind == "pdf":
            return self._extract_pdf(result, body, url)
        if kind == "html":
            return self._extract_html(result, body, url)
        if kind == "text":
            text = _decode(body).strip()
            result.content = text
            result.content_hash = sha256_of(text)
            result.strategy = "plain_text"
            result.meta = {"url": url, "bytes": len(body) if body else 0}
            if not text:
                result.error = "空正文"
            return result

        result.error = f"无法识别内容类型(content_type={content_type!r})"
        result.meta = {"url": url}
        return result

    # ------------------------------------------------------------------ 内部

    def _extract_json(self, result: ExtractResult, body: Any, url: str) -> ExtractResult:
        """JSON 提取。"""
        parsed = parse_json_response(_decode(body))
        if not parsed.ok:
            result.error = parsed.notes[0] if parsed.notes else "JSON 解析失败"
            result.meta = {"url": url}
            return result
        result.kind = "json"
        result.title = parsed.title
        result.content = to_markdown(parsed.data)
        result.content_hash = sha256_of(result.content)
        result.strategy = "json_api"
        result.meta = {
            "url": url,
            "records": parsed.count,
            "json_kind": parsed.kind,
        }
        return result

    def _extract_html(self, result: ExtractResult, body: Any, url: str) -> ExtractResult:
        """HTML 提取:先走正文提取,再尝试结构化数据(JSON-LD / hydration state)。"""
        html = _decode(body)
        extraction = extract_html(html, use_fallback=self.use_fallback)
        result.kind = "html"
        result.title = extraction.title
        result.content = extraction.markdown
        result.content_hash = sha256_of(result.content)
        result.strategy = extraction.strategy
        result.meta = {
            "url": url,
            "density": round(extraction.density, 3),
            "text_length": extraction.text_length,
            "notes": extraction.notes,
        }
        # HTML 里若嵌了结构化数据,额外记一笔(不覆盖正文)
        structured = extract_from_html(html)
        if structured.ok:
            result.meta["structured_kind"] = structured.kind
            result.meta["structured_records"] = structured.count
            if not result.title and structured.title:
                result.title = structured.title
        if not result.content.strip():
            result.error = "HTML 正文为空"
        return result

    def _extract_pdf(self, result: ExtractResult, body: Any, url: str) -> ExtractResult:
        """PDF 提取。"""
        data = body if isinstance(body, (bytes, bytearray)) else _decode(body).encode("utf-8")
        pdf = extract_pdf(bytes(data), max_pages=self.max_pdf_pages)
        result.kind = "pdf"
        result.title = pdf.title
        result.content = pdf.content
        result.content_hash = sha256_of(result.content)
        result.strategy = pdf.engine
        result.meta = {
            "url": url,
            "pages": pdf.pages,
            "pdf_metadata": pdf.metadata,
            "pdf_supported": pdf.supported,
        }
        if not pdf.ok:
            result.error = pdf.error
        return result
