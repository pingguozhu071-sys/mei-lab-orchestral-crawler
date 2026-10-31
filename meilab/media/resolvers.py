"""五平台媒体解析器。

**分发原则**(与 kiana 一致):平台专用解析器拿不到就**保留原 URL 走通用通道**,
而不是报错终止。

* **B 站**:原生 API(`view` 拿 cid → `playurl` 拿直链),匿名即可拿到 720P;
  另提供评论接口(`mel comments`)。
* **抖音 / 小红书 / 快手 / 网易云**:这四家的直链都需要请求签名
  (a_bogus / x-s / weijia),自行复刻签名不稳定且易被平台变更打断;
  因此**统一委托给通用 yt-dlp 通道**(yt-dlp 自带这四家的 extractor),
  解析失败时明确报错,不冒充成功。

本模块只做"解析",下载由 :mod:`meilab.media.downloader` 负责。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from ..core.types import MediaItem, MediaKind, MediaPlatform
from ..workers.http import ensure_ascii_ca_bundle

#: B 站播放页的 Referer(缺失会被 403)
BILIBILI_REFERER = "https://www.bilibili.com/"

#: 从 URL 里取 BV 号
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")

#: 从 URL 里取 aid(av 号)
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)


def _get_json(
    url: str,
    *,
    referer: str = "",
    timeout: float = 25.0,
    cookie_header: str = "",
) -> dict[str, Any]:
    """抓一个 JSON 接口。

    :param url: 接口地址。
    :param referer: Referer 头。
    :param timeout: 超时(秒)。
    :param cookie_header: 可选的 Cookie 头(用户自备 cookie)。
    :return: 解析后的 JSON;失败时返回 ``{"code": -1, "message": ...}``。
    """
    try:
        from curl_cffi import requests as curl_requests  # 第三方库:TLS 指纹模拟

        ensure_ascii_ca_bundle()
        headers = {"User-Agent": _UA}
        if referer:
            headers["Referer"] = referer
        if cookie_header:
            headers["Cookie"] = cookie_header
        response = curl_requests.get(
            url, timeout=timeout, impersonate="chrome", headers=headers
        )
        return response.json()
    except Exception as exc:
        return {"code": -1, "message": f"请求失败: {exc!r}"}


#: 统一的 UA(与 HTTP 通道一致)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def bilibili_video_id(url: str) -> str:
    """从 B 站 URL 里取 BV 号。

    :param url: 页面 URL。
    :return: BV 号;取不到返回空串。
    """
    match = _BV_RE.search(url)
    return match.group(1) if match else ""


def bilibili_aid(url: str) -> str:
    """从 B 站 URL 里取 av 号。

    :param url: 页面 URL。
    :return: aid;取不到返回空串。
    """
    match = _AV_RE.search(url)
    return match.group(1) if match else ""


def bilibili_resolve(
    url: str,
    *,
    cookies: str | None = None,
    timeout: float = 25.0,
    quality: int = 64,
) -> MediaItem | None:
    """解析 B 站视频直链(原生 API,匿名可用)。

    :param url: 视频页 URL。
    :param cookies: 用户自备 cookie(有则带上,可提升清晰度)。
    :param timeout: 超时(秒)。
    :param quality: 请求的清晰度(64 = 720P,16 = 360P)。
    :return: 媒体条目;解析失败返回 ``None``。
    """
    bvid = bilibili_video_id(url)
    if not bvid:
        return None

    cookie_value = ""
    if cookies:
        try:
            from .cookies import cookie_header

            cookie_value = cookie_header(cookies)
        except Exception:
            cookie_value = ""  # cookie 不可用时按匿名继续

    view = _get_json(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        referer=BILIBILI_REFERER, timeout=timeout, cookie_header=cookie_value,
    )
    if view.get("code") != 0:
        return None
    data = view.get("data") or {}
    cid = data.get("cid")
    if not cid:
        return None

    play = _get_json(
        f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}"
        f"&qn={quality}&fnval=1&fourk=1",
        referer=BILIBILI_REFERER, timeout=timeout, cookie_header=cookie_value,
    )
    if play.get("code") != 0:
        return None
    play_data = play.get("data") or {}
    durl = play_data.get("durl") or []
    if not durl:
        return None

    duration = float(data.get("duration") or 0.0)
    streams = []
    for part in durl:
        stream_url = str(part.get("url") or "")
        if not stream_url:
            continue
        size = int(part.get("size") or 0)
        streams.append(
            {
                "url": stream_url,
                "size": size,
                "quality": play_data.get("quality"),
                "quality_desc": (play_data.get("accept_description") or [""])[0],
                "ext": "mp4",
                "bitrate": (size * 8 / duration) if duration else 0,
            }
        )
    if not streams:
        return None

    return MediaItem(
        source=url,
        media_id=bvid,
        kind=MediaKind.VIDEO,
        platform=MediaPlatform.BILIBILI,
        streams=streams,
        method="bilibili_api",
        title=str(data.get("title") or ""),
        duration=duration,
        needs_cookie=not cookies,
        meta={
            "cid": cid,
            "aid": data.get("aid"),
            "owner": ((data.get("owner") or {}).get("name") or ""),
            "accept_quality": play_data.get("accept_quality") or [],
        },
    )


def bilibili_comments(
    url: str,
    *,
    page: int = 1,
    cookies: str | None = None,
    timeout: float = 25.0,
) -> tuple[list[dict[str, Any]], str]:
    """抓 B 站视频评论。

    旧接口 ``/x/v2/reply`` 已被平台弃用(返回 0 条),现在走
    ``/x/v2/reply/wbi/main`` —— 需要 **WBI 签名**;并且平台对匿名请求返回
    ``-403 访问权限不足``,此时需要**用户自备 cookie**(含 SESSDATA)才能读到评论。

    :param url: 视频页 URL。
    :param page: 页码(从 1 开始)。
    :param cookies: 用户自备 cookie 文件路径。
    :param timeout: 超时(秒)。
    :return: ``(评论列表, 说明)``;说明里写明失败原因(如需登录态)。
    """
    cookie_value = ""
    if cookies:
        try:
            from .cookies import cookie_header

            cookie_value = cookie_header(cookies)
        except Exception as exc:
            return [], f"cookie 不可用: {exc!r}"

    bvid = bilibili_video_id(url)
    if not bvid:
        return [], "URL 里没有 BV 号"
    view = _get_json(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        referer=BILIBILI_REFERER, timeout=timeout, cookie_header=cookie_value,
    )
    aid = ((view.get("data") or {}).get("aid")) if view.get("code") == 0 else None
    if not aid:
        return [], f"取不到 aid(code={view.get('code')})"

    # WBI 签名(拿不到 key 就明确降级,不静默返回空)
    try:
        from .wbi import get_mixin_key, sign

        mixin = get_mixin_key(timeout=timeout)
        params = sign(
            {
                "oid": aid,
                "type": 1,
                "mode": 3,
                "pagination_str": json.dumps({"offset": ""}, ensure_ascii=False),
                "plat": 1,
                "seek_rpid": "",
                "web_location": "1315875",
            },
            mixin,
        )
    except Exception as exc:
        return [], f"WBI 签名失败: {exc!r}"

    query = urllib.parse.urlencode(params)
    reply = _get_json(
        f"https://api.bilibili.com/x/v2/reply/wbi/main?{query}",
        referer=BILIBILI_REFERER, timeout=timeout, cookie_header=cookie_value,
    )
    if reply.get("code") != 0:
        reason = str(reply.get("message") or reply.get("code"))
        if reply.get("code") == -403:
            reason += "(平台已限制匿名访问;需要用户自备 cookie 才能读取评论)"
        return [], f"评论接口返回 {reason}"
    data = reply.get("data") or {}
    replies = data.get("replies") or []
    out: list[dict[str, Any]] = []
    for item in replies:
        member = (item.get("member") or {})
        content = (item.get("content") or {})
        out.append(
            {
                "rpid": item.get("rpid"),
                "author": member.get("uname"),
                "message": content.get("message"),
                "like": item.get("like"),
                "ctime": item.get("ctime"),
                "replies": [
                    {
                        "author": ((sub.get("member") or {}).get("uname")),
                        "message": ((sub.get("content") or {}).get("message")),
                    }
                    for sub in (item.get("replies") or [])[:5]
                ],
            }
        )
    if not out:
        return [], "该视频没有评论(平台返回 0 条)"
    return out, f"取到 {len(out)} 条评论"


#: 委托给通用 yt-dlp 通道的平台
_YTDLP_DELEGATED = (
    MediaPlatform.DOUYIN,
    MediaPlatform.XIAOHONGSHU,
    MediaPlatform.KUAISHOU,
    MediaPlatform.NETEASE_MUSIC,
)


def delegated_resolve(
    url: str,
    platform: MediaPlatform,
    *,
    cookies: str | None = None,
    timeout: float = 120.0,
) -> MediaItem | None:
    """用通用 yt-dlp 通道解析四家平台的媒体。

    :param url: 页面 URL。
    :param platform: 平台标识。
    :param cookies: 用户自备 cookie。
    :param timeout: 超时(秒)。
    :return: 媒体条目;解析失败返回 ``None``。
    """
    from . import ytdlp_channel

    result = ytdlp_channel.resolve(url, cookies=cookies, timeout=timeout)
    if not result.ok:
        return None
    info = result.info
    streams = [
        {
            "url": fmt.get("url", ""),
            "size": 0,
            "quality": fmt.get("format_note", ""),
            "ext": fmt.get("ext", "mp4"),
            "bitrate": float(fmt.get("tbr") or 0) * 1000,
            "format_id": fmt.get("format_id"),
        }
        for fmt in (info.get("formats") or [])
        if fmt.get("url") and fmt.get("vcodec") != "none"
    ]
    return MediaItem(
        source=url,
        media_id=str(info.get("id") or ""),
        kind=MediaKind.AUDIO if info.get("vcodec") == "none" else MediaKind.VIDEO,
        platform=platform,
        streams=streams,
        method="ytdlp_delegate",
        title=str(info.get("title") or ""),
        duration=float(info.get("duration") or 0.0),
        meta={
            "extractor": info.get("extractor_key") or info.get("extractor") or "",
            "uploader": info.get("uploader") or "",
            "webpage_url": info.get("webpage_url") or url,
        },
    )


def resolve_media(
    url: str,
    *,
    platform: MediaPlatform | None = None,
    cookies: str | None = None,
    timeout: float = 120.0,
) -> tuple[MediaItem | None, str]:
    """解析一个媒体 URL(平台优先,失败回退通用通道)。

    :param url: 媒体页面 URL 或直链。
    :param platform: 已知平台;为 ``None`` 时自动判断。
    :param cookies: 用户自备 cookie。
    :param timeout: 超时(秒)。
    :return: ``(媒体条目, 说明)``;条目为 ``None`` 时说明里是失败原因。
    """
    from . import ytdlp_channel

    target = platform or detect_platform(url)
    if target is MediaPlatform.BILIBILI:
        item = bilibili_resolve(url, cookies=cookies)
        if item is not None:
            return item, "B 站原生 API"
        # 原生失败 → 回退通用通道
        item = delegated_resolve(
            url, MediaPlatform.BILIBILI, cookies=cookies, timeout=timeout
        )
        if item is not None:
            return item, "B 站原生 API 失败,已回退通用 yt-dlp 通道"
        return None, "B 站解析失败(原生 API 与通用通道都没拿到直链)"

    if target in _YTDLP_DELEGATED:
        item = delegated_resolve(url, target, cookies=cookies, timeout=timeout)
        if item is not None:
            return item, f"{target.value} 走通用 yt-dlp 通道"
        return None, f"{target.value} 解析失败(通用通道没能拿到直链)"

    # generic:直接通用通道
    item = delegated_resolve(url, MediaPlatform.GENERIC, cookies=cookies, timeout=timeout)
    if item is not None:
        return item, "通用 yt-dlp 通道"
    return None, "通用 yt-dlp 通道解析失败(该站点可能不受支持或需要 cookie)"


def detect_platform(url: str) -> MediaPlatform:
    """根据 URL 判断平台。

    :param url: 媒体 URL。
    :return: 平台标识。
    """
    from ..router.sniff import detect_media

    detected = detect_media(url)
    if detected is None:
        return MediaPlatform.GENERIC
    name = detected[0]
    try:
        return MediaPlatform(name)
    except ValueError:
        return MediaPlatform.GENERIC
