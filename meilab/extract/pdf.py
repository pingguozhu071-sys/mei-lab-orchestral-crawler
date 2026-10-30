"""PDF 文本提取(可选依赖 pymupdf)。

``pymupdf`` 是**可选**依赖(extras: ``pip install -e ".[pdf]"`)。
未安装时本模块不报错,而是返回"不支持 PDF"的可识别结果,
让框架整体继续可跑(降级 ≠ 失败)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: PDF 文件魔数
PDF_MAGIC = b"%PDF-"


@dataclass
class PdfExtraction:
    """PDF 提取结果。"""

    ok: bool = False
    title: str = ""
    content: str = ""
    pages: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    engine: str = "none"

    @property
    def supported(self) -> bool:
        """当前环境是否具备 PDF 提取能力。"""
        return self.engine != "none"


def pdf_available() -> bool:
    """当前环境是否装有 pymupdf。

    :return: 是否可用。
    """
    try:
        import pymupdf  # 第三方库:PDF 解析(可选依赖)  # noqa: F401
        return True
    except Exception:
        pass
    try:
        import fitz  # pymupdf 的旧模块名

        return hasattr(fitz, "open")
    except Exception:
        return False


def _load_backend() -> Any | None:
    """载入 PDF 后端模块,不可用返回 ``None``。"""
    try:
        import pymupdf  # 第三方库:PDF 解析(可选依赖)

        return pymupdf
    except Exception:
        pass
    try:
        import fitz  # pymupdf 的旧模块名

        return fitz
    except Exception:
        return None


def looks_like_pdf(data: bytes | None) -> bool:
    """判断字节流是不是 PDF。

    :param data: 字节流。
    :return: 是否是 PDF。
    """
    if not data:
        return False
    return bytes(data[:5]) == PDF_MAGIC


def extract_pdf(data: bytes | None, *, max_pages: int = 100) -> PdfExtraction:
    """提取 PDF 的文本。

    :param data: PDF 字节流。
    :param max_pages: 最多提取多少页。
    :return: 提取结果(缺依赖时返回 ``ok=False`` 且 ``error`` 说明原因)。
    """
    result = PdfExtraction()
    if not data:
        result.error = "空 PDF 数据"
        return result
    if not looks_like_pdf(data):
        result.error = "数据不是 PDF(缺少 %PDF- 魔数)"
        return result

    backend = _load_backend()
    if backend is None:
        result.error = "不支持 PDF:未安装 pymupdf,请执行 pip install -e \".[pdf]\""
        return result

    document = None
    try:
        document = backend.open(stream=bytes(data), filetype="pdf")
        metadata = document.metadata or {}
        result.metadata = {str(k): str(v) for k, v in metadata.items() if v is not None}
        result.title = str(metadata.get("title") or "").strip()

        pages: list[str] = []
        page_count = int(getattr(document, "page_count", 0))
        limit = min(page_count, max(1, int(max_pages)))
        for index in range(limit):
            page = document.load_page(index)
            text = page.get_text("text") or ""
            if text.strip():
                pages.append(text.strip())
        result.content = "\n\n".join(pages)
        result.pages = limit
        result.ok = bool(result.content)
        result.engine = "pymupdf"
        if not result.ok:
            result.error = "PDF 页面无文本(可能是扫描件,需要 OCR)"
    except Exception as exc:
        result.error = f"PDF 解析失败: {exc!r}"
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
    return result
