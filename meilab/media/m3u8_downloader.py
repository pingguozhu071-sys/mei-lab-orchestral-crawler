"""m3u8(HLS)分片下载 + AES-128 解密。

标准库解析 m3u8 播放列表,逐片下载并按需解密,最后合并成一个文件。

* 支持 ``EXT-X-KEY:METHOD=AES-128``(IV 取自标签,缺省用分片序号);
* 支持 ``EXT-X-MAP``(fMP4 初始化段);
* 分片 URL 逐个过 SSRF 校验(防止播放列表指向内网);
* 服务端不支持 Range 时无法续传 —— 这是协议本身的事实,不做伪装。

加密分片用 ``pycryptodome`` 的 AES-128-CBC(工程书第 10 节指定组件)。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ..workers.base import check_url_safe
from ..workers.http import ensure_ascii_ca_bundle

#: ``EXT-X-KEY`` 标签
_KEY_RE = re.compile(r'#EXT-X-KEY:(?P<attrs>[^\r\n]+)', re.IGNORECASE)

#: ``EXT-X-MAP`` 标签
_MAP_RE = re.compile(r'#EXT-X-MAP:(?P<attrs>[^\r\n]+)', re.IGNORECASE)

#: ``EXT-X-MEDIA-SEQUENCE``
_MEDIA_SEQUENCE_RE = re.compile(r'#EXT-X-MEDIA-SEQUENCE:(?P<value>\d+)', re.IGNORECASE)


class M3u8Error(RuntimeError):
    """m3u8 解析或下载失败。"""


@dataclass
class M3u8Playlist:
    """解析后的播放列表。"""

    segments: list[str] = field(default_factory=list)
    keys: list[dict[str, Any]] = field(default_factory=list)
    init_url: str = ""
    target_duration: float = 0.0
    is_master: bool = False
    variant_url: str = ""


def _attr(attrs: str, name: str) -> str:
    """从标签属性串里取一个属性值。"""
    match = re.search(rf'{name}=(?:"([^"]*)"|([^,\s]+))', attrs, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def parse_playlist(text: str, base_url: str = "") -> M3u8Playlist:
    """解析 m3u8 文本。

    :param text: 播放列表内容。
    :param base_url: 相对地址的基准 URL。
    :return: 解析结果。
    """
    playlist = M3u8Playlist()
    lines = [line.strip() for line in text.splitlines()]
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        # 主播放列表:挑第一条变体流
        playlist.is_master = True
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF") and index + 1 < len(lines):
                candidate = lines[index + 1]
                if candidate and not candidate.startswith("#"):
                    playlist.variant_url = urljoin(base_url, candidate)
                    break
        return playlist

    current_key: dict[str, Any] | None = None
    sequence = 0
    for line in lines:
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(_MEDIA_SEQUENCE_RE.search(line).group("value"))  # type: ignore[union-attr]
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                playlist.target_duration = float(line.split(":", 1)[1])
            except (ValueError, IndexError):
                pass
            continue
        if line.startswith("#EXT-X-KEY:"):
            match = _KEY_RE.search(line)
            attrs = match.group("attrs") if match else ""
            method = _attr(attrs, "METHOD").upper()
            if method in ("", "NONE"):
                current_key = None
            else:
                current_key = {
                    "method": method,
                    "uri": urljoin(base_url, _attr(attrs, "URI")),
                    "iv": _attr(attrs, "IV"),
                }
            continue
        if line.startswith("#EXT-X-MAP:"):
            match = _MAP_RE.search(line)
            attrs = match.group("attrs") if match else ""
            playlist.init_url = urljoin(base_url, _attr(attrs, "URI"))
            continue
        if line.startswith("#"):
            continue
        segment = urljoin(base_url, line)
        playlist.segments.append(segment)
        if current_key is not None:
            playlist.keys.append(
                {
                    "method": current_key["method"],
                    "uri": current_key["uri"],
                    "iv": current_key["iv"],
                    "index": len(playlist.segments) - 1,
                    "sequence": sequence + len(playlist.segments) - 1,
                }
            )
    return playlist


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    """抓一个 URL 的字节(走 curl_cffi,带 CA 修复)。"""
    from curl_cffi import requests as curl_requests  # 第三方库:TLS 指纹模拟

    ensure_ascii_ca_bundle()
    response = curl_requests.get(url, timeout=timeout, impersonate="chrome")
    return bytes(response.content or b"")


def decrypt_aes128(
    data: bytes,
    key_uri: str,
    iv_hex: str = "",
    sequence: int = 0,
    key: bytes | None = None,
) -> bytes:
    """AES-128-CBC 解密一个分片。

    :param data: 密文分片。
    :param key_uri: 密钥 URL(会被缓存)。
    :param iv_hex: 标签里的 IV(十六进制);为空则用分片序号推导。
    :param sequence: 分片序号(用于推导 IV)。
    :param key: 直接给定密钥(测试与已知密钥场景;为空则按 URI 抓取)。
    :return: 明文。
    """
    from Crypto.Cipher import AES  # 第三方库:AES-128 分片解密

    material = key if key is not None else _fetch_key(key_uri)
    if iv_hex:
        iv = bytes.fromhex(iv_hex.replace("0x", ""))
    else:
        iv = sequence.to_bytes(16, "big")
    cipher = AES.new(material, AES.MODE_CBC, iv)
    plain = cipher.decrypt(data)
    # 去 PKCS#7 填充
    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
    return plain


_KEY_CACHE: dict[str, bytes] = {}


def _fetch_key(key_uri: str) -> bytes:
    """取密钥(带缓存)。"""
    if key_uri in _KEY_CACHE:
        return _KEY_CACHE[key_uri]
    raw = _http_get(key_uri)
    key = raw[:16]
    if len(key) != 16:
        raise M3u8Error(f"AES-128 密钥长度异常: {len(key)} 字节(期望 16)")
    _KEY_CACHE[key_uri] = key
    return key


def _fetch_segment(url: str, *, timeout: float, retries: int = 3) -> bytes:
    """抓一个分片(带重试)。

    :param url: 分片 URL。
    :param timeout: 单次超时(秒)。
    :param retries: 重试次数。
    :return: 分片字节。
    """
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return _http_get(url, timeout=timeout)
        except Exception as exc:
            last = exc
            time.sleep(min(5.0, 1.5 * (attempt + 1)))
    raise M3u8Error(f"分片抓取失败: {last!r}")


def _write_state(state_path: Path, segments: int, size: int) -> None:
    """记录续传状态(已完成分片数与字节数)。"""
    payload = json.dumps({"segments": segments, "bytes": size}, ensure_ascii=False)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, state_path)


def _read_state(state_path: Path) -> tuple[int, int]:
    """读取续传状态(分片数, 字节数);没有或损坏返回 (0, 0)。"""
    if not state_path.is_file():
        return 0, 0
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return int(data.get("segments", 0)), int(data.get("bytes", 0))
    except Exception:
        return 0, 0


def download_playlist(
    playlist_url: str,
    output_path: str | os.PathLike[str],
    *,
    timeout: float = 120.0,
    max_segments: int = 0,
    retries: int = 3,
) -> dict[str, Any]:
    """下载并合并一个 m3u8 播放列表(支持分片级续传与重试)。

    :param playlist_url: 播放列表 URL。
    :param output_path: 输出文件路径。
    :param timeout: 单次请求超时(秒)。
    :param max_segments: 最多下载多少个分片(0 = 全部)。
    :param retries: 单个分片的重试次数。
    :return: 结果字典 ``{"ok", "file", "segments", "bytes", "resumed", "error"}``。
    """
    result: dict[str, Any] = {
        "ok": False, "file": "", "segments": 0, "bytes": 0, "resumed": False, "error": "",
    }
    safe, why = check_url_safe(playlist_url)
    if not safe:
        result["error"] = f"SSRF 防护拒绝播放列表: {why}"
        return result

    try:
        text = _http_get(playlist_url, timeout=timeout).decode("utf-8", errors="replace")
    except Exception as exc:
        result["error"] = f"播放列表抓取失败: {exc!r}"
        return result

    playlist = parse_playlist(text, playlist_url)
    if playlist.is_master:
        if not playlist.variant_url:
            result["error"] = "主播放列表没有可用变体流"
            return result
        safe, why = check_url_safe(playlist.variant_url)
        if not safe:
            result["error"] = f"SSRF 防护拒绝变体流: {why}"
            return result
        try:
            text = _http_get(playlist.variant_url, timeout=timeout).decode(
                "utf-8", errors="replace")
        except Exception as exc:
            result["error"] = f"变体流抓取失败: {exc!r}"
            return result
        playlist = parse_playlist(text, playlist.variant_url)

    if not playlist.segments:
        result["error"] = "播放列表里没有分片"
        return result

    segments = playlist.segments[:max_segments] if max_segments else playlist.segments
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    state_path = target.with_suffix(target.suffix + ".state.json")

    done_segments, done_bytes = _read_state(state_path)
    resumed = done_segments > 0
    written = done_segments

    try:
        with part.open("ab" if resumed else "wb") as handle:
            if resumed:
                # 截断到状态记录的位置,避免上次写了一半的残留
                handle.truncate(done_bytes)
            elif playlist.init_url:
                handle.write(_http_get(playlist.init_url, timeout=timeout))
            for index, segment in enumerate(segments):
                if index < done_segments:
                    continue
                safe, why = check_url_safe(segment)
                if not safe:
                    raise M3u8Error(f"SSRF 防护拒绝分片: {why}")
                blob = _fetch_segment(segment, timeout=timeout, retries=retries)
                key_entry = next(
                    (k for k in playlist.keys if k["index"] == index), None
                )
                if key_entry is not None:
                    blob = decrypt_aes128(
                        blob, key_entry["uri"], key_entry["iv"], key_entry["sequence"]
                    )
                handle.write(blob)
                handle.flush()
                written += 1
                _write_state(state_path, written, part.stat().st_size)
    except Exception as exc:
        result["error"] = f"分片下载失败(已完成 {written}/{len(segments)} 片): {exc!r}"
        result["segments"] = written
        return result

    # 原子改名(并清理状态文件)
    os.replace(part, target)
    try:
        state_path.unlink()
    except OSError:
        pass
    result.update(
        {
            "ok": True,
            "file": str(target),
            "segments": written,
            "bytes": target.stat().st_size,
            "resumed": resumed,
        }
    )
    return result
