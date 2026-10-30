"""验证码分支:检测 → 识别 → 回填重试。

框架只在**页面真的出现验证码挑战**时才介入(判据查结构不查文本,见
``router/sniff.py::detect_anti_bot_signals``),识别能力用已装的
``ddddocr``(OCR)与 ``opencv``(滑块缺口检测)。

能力缺失时的行为(依工程书 §10.4 降级矩阵):
* ddddocr 不可用 → 返回"不支持验证码识别",任务直接入死信队列,**不静默通过**;
* 识别成功 → 返回识别文本,由调用方回填并重试一次(不无限重试)。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CaptchaResult:
    """一次验证码识别的结果。"""

    ok: bool = False
    text: str = ""
    kind: str = ""  # text / slider / unknown
    engine: str = "none"
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def ocr_available() -> bool:
    """ddddocr 是否可用。"""
    try:
        import ddddocr  # 第三方库:OCR 识别  # noqa: F401

        return True
    except Exception:
        return False


def cv2_available() -> bool:
    """opencv 是否可用。"""
    try:
        import cv2  # 第三方库:图像处理  # noqa: F401

        return True
    except Exception:
        return False


def _decode_image(data: bytes | str) -> bytes:
    """把 base64 字符串或原始字节统一成字节。"""
    if isinstance(data, bytes):
        return data
    text = (data or "").strip()
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        return base64.b64decode(text, validate=False)
    except Exception:
        return b""


def recognize_text(image: bytes | str) -> CaptchaResult:
    """识别图片验证码(纯 OCR)。

    :param image: 图片字节或 base64 字符串。
    :return: 识别结果。
    """
    if not ocr_available():
        return CaptchaResult(
            error="不支持验证码识别:未安装 ddddocr,请执行 pip install ddddocr"
        )
    payload = _decode_image(image)
    if not payload:
        return CaptchaResult(error="验证码图片为空")
    try:
        import ddddocr  # 第三方库:OCR 识别

        reader = ddddocr.DdddOcr(show_ad=False)
        text = str(reader.classification(payload) or "").strip()
        if not text:
            return CaptchaResult(error="未能识别出验证码文本", engine="ddddocr")
        return CaptchaResult(ok=True, text=text, kind="text", engine="ddddocr")
    except Exception as exc:
        return CaptchaResult(error=f"验证码识别失败: {exc!r}", engine="ddddocr")


def find_slider_gap(background: bytes | str, piece: bytes | str) -> CaptchaResult:
    """用 OpenCV 找滑块缺口位置(返回 x 坐标)。

    :param background: 背景图字节或 base64。
    :param piece: 滑块图字节或 base64。
    :return: 识别结果(``meta['gap_x']`` 为横坐标)。
    """
    if not cv2_available():
        return CaptchaResult(
            error="不支持滑块识别:未安装 opencv-python-headless,请执行 pip install opencv-python-headless"
        )
    bg = _decode_image(background)
    piece_data = _decode_image(piece)
    if not bg:
        return CaptchaResult(error="滑块背景图为空")
    try:
        import cv2  # 第三方库:图像处理
        import numpy as np  # 第三方库:数值底座

        bg_image = cv2.imdecode(np.frombuffer(bg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if bg_image is None:
            return CaptchaResult(error="滑块背景图无法解码")
        gray = cv2.cvtColor(bg_image, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 100, 200)

        if piece_data:
            piece_image = cv2.imdecode(np.frombuffer(piece_data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if piece_image is not None:
                template = cv2.matchTemplate(edges, piece_image, cv2.TM_CCOEFF_NORMED)
                _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(template)
                if max_val > 0.4:
                    return CaptchaResult(
                        ok=True, kind="slider", engine="opencv",
                        meta={"gap_x": int(max_loc[0]), "score": float(max_val)},
                    )
                return CaptchaResult(
                    kind="slider", engine="opencv",
                    error=f"滑块匹配置信度偏低({max_val:.2f})",
                    meta={"gap_x": int(max_loc[0]), "score": float(max_val)},
                )

        # 没有滑块图:退化为"边缘最密集的竖直条"启发
        columns = edges.sum(axis=0)
        gap_x = int(columns.argmax())
        return CaptchaResult(
            ok=True, kind="slider", engine="opencv",
            meta={"gap_x": gap_x, "method": "edge_density"},
        )
    except Exception as exc:
        return CaptchaResult(error=f"滑块识别失败: {exc!r}", engine="opencv")


def solve(
    html: str | None = None,
    *,
    image: bytes | str | None = None,
    background: bytes | str | None = None,
    piece: bytes | str | None = None,
) -> CaptchaResult:
    """识别一次验证码挑战。

    优先走滑块(有背景图时),否则走纯 OCR。

    :param html: 触发挑战的页面 HTML(仅用于记录上下文)。
    :param image: 验证码图片。
    :param background: 滑块背景图。
    :param piece: 滑块图。
    :return: 识别结果。
    """
    if background:
        result = find_slider_gap(background, piece or b"")
        if result.ok:
            result.meta["context_len"] = len(html or "")
            return result
        # 滑块失败再试 OCR
    if image:
        result = recognize_text(image)
        result.meta["context_len"] = len(html or "")
        return result
    return CaptchaResult(error="没有可识别的验证码图片")
