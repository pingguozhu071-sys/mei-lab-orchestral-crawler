"""分层路由器:根据任务与嗅探结果,决定该任务交给哪个 Worker 通道。

"分层"指两层决策:
* **第一层(内容层)**:这个 URL 是什么内容?→ HTML / JSON / PDF / 图片;
* **第二层(成本层)**:拿到这个内容需要多贵的手段?→ 轻量 HTTP 还是完整浏览器。

原则是**最快路径优先**:能用一次 HTTP 请求解决,绝不开浏览器。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ..core.types import Task, TaskType
from .sniff import Channel, ContentType, SniffResult, sniff_url


class WorkerKind(str, Enum):
    """Worker 种类,对应 workers/ 下的实现与媒体通道。"""

    HTTP = "http"
    BROWSER = "browser"
    MEDIA = "media"  # 媒体通道:由 media/ 子系统处理(yt-dlp / m3u8 / 平台解析器)


class ChannelDecision(BaseModel):
    """一次路由决策。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(description="任务 ID")
    url: str = Field(description="目标 URL")
    worker: WorkerKind = Field(description="交给哪类 Worker")
    channel: Channel = Field(description="内容通道")
    expected_content_type: ContentType = Field(description="预期的内容类型")
    reason: str = Field(description="决策依据(人类可读)")
    timeout: float = Field(default=20.0, gt=0, description="单次抓取超时(秒)")
    max_rounds: int = Field(default=1, ge=1, description="允许的最大渲染轮次")
    requires_js: bool = Field(default=False, description="是否需要执行 JS")
    stealth: bool = Field(default=False, description="是否启用强反检测档案")
    retry_hint: int = Field(default=1, ge=0, description="建议重试次数")

    @property
    def is_browser(self) -> bool:
        """是否走浏览器通道。"""
        return self.worker is WorkerKind.BROWSER


class LayeredRouter:
    """把 :class:`~meilab.core.types.Task` 映射到 :class:`ChannelDecision`。"""

    def __init__(
        self,
        http_timeout: float = 20.0,
        browser_timeout: float = 45.0,
        browser_max_rounds: int = 3,
    ) -> None:
        """初始化路由器。

        :param http_timeout: HTTP 通道超时(秒)。
        :param browser_timeout: 浏览器通道超时(秒)。
        :param browser_max_rounds: 浏览器通道默认允许的渲染轮次上限。
        """
        self.http_timeout = http_timeout
        self.browser_timeout = browser_timeout
        self.browser_max_rounds = browser_max_rounds

    def route(self, task: Task, sniff_result: SniffResult | None = None) -> ChannelDecision:
        """对单个任务做路由决策。

        :param task: 待路由任务。
        :param sniff_result: 已有的嗅探结果;为 ``None`` 时现场嗅探。
        :return: 路由决策。
        """
        result = sniff_result if sniff_result is not None else sniff_url(task.url)

        # 任务类型优先于 URL 猜测:调用方显式声明的语义更可信。
        if task.task_type is TaskType.MEDIA:
            return self._decision(
                task, result, WorkerKind.MEDIA, Channel.MEDIA, ContentType.UNKNOWN,
                "任务类型显式声明为媒体,走媒体通道",
                timeout=self.http_timeout, max_rounds=1,
            )
        if task.task_type is TaskType.PDF:
            return self._decision(
                task, result, WorkerKind.HTTP, Channel.PDF, ContentType.PDF,
                "任务类型显式声明为 PDF,走 HTTP + PDF 提取管线",
                timeout=self.http_timeout, max_rounds=1,
            )
        if task.task_type is TaskType.JSON_API:
            return self._decision(
                task, result, WorkerKind.HTTP, Channel.DIRECT_JSON, ContentType.JSON,
                "任务类型显式声明为 JSON API,直接解析响应体",
                timeout=self.http_timeout, max_rounds=1,
            )
        if task.task_type is TaskType.IMAGE:
            return self._decision(
                task, result, WorkerKind.HTTP, Channel.LIGHTWEIGHT, ContentType.IMAGE,
                "任务类型显式声明为图片,直接下载二进制",
                timeout=self.http_timeout, max_rounds=1,
            )

        # 内容层 → 成本层
        if result.channel is Channel.MEDIA:
            return self._decision(
                task, result, WorkerKind.MEDIA, Channel.MEDIA, ContentType.UNKNOWN,
                "嗅探判定为媒体资源,走媒体通道(平台解析器 → 通用 yt-dlp)",
                timeout=self.http_timeout, max_rounds=1,
            )
        if result.channel is Channel.BROWSER:
            stealth = result.anti_bot_hint
            return self._decision(
                task, result, WorkerKind.BROWSER, Channel.BROWSER, ContentType.HTML,
                "嗅探判定需要 JS 渲染或存在反爬特征,升级到浏览器通道",
                timeout=self.browser_timeout, max_rounds=self.browser_max_rounds,
                requires_js=True, stealth=stealth, retry_hint=2 if stealth else 1,
            )
        if result.channel is Channel.DIRECT_JSON:
            return self._decision(
                task, result, WorkerKind.HTTP, Channel.DIRECT_JSON, ContentType.JSON,
                "嗅探判定为 JSON 接口,轻量通道直取",
                timeout=self.http_timeout, max_rounds=1,
            )
        if result.channel is Channel.PDF:
            return self._decision(
                task, result, WorkerKind.HTTP, Channel.PDF, ContentType.PDF,
                "嗅探判定为 PDF,轻量通道下载后走 PDF 提取",
                timeout=self.http_timeout, max_rounds=1,
            )
        return self._decision(
            task, result, WorkerKind.HTTP, Channel.LIGHTWEIGHT, ContentType.HTML,
            "嗅探判定为静态 HTML,轻量通道直取",
            timeout=self.http_timeout, max_rounds=1,
        )

    def route_url(self, url: str) -> ChannelDecision:
        """便捷方法:直接对一个 URL 做路由(构造一个临时 Task)。"""
        return self.route(Task(url=url))

    def route_browser(self, task: Task, reason: str = "升级到浏览器通道") -> ChannelDecision:
        """强制走浏览器通道(分层升级)。

        用于"轻量通道抓到的是空壳 HTML"这类情况:静态启发无法预知页面是否需要
        JS 渲染,只能先便宜地试一次,再按结果升级。

        :param task: 待路由任务。
        :param reason: 升级原因(写进决策说明)。
        :return: 路由决策。
        """
        result = sniff_url(task.url)
        return self._decision(
            task, result, WorkerKind.BROWSER, Channel.BROWSER, ContentType.HTML,
            reason,
            timeout=self.browser_timeout, max_rounds=self.browser_max_rounds,
            requires_js=True, stealth=result.anti_bot_hint, retry_hint=2,
        )

    @staticmethod
    def _decision(
        task: Task,
        result: SniffResult,
        worker: WorkerKind,
        channel: Channel,
        expected: ContentType,
        reason: str,
        *,
        timeout: float,
        max_rounds: int,
        requires_js: bool = False,
        stealth: bool = False,
        retry_hint: int = 1,
    ) -> ChannelDecision:
        """组装一条路由决策(静态方法,便于测试直接调用)。"""
        detail = "; ".join(result.reasons) if result.reasons else "无嗅探依据"
        return ChannelDecision(
            task_id=task.id,
            url=task.url,
            worker=worker,
            channel=channel,
            expected_content_type=expected,
            reason=f"{reason}(依据:{detail})",
            timeout=timeout,
            max_rounds=max_rounds,
            requires_js=requires_js,
            stealth=stealth,
            retry_hint=retry_hint,
        )
