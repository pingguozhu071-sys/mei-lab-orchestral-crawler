"""SQLite 任务队列:状态机 + 幂等入队 + 并发安全 claim + 断点续传。

设计要点:
* WAL 模式 + ``busy_timeout``,允许多个 worker 进程并发读写同一个库文件。
* ``tasks`` 表以 ``UNIQUE(url, task_type)`` 保证幂等:同一 URL + 同一任务类型只入队一次。
* 状态机 ``pending → running → done/failed → dead_letter`` 显式建模,
  非法跃迁抛 :class:`InvalidTransition`。
* 断点续传:进程重启时调用 :meth:`TaskQueue.reclaim_running`,把僵死的
  ``running`` 任务回收为 ``pending``(可指定 worker 存活标记做更精确的回收)。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .types import Episode, Outcome, Task, TaskStatus, TaskType

#: 允许的状态跃迁表。任何不在表内的跃迁都会被拒绝。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.PENDING.value: {TaskStatus.RUNNING.value, TaskStatus.DEAD_LETTER.value},
    TaskStatus.RUNNING.value: {
        TaskStatus.DONE.value,
        TaskStatus.FAILED.value,
        TaskStatus.PENDING.value,  # 回滚重试
    },
    TaskStatus.FAILED.value: {TaskStatus.PENDING.value, TaskStatus.DEAD_LETTER.value},
    TaskStatus.DONE.value: set(),
    TaskStatus.DEAD_LETTER.value: set(),
}

#: 默认最大重试次数,超过则进死信队列。
DEFAULT_MAX_RETRIES = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    priority     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending',
    retry_count  INTEGER NOT NULL DEFAULT 0,
    depth        INTEGER NOT NULL DEFAULT 0,
    parent_id    TEXT,
    payload      TEXT NOT NULL DEFAULT '{}',
    worker_id    TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    UNIQUE (url, task_type)
);

CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS episodes (
    id                TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL,
    url               TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    rounds_runned     INTEGER NOT NULL DEFAULT 0,
    anti_bot_signals  TEXT NOT NULL DEFAULT '[]',
    channel           TEXT NOT NULL DEFAULT 'unknown',
    http_status       INTEGER,
    content_type      TEXT,
    title             TEXT NOT NULL DEFAULT '',
    content_hash      TEXT NOT NULL DEFAULT '',
    content           TEXT NOT NULL DEFAULT '',
    raw_meta          TEXT NOT NULL DEFAULT '{}',
    error             TEXT NOT NULL DEFAULT '',
    started_at        REAL NOT NULL,
    finished_at       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_task ON episodes (task_id);
CREATE INDEX IF NOT EXISTS idx_episodes_outcome ON episodes (outcome);
"""


class InvalidTransition(RuntimeError):
    """非法的任务状态跃迁。"""


class TaskQueue:
    """基于 SQLite 的持久化任务队列。

    该类不是线程安全的:每个进程/线程应各自持有一个实例与自己的连接。
    """

    def __init__(self, db_path: str | os.PathLike[str], max_retries: int = DEFAULT_MAX_RETRIES) -> None:
        """初始化队列。

        :param db_path: SQLite 文件路径;``:memory:`` 仅供测试使用(单连接)。
        :param max_retries: 超过该重试次数的任务转入死信队列。
        """
        self.db_path = str(db_path)
        self.max_retries = max_retries
        self._conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    # ------------------------------------------------------------------ 基础

    def init_schema(self) -> None:
        """创建表与索引(幂等)。"""
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        """关闭底层连接。"""
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭失败无需再抛
            pass

    def __enter__(self) -> "TaskQueue":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """暴露底层连接(供引擎层做只读查询)。"""
        return self._conn

    # ------------------------------------------------------------------ 入队

    def enqueue(self, task: Task | None = None, **kwargs: Any) -> str:
        """幂等入队,返回任务 ID。

        若 ``(url, task_type)`` 已存在,不会插入新行,只会把 priority 抬升到
        两者较大值并刷新 ``updated_at``(等价于 "提高优先级但不重复执行")。

        :param task: 已构造好的 :class:`Task`;为 ``None`` 时用关键字参数构造。
        :param kwargs: 传给 :class:`Task` 的字段。
        """
        if task is None:
            task = Task(**kwargs)
        now = time.time()
        cur = self._conn.execute(
            """
            INSERT INTO tasks (id, url, task_type, priority, status, retry_count, depth,
                               parent_id, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)
            ON CONFLICT (url, task_type) DO UPDATE SET
                priority = MAX(tasks.priority, excluded.priority),
                updated_at = excluded.updated_at
            RETURNING id
            """,
            (
                task.id,
                task.url,
                task.task_type.value,
                task.priority,
                task.depth,
                task.parent_id,
                json.dumps(task.payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        row = cur.fetchone()
        return str(row["id"])

    def enqueue_many(self, tasks: Iterable[Task]) -> list[str]:
        """批量幂等入队,返回任务 ID 列表。"""
        return [self.enqueue(t) for t in tasks]

    # ------------------------------------------------------------------ claim

    def claim(self, limit: int = 1, worker_id: str | None = None) -> list[Task]:
        """原子地领取待执行任务。

        使用 ``UPDATE ... RETURNING`` 在单条语句内完成 "选中 + 置为 running",
        因此多进程并发 claim 不会拿到同一个任务。

        :param limit: 最多领取多少条。
        :param worker_id: 领取者标识,写入 ``tasks.worker_id`` 便于回收。
        :return: 领取到的任务列表(已按优先级降序、时间升序排列)。
        """
        if limit <= 0:
            return []
        now = time.time()
        rows = self._conn.execute(
            """
            UPDATE tasks SET status = 'running', worker_id = ?, updated_at = ?
            WHERE id IN (
                SELECT id FROM tasks
                WHERE status = 'pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
            )
            RETURNING id, url, task_type, priority, status, retry_count, depth,
                      parent_id, payload, created_at, updated_at
            """,
            (worker_id, now, limit),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def claim_by_urls(self, urls: list[str], limit: int = 1) -> list[Task]:
        """按 URL 精确领取任务(交互式会话用)。

        与 :meth:`claim` 的区别:只领取这些 URL 的 pending 任务,
        不会把队列里其它遗留任务一起捞走。

        :param urls: 目标 URL 列表。
        :param limit: 最多领取多少条。
        :return: 领取到的任务列表。
        """
        targets = [u for u in urls if u]
        if not targets or limit <= 0:
            return []
        now = time.time()
        marks = ",".join("?" for _ in targets)
        rows = self._conn.execute(
            f"""
            UPDATE tasks SET status = 'running', updated_at = ?
            WHERE id IN (
                SELECT id FROM tasks
                WHERE status = 'pending' AND url IN ({marks})
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
            )
            RETURNING id, url, task_type, priority, status, retry_count, depth,
                      parent_id, payload, created_at, updated_at
            """,
            (now, *targets, limit),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    # ------------------------------------------------------------ 状态与回写

    def transition(self, task_id: str, new_status: TaskStatus | str) -> None:
        """执行一次状态跃迁,非法跃迁抛 :class:`InvalidTransition`。

        :param task_id: 目标任务 ID。
        :param new_status: 目标状态。
        """
        target = new_status.value if isinstance(new_status, TaskStatus) else str(new_status)
        row = self._conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise InvalidTransition(f"任务不存在: {task_id}")
        current = str(row["status"])
        if target == current:
            return
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise InvalidTransition(f"非法状态跃迁: {current} → {target} (task={task_id})")
        self._conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (target, time.time(), task_id),
        )

    def mark_done(self, task_id: str) -> None:
        """任务完成。"""
        self.transition(task_id, TaskStatus.DONE)

    def mark_failed(self, task_id: str, retry: bool = True) -> str:
        """标记失败并按重试次数决定回滚重试还是进死信队列。

        :param task_id: 任务 ID。
        :param retry: 为 ``False`` 时直接进死信队列。
        :return: 失败后的实际状态字符串。
        """
        row = self._conn.execute(
            "SELECT status, retry_count FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise InvalidTransition(f"任务不存在: {task_id}")
        current = str(row["status"])
        retry_count = int(row["retry_count"]) + 1
        exhausted = retry_count >= self.max_retries
        if not retry or exhausted or current == TaskStatus.FAILED.value:
            target = TaskStatus.DEAD_LETTER.value
        else:
            target = TaskStatus.FAILED.value
        if current == TaskStatus.FAILED.value and target == TaskStatus.FAILED.value:
            # 已经是 failed,保持 failed(由下一次 mark_failed 决定是否死信)
            pass
        self._conn.execute(
            "UPDATE tasks SET status = ?, retry_count = ?, updated_at = ? WHERE id = ?",
            (target, retry_count, time.time(), task_id),
        )
        return target

    def requeue(self, task_id: str) -> None:
        """把 failed 任务放回 pending 以便重试。"""
        self.transition(task_id, TaskStatus.PENDING)

    def reclaim_running(self, worker_ids: Iterable[str] | None = None) -> int:
        """断点续传:回收僵死的 ``running`` 任务。

        :param worker_ids: 若给出,只回收这些 worker 名下的任务;
            为 ``None`` 时回收全部 running 任务(进程重启场景)。
        :return: 回收的任务条数。
        """
        now = time.time()
        if worker_ids is None:
            cur = self._conn.execute(
                "UPDATE tasks SET status = 'pending', worker_id = NULL, updated_at = ? "
                "WHERE status = 'running'",
                (now,),
            )
        else:
            ids = list(worker_ids)
            if not ids:
                return 0
            marks = ",".join("?" for _ in ids)
            cur = self._conn.execute(
                f"UPDATE tasks SET status = 'pending', worker_id = NULL, updated_at = ? "
                f"WHERE status = 'running' AND worker_id IN ({marks})",
                (now, *ids),
            )
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ Episode

    def save_episode(self, episode: Episode) -> str:
        """写入一条 Episode 记录(幂等:同 ID 覆盖)。"""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO episodes (
                id, task_id, url, outcome, rounds_runned, anti_bot_signals, channel,
                http_status, content_type, title, content_hash, content, raw_meta,
                error, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode.id,
                episode.task_id,
                episode.url,
                episode.outcome.value,
                episode.rounds_runned,
                json.dumps(episode.anti_bot_signals, ensure_ascii=False),
                episode.channel,
                episode.http_status,
                episode.content_type,
                episode.title,
                episode.content_hash,
                episode.content,
                json.dumps(episode.raw_meta, ensure_ascii=False),
                episode.error,
                episode.started_at,
                episode.finished_at,
            ),
        )
        return episode.id

    def episodes(self, task_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """读取 Episode 记录(按完成时间倒序)。

        :param task_id: 只看某个任务的记录;``None`` 看全部。
        :param limit: 最多返回条数。
        """
        sql = "SELECT * FROM episodes"
        params: list[Any] = []
        if task_id is not None:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        sql += " ORDER BY finished_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["anti_bot_signals"] = json.loads(d.get("anti_bot_signals") or "[]")
            d["raw_meta"] = json.loads(d.get("raw_meta") or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------------ 统计

    def stats(self) -> dict[str, int]:
        """按状态统计任务数量。"""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        out = {s.value: 0 for s in TaskStatus}
        for r in rows:
            out[str(r["status"])] = int(r["n"])
        return out

    def outcome_stats(self) -> dict[str, int]:
        """按 outcome 统计 Episode 数量。"""
        rows = self._conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM episodes GROUP BY outcome"
        ).fetchall()
        out = {o.value: 0 for o in Outcome}
        for r in rows:
            out[str(r["outcome"])] = int(r["n"])
        return out

    def list_tasks(self, status: TaskStatus | str | None = None, limit: int = 100) -> list[Task]:
        """列出任务,按优先级降序。"""
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status.value if isinstance(status, TaskStatus) else str(status))
        sql += " ORDER BY priority DESC, created_at ASC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_task(r) for r in rows]

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        """把数据库行还原成 :class:`Task` 对象。"""
        return Task(
            id=str(row["id"]),
            url=str(row["url"]),
            task_type=TaskType(str(row["task_type"])),
            priority=int(row["priority"]),
            status=TaskStatus(str(row["status"])),
            retry_count=int(row["retry_count"]),
            depth=int(row["depth"]),
            parent_id=row["parent_id"],
            payload=json.loads(row["payload"] or "{}"),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


def default_db_path(root: str | os.PathLike[str] = ".") -> Path:
    """返回默认的队列数据库路径(``<root>/data/queue.db``)。"""
    p = Path(root) / "data" / "queue.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
