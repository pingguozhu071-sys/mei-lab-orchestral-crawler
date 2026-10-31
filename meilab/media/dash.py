"""DASH 分轨下载与 ffmpeg 合流。

B 站 DASH(``fnval=4048``)把视频与音频拆成两条独立的轨:

* ``dash.video`` —— 每条对应一个「清晰度 id × 编码」组合;
* ``dash.audio`` —— 音频轨,按码率从低到高排列。

本模块负责三件事:选轨、下载两条分轨、用 ffmpeg 把它们合流成一个 mp4。

* **选轨**:清晰度 id 取最高的一档;同一档内优先 H.264(``avc1``),兼容性最好;
* **下载**:CDN 直链需要 ``Referer`` 与登录 cookie,缺一个就被拒(403);
  ``baseUrl`` 挂了会依次退到 ``backupUrl``;
* **合流**:``ffmpeg -c copy`` 直接封装不重编码,速度快且不损失画质;
  机器上没有 ffmpeg 时**明确报错并保留两条分轨**,不冒充成功。

真实 AES-128 的 DASH 分片不在本模块职责内 —— B 站 DASH 的分轨是完整 fMP4 文件,
不需要像 HLS 那样逐片解密。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..workers.base import check_url_safe
from ..workers.http import ensure_ascii_ca_bundle

#: B 站清晰度 id → 名称
QUALITY_LABELS: dict[int, str] = {
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
    125: "HDR",
    126: "杜比视界",
}

#: 优先使用的视频编码(H.264 兼容性最好)
PREFERRED_VIDEO_CODEC = "avc1"

#: ffmpeg 合流超时(秒)
MUX_TIMEOUT = 600.0


class DashError(RuntimeError):
    """DASH 解析或下载失败。"""


@dataclass
class DashTracks:
    """选好的两条轨(视频 + 音频)。"""

    video: dict[str, Any] = field(default_factory=dict)
    audio: dict[str, Any] = field(default_factory=dict)
    quality: int = 0
    quality_desc: str = ""
    width: int = 0
    height: int = 0
    codecs: str = ""
    duration: float = 0.0
    #: 备选直链(``baseUrl`` 失败时依次尝试)
    video_backups: list[str] = field(default_factory=list)
    audio_backups: list[str] = field(default_factory=list)

    @property
    def has_video(self) -> bool:
        """是否选到了视频轨。"""
        return bool(self.video.get("url"))

    @property
    def has_audio(self) -> bool:
        """是否选到了音频轨。"""
        return bool(self.audio.get("url"))


def _codec_rank(codecs: str) -> int:
    """编码偏好排序:H.264 优先,其次 H.265,其余靠后。"""
    text = (codecs or "").lower()
    if text.startswith(PREFERRED_VIDEO_CODEC):
        return 0
    if text.startswith(("hev1", "hvc1")):
        return 1
    return 2


def _entry_url(entry: dict[str, Any]) -> str:
    """取一条轨的主直链(B 站字段名有 ``baseUrl`` 与 ``base_url`` 两种写法)。"""
    for key in ("baseUrl", "base_url", "url"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def _entry_backups(entry: dict[str, Any]) -> list[str]:
    """取一条轨的备用直链。"""
    for key in ("backupUrl", "backup_url"):
        values = entry.get(key) or []
        if values:
            return [str(v) for v in values if v]
    return []


def _as_int(value: Any) -> int:
    """把接口里的数字字段安全转成 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def select_dash_tracks(dash: dict[str, Any]) -> DashTracks | None:
    """从 DASH 响应里选出最高清晰度的视频轨与最佳音频轨。

    :param dash: ``playurl`` 响应里的 ``dash`` 字段。
    :return: 选好的轨;没有可用轨时返回 ``None``。
    """
    if not isinstance(dash, dict):
        return None
    video_entries = [e for e in (dash.get("video") or []) if isinstance(e, dict)]
    audio_entries = [e for e in (dash.get("audio") or []) if isinstance(e, dict)]
    if not video_entries:
        return None

    def quality_of(entry: dict[str, Any]) -> int:
        return _as_int(entry.get("id"))

    best_quality = max(quality_of(e) for e in video_entries)
    same_quality = [e for e in video_entries if quality_of(e) == best_quality]
    # 同档内:编码偏好优先,其次带宽
    same_quality.sort(
        key=lambda e: (
            _codec_rank(str(e.get("codecs") or "")),
            -_as_int(e.get("bandwidth")),
        )
    )
    video = same_quality[0]

    tracks = DashTracks(
        video={"url": _entry_url(video), "size": 0,
               "bitrate": _as_int(video.get("bandwidth"))},
        quality=best_quality,
        quality_desc=QUALITY_LABELS.get(best_quality, f"Q{best_quality}"),
        width=_as_int(video.get("width")),
        height=_as_int(video.get("height")),
        codecs=str(video.get("codecs") or ""),
        duration=float(dash.get("duration") or 0.0),
        video_backups=_entry_backups(video),
    )

    # 音频轨:取码率最高的一条(FLAC / 杜比在 dash.flac / dash.dolby 里,不在这里)
    if audio_entries:
        best_audio = max(audio_entries, key=lambda e: _as_int(e.get("bandwidth")))
        tracks.audio = {"url": _entry_url(best_audio), "size": 0,
                        "bitrate": _as_int(best_audio.get("bandwidth"))}
        tracks.audio_backups = _entry_backups(best_audio)
    return tracks


def ffmpeg_path() -> str | None:
    """找 ffmpeg 可执行文件。

    :return: 路径;找不到返回 ``None``。
    """
    return shutil.which("ffmpeg")


def _download_track(
    urls: list[str],
    target: Path,
    *,
    referer: str,
    cookie_header: str = "",
    timeout: float = 120.0,
    retries: int = 3,
) -> dict[str, Any]:
    """下载一条分轨(支持 Range 续传与多直链退避)。

    :param urls: 候选直链(第一个是主链,后面是备用链)。
    :param target: 目标文件路径。
    :param referer: Referer 头(CDN 必需)。
    :param cookie_header: Cookie 头。
    :param timeout: 单次请求超时(秒)。
    :param retries: 单条直链的重试次数。
    :return: ``{"ok", "file", "bytes", "resumed", "error"}``。
    """
    from curl_cffi import requests as curl_requests  # 第三方库:TLS 指纹模拟

    result: dict[str, Any] = {
        "ok": False, "file": str(target), "bytes": 0, "resumed": False, "error": "",
    }
    if not urls:
        result["error"] = "没有可用直链"
        return result

    headers = {"User-Agent": _UA}
    if referer:
        headers["Referer"] = referer
    if cookie_header:
        headers["Cookie"] = cookie_header

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    last_error = ""
    for url in urls:
        safe, why = check_url_safe(url)
        if not safe:
            last_error = f"SSRF 防护拒绝该直链: {why}"
            continue
        have = part.stat().st_size if part.exists() else 0
        attempt_headers = dict(headers)
        if have:
            attempt_headers["Range"] = f"bytes={have}-"
        for attempt in range(max(1, retries)):
            try:
                ensure_ascii_ca_bundle()
                response = curl_requests.get(
                    url, timeout=timeout, impersonate="chrome",
                    headers=attempt_headers, stream=True,
                )
            except Exception as exc:
                last_error = f"分轨请求失败: {exc!r}"
                time.sleep(min(5.0, 1.5 * (attempt + 1)))
                continue
            if response.status_code >= 400:
                last_error = f"分轨请求失败: HTTP {response.status_code}"
                break
            mode = "ab" if (have and response.status_code == 206) else "wb"
            try:
                with part.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        if chunk:
                            handle.write(chunk)
            except Exception as exc:
                last_error = f"分轨写入失败: {exc!r}"
                break
            os.replace(part, target)
            result.update(
                {"ok": True, "bytes": target.stat().st_size, "resumed": bool(have)}
            )
            return result
        # 这条直链不行,退到下一条
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
    result["error"] = last_error or "分轨下载失败"
    return result


def mux_tracks(
    video_file: str | os.PathLike[str],
    audio_file: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    timeout: float = MUX_TIMEOUT,
) -> dict[str, Any]:
    """用 ffmpeg 把视频轨与音频轨合流成一个 mp4(不重编码)。

    :param video_file: 视频轨文件。
    :param audio_file: 音频轨文件。
    :param target: 输出文件路径。
    :param timeout: ffmpeg 超时(秒)。
    :return: ``{"ok", "file", "bytes", "error"}``。
    """
    result: dict[str, Any] = {"ok": False, "file": str(target), "bytes": 0, "error": ""}
    binary = ffmpeg_path()
    if not binary:
        result["error"] = "未找到 ffmpeg,无法合流(两条分轨已保留)"
        return result
    out_path = Path(target)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        binary, "-y", "-nostdin",
        "-i", str(video_file),
        "-i", str(audio_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        # 参数是列表、不经 shell,没有注入面
        completed = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        result["error"] = f"ffmpeg 合流超时({timeout:.0f}s)"
        return result
    except Exception as exc:
        result["error"] = f"ffmpeg 合流失败: {exc!r}"
        return result
    if completed.returncode != 0 or not out_path.is_file():
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        result["error"] = f"ffmpeg 合流失败(退出码 {completed.returncode}): {detail[-400:]}"
        return result
    result.update({"ok": True, "bytes": out_path.stat().st_size})
    return result


def download_dash(
    tracks: DashTracks,
    target: str | os.PathLike[str],
    *,
    referer: str = "https://www.bilibili.com/",
    cookie_header: str = "",
    timeout: float = 120.0,
    audio_only: bool = False,
) -> dict[str, Any]:
    """下载 DASH 的两条轨并合流成一个文件。

    :param tracks: 选好的轨。
    :param target: 输出文件路径。
    :param referer: Referer 头。
    :param cookie_header: Cookie 头。
    :param timeout: 单次请求超时(秒)。
    :param audio_only: 只取音频(不下载视频轨、不合流)。
    :return: ``{"ok", "file", "bytes", "video_bytes", "audio_bytes", "resumed", "error"}``。
    """
    result: dict[str, Any] = {
        "ok": False, "file": str(target), "bytes": 0, "video_bytes": 0,
        "audio_bytes": 0, "resumed": False, "error": "",
    }
    out_path = Path(target)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if audio_only:
        if not tracks.has_audio:
            result["error"] = "没有可用的音频轨"
            return result
        audio_target = out_path.with_suffix(".m4a")
        audio = _download_track(
            [tracks.audio["url"], *tracks.audio_backups], audio_target,
            referer=referer, cookie_header=cookie_header, timeout=timeout,
        )
        result["audio_bytes"] = int(audio.get("bytes") or 0)
        if not audio.get("ok"):
            result["error"] = str(audio.get("error") or "音频轨下载失败")
            return result
        result.update({"ok": True, "file": str(audio_target), "bytes": result["audio_bytes"]})
        return result

    if not tracks.has_video:
        result["error"] = "没有可用的视频轨"
        return result

    video_target = out_path.with_suffix(".video.m4s")
    audio_target = out_path.with_suffix(".audio.m4s")

    video = _download_track(
        [tracks.video["url"], *tracks.video_backups], video_target,
        referer=referer, cookie_header=cookie_header, timeout=timeout,
    )
    if not video.get("ok"):
        result["error"] = f"视频轨下载失败: {video.get('error')}"
        return result
    result["video_bytes"] = int(video.get("bytes") or 0)
    result["resumed"] = bool(video.get("resumed"))

    if tracks.has_audio:
        audio = _download_track(
            [tracks.audio["url"], *tracks.audio_backups], audio_target,
            referer=referer, cookie_header=cookie_header, timeout=timeout,
        )
        if not audio.get("ok"):
            result["error"] = f"音频轨下载失败(视频轨已保留): {audio.get('error')}"
            return result
        result["audio_bytes"] = int(audio.get("bytes") or 0)
    else:
        result["error"] = "没有音频轨,无法合流(视频轨已保留)"
        return result

    muxed = mux_tracks(video_target, audio_target, out_path)
    if not muxed.get("ok"):
        result["error"] = str(muxed.get("error") or "合流失败")
        return result
    result.update({"ok": True, "file": str(out_path), "bytes": int(muxed.get("bytes") or 0)})

    # 合流成功后清掉中间分轨
    for path in (video_target, audio_target):
        try:
            path.unlink()
        except OSError:
            pass
    return result


#: 统一的 UA(与 HTTP 通道一致)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
