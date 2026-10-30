"""验证码分支单测(离线):能力探测、缺依赖降级、OCR 与滑块路径。"""

from __future__ import annotations

import base64
import zlib

from meilab.stealth.captcha import (
    cv2_available,
    find_slider_gap,
    ocr_available,
    recognize_text,
    solve,
)


def _make_png(width: int = 40, height: int = 20) -> bytes:
    """构造一张最小的合法 PNG(灰度,便于本地测试用)。"""
    import struct

    raw = b"".join(b"\x00" + bytes([(x * 6) % 256] * width) for x in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_capability_probes() -> None:
    """能力探测:本环境装了 ddddocr 与 opencv。"""
    assert ocr_available() is True
    assert cv2_available() is True


def test_recognize_text_rejects_empty() -> None:
    """空图片要明确报错,不返回成功。"""
    result = recognize_text(b"")
    assert result.ok is False
    assert result.error


def test_slider_gap_rejects_empty() -> None:
    """空背景图要明确报错。"""
    result = find_slider_gap(b"", b"")
    assert result.ok is False
    assert result.error


def test_slider_gap_on_real_image() -> None:
    """真实 PNG 上跑滑块缺口检测(本地构造,无网络)。"""
    png = _make_png()
    result = find_slider_gap(png, None)
    assert result.ok is True
    assert result.kind == "slider"
    assert "gap_x" in result.meta


def test_solve_without_image_reports_error() -> None:
    """没有任何图片时明确报错。"""
    result = solve("<html></html>")
    assert result.ok is False
    assert result.error


def test_solve_prefers_slider_when_background_given() -> None:
    """给了背景图就走滑块路径。"""
    png = _make_png()
    result = solve("", background=base64.b64encode(png).decode(), piece=None)
    assert result.kind == "slider"
    assert result.meta.get("gap_x") is not None
