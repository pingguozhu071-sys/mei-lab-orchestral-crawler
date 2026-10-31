"""media 子包:媒体通道(通用 yt-dlp + m3u8/AES-128 + DASH 合流 + 五平台解析器 + cookie)。

分发优先级:
1. m3u8 直链 → 自研 HLS 下载器;
2. 平台原生直链(B 站等)→ 自研可续传直链下载器;
3. DASH 分轨(B 站)→ 分轨下载 + ffmpeg 合流,清晰度上限最高;
4. 其余 → 通用 yt-dlp 通道(覆盖 yt-dlp 支持的所有站点)。
"""

from .cookies import (
    COOKIE_KEY_ENV,
    CookieError,
    CookieStore,
    cookie_header,
    cookie_key,
    describe_cookie_source,
    validate_cookie_file,
)
from .dash import DashError, DashTracks, download_dash, select_dash_tracks
from .downloader import MediaResult, download_direct, download_media
from .m3u8_downloader import M3u8Error, download_playlist, parse_playlist
from .resolvers import (
    bilibili_comments,
    bilibili_resolve,
    bilibili_video_id,
    detect_platform,
    resolve_media,
)
from .ytdlp_channel import (
    YtdlpResult,
    download as ytdlp_download,
    js_runtime_args,
    probe_supported,
    resolve as ytdlp_resolve,
    ytdlp_available,
    ytdlp_version,
)

__all__ = [
    "COOKIE_KEY_ENV",
    "CookieError",
    "CookieStore",
    "DashError",
    "DashTracks",
    "M3u8Error",
    "MediaResult",
    "YtdlpResult",
    "bilibili_comments",
    "bilibili_resolve",
    "bilibili_video_id",
    "cookie_header",
    "cookie_key",
    "describe_cookie_source",
    "detect_platform",
    "download_dash",
    "download_direct",
    "download_media",
    "download_playlist",
    "js_runtime_args",
    "parse_playlist",
    "probe_supported",
    "resolve_media",
    "select_dash_tracks",
    "validate_cookie_file",
    "ytdlp_available",
    "ytdlp_download",
    "ytdlp_resolve",
    "ytdlp_version",
]
