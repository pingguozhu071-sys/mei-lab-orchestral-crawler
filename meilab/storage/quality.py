"""内容指纹:SimHash 近重复检测 + 质量闸。

**SimHash**:把文本映射成 64 位指纹,汉明距离小 ⇒ 内容近似。
解决 SHA256 只能判"完全相同"的问题:同一篇文章换了个 URL / 改了标题,
SHA256 认不出,但 SimHash 能。

**质量闸**:太短、链接密度太高、几乎没有实词的页面判为低质量,拒收隔离,
不写进结果库——避免把"加载失败页""空壳页"当成果。

关于词哈希:SimHash 原始论文用 MD5,但那只是惯例——算法只要求词哈希
**均匀分布**,任何合适的哈希都可以。本实现用 SHA-256 截断到 8 字节,
分布性质与 MD5 截断等价,且避免弱哈希的碰撞隐患。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

#: SimHash 指纹位数
FINGERPRINT_BITS = 64

#: 判定"近似重复"的汉明距离阈值
NEAR_DUPLICATE_DISTANCE = 3

#: 质量闸:正文的最低字符数
MIN_TEXT_LENGTH = 200

#: 质量闸:链接字符占比上限
MAX_LINK_RATIO = 0.5

#: 质量闸:实词最低数量(中文按字、英文按词)
MIN_WORDS = 20

#: 链接标签的正则(算链接密度用)
_LINK_RE = re.compile(r"<a\s[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)

#: 标签正则
_TAG_RE = re.compile(r"<[^>]+>")

#: 中文单字
_CJK_RE = re.compile(r"[一-鿿]")

#: 英文单词
_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def tokenize(text: str) -> list[str]:
    """分词(中文按字、英文按词、其余按符号切分)。

    :param text: 原文。
    :return: 词元列表。
    """
    if not text:
        return []
    tokens: list[str] = []
    tokens.extend(_CJK_RE.findall(text))
    tokens.extend(word.lower() for word in _WORD_RE.findall(text))
    if not tokens:
        tokens = [chunk for chunk in re.split(r"\W+", text) if chunk]
    return tokens


def simhash(text: str, bits: int = FINGERPRINT_BITS) -> int:
    """计算文本的 SimHash 指纹。

    :param text: 原文。
    :param bits: 指纹位数。
    :return: 指纹整数。
    """
    tokens = tokenize(text)
    if not tokens:
        return 0
    vector = [0] * bits
    for token in tokens:
        # 词哈希只需均匀分布;用 SHA-256 截断到指纹所需的字节数
        digest = int.from_bytes(
            hashlib.sha256(token.encode("utf-8")).digest()[: bits // 8], "big"
        )
        for index in range(bits):
            if digest & (1 << index):
                vector[index] += 1
            else:
                vector[index] -= 1
    fingerprint = 0
    for index in range(bits):
        if vector[index] > 0:
            fingerprint |= 1 << index
    return fingerprint


def hamming_distance(left: int, right: int) -> int:
    """两个指纹的汉明距离。

    :param left: 左指纹。
    :param right: 右指纹。
    :return: 不同位数。
    """
    return bin(left ^ right).count("1")


def is_near_duplicate(
    left: int, right: int, threshold: int = NEAR_DUPLICATE_DISTANCE
) -> bool:
    """判断两个指纹是否近似。

    :param left: 左指纹。
    :param right: 右指纹。
    :param threshold: 距离阈值。
    :return: 是否近似。
    """
    if left == 0 or right == 0:
        return False
    return hamming_distance(left, right) <= threshold


@dataclass
class QualityResult:
    """质量评估结果。"""

    ok: bool = True
    score: float = 1.0
    reasons: list[str] = field(default_factory=list)
    text_length: int = 0
    link_ratio: float = 0.0
    word_count: int = 0


def assess_quality(html: str | None) -> QualityResult:
    """评估页面正文质量。

    :param html: 页面 HTML(或纯文本)。
    :return: 评估结果。
    """
    result = QualityResult()
    source = html or ""
    plain = _TAG_RE.sub(" ", source)
    plain = re.sub(r"\s+", " ", plain).strip()
    result.text_length = len(plain)

    link_chars = sum(len(_TAG_RE.sub(" ", m)) for m in _LINK_RE.findall(source))
    result.link_ratio = (link_chars / len(plain)) if plain else 1.0
    result.word_count = len(tokenize(plain))

    score = 1.0
    if result.text_length < MIN_TEXT_LENGTH:
        score -= 0.5
        result.reasons.append(f"正文过短({result.text_length} < {MIN_TEXT_LENGTH})")
    if result.link_ratio > MAX_LINK_RATIO:
        score -= 0.3
        result.reasons.append(f"链接密度过高({result.link_ratio:.0%})")
    if result.word_count < MIN_WORDS:
        score -= 0.2
        result.reasons.append(f"实词过少({result.word_count} < {MIN_WORDS})")

    result.score = max(0.0, min(1.0, score))
    result.ok = not result.reasons
    return result
