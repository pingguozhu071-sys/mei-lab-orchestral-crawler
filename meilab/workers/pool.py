"""Worker 池:进程生命周期管理 + 崩溃检测重启 + 优雅停机。

职责边界:
* 池**只管进程**,不管任务语义——任务分发由 :meth:`WorkerPool.dispatch` 按
  :class:`~meilab.router.layer.ChannelDecision` 的 worker 类型投递;
* 每个 Worker 拥有**独立的输入队列**,池按"最空闲优先"投递,避免某个 Worker 堆积;
* 崩溃检测靠 ``is_alive()`` 轮询,死了就用同样配置重新拉起一个新进程;
* 停机时先发哨兵让 Worker 干完手上的活退出,超时再强制 terminate。

本模块只应在父进程使用;若在子进程里创建,必须放在
``if __name__ == "__main__":`` 守卫内(Windows spawn 要求)。
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..core.types import Task
from ..router.layer import ChannelDecision, WorkerKind
from .base import BaseWorker, WorkerResult
from .browser import BrowserWorker
from .http import HttpWorker


@dataclass
class WorkerSlot:
    """一个 Worker 槽位:进程 + 它自己的输入队列。"""

    kind: str
    inbox: "mp.Queue[Any]"
    process: BaseWorker
    started_at: float = field(default_factory=time.time)
    restarts: int = 0

    def is_alive(self) -> bool:
        """进程是否存活。"""
        return bool(self.process.is_alive())

    def pending(self) -> int:
        """队列中待处理的任务数(尽力而为,队列未提供时返回 0)。"""
        try:
            return int(self.inbox.qsize())
        except (NotImplementedError, OSError):
            return 0


class WorkerPool:
    """HTTP / 浏览器两类 Worker 的统一管理器。"""

    def __init__(
        self,
        http_workers: int = 2,
        browser_workers: int = 1,
        *,
        outbox: Any | None = None,
        headless: bool = True,
        device_library: Any | None = None,
        http_kwargs: dict[str, Any] | None = None,
        browser_kwargs: dict[str, Any] | None = None,
        max_per_domain: int = 20,
    ) -> None:
        """初始化 Worker 池(此时还不启动任何进程)。

        :param http_workers: HTTP Worker 数量。
        :param browser_workers: 浏览器 Worker 数量。
        :param outbox: 共享的结果队列;为空时自动创建(Manager 代理队列)。
        :param headless: 浏览器是否无头运行。
        :param device_library: 设备档案库(注入给浏览器 Worker)。
        :param http_kwargs: 透传给 :class:`HttpWorker` 的额外参数。
        :param browser_kwargs: 透传给 :class:`BrowserWorker` 的额外参数。
        :param max_per_domain: 同一域名同时在途的任务上限。

        .. note::
           队列使用 ``multiprocessing.Manager().Queue()`` 代理而非裸 ``mp.Queue``:
           Windows + spawn 下,裸队列作为 Process 子类的实例属性会在子进程
           反序列化时抛 ``PermissionError [WinError 5] DuplicateHandle``。
           代理队列基于套接字,可安全 pickle,代价是每次收发多一次 IPC 往返。
        """
        self.http_workers = max(0, int(http_workers))
        self.browser_workers = max(0, int(browser_workers))
        self.outbox = outbox
        self.headless = headless
        self.device_library = device_library
        self.http_kwargs = dict(http_kwargs or {})
        self.browser_kwargs = dict(browser_kwargs or {})
        self.max_per_domain = max(1, int(max_per_domain))
        self.slots: list[WorkerSlot] = []
        self._started = False
        self._manager: Any | None = None
        # 域名在途计数与"哪个槽位在跑哪个任务",用于每域上限与崩溃释放
        self._inflight_by_host: dict[str, int] = {}
        self._task_host: dict[str, str] = {}
        self._slot_tasks: dict[int, set[str]] = {}

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动所有 Worker 进程。重复调用是安全的(幂等)。"""
        if self._started:
            return
        ctx = mp.get_context("spawn")
        if self.outbox is None:
            self._manager = ctx.Manager()
            self.outbox = self._manager.Queue()
        for _ in range(self.http_workers):
            self.slots.append(self._spawn(ctx, WorkerKind.HTTP.value))
        for _ in range(self.browser_workers):
            self.slots.append(self._spawn(ctx, WorkerKind.BROWSER.value))
        self._started = True

    def _spawn(self, ctx: Any, kind: str) -> WorkerSlot:
        """创建一个 Worker 进程并返回槽位。"""
        if self._manager is None:
            self._manager = ctx.Manager()
        inbox = self._manager.Queue()
        if kind == WorkerKind.BROWSER.value:
            worker: BaseWorker = BrowserWorker(
                inbox,
                self.outbox,
                headless=self.headless,
                device_library=self.device_library,
                **self.browser_kwargs,
            )
        else:
            worker = HttpWorker(inbox, self.outbox, **self.http_kwargs)
        worker.start()
        return WorkerSlot(kind=kind, inbox=inbox, process=worker)

    def stop(self, timeout: float = 10.0) -> None:
        """优雅停机:先发哨兵,超时后强制结束。

        :param timeout: 等待进程退出的时间(秒)。
        """
        for slot in self.slots:
            try:
                slot.process.stop()
            except Exception:
                pass
        deadline = time.time() + timeout
        for slot in self.slots:
            remaining = max(0.0, deadline - time.time())
            try:
                slot.process.join(timeout=remaining)
            except Exception:
                pass
        for slot in self.slots:
            if slot.process.is_alive():
                try:
                    slot.process.terminate()
                    slot.process.join(timeout=2.0)
                except Exception:
                    pass
        if self._manager is not None:
            try:
                self._manager.shutdown()
            except Exception:
                pass
            self._manager = None
        self._started = False

    def __enter__(self) -> "WorkerPool":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ 分发

    def dispatch(self, task: Task, decision: ChannelDecision) -> bool:
        """把任务投递给对应类型的 Worker。

        同类型有多个 Worker 时,选队列里待办最少的那个(最空闲优先);
        **同一域名的在途任务不超过** ``max_per_domain``(超出则本次不派发,
        由调用方稍后重试,避免把一个站点打爆)。

        :param task: 待执行任务。
        :param decision: 路由决策,决定 worker 类型。
        :return: 是否成功投递。
        """
        kind = WorkerKind.BROWSER.value if decision.worker is WorkerKind.BROWSER else WorkerKind.HTTP.value
        candidates = [s for s in self.slots if s.kind == kind and s.is_alive()]
        if not candidates:
            return False

        host = ""
        try:
            host = (urlsplit(task.url).hostname or "").lower()
        except ValueError:
            host = ""
        if host and self._inflight_by_host.get(host, 0) >= self.max_per_domain:
            return False

        slot = min(candidates, key=lambda s: s.pending())
        try:
            slot.inbox.put(task)
        except Exception:
            return False
        if host:
            self._inflight_by_host[host] = self._inflight_by_host.get(host, 0) + 1
        self._task_host[task.id] = host
        self._slot_tasks.setdefault(id(slot), set()).add(task.id)
        return True

    def release(self, task_id: str) -> None:
        """释放一个已完成的在途名额(引擎收到结果后调用)。"""
        host = self._task_host.pop(task_id, None)
        if host:
            count = self._inflight_by_host.get(host, 0) - 1
            if count > 0:
                self._inflight_by_host[host] = count
            else:
                self._inflight_by_host.pop(host, None)
        for tasks in self._slot_tasks.values():
            tasks.discard(task_id)

    def inflight_by_host(self) -> dict[str, int]:
        """返回各域名的在途任务数(监控用)。"""
        return dict(self._inflight_by_host)

    def drain(self, max_items: int = 100, timeout: float = 0.5) -> list[WorkerResult]:
        """从结果队列里取回结果。

        :param max_items: 最多取多少条。
        :param timeout: 队列为空时的等待时间(秒)。
        :return: 结果列表。
        """
        out: list[WorkerResult] = []
        deadline = time.time() + timeout
        while len(out) < max_items:
            try:
                out.append(self.outbox.get(timeout=0.05))
            except queue.Empty:
                if time.time() >= deadline:
                    break
        return out

    # ------------------------------------------------------------------ 监控

    def reap(self) -> list[str]:
        """检测并重启崩溃的 Worker。

        :return: 被重启的槽位描述列表。
        """
        restarted: list[str] = []
        for index, slot in enumerate(list(self.slots)):
            if slot.is_alive():
                continue
            try:
                slot.process.join(timeout=0.1)
            except Exception:
                pass
            # 崩溃槽位在途的任务要释放名额,否则域名并发计数会一直占用
            for task_id in self._slot_tasks.pop(index, set()):
                self.release(task_id)
            ctx = mp.get_context("spawn")
            new_slot = self._spawn(ctx, slot.kind)
            new_slot.restarts = slot.restarts + 1
            self.slots[index] = new_slot
            restarted.append(f"{slot.kind}#{index}(重启{new_slot.restarts}次)")
        return restarted

    def alive_counts(self) -> dict[str, int]:
        """统计各类 Worker 的存活数量。"""
        counts: dict[str, int] = {}
        for slot in self.slots:
            if slot.is_alive():
                counts[slot.kind] = counts.get(slot.kind, 0) + 1
        return counts

    def status(self) -> list[dict[str, Any]]:
        """返回每个槽位的状态快照。"""
        out: list[dict[str, Any]] = []
        for slot in self.slots:
            out.append(
                {
                    "kind": slot.kind,
                    "alive": slot.is_alive(),
                    "pending": slot.pending(),
                    "restarts": slot.restarts,
                    "pid": getattr(slot.process, "pid", None),
                }
            )
        return out
