"""B 站 WBI 签名。

B 站部分接口(评论、动态等)要求 ``w_rid`` / ``wts`` 参数,算法是公开的:
1. 从 ``/x/web-interface/nav`` 取 ``wbi_img.img_url`` 与 ``sub_url``,各取文件名;
2. 按固定的置换表把两把 key 交错混合,取前 32 位作为 mixin key;
3. 把参数按 key 排序、加上 ``wts``,拼上 mixin key,取 MD5 作为 ``w_rid``。

签名失败时接口会返回 -403;本模块在拿不到 key 时**明确报错**,不静默降级。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Any

#: mixin key 置换表(B 站固定值)
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

#: nav 接口(取 wbi key)
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

#: mixin key 缓存(key 对当天有效)
_MIXIN_CACHE: dict[str, float] = {}


def _extract_key(url: str) -> str:
    """从图片 URL 里取出 key(文件名去掉扩展名)。"""
    name = url.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def get_mixin_key(timeout: float = 15.0, referer: str = "https://www.bilibili.com/") -> str:
    """取 mixin key(带当天缓存)。

    :param timeout: 超时(秒)。
    :param referer: Referer 头。
    :return: 32 字节 mixin key。
    :raises RuntimeError: 取不到 key 时明确报错。
    """
    cached = _MIXIN_CACHE.get("key")
    if cached and time.time() - cached < 3600:
        return cached  # type: ignore[return-value]

    from curl_cffi import requests as curl_requests  # 第三方库:TLS 指纹模拟

    from ..workers.http import ensure_ascii_ca_bundle

    ensure_ascii_ca_bundle()
    response = curl_requests.get(
        NAV_API,
        timeout=timeout,
        impersonate="chrome",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
                 "Referer": referer},
    )
    payload = response.json()
    wbi = (payload.get("data") or {}).get("wbi_img") or {}
    img_url = str(wbi.get("img_url") or "")
    sub_url = str(wbi.get("sub_url") or "")
    if not img_url or not sub_url:
        raise RuntimeError(f"取不到 wbi key(nav code={payload.get('code')})")

    img_key = _extract_key(img_url)
    sub_key = _extract_key(sub_url)
    mixin = "".join(
        (img_key + sub_key)[index] for index in MIXIN_KEY_ENC_TAB
    )[:32]
    _MIXIN_CACHE["key"] = time.time()
    return mixin


def sign(params: dict[str, Any], mixin_key: str) -> dict[str, Any]:
    """给参数加上 ``wts`` 与 ``w_rid``。

    :param params: 原始参数。
    :param mixin_key: mixin key。
    :return: 签名后的参数(新增 ``wts`` / ``w_rid``)。
    """
    signed = {k: v for k, v in params.items() if v is not None}
    signed["wts"] = int(time.time())
    ordered = sorted(signed.items(), key=lambda kv: str(kv[0]))
    query = urllib.parse.urlencode(ordered)
    # MD5 是 B 站接口**协议强制**的签名算法(服务端按同样规则校验),
    # 不是用来做完整性保护的安全原语;换成 SHA-256 会直接被服务端拒绝(-403)。
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed
