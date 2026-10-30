"""主引擎:任务 → 嗅探 → 路由 → Worker 执行 → 提取 → 落库。

引擎本身**不抓取**,只做编排:把任务从队列里取出来,交给 Worker 池,
收到结果后提取内容、写 Episode 与内容仓库,再决定任务成功还是失败。

本模块是 spawn-safe 的:它只在**父进程**里创建子进程,
Worker 的创建逻辑封装在 :class:`~meilab.workers.pool.WorkerPool` 内。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .core.queue import TaskQueue, default_db_path
from .core.types import Episode, Outcome, Task, TaskStatus
from .extract.pipeline import ExtractionPipeline
from .router.layer import LayeredRouter, WorkerKind
from .router.sniff import sniff_url
from .stealth.profiles import DeviceLibrary
from .storage.export import export as export_records
from .storage.vault import Vault
from .workers.base import WorkerResult
from .workers.pool import WorkerPool

#: 登录/注册类路径:本框架只采集公开页面,遇到这些一律不跟
AUTH_PATH_HINTS = (
    "/login", "/signin", "/sign-in", "/signup", "/sign-up", "/register",
    "/logout", "/account", "/auth/", "/password", "/session",
)


@dataclass
class EngineConfig:
    """引擎配置。"""

    db_path: str = ""
    output_dir: str = "data"
    http_workers: int = 2
    browser_workers: int = 1
    delay: float = 1.5
    jitter: float = 0.5
    timeout: float = 20.0
    retries: int = 2
    headless: bool = True
    obey_robots: bool = True
    use_curl: bool = True
    max_depth: int = 1
    max_links_per_page: int = 20
    max_inflight: int = 8
    vault_key: str | None = None
    cookie_file: str | None = None
    captcha_retry: bool = True
    max_per_domain: int = 20
    verbose: bool = False

    def resolved_db_path(self) -> str:
        """返回最终的队列数据库路径(默认 ``<output_dir>/queue.db``)。"""
        if self.db_path:
            return self.db_path
        path = Path(self.output_dir) / "queue.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def resolved_vault_path(self) -> str:
        """返回内容仓库路径(与队列库同目录,文件名 ``vault.db``)。"""
        if self.db_path:
            return str(Path(self.db_path).with_name("vault.db"))
        path = Path(self.output_dir) / "vault.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)


@dataclass
class CrawlSummary:
    """一次抓取的统计。"""

    queued: int = 0
    dispatched: int = 0
    succeeded: int = 0
    failed: int = 0
    blocked: int = 0
    unchanged: int = 0
    escalated: int = 0
    elapsed: float = 0.0
    channel_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """转成字典(便于 CLI 打印与 JSON 输出)。"""
        return {
            "queued": self.queued,
            "dispatched": self.dispatched,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "blocked": self.blocked,
            "unchanged": self.unchanged,
            "escalated": self.escalated,
            "elapsed": round(self.elapsed, 2),
            "channel_counts": dict(self.channel_counts),
            "errors": self.errors[:10],
            "urls": self.urls[:20],
        }


class Engine:
    """爬虫主引擎。"""

    def __init__(self, config: EngineConfig | None = None) -> None:
        """初始化引擎。

        :param config: 引擎配置;为 ``None`` 时用默认配置。
        """
        self.config = config or EngineConfig()
        self.router = LayeredRouter(http_timeout=self.config.timeout)
        self.pipeline = ExtractionPipeline()
        self.device_library = DeviceLibrary()
        self.queue = TaskQueue(self.config.resolved_db_path())
        self.vault = Vault(self.config.resolved_vault_path(), key=self.config.vault_key)
        self._pool: WorkerPool | None = None
        # 领取者标识:写进任务的 worker_id,便于定位是谁领的、回收谁的
        self._worker_id = f"engine-{os.getpid()}"

    def close(self) -> None:
        """关闭 Worker 池与数据库连接。"""
        try:
            self._shutdown_pool()
        finally:
            try:
                self.queue.close()
            finally:
                self.vault.close()

    # ------------------------------------------------------------------ Worker 池

    def _build_pool(self) -> WorkerPool:
        """按配置构造 Worker 池。"""
        return WorkerPool(
            http_workers=self.config.http_workers,
            browser_workers=self.config.browser_workers,
            headless=self.config.headless,
            device_library=self.device_library,
            max_per_domain=self.config.max_per_domain,
            http_kwargs={
                "delay": self.config.delay,
                "jitter": self.config.jitter,
                "timeout": self.config.timeout,
                "retries": self.config.retries,
                "obey_robots": self.config.obey_robots,
                "use_curl": self.config.use_curl,
            },
            browser_kwargs={"timeout": max(self.config.timeout, 45.0)},
        )

    def _ensure_pool(self) -> WorkerPool:
        """取得可复用的 Worker 池(交互式会话里避免每次重起进程)。"""
        if self._pool is None:
            self._pool = self._build_pool()
            self._pool.start()
        return self._pool

    def _shutdown_pool(self) -> None:
        """停止 Worker 池。"""
        if self._pool is not None:
            try:
                self._pool.stop()
            except Exception:
                pass
            self._pool = None

    # ------------------------------------------------------------------ 主循环

    def crawl(self, urls: list[str], max_tasks: int = 0) -> CrawlSummary:
        """抓取一批 URL(主循环)。

        :param urls: 种子 URL 列表。
        :param max_tasks: 最多处理多少个任务(0 = 不限)。
        :return: 抓取统计。
        """
        summary = CrawlSummary()
        started = time.time()
        limit = max_tasks if max_tasks and max_tasks > 0 else 0

        # 断点续传:回收上次崩溃留下的 running 任务
        self.queue.reclaim_running()

        for url in urls:
            self.queue.enqueue(Task(url=url))
            summary.queued += 1
        pending_urls = list(urls)
        # 显式点名的 URL 允许重跑(之前抓过也要再抓一次)
        self._reset_urls(urls)

        pool = self._build_pool()
        pool.start()

        inflight = 0
        processed = 0
        try:
            while True:
                if limit and processed >= limit:
                    break

                # 1) 取任务并派发
                #    显式指定的 URL 优先——否则队列里的历史遗留任务会插队,
                #    用户明明点了某个链接,跑出来的却是别页面。
                if inflight < self.config.max_inflight:
                    # 剩余预算:--max 1 时只领 1 个,别把队列里的遗留任务一起发出去。
                    # 在途任务(inflight)也占预算,否则空转等待时会多派发。
                    budget = (limit - processed) if limit else self.config.max_inflight
                    capacity = max(0, min(self.config.max_inflight - inflight, budget - inflight))
                    tasks = self._claim_prioritised(pending_urls, capacity)
                    for task in tasks:
                        if task.url in pending_urls:
                            pending_urls.remove(task.url)
                        if task.payload.get("escalate") == "browser":
                            decision = self.router.route_browser(
                                task, reason="轻量通道抓到空壳,分层升级到浏览器通道")
                        else:
                            decision = self.router.route(task, sniff_url(task.url))
                        summary.channel_counts[decision.worker.value] = (
                            summary.channel_counts.get(decision.worker.value, 0) + 1
                        )
                        if decision.worker is WorkerKind.MEDIA:
                            # 媒体任务不进 Worker 池:由 media/ 子系统直接处理
                            self._handle_media_task(task, summary)
                            continue
                        if pool.dispatch(task, decision):
                            inflight += 1
                            summary.dispatched += 1
                        else:
                            self.queue.mark_failed(task.id, retry=False)
                            summary.failed += 1
                            summary.errors.append(f"无可用 Worker: {task.url}")

                # 2) 收回结果
                for result in pool.drain(max_items=10, timeout=0.4):
                    inflight = max(0, inflight - 1)
                    pool.release(result.task_id)  # 释放域名在途名额
                    self._handle_result(result, summary)
                    processed += 1

                # 3) 崩溃检测与重启
                restarted = pool.reap()
                for note in restarted:
                    summary.errors.append(f"Worker 重启: {note}")

                # 3b) 回收租约过期的任务(Worker 崩溃 → 任务不会永久卡住)
                expired = self.queue.reclaim_expired()
                if expired:
                    summary.errors.append(f"回收租约过期任务 {expired} 条")

                # 4) 退出条件
                stats = self.queue.stats()
                if stats.get(TaskStatus.PENDING.value, 0) == 0 and inflight == 0:
                    break
                if not stats.get(TaskStatus.PENDING.value, 0) and not pool.status():
                    break
                time.sleep(0.15)
        finally:
            pool.stop()

        summary.elapsed = time.time() - started
        return summary

    def crawl_now(self, url: str) -> dict[str, Any]:
        """立刻抓一个 URL(交互式会话用):只处理这一个任务,不牵连队列里的其它任务。

        复用常驻 Worker 池,因此连续粘贴多条链接时不必反复起进程。

        :param url: 目标 URL(调用方需先做协议/地址校验)。
        :return: 结果摘要 ``{"ok", "url", "channel", "title", "length", "elapsed", "error"}``。
        """
        started = time.time()
        outcome: dict[str, Any] = {
            "ok": False, "url": url, "channel": "", "title": "",
            "length": 0, "elapsed": 0.0, "error": "", "signals": [],
        }
        try:
            self.queue.enqueue(Task(url=url))
            # 交互场景:同一个 URL 可能之前已经抓过(done/failed),
            # 入队是幂等的(只抬优先级),这里显式重置为 pending 以便重跑。
            self.queue.connection.execute(
                "UPDATE tasks SET status = 'pending', retry_count = 0, updated_at = ? "
                "WHERE url = ? AND status IN ('done', 'failed', 'dead_letter')",
                (time.time(), url),
            )
        except Exception as exc:
            outcome["error"] = f"入队失败: {exc!r}"
            outcome["elapsed"] = time.time() - started
            return outcome

        pool = self._ensure_pool()
        # 最多两轮:轻量通道抓到空壳 → 引擎把它标成待升级 → 第二轮走浏览器通道
        for _attempt in range(2):
            claimed = self.queue.claim_by_urls([url], limit=1, worker_id=self._worker_id)
            if not claimed:
                break
            task = claimed[0]
            if task.payload.get("escalate") == "browser":
                decision = self.router.route_browser(
                    task, reason="轻量通道抓到空壳,分层升级到浏览器通道")
            else:
                decision = self.router.route(task, sniff_url(task.url))
            outcome["channel"] = decision.worker.value
            if not pool.dispatch(task, decision):
                outcome["error"] = "没有可用的 Worker"
                outcome["elapsed"] = time.time() - started
                return outcome

            deadline = time.time() + max(60.0, self.config.timeout * 3)
            got = False
            while time.time() < deadline and not got:
                for result in pool.drain(max_items=1, timeout=1.0):
                    summary = CrawlSummary()
                    self._handle_result(result, summary)
                    payload = result.payload or {}
                    outcome["signals"] = list(payload.get("signals") or [])
                    outcome["url"] = str(payload.get("final_url") or url)
                    outcome["channel"] = result.channel
                    if summary.succeeded:
                        outcome["ok"] = True
                    else:
                        outcome["error"] = (summary.errors[0] if summary.errors else "抓取失败")
                    got = True
            if outcome["ok"] or not got:
                break

        outcome["elapsed"] = time.time() - started
        return outcome

    def latest_episode(self, url: str) -> Episode | None:
        """取某个 URL 最近一次抓取的 Episode(交互式会话显示详情用)。

        :param url: 目标 URL。
        :return: Episode;没有则返回 ``None``。
        """
        rows = self.queue.episodes(limit=50)
        for row in rows:
            if row.get("url") == url:
                return Episode(
                    id=str(row["id"]),
                    task_id=str(row["task_id"]),
                    url=str(row["url"]),
                    outcome=Outcome(str(row["outcome"])),
                    rounds_runned=int(row["rounds_runned"]),
                    anti_bot_signals=list(row.get("anti_bot_signals") or []),
                    channel=str(row["channel"]),
                    http_status=row.get("http_status"),
                    content_type=row.get("content_type") or "",
                    title=str(row.get("title") or ""),
                    content_hash=str(row.get("content_hash") or ""),
                    content=str(row.get("content") or ""),
                    raw_meta=dict(row.get("raw_meta") or {}),
                    error=str(row.get("error") or ""),
                    started_at=float(row.get("started_at") or 0.0),
                    finished_at=float(row.get("finished_at") or 0.0),
                )
        return None

    def resume(self, max_tasks: int = 0) -> CrawlSummary:
        """断点续传:继续处理队列里剩下的任务。

        :param max_tasks: 最多处理多少个任务。
        :return: 抓取统计。
        """
        pending = self.queue.stats().get(TaskStatus.PENDING.value, 0)
        summary = self.crawl([], max_tasks=max_tasks)
        summary.queued = pending  # 续传处理的是既有任务,不是新入队
        return summary

    # ------------------------------------------------------------------ 结果处理

    def _reset_urls(self, urls: list[str]) -> None:
        """把指定 URL 的任务重置为 pending(允许重跑已抓过的页面)。

        :param urls: URL 列表。
        """
        targets = [u for u in urls if u]
        if not targets:
            return
        now = time.time()
        for url in targets:
            try:
                self.queue.connection.execute(
                    "UPDATE tasks SET status = 'pending', retry_count = 0, updated_at = ? "
                    "WHERE url = ? AND status IN ('done', 'failed', 'dead_letter')",
                    (now, url),
                )
            except Exception:
                pass

    def _try_solve_captcha(self, url: str, payload: dict[str, Any]) -> str:
        """尝试识别验证码并回填。

        :param url: 触发挑战的 URL。
        :param payload: Worker 回传的内容。
        :return: 识别出的文本;失败返回空串。
        """
        from .stealth.captcha import solve

        image = payload.get("captcha_image")
        background = payload.get("captcha_background")
        piece = payload.get("captcha_piece")
        if not (image or background):
            return ""
        result = solve(payload.get("html") or "", image=image,
                       background=background, piece=piece)
        if not result.ok:
            return ""
        if result.kind == "slider":
            return str(result.meta.get("gap_x", ""))
        return result.text

    def _retry_task(self, task_id: str) -> None:
        """把任务放回 pending 以便重试(验证码回填场景)。"""
        try:
            self.queue.transition(task_id, TaskStatus.PENDING)
        except Exception:
            pass

    def _handle_media_task(self, task: Task, summary: CrawlSummary) -> None:
        """处理一个媒体任务:走 media/ 子系统(平台解析器 → 通用 yt-dlp 通道)。

        :param task: 媒体任务。
        :param summary: 统计累加器。
        """
        from .media import download_media

        target_dir = Path(self.config.output_dir) / "media"
        try:
            result = download_media(
                task.url,
                target_dir,
                cookies=self.config.cookie_file,
                timeout=self.config.timeout,
                queue=self.queue,
            )
        except Exception as exc:
            self.queue.mark_failed(task.id, retry=False)
            summary.failed += 1
            summary.errors.append(f"媒体任务异常 {task.url}: {exc!r}")
            return

        episode = Episode(
            task_id=task.id,
            url=task.url,
            outcome=Outcome.SUCCESS if result.ok else Outcome.FAILED,
            rounds_runned=1,
            anti_bot_signals=[],
            channel="media",
            title=result.title,
            content="\n".join(result.files),
            raw_meta={
                "platform": result.platform,
                "method": result.method,
                "files": result.files,
                "bytes": result.bytes,
                "duration": result.duration,
                "note": result.note,
            },
            error=result.error,
        )
        self.queue.save_episode(episode)
        if result.ok:
            summary.succeeded += 1
            if task.url not in summary.urls:
                summary.urls.append(task.url)
        else:
            summary.failed += 1
            summary.errors.append(f"{task.url}: {result.error or '媒体抓取失败'}")

    def _claim_prioritised(self, preferred: list[str], limit: int) -> list[Task]:
        """领取任务:优先给指定的 URL,剩下的名额再从通用队列里取。

        :param preferred: 用户显式指定的 URL 列表。
        :param limit: 最多领取多少条。
        :return: 领取到的任务列表。
        """
        tasks: list[Task] = []
        remaining = max(0, limit)
        if preferred and remaining:
            picked = self.queue.claim_by_urls(
                preferred, limit=remaining, worker_id=self._worker_id
            )
            tasks.extend(picked)
            remaining -= len(picked)
        if remaining:
            tasks.extend(self.queue.claim(limit=remaining, worker_id=self._worker_id))
        return tasks

    def _handle_result(self, result: WorkerResult, summary: CrawlSummary) -> None:
        """把 Worker 结果转成 Episode,提取内容并落库。"""
        payload = result.payload or {}
        url = str(payload.get("url") or "")
        if url and url not in summary.urls:
            summary.urls.append(url)
        status = payload.get("status")
        content_type = str(payload.get("content_type") or "")
        signals = list(payload.get("signals") or [])
        ok = bool(payload.get("ok"))

        episode = Episode(
            task_id=result.task_id,
            url=url,
            outcome=Outcome.SUCCESS,
            rounds_runned=1,
            anti_bot_signals=signals,
            channel=result.channel,
            http_status=int(status) if isinstance(status, int) else None,
            content_type=content_type,
        )

        if not ok:
            # 命中验证码 → 尝试识别并回填重试一次(识别不了直接入死信,不无限重试)
            if "captcha" in signals and self.config.captcha_retry:
                solved = self._try_solve_captcha(url, payload)
                if solved:
                    episode.outcome = Outcome.DEGRADED
                    episode.error = f"验证码已识别({solved}),回填后重试"
                    episode.raw_meta = {"captcha_solved": True, "captcha_text_len": len(solved)}
                    summary.errors.append(f"{url}: {episode.error}")
                    self._retry_task(result.task_id)
                    self.queue.save_episode(episode)
                    return

            episode.outcome = Outcome.BLOCKED if signals else Outcome.FAILED
            episode.error = str(payload.get("error") or result.error or "未知错误")
            if signals and "captcha" in signals:
                episode.error += "(验证码识别不可用,按降级策略入死信)"
            summary.failed += 1
            if signals:
                summary.blocked += 1
            summary.errors.append(f"{url}: {episode.error[:120]}")
            self._finish_task(result.task_id, success=False)
            self.queue.save_episode(episode)
            return

        # 提取内容:浏览器通道给的是 html,HTTP 通道给的是原始字节
        body: Any = payload.get("html")
        if body is None:
            body = payload.get("body")
        extraction = self.pipeline.extract(body, content_type, url=url)
        episode.title = extraction.title
        episode.content = extraction.content
        episode.content_hash = extraction.content_hash
        episode.raw_meta = dict(extraction.meta)
        episode.raw_meta["depth"] = self._task_depth(result.task_id)
        episode.raw_meta["device_profile"] = payload.get("device_profile")
        episode.raw_meta["engine"] = payload.get("engine")
        episode.raw_meta["elapsed"] = payload.get("elapsed")

        extracted_len = int(extraction.meta.get("text_length", len(extraction.content)) or 0)
        if not extraction.content.strip() or extracted_len < 200:
            # 分层升级:轻量通道只提到"导航栏级"的文字(JS 渲染页的典型特征)
            # → 交给浏览器通道重试一次
            if (
                result.channel == "http"
                and self._looks_like_shell(body)
                and self._escalate(result.task_id, summary)
            ):
                episode.outcome = Outcome.DEGRADED
                episode.error = "空壳 HTML,已升级到浏览器通道重试"
                episode.raw_meta["escalated"] = True
                self.queue.save_episode(episode)
                return
            episode.outcome = Outcome.DEGRADED
            episode.error = extraction.error or "未提取到内容"
            summary.failed += 1
            summary.errors.append(f"{url}: {episode.error}")
            self._finish_task(result.task_id, success=False)
            self.queue.save_episode(episode)
            return

        # 落内容仓库(增量 diff)
        try:
            record, changed = self.vault.store(
                extraction.content,
                url=url,
                title=extraction.title,
                meta={"content_hash": extraction.content_hash, "kind": extraction.kind},
                content_hash=extraction.content_hash,
            )
            episode.raw_meta["vault_version"] = record.version
            episode.raw_meta["vault_changed"] = changed
            if not changed:
                summary.unchanged += 1
        except Exception as exc:
            episode.raw_meta["vault_error"] = repr(exc)

        summary.succeeded += 1
        self._finish_task(result.task_id, success=True)
        self.queue.save_episode(episode)

        # URL 发现:把同域新链接入队(受深度与数量限制)
        self._discover_links(url, body, episode, summary)

    def _looks_like_shell(self, body: Any) -> bool:
        """判断响应是不是"空壳 HTML"(正文要靠 JS 渲染)。

        判据:① 页面里有脚本;② **最长的一段可见文本**很短(导航标签而已),
        说明真正的正文是脚本生成的,静态 HTML 只是个壳。

        :param body: 响应体。
        :return: 是否为空壳。
        """
        if body is None:
            return False
        if isinstance(body, (bytes, bytearray)):
            text = bytes(body[:65536]).decode("utf-8", errors="replace")
        else:
            text = str(body)[:65536]
        if "<script" not in text.lower():
            return False

        import re

        without_scripts = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        blocks = re.findall(r">([^<>]+)<", without_scripts)
        longest = max((len(block.strip()) for block in blocks), default=0)
        return longest < 200

    def _escalate(self, task_id: str, summary: CrawlSummary) -> bool:
        """把任务升级到浏览器通道(只升级一次,避免打转)。

        :param task_id: 任务 ID。
        :param summary: 统计累加器。
        :return: 是否成功升级。
        """
        try:
            row = self.queue.connection.execute(
                "SELECT payload FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            payload = json.loads(row["payload"] or "{}") if row is not None else {}
            if payload.get("escalate"):
                return False  # 已经升级过一次
            payload["escalate"] = "browser"
            self.queue.transition(task_id, TaskStatus.PENDING)
            self.queue.connection.execute(
                "UPDATE tasks SET payload = ?, priority = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), 5, task_id),
            )
            summary.escalated += 1
            return True
        except Exception as exc:
            summary.errors.append(f"升级到浏览器通道失败: {exc!r}")
            return False

    def _task_depth(self, task_id: str) -> int:
        """查任务的 URL 深度(找不到按 0 处理)。"""
        try:
            row = self.queue.connection.execute(
                "SELECT depth FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return int(row["depth"]) if row is not None else 0
        except Exception:
            return 0

    def _discover_links(
        self,
        url: str,
        body: Any,
        episode: Episode,
        summary: CrawlSummary,
    ) -> None:
        """从页面里发现同域链接并入队。"""
        depth = int(episode.raw_meta.get("depth", 0) or 0)
        if depth >= self.config.max_depth:
            return
        if not body:
            return
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8", errors="replace")
        if not isinstance(body, str):
            return
        try:
            from .extract.html import extract_links, soup_of

            links = extract_links(soup_of(body), base_url=url)
        except Exception:
            return

        host = (urlsplit(url).hostname or "").lower()
        count = 0
        for link in links:
            if count >= self.config.max_links_per_page:
                break
            parts = urlsplit(link)
            if parts.scheme not in ("http", "https"):
                continue
            link_host = (parts.hostname or "").lower()
            if not link_host or link_host != host:
                continue  # 只跟同域,避免跑偏
            if any(hint in parts.path.lower() for hint in AUTH_PATH_HINTS):
                continue  # 登录/注册类页面不采集(只做公开页面)
            self.queue.enqueue(Task(url=link, depth=depth + 1))
            summary.queued += 1
            count += 1

    def _finish_task(self, task_id: str, *, success: bool) -> None:
        """回写任务状态(用 CAS 打卡,避免覆盖别人已完成的结果)。"""
        try:
            if success:
                self.queue.mark_done_checked(task_id)
            else:
                self.queue.mark_failed(task_id, retry=True)
        except Exception:
            pass

    # ------------------------------------------------------------------ 统计与导出

    def stats(self) -> dict[str, Any]:
        """返回队列与仓库的统计。"""
        queue_stats = self.queue.stats()
        outcome_stats = self.queue.outcome_stats()
        vault_stats = self.vault.stats()
        channels: dict[str, int] = {}
        for row in self.queue.connection.execute(
            "SELECT channel, COUNT(*) AS n FROM episodes GROUP BY channel"
        ):
            channels[str(row["channel"])] = int(row["n"])
        total = sum(channels.values()) or 1
        lightweight = channels.get("http", 0)
        browser = channels.get("browser", 0)
        return {
            "tasks": queue_stats,
            "episodes": outcome_stats,
            "vault": vault_stats,
            "channels": channels,
            "channel_ratio": {
                "lightweight": round(lightweight / total, 4),
                "browser": round(browser / total, 4),
            },
            "domains": self.vault.domains()[:20],
        }

    def export(self, fmt: str = "markdown", output_dir: str | None = None) -> dict[str, Any]:
        """导出仓库内容。

        :param fmt: 导出格式。
        :param output_dir: 输出目录;为空用配置里的 ``output_dir``。
        :return: 导出摘要。
        """
        target = output_dir or self.config.output_dir
        records = self.vault.list_records(limit=100000)
        return export_records(records, target, fmt)
