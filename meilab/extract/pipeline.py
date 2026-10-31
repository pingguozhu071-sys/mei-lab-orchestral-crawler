"""提取管线:按内容类型把响应分发给对应的提取器。

输入是 Worker 拿到的原始响应(``Content-Type`` + 字节流),
输出是统一的 :class:`ExtractResult`:标题 + 正文 + 内容哈希 + 元数据。

分派依据的优先级:
1. 响应头 ``Content-Type``;
2. 响应头缺失或不可信时,**嗅探字节流**(PDF 魔数 / JSON 首字符 / HTML 标签)。
"""

from __future__ import annotations

import hashlib
import json
import re
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


#: 常见别名 → Python 编解码器名。gb2312/gbk 统一用超集 gb18030,
#: 避免"声明 gb2312 实际含 GBK 扩展字"时个别字解码失败。
_CHARSET_ALIASES: dict[str, str] = {
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "gb18030": "gb18030",
    "shift-jis": "shift_jis",
    "shift_jis": "shift_jis",
    "sjis": "shift_jis",
    "euc-jp": "euc_jp",
    "euc_jp": "euc_jp",
    "big5": "big5",
    "big5-hkscs": "big5",
    "utf-8": "utf-8",
    "utf8": "utf-8",
}

#: HTML 里的 <meta charset> 声明(只看前 2KB,与浏览器行为一致)
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_-]+)""", re.IGNORECASE
)


def _declared_charset(content_type: str, body: bytes | None) -> str:
    """取声明的字符集:优先响应头,缺失时看 HTML 的 meta charset。

    :param content_type: 响应头 Content-Type。
    :param body: 响应体字节。
    :return: 规范化后的编解码器名;没有声明返回空串。
    """
    name = ""
    match = re.search(r"charset\s*=\s*\"?([A-Za-z0-9_-]+)", content_type or "", re.IGNORECASE)
    if match:
        name = match.group(1)
    elif body:
        meta = _META_CHARSET_RE.search(bytes(body[:2048]))
        if meta:
            name = meta.group(1).decode("ascii", errors="ignore")
    return _CHARSET_ALIASES.get(name.lower(), name.lower())


def _decode(body: bytes | str | None, content_type: str = "") -> str:
    """把字节流解码为文本。

    编码取声明的 charset(响应头优先,meta charset 兜底);不声明按 UTF-8。
    非字节输入原样返回。这一步修掉的问题:gb2312 / Shift_JIS / EUC-JP 页面
    按 UTF-8 强解会得到满屏 U+FFFD(见工程经验记录 G-17)。

    :param body: 响应体。
    :param content_type: 响应头 Content-Type(用于取 charset)。
    :return: 解码后的文本。
    """
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    data = bytes(body)
    charset = _declared_charset(content_type, data)
    if charset and charset not in ("utf-8", "utf8", "ascii"):
        try:
            return data.decode(charset, errors="replace")
        except LookupError:
            pass  # 声明的编码器不存在,退回 UTF-8
    return data.decode("utf-8", errors="replace")


def _structured_fields(data: Any) -> dict[str, Any]:
    """从 JSON-LD / hydration state 里挑出有价值的标量字段。

    :param data: 结构化数据。
    :return: 字段字典(值为标量);没有则返回空字典。
    """
    interesting = (
        "@type", "name", "headline", "description", "datePublished",
        "dateCreated", "uploadDate", "duration", "author", "keywords",
        "genre", "interactionStatistic", "viewCount", "interactionCount",
    )
    out: dict[str, Any] = {}

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, list):
            for item in node[:10]:
                visit(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in interesting and key not in out:
                if isinstance(value, (str, int, float, bool)):
                    out[key] = value
                elif isinstance(value, dict):
                    name = value.get("name")
                    if isinstance(name, str):
                        out[key] = name
                    else:
                        out[key] = _compact(value)
                elif isinstance(value, list):
                    compact = _compact(value)
                    if compact:
                        out[key] = compact
            elif key in ("mainEntity", "video", "article", "author", "creator"):
                visit(value, depth + 1)

    visit(data)
    return out


def _compact(value: Any, limit: int = 600) -> Any:
    """把嵌套结构压成简短可读的形式。

    :param value: 任意结构。
    :param limit: 字符串截断长度。
    :return: 压缩后的值。
    """
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


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
            return self._extract_json(result, body, content_type or "", url)
        if kind == "pdf":
            return self._extract_pdf(result, body, url)
        if kind == "html":
            return self._extract_html(result, body, content_type or "", url)
        if kind == "text":
            text = _decode(body, content_type or "").strip()
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

    def _extract_json(self, result: ExtractResult, body: Any, content_type: str, url: str) -> ExtractResult:
        """JSON 提取。"""
        parsed = parse_json_response(_decode(body, content_type))
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

    def _extract_html(self, result: ExtractResult, body: Any, content_type: str, url: str) -> ExtractResult:
        """HTML 提取:先走正文提取,再尝试结构化数据(JSON-LD / hydration state)。"""
        html = _decode(body, content_type)
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
            fields = _structured_fields(structured.data)
            if fields:
                result.meta["structured"] = fields
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
