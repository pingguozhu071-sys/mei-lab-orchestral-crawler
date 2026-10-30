"""Worker 进程基类 + 网络出口安全校验(SSRF 防护)。

每个 Worker 都是一个独立的 :class:`multiprocessing.Process`:
* 从 ``inbox`` 队列取任务(``Task`` 或 ``STOP`` 哨兵);
* 由子类实现 :meth:`BaseWorker.handle`,产出可 pickle 的结果字典;
* 把 :class:`WorkerResult` 放回 ``outbox`` 队列。

基类只负责"进程骨架 + 异常兜底 + 优雅停机",具体抓取逻辑在子类里。
所有子进程代码都必须能通过 ``spawn`` 启动(Windows 默认),因此
Worker 实例本身必须可 pickle:只持有队列、Event 与普通配置。
"""

from __future__ import annotations

import ipaddress
import multiprocessing as mp
import queue
import socket
import traceback
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..core.types import Task

#: 停止哨兵:放进 inbox 即表示"干完手上的活就退出"。
STOP = "__STOP__"

#: 允许被抓取的协议白名单(其余一律拒绝,防 file:// / gopher / data: 读取)
ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass
class WorkerResult:
    """Worker 回传的结果。"""

    task_id: str = field(default="")
    ok: bool = field(default=False)
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = field(default="")
    channel: str = field(default="unknown")
    elapsed: float = field(default=0.0)


def resolve_host_ips(host: str) -> list[str]:
    """解析主机名的所有 IP 地址。

    :param host: 主机名或 IP 字面量。
    :return: IP 字符串列表;解析失败时返回空列表。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(str(ip))
    return out


def is_public_ip(ip: str) -> bool:
    """判断一个 IP 是否属于公网可路由地址。

    环回、私网、链路本地、保留、多播一律判为不安全。

    :param ip: IP 字面量。
    :return: 是否为公网地址。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return False
    if addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return False
    return bool(addr.is_global)


def check_url_safe(url: str, allow_private: bool = False) -> tuple[bool, str]:
    """校验一个 URL 是否可以安全发起请求(SSRF 防护)。

    校验内容:
    1. 协议必须在 http/https 白名单内;
    2. 主机名非空;
    3. 主机解析出的**所有** IP 都必须是公网地址(阻断 127.0.0.1 / 10.0.0.0/8 /
       169.254.169.254 云元数据 / 局域网地址)。

    :param url: 待校验 URL。
    :param allow_private: 是否允许访问内网地址(默认不允许,符合"仅采集公开页面"红线)。
    :return: ``(是否安全, 原因)``。
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return False, f"URL 非法: {exc!r}"
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return False, f"协议 {scheme!r} 不在允许列表 {sorted(ALLOWED_SCHEMES)}"
    host = parts.hostname
    if not host:
        return False, "URL 缺少主机名"
    if allow_private:
        return True, "允许内网访问(显式开启)"
    ips = resolve_host_ips(host)
    if not ips:
        return False, f"主机 {host!r} 无法解析出 IP 地址"
    for ip in ips:
        if not is_public_ip(ip):
            return False, f"目标 IP {ip}({host}) 不是公网地址,按 SSRF 防护策略拒绝"
    return True, f"目标 IP {','.join(ips)} 均为公网地址"


class BaseWorker(mp.Process):
    """Worker 进程抽象基类。

    子类必须实现 :meth:`handle`;可选覆写 :meth:`setup` 与 :meth:`teardown`
    (例如浏览器 Worker 在 setup 里启动 Chromium,teardown 里关闭)。
    """

    #: 通道名,会写进 :class:`WorkerResult.channel`
    channel_name: str = "base"

    def __init__(
        self,
        inbox: "mp.Queue[Any]",
        outbox: "mp.Queue[WorkerResult]",
        *,
        poll_interval: float = 0.2,
    ) -> None:
        """初始化 Worker。

        :param inbox: 任务队列,元素为 :class:`~meilab.core.types.Task` 或 :data:`STOP`。
        :param outbox: 结果队列,元素为 :class:`WorkerResult`。
        :param poll_interval: 空队列时的轮询间隔(秒)。

        .. note::
           Windows + spawn 下,``multiprocessing.Queue`` / ``Event`` 这类同步原语
           **不能**作为 Process 子类的实例属性:子进程反序列化实例时不会继承句柄,
           会抛 ``PermissionError [WinError 5] DuplicateHandle``。
           因此本类不使用 ``mp.Event``,停机靠 :data:`STOP` 哨兵;队列请使用
           ``multiprocessing.Manager().Queue()`` 代理(可安全 pickle)。
        """
        super().__init__(daemon=True)
        self.inbox = inbox
        self.outbox = outbox
        self.poll_interval = poll_interval
        self._stopping = False

    # ------------------------------------------------------------------ 生命周期

    def setup(self) -> None:
        """子进程启动时的初始化钩子(默认空实现)。"""

    def teardown(self) -> None:
        """子进程退出前的清理钩子(默认空实现)。"""

    def handle(self, task: Task) -> dict[str, Any]:
        """处理一个任务并返回结果字典(子类必须实现)。"""
        raise NotImplementedError("子类必须实现 handle()")

    def run(self) -> None:
        """进程主循环:取任务 → 处理 → 回传结果。"""
        try:
            self.setup()
        except Exception as exc:  # setup 失败:回传错误并退出
            self._emit(WorkerResult(ok=False, error=f"setup 失败: {exc!r}", channel=self.channel_name))
            return

        while not self._stopping:
            try:
                item = self.inbox.get(timeout=self.poll_interval)
            except queue.Empty:
                continue
            except (EOFError, OSError):  # 父进程已关闭队列
                break

            if item is None or item == STOP:
                break
            if not isinstance(item, Task):
                self._emit(WorkerResult(ok=False, error=f"收到非法任务类型: {type(item)!r}",
                                        channel=self.channel_name))
                continue

            try:
                payload = self.handle(item)
            except Exception as exc:
                self._emit(
                    WorkerResult(
                        task_id=item.id,
                        ok=False,
                        error=f"{exc!r}\n{traceback.format_exc(limit=5)}",
                        channel=self.channel_name,
                    )
                )
                continue
            self._emit(WorkerResult(task_id=item.id, ok=True, payload=payload,
                                    channel=self.channel_name))

        try:
            self.teardown()
        except Exception:  # 清理失败不应影响退出
            pass

    # ------------------------------------------------------------------ 控制

    def stop(self) -> None:
        """请求该 Worker 优雅停机(向 inbox 投 :data:`STOP` 哨兵)。"""
        self._stopping = True
        try:
            self.inbox.put(STOP)
        except Exception:
            pass

    def is_busy(self) -> bool:
        """粗略判断是否正在处理任务(仅用于监控)。"""
        return not self._stopping and self.inbox.empty()

    # ------------------------------------------------------------------ 内部

    def _emit(self, result: WorkerResult) -> None:
        """把结果放进 outbox,失败时静默忽略(父进程可能已退出)。"""
        try:
            self.outbox.put(result)
        except Exception:
            pass

    # ------------------------------------------------------------------ 序列化

    #: 运行时才产生的属性,不可 pickle,序列化时剔除
    RUNTIME_ATTRS: frozenset[str] = frozenset(
        {"_opener", "_curl", "_pw", "_browser", "_context"}
    )

    def __getstate__(self) -> dict[str, Any]:
        """序列化时剔除不可 pickle 的运行时对象。

        Windows 的 spawn 启动方式要求整个 Process 实例可 pickle;浏览器句柄、
        urllib opener(含 lambda)等运行时对象必须排除,否则父进程 pickle 失败,
        子进程会以 ``DuplicateHandle [WinError 5]`` 的形式报错(症状与根因不符)。
        """
        state = dict(self.__dict__)
        for name in self.RUNTIME_ATTRS:
            state.pop(name, None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        """反序列化后恢复运行时属性占位。"""
        self.__dict__.update(state)
        for name in self.RUNTIME_ATTRS:
            self.__dict__.setdefault(name, None)
