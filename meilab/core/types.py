"""核心数据模型:Task / Episode / DeviceProfile / RoundState / MediaItem。

全部使用 Pydantic v2(BaseModel + ConfigDict),用于在进程边界(multiprocessing
队列)传递结构化数据。所有模型均 ``extra="forbid"``,避免拼写错误的字段被静默吞掉。
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskStatus(str, Enum):
    """任务状态机取值,与 core/queue.py 中的 SQLite 状态机一一对应。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class TaskType(str, Enum):
    """任务种类,决定路由器选择哪条通道。"""

    CRAWL = "crawl"
    JSON_API = "json_api"
    PDF = "pdf"
    IMAGE = "image"
    MEDIA = "media"


class MediaKind(str, Enum):
    """媒体种类。"""

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    LIVE = "live"


class MediaStatus(str, Enum):
    """媒体下载任务的状态机取值,与 ``media_downloads`` 表一一对应。"""

    PENDING = "pending"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class MediaPlatform(str, Enum):
    """平台标识。``GENERIC`` 表示走通用 yt-dlp 通道。"""

    GENERIC = "generic"
    BILIBILI = "bilibili"
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    KUAISHOU = "kuaishou"
    NETEASE_MUSIC = "netease_music"


class MediaItem(BaseModel):
    """一个媒体条目的统一描述(对齐 kiana-crawler 的 MediaItem schema)。

    平台解析器与通用 yt-dlp 通道都产出这个结构,下游不关心来源差异。
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(description="来源 URL(页面或直链)")
    media_id: str = Field(default="", description="平台内的媒体 ID(如 B 站 BV 号)")
    kind: MediaKind = Field(default=MediaKind.VIDEO, description="媒体种类")
    platform: MediaPlatform = Field(default=MediaPlatform.GENERIC, description="平台")
    streams: list[dict[str, Any]] = Field(default_factory=list, description="可下载流列表")
    images: list[str] = Field(default_factory=list, description="图片直链列表")
    method: str = Field(default="", description="解析方式:platform_resolver / ytdlp / m3u8")
    title: str = Field(default="", description="标题")
    duration: float = Field(default=0.0, ge=0, description="时长(秒)")
    needs_cookie: bool = Field(default=False, description="是否需要登录态才能拿到最佳流")
    meta: dict[str, Any] = Field(default_factory=dict, description="平台附加元数据")

    @property
    def best_stream(self) -> dict[str, Any] | None:
        """挑一个最优流:优先有直链、码率高的那条。"""
        usable = [s for s in self.streams if s.get("url")]
        if not usable:
            return None
        return max(usable, key=lambda s: float(s.get("bitrate") or 0))


class Outcome(str, Enum):
    """Episode 的最终结果。"""

    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    DEGRADED = "degraded"


class RoundState(BaseModel):
    """浏览器通道中「一个页面轮次」的状态。

    一个 Task 可能需要多轮渲染(等待 hydration、翻页、滚动触发懒加载),
    每轮由引擎创建一个 RoundState 记录观测。
    """

    model_config = ConfigDict(extra="forbid")

    round_index: int = Field(ge=0, description="轮次序号,从 0 开始")
    url: str = Field(description="本轮访问的 URL")
    action: str = Field(default="goto", description="本轮动作:goto / scroll / click / wait")
    started_at: float = Field(default_factory=time.time, description="本轮开始时间戳(Unix 秒)")
    finished_at: float | None = Field(default=None, description="本轮结束时间戳(Unix 秒)")
    html_length: int = Field(default=0, ge=0, description="本轮结束时 DOM 文本长度")
    ok: bool = Field(default=True, description="本轮是否成功")
    note: str = Field(default="", description="本轮备注")

    @model_validator(mode="after")
    def _check_timestamps(self) -> "RoundState":
        """结束时间不得早于开始时间。"""
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at 早于 started_at")
        return self


class DeviceProfile(BaseModel):
    """一台自洽的虚拟设备指纹档案。

    一致性约束(由 stealth/profiles.py 的 DeviceLibrary 保证):
    移动端 UA 必须 ``has_touch=True`` 且 viewport 为移动尺寸;
    ``locale`` 必须与 ``sale_region`` 的默认 locale 相符。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="档案唯一名,如 'win11_chrome_1920'")
    ua: str = Field(min_length=1, description="完整 User-Agent 字符串")
    viewport: tuple[int, int] = Field(description="视口宽高(像素)")
    timezone: str = Field(min_length=1, description="IANA 时区,如 'Asia/Tokyo'")
    locale: str = Field(min_length=2, description="语言标签,如 'ja-JP'")
    gpu_vendor: str = Field(min_length=1, description="WebGL UNMASKED_VENDOR_WEBGL")
    gpu_renderer: str = Field(min_length=1, description="WebGL UNMASKED_RENDERER_WEBGL")
    fonts: list[str] = Field(default_factory=list, description="字体名列表")
    has_touch: bool = Field(default=False, description="是否支持触摸事件")
    platform: str = Field(description="platform 属性:Win32 / MacIntel / Linux armv8l 等")
    sale_region: str = Field(description="销售地区标识,如 'JP' / 'US' / 'DE'")
    mobile: bool = Field(default=False, description="是否移动设备")
    device_scale_factor: float = Field(default=1.0, gt=0, description="设备像素比")
    is_mobile: bool = Field(default=False, description="是否移动 UA(与 mobile 同义保留位)")

    @field_validator("viewport")
    @classmethod
    def _viewport_positive(cls, v: tuple[int, int]) -> tuple[int, int]:
        """视口宽高必须为正整数。"""
        if v[0] <= 0 or v[1] <= 0:
            raise ValueError("viewport 宽高必须为正")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "DeviceProfile":
        """硬一致性:移动设备必须带触摸。"""
        if (self.mobile or self.is_mobile) and not self.has_touch:
            raise ValueError(f"档案 {self.name}: 移动设备必须 has_touch=True")
        return self

    def as_context_options(self) -> dict[str, Any]:
        """返回可直接传给 Playwright ``browser.new_context(**kwargs)`` 的参数子集。

        字体列表无法通过 context 参数设置,由 stealth/inject.py 通过 init script 注入,
        因此此处不返回 ``fonts``。
        """
        return {
            "user_agent": self.ua,
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "timezone_id": self.timezone,
            "locale": self.locale,
            "has_touch": self.has_touch,
            "is_mobile": self.is_mobile,
            "device_scale_factor": self.device_scale_factor,
        }


class Task(BaseModel):
    """一个待抓取的任务单元。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="任务唯一 ID")
    url: str = Field(min_length=1, description="目标 URL")
    task_type: TaskType = Field(default=TaskType.CRAWL, description="任务种类")
    priority: int = Field(default=0, description="数值越大越优先")
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="当前状态")
    retry_count: int = Field(default=0, ge=0, description="已重试次数")
    depth: int = Field(default=0, ge=0, description="URL 深度(种子为 0)")
    parent_id: str | None = Field(default=None, description="父任务 ID")
    payload: dict[str, Any] = Field(default_factory=dict, description="附加参数")
    created_at: float = Field(default_factory=time.time, description="创建时间戳")
    updated_at: float = Field(default_factory=time.time, description="更新时间戳")

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        """只接受 http/https,其余协议直接拒绝(防御 SSRF 与 file:// 读取)。"""
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError(f"不支持的 URL 协议: {v!r}")
        return v


class Episode(BaseModel):
    """一次抓取尝试的完整记录,落库单元。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, description="Episode 唯一 ID")
    task_id: str = Field(description="关联的任务 ID")
    url: str = Field(description="实际抓取的 URL")
    outcome: Outcome = Field(default=Outcome.FAILED, description="最终结果")
    rounds_runned: int = Field(default=0, ge=0, description="执行轮次数(字段名沿用工程书原文)")
    anti_bot_signals: list[str] = Field(default_factory=list, description="命中的反爬信号")
    channel: str = Field(default="unknown", description="使用的通道名")
    http_status: int | None = Field(default=None, description="HTTP 状态码")
    content_type: str | None = Field(default=None, description="响应的 Content-Type")
    title: str = Field(default="", description="提取出的标题")
    content_hash: str = Field(default="", description="正文 SHA256")
    content: str = Field(default="", description="提取出的正文(markdown / 文本)")
    raw_meta: dict[str, Any] = Field(default_factory=dict, description="额外元数据")
    error: str = Field(default="", description="失败原因(成功时为空)")
    started_at: float = Field(default_factory=time.time, description="开始时间戳")
    finished_at: float = Field(default_factory=time.time, description="结束时间戳")

    @property
    def duration(self) -> float:
        """本次尝试的耗时(秒)。"""
        return max(0.0, self.finished_at - self.started_at)

    @property
    def blocked(self) -> bool:
        """是否被反爬拦截。"""
        return self.outcome in (Outcome.BLOCKED, Outcome.DEGRADED) or bool(self.anti_bot_signals)
