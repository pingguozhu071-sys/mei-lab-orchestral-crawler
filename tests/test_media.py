"""媒体通道单测(全部离线,不联网)。

覆盖:m3u8 解析、AES-128 分片解密(本地构造密文)、平台识别、
文件名安全化、队列媒体记录、cookie 文件校验、产物认定规则。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meilab.core.types import MediaItem, MediaKind, MediaPlatform
from meilab.media.cookies import CookieError, CookieStore, validate_cookie_file
from meilab.media.downloader import _safe_filename
from meilab.media.m3u8_downloader import (
    M3u8Error,
    decrypt_aes128,
    parse_playlist,
)
from meilab.media.resolvers import bilibili_video_id, detect_platform
from meilab.media.ytdlp_channel import js_runtime_args, ytdlp_available


def test_parse_media_playlist() -> None:
    """解析普通媒体播放列表。"""
    text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:9.9,
seg0.ts
#EXTINF:9.9,
seg1.ts
#EXTINF:9.9,
seg2.ts
#EXT-X-ENDLIST
"""
    playlist = parse_playlist(text, "https://cdn.example.com/hls/index.m3u8")
    assert len(playlist.segments) == 3
    assert playlist.segments[0] == "https://cdn.example.com/hls/seg0.ts"
    assert playlist.target_duration == 10.0
    assert playlist.is_master is False
    assert playlist.keys == []


def test_parse_master_playlist() -> None:
    """主播放列表给出变体流。"""
    text = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
high/index.m3u8
"""
    playlist = parse_playlist(text, "https://cdn.example.com/hls/master.m3u8")
    assert playlist.is_master is True
    assert playlist.variant_url == "https://cdn.example.com/hls/low/index.m3u8"


def test_parse_playlist_with_aes_key() -> None:
    """带 EXT-X-KEY 的播放列表要记录密钥与分片对应关系。"""
    text = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.example.com/k/1.key",IV=0x000102030405060708090a0b0c0d0e
#EXTINF:9.9,
seg0.ts
#EXTINF:9.9,
seg1.ts
"""
    playlist = parse_playlist(text, "https://cdn.example.com/hls/index.m3u8")
    assert len(playlist.keys) == 2
    assert playlist.keys[0]["method"] == "AES-128"
    assert playlist.keys[0]["uri"] == "https://cdn.example.com/k/1.key"
    assert playlist.keys[0]["index"] == 0


def test_aes128_decrypt_roundtrip() -> None:
    """本地构造 AES-128 密文 → 解密还原(验证 pycryptodome 接线正确)。"""
    from Crypto.Cipher import AES

    key = bytes(range(16))
    iv = bytes(range(16, 32))
    plain = b"hello media segment" * 3  # 48 字节,需补齐到 16 的倍数
    padding = 16 - (len(plain) % 16)
    padded = plain + bytes([padding]) * padding
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(padded)

    decrypted = decrypt_aes128(
        encrypted, "https://unused/key", iv_hex=iv.hex(), key=key
    )
    assert decrypted == plain


def test_aes128_uses_sequence_derived_iv() -> None:
    """标签没给 IV 时按分片序号推导。"""
    from Crypto.Cipher import AES

    key = b"0123456789abcdef"
    sequence = 7
    iv = sequence.to_bytes(16, "big")
    plain = b"segment-data-1234" * 2
    padding = 16 - (len(plain) % 16)
    padded = plain + bytes([padding]) * padding
    encrypted = AES.new(key, AES.MODE_CBC, iv).encrypt(padded)

    assert decrypt_aes128(encrypted, "https://unused/key", sequence=sequence, key=key) == plain


def test_detect_platform_variants() -> None:
    """平台识别。"""
    assert detect_platform("https://www.bilibili.com/video/BV1x/") is MediaPlatform.BILIBILI
    assert detect_platform("https://v.douyin.com/abc/") is MediaPlatform.DOUYIN
    assert detect_platform("https://www.xiaohongshu.com/explore/1") is MediaPlatform.XIAOHONGSHU
    assert detect_platform("https://www.kuaishou.com/short-video/x") is MediaPlatform.KUAISHOU
    assert detect_platform("https://music.163.com/song?id=1") is MediaPlatform.NETEASE_MUSIC
    assert detect_platform("https://www.youtube.com/watch?v=abc") is MediaPlatform.GENERIC


def test_bilibili_video_id() -> None:
    """从 URL 里取 BV 号。"""
    assert bilibili_video_id("https://www.bilibili.com/video/BV1Dvpg6BEt5/") == "BV1Dvpg6BEt5"
    assert bilibili_video_id("https://example.com/x") == ""


def test_safe_filename() -> None:
    """文件名安全化:去掉路径分隔符,长度截断,补后缀。"""
    name = _safe_filename("a/b\\c:d*标题", "fallback", "mp4")
    assert "/" not in name and "\\" not in name
    assert name.endswith(".mp4")
    assert len(name) <= 84
    assert _safe_filename("", "fb", "mp4") == "fb.mp4"


def test_media_item_best_stream() -> None:
    """选最优流:有直链且码率最高的那条。"""
    item = MediaItem(
        source="https://x",
        streams=[
            {"url": "a", "bitrate": 100},
            {"url": "", "bitrate": 9999},
            {"url": "b", "bitrate": 500},
        ],
    )
    assert item.best_stream is not None
    assert item.best_stream["url"] == "b"


def test_media_queue_records(tmp_path: Path) -> None:
    """媒体记录入库与更新。"""
    from meilab.core.queue import TaskQueue

    queue = TaskQueue(tmp_path / "q.db")
    record_id = queue.upsert_media(
        "https://x/video.mp4", platform="bilibili", media_id="BV1", title="标题"
    )
    queue.update_media(record_id, status="downloading", progress=0.5, file_size=42)
    rows = queue.list_media()
    assert len(rows) == 1
    assert rows[0]["status"] == "downloading"
    assert rows[0]["progress"] == 0.5
    assert rows[0]["file_size"] == 42
    # 未传的字段保持原值
    assert rows[0]["title"] == "标题"
    assert queue.media_stats()["downloading"] == 1
    queue.close()


def test_media_queue_idempotent(tmp_path: Path) -> None:
    """同一来源 + 种类只留一条记录。"""
    from meilab.core.queue import TaskQueue

    queue = TaskQueue(tmp_path / "q.db")
    first = queue.upsert_media("https://x/a", title="一")
    second = queue.upsert_media("https://x/a", title="二")
    assert first == second
    assert len(queue.list_media()) == 1
    queue.close()


def test_cookie_file_validation(tmp_path: Path) -> None:
    """cookie 文件校验:不存在/为空都报错。"""
    with pytest.raises(CookieError):
        validate_cookie_file(tmp_path / "missing.txt")
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(CookieError):
        validate_cookie_file(empty)


def test_cookie_store_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cookie 加密存储与解密取回(密钥来自环境变量)。"""
    monkeypatch.setenv("MELAB_COOKIE_KEY", "test-passphrase")
    plain = tmp_path / "cookie.txt"
    plain.write_text("# Netscape HTTP Cookie File\nexample.com\tTRUE\t/\tFALSE\t0\tk\tv\n",
                     encoding="utf-8")
    store = CookieStore(tmp_path / "accounts")
    assert store.enabled is True
    saved = store.save("demo", plain)
    assert Path(saved).read_bytes() != plain.read_bytes()  # 落盘是密文
    recovered = store.load("demo")
    assert Path(recovered).read_text(encoding="utf-8") == plain.read_text(encoding="utf-8")
    os.unlink(recovered)
    assert store.names() == ["demo"]


def test_cookie_store_requires_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有密钥时明确报错,不静默明文存储。"""
    monkeypatch.delenv("MELAB_COOKIE_KEY", raising=False)
    monkeypatch.delenv("MELAB_KEY", raising=False)
    store = CookieStore(tmp_path / "accounts")
    assert store.enabled is False
    plain = tmp_path / "c.txt"
    plain.write_text("x", encoding="utf-8")
    with pytest.raises(CookieError):
        store.save("demo", plain)


def test_ytdlp_available_and_js_runtime() -> None:
    """yt-dlp 可用性探测;JS 运行时参数要么为空要么带运行时名。"""
    if not ytdlp_available():
        pytest.skip("yt-dlp 未安装")
    args = js_runtime_args()
    assert args == [] or args[0] == "--js-runtimes"


def test_m3u8_error_type() -> None:
    """m3u8 异常类型存在。"""
    assert issubclass(M3u8Error, RuntimeError)


def test_select_dash_tracks_prefers_highest_and_avc() -> None:
    """DASH 选轨:取最高清晰度档;同档内 H.264 优先于 H.265。"""
    from meilab.media.dash import select_dash_tracks

    dash = {
        "duration": 60.0,
        "video": [
            {"id": 116, "codecs": "hev1.1.6.L123.90", "bandwidth": 2000000,
             "width": 1920, "height": 1080, "baseUrl": "https://cdn.test/hev.m4s",
             "backupUrl": ["https://b.test/hev.m4s"]},
            {"id": 116, "codecs": "avc1.640033", "bandwidth": 1500000,
             "width": 1920, "height": 1080, "baseUrl": "https://cdn.test/avc.m4s"},
            {"id": 80, "codecs": "avc1.640032", "bandwidth": 900000,
             "width": 1920, "height": 1080, "baseUrl": "https://cdn.test/80.m4s"},
        ],
        "audio": [
            {"id": 30280, "codecs": "mp4a.40.2", "bandwidth": 65000,
             "baseUrl": "https://cdn.test/a-low.m4s"},
            {"id": 30216, "codecs": "mp4a.40.2", "bandwidth": 195000,
             "baseUrl": "https://cdn.test/a-high.m4s"},
        ],
    }
    tracks = select_dash_tracks(dash)
    assert tracks is not None
    assert tracks.quality == 116
    assert tracks.quality_desc == "1080P60"
    # 同档内 avc1 排在前面(即便带宽更低);选中的是 avc 条目,它没有备用直链
    assert "avc" in tracks.video["url"]
    assert tracks.video_backups == []
    # 音频取码率最高的一条
    assert tracks.audio["url"] == "https://cdn.test/a-high.m4s"
    assert tracks.has_video and tracks.has_audio


def test_select_dash_tracks_empty_dash() -> None:
    """没有视频轨时返回 None,不让下游拿到空轨。"""
    from meilab.media.dash import select_dash_tracks

    assert select_dash_tracks({}) is None
    assert select_dash_tracks({"video": [], "audio": []}) is None


def test_dash_tracks_defaults() -> None:
    """DashTracks 的空状态判定。"""
    from meilab.media.dash import DashTracks

    empty = DashTracks()
    assert not empty.has_video
    assert not empty.has_audio
    assert empty.quality_desc == ""
