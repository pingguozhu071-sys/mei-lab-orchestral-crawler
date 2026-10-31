"""媒体下载编排:解析 → 选流 → 下载 → 落记录。

三条下载路径,按可用性自动选择:

1. **m3u8 直链** → 自研 HLS 下载器(支持 AES-128 分片解密);
2. **平台原生直链**(如 B 站 playurl) → 自研可续传直链下载器(Range + ``.part`` + 原子改名);
3. **其余一切** → 通用 yt-dlp 通道。

所有直链在下载前都要过 SSRF 校验;失败一律给出可读原因,不产出半成品。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.types import MediaItem, MediaPlatform
from ..workers.base import check_url_safe
from ..workers.http import ensure_ascii_ca_bundle
from . import m3u8_downloader, ytdlp_channel
from .resolvers import detect_platform, resolve_media


@dataclass
class MediaResult:
    """一次媒体抓取的结果。"""

    ok: bool = False
    url: str = ""
    title: str = ""
    platform: str = "generic"
    method: str = ""
    files: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    bytes: int = 0
    duration: float = 0.0
    error: str = ""
    note: str = ""
    elapsed: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """转成字典。"""
        return {
            "ok": self.ok, "url": self.url, "title": self.title,
            "platform": self.platform, "method": self.method,
            "files": list(self.files), "partial": list(self.partial),
            "bytes": self.bytes,
            "duration": self.duration, "error": self.error,
            "note": self.note, "elapsed": round(self.elapsed, 2),
        }


def _safe_filename(title: str, fallback: str, ext: str) -> str:
    """生成安全的文件名(去掉路径分隔符与控制字符,限制长度)。"""
    cleaned = "".join(
        ch for ch in (title or "") if ch.isalnum() or ch in (" ", "-", "_", ".", "·", "【", "】")
    ).strip()
    cleaned = cleaned[:80] or fallback
    return f"{cleaned}.{ext}"


def download_direct(
    url: str,
    target: str | os.PathLike[str],
    *,
    timeout: float = 120.0,
    referer: str = "",
    retries: int = 3,
) -> dict[str, Any]:
    """下载一个直链(支持断点续传与重试)。

    续传策略:先看本地 ``.part`` 大小,带 ``Range: bytes=<size>-`` 请求;
    服务端返回 206 就追加,返回 200(不支持 Range)就从头重下。

    :param url: 直链。
    :param target: 目标文件路径。
    :param timeout: 单次请求超时(秒)。
    :param referer: Referer 头。
    :param retries: 网络失败时的重试次数。
    :return: ``{"ok", "file", "bytes", "resumed", "error"}``。
    """
    from curl_cffi import requests as curl_requests  # 第三方库:TLS 指纹模拟

    result: dict[str, Any] = {
        "ok": False, "file": str(target), "bytes": 0, "resumed": False, "error": "",
    }
    safe, why = check_url_safe(url)
    if not safe:
        result["error"] = f"SSRF 防护拒绝该直链: {why}"
        return result

    ensure_ascii_ca_bundle()
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    part = target_path.with_suffix(target_path.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0"}
    if referer:
        headers["Referer"] = referer
    if have:
        headers["Range"] = f"bytes={have}-"

    response = None
    last_error = ""
    for attempt in range(max(1, retries)):
        try:
            response = curl_requests.get(
                url, timeout=timeout, impersonate="chrome", headers=headers, stream=True
            )
            break
        except Exception as exc:
            last_error = f"直链请求失败: {exc!r}"
            response = None
            time.sleep(min(5.0, 1.5 * (attempt + 1)))
    if response is None:
        result["error"] = last_error
        return result

    if response.status_code == 416 and have:
        # 已经下完
        os.replace(part, target_path)
        result.update({"ok": True, "bytes": target_path.stat().st_size, "resumed": True})
        return result

    mode = "ab" if (have and response.status_code == 206) else "wb"
    if mode == "wb":
        have = 0
    try:
        with part.open(mode) as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if chunk:
                    handle.write(chunk)
    except Exception as exc:
        result["error"] = f"写入失败(已保留 .part 便于续传): {exc!r}"
        return result

    total = response.headers.get("content-length")
    try:
        expected = int(total) + have if total else 0
    except (TypeError, ValueError):
        expected = 0
    actual = part.stat().st_size
    if expected and actual != expected:
        result["error"] = f"字节数校验失败: 期望 {expected},实际 {actual}(保留 .part 可续传)"
        return result

    os.replace(part, target_path)
    result.update({"ok": True, "bytes": actual, "resumed": bool(have)})
    return result


def _download_dash_item(
    item: MediaItem,
    out_dir: Path,
    *,
    audio_only: bool,
    timeout: float,
) -> dict[str, Any]:
    """下载一个 DASH 条目(视频轨 + 音频轨 + ffmpeg 合流)。

    :param item: 媒体条目(``method`` 为 ``bilibili_dash``)。
    :param out_dir: 输出目录。
    :param audio_only: 只取音频。
    :param timeout: 单次请求超时(秒)。
    :return: ``{"ok", "file", "bytes", "error"}``。
    """
    from . import dash as dash_module

    streams = {str(s.get("kind") or "video"): s for s in item.streams if s.get("url")}
    video = streams.get("video") or {}
    audio = streams.get("audio") or {}
    if not video.get("url"):
        return {"ok": False, "file": "", "bytes": 0, "error": "没有可用的视频轨"}

    dash_meta = (item.meta or {}).get("dash") or {}
    tracks = dash_module.DashTracks(
        video={"url": str(video["url"]), "bitrate": video.get("bitrate", 0)},
        audio=(
            {"url": str(audio["url"]), "bitrate": audio.get("bitrate", 0)}
            if audio.get("url") else {}
        ),
        quality=int(dash_meta.get("quality") or 0),
        quality_desc=str(dash_meta.get("quality_desc") or ""),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        codecs=str(video.get("codecs") or ""),
        duration=item.duration,
        video_backups=[str(u) for u in (dash_meta.get("video_backups") or [])],
        audio_backups=[str(u) for u in (dash_meta.get("audio_backups") or [])],
    )

    ext = "m4a" if audio_only else "mp4"
    target = out_dir / _safe_filename(item.title or item.media_id, item.media_id, ext)
    # CDN 在 bilivideo.com,与 cookie 的域不同 —— 只带 Referer,不带 cookie
    outcome = dash_module.download_dash(
        tracks, target,
        referer="https://www.bilibili.com/",
        timeout=timeout,
        audio_only=audio_only,
    )
    outcome["quality_desc"] = tracks.quality_desc
    return outcome


def download_media(
    url: str,
    output_dir: str | os.PathLike[str],
    *,
    cookies: str | None = None,
    audio_only: bool = False,
    prefer: str = "auto",
    timeout: float = 120.0,
    queue: Any | None = None,
    quality: int = 0,
) -> MediaResult:
    """抓取一个媒体 URL(解析 + 下载)。

    :param url: 媒体页面 URL 或直链。
    :param output_dir: 输出目录。
    :param cookies: 用户自备 cookie 文件路径。
    :param audio_only: 只要音频。
    :param prefer: ``auto`` / ``native`` / ``ytdlp``——优先用哪种解析路径。
    :param timeout: 解析超时(秒)。
    :param queue: 可选的 :class:`~meilab.core.queue.TaskQueue`,用于落下载记录。
    :param quality: 请求的清晰度 id(``0`` = 取账号可用最高档)。
    :return: 抓取结果。
    """
    started = time.time()
    result = MediaResult(url=url)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    platform = detect_platform(url)
    result.platform = platform.value

    record_id = ""
    if queue is not None:
        record_id = queue.upsert_media(
            url, platform=platform.value, status="resolving"
        )

    # 1) m3u8 直链
    if url.lower().split("?")[0].endswith(".m3u8"):
        target = out_dir / _safe_filename(url.rsplit("/", 1)[-1], "stream", "mp4")
        m3u8_result = m3u8_downloader.download_playlist(url, target)
        result.method = "m3u8"
        result.ok = bool(m3u8_result.get("ok"))
        result.error = str(m3u8_result.get("error") or "")
        if result.ok:
            result.files = [str(m3u8_result.get("file"))]
            result.bytes = int(m3u8_result.get("bytes") or 0)
        if queue is not None and record_id:
            queue.update_media(
                record_id,
                status="done" if result.ok else "failed",
                method="m3u8",
                file_path=result.files[0] if result.files else "",
                file_size=result.bytes,
                error=result.error,
            )
        result.elapsed = time.time() - started
        return result

    # 2) 解析
    item: MediaItem | None = None
    note = ""
    if prefer == "ytdlp":
        item = None
        note = "按要求只走通用 yt-dlp 通道"
    else:
        item, note = resolve_media(
            url, platform=platform, cookies=cookies, timeout=timeout, quality=quality
        )
        if item is None and prefer == "native":
            note = "原生解析失败(按要求不回落通用通道)"
    result.note = note

    if item is None:
        if prefer != "ytdlp":
            # 3) 原生解析失败 → 通用 yt-dlp 通道
            item, note2 = resolve_media(
                url, platform=MediaPlatform.GENERIC, cookies=cookies, timeout=timeout
            )
            result.note = (note + ";" if note else "") + note2
        if item is None:
            result.error = result.note or "解析失败:未能拿到媒体直链"
            if queue is not None and record_id:
                queue.update_media(record_id, status="failed", error=result.error)
            result.elapsed = time.time() - started
            return result

    result.title = item.title
    result.duration = item.duration
    result.method = item.method
    result.platform = item.platform.value

    # 4) 下载:DASH 分轨走合流器,原生单文件直链走续传下载器,其余交给通用通道
    if item.method == "bilibili_dash":
        dash_result = _download_dash_item(item, out_dir, audio_only=audio_only, timeout=timeout)
        result.ok = bool(dash_result.get("ok"))
        result.error = str(dash_result.get("error") or "")
        if result.ok:
            result.files = [str(dash_result.get("file"))]
            result.bytes = int(dash_result.get("bytes") or 0)
        result.note = (
            f"{result.note};" if result.note else ""
        ) + f"DASH 合流:{dash_result.get('quality_desc') or '未知清晰度'}"
    elif item.method == "bilibili_api" and item.best_stream:
        stream = item.best_stream
        ext = str(stream.get("ext") or "mp4")
        target = out_dir / _safe_filename(item.title or item.media_id, item.media_id, ext)
        direct = download_direct(
            str(stream["url"]), target,
            timeout=timeout, referer="https://www.bilibili.com/",
        )
        result.ok = bool(direct.get("ok"))
        result.error = str(direct.get("error") or "")
        if result.ok:
            result.files = [str(direct.get("file"))]
            result.bytes = int(direct.get("bytes") or 0)
    else:
        ytdlp_result = ytdlp_channel.download(
            url, out_dir, cookies=cookies, audio_only=audio_only, timeout=max(timeout, 600.0)
        )
        result.ok = bool(ytdlp_result.ok)
        result.error = ytdlp_result.error
        result.files = list(ytdlp_result.files)
        result.partial = list(ytdlp_result.partial)
        for path in result.files:
            try:
                result.bytes += Path(path).stat().st_size
            except OSError:
                pass

    if queue is not None and record_id:
        queue.update_media(
            record_id,
            status="done" if result.ok else "failed",
            method=result.method,
            platform=result.platform,
            media_id=item.media_id,
            title=result.title,
            file_path=result.files[0] if result.files else "",
            file_size=result.bytes,
            progress=1.0 if result.ok else 0.0,
            error=result.error,
        )
    result.elapsed = time.time() - started
    return result
