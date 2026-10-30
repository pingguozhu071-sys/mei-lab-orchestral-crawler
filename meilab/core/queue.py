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
import uuid
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

#: 队列库的 schema 版本(用 PRAGMA user_version 记录,升级时只增不减)。
SCHEMA_VERSION = 2

#: 租约默认时长(秒):超过这个时间没心跳,任务会被回收重跑。
DEFAULT_LEASE_SECONDS = 120

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

CREATE TABLE IF NOT EXISTS media_downloads (
    id            TEXT PRIMARY KEY,
    task_id       TEXT,
    source_url    TEXT NOT NULL,
    platform      TEXT NOT NULL DEFAULT 'generic',
    media_id      TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'video',
    title         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    method        TEXT NOT NULL DEFAULT '',
    file_path     TEXT NOT NULL DEFAULT '',
    file_size     INTEGER NOT NULL DEFAULT 0,
    progress      REAL NOT NULL DEFAULT 0,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    meta          TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    UNIQUE (source_url, kind)
);

CREATE INDEX IF NOT EXISTS idx_media_status ON media_downloads (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_platform ON media_downloads (platform);
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
        """创建表与索引(幂等),并推进 schema 版本号。"""
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """按 ``PRAGMA user_version`` 记录并推进 schema 版本。

        建表语句都是 ``IF NOT EXISTS`` 的幂等形式;新增列用"先查列、再 ALTER"的
        幂等方式补齐。PRAGMA 不支持参数绑定,所以版本号与 ALTER 语句都写成字面量。
        """
        row = self._conn.execute("PRAGMA user_version").fetchone()
        current = int(row[0]) if row else 0
        if current >= SCHEMA_VERSION:
            return

        # v2:租约字段(幂等 ALTER,语句写死不走拼接)
        if not self._tasks_has_column("leased_at"):
            self._conn.execute("ALTER TABLE tasks ADD COLUMN leased_at REAL")
        if not self._tasks_has_column("lease_expires"):
            self._conn.execute("ALTER TABLE tasks ADD COLUMN lease_expires REAL")

        if SCHEMA_VERSION == 2:
            self._conn.execute("PRAGMA user_version = 2")
        elif SCHEMA_VERSION == 1:
            self._conn.execute("PRAGMA user_version = 1")
        else:
            raise RuntimeError(f"缺少 schema 版本 {SCHEMA_VERSION} 的迁移语句")

    def _tasks_has_column(self, column: str) -> bool:
        """判断 ``tasks`` 表是否已有某列(列名走参数绑定)。

        :param column: 列名。
        :return: 是否存在。
        """
        row = self._conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('tasks') WHERE name = ?",
            (column,),
        ).fetchone()
        return bool(row[0]) if row else False

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
        """原子地领取待执行任务(带租约)。

        使用 ``UPDATE ... RETURNING`` 在单条语句内完成 "选中 + 置为 running + 写租约",
        因此多进程并发 claim 不会拿到同一个任务。租约到期后任务会被回收重跑,
        避免"Worker 崩溃 → 任务永久卡在 running"。

        :param limit: 最多领取多少条。
        :param worker_id: 领取者标识,写入 ``tasks.worker_id`` 便于回收。
        :return: 领取到的任务列表(已按优先级降序、时间升序排列)。
        """
        if limit <= 0:
            return []
        now = time.time()
        expires = now + DEFAULT_LEASE_SECONDS
        rows = self._conn.execute(
            """
            UPDATE tasks
            SET status = 'running', worker_id = ?, leased_at = ?, lease_expires = ?, updated_at = ?
            WHERE id IN (
                SELECT id FROM tasks
                WHERE status = 'pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
            )
            RETURNING id, url, task_type, priority, status, retry_count, depth,
                      parent_id, payload, created_at, updated_at
            """,
            (worker_id, now, expires, now, limit),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def heartbeat(self, task_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
        """续租(长任务执行期间调用)。

        :param task_id: 任务 ID。
        :param lease_seconds: 新的租约时长(秒)。
        :return: 是否续租成功(任务已被别人接管时返回 ``False``)。
        """
        now = time.time()
        cur = self._conn.execute(
            "UPDATE tasks SET lease_expires = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (now + lease_seconds, now, task_id),
        )
        return int(cur.rowcount or 0) > 0

    def reclaim_expired(self, now: float | None = None) -> int:
        """回收**租约过期**的 running 任务(Worker 崩溃/失联时的自愈)。

        :param now: 当前时间戳(默认取系统时间)。
        :return: 回收条数。
        """
        moment = time.time() if now is None else now
        cur = self._conn.execute(
            "UPDATE tasks SET status = 'pending', worker_id = NULL, leased_at = NULL, "
            "lease_expires = NULL, updated_at = ? "
            "WHERE status = 'running' AND lease_expires IS NOT NULL AND lease_expires < ?",
            (moment, moment),
        )
        return int(cur.rowcount or 0)

    def mark_done_checked(self, task_id: str, leased_at: float | None = None) -> bool:
        """CAS 打卡:只有**当前租约持有者**才能把任务标记为完成。

        防止"两个 worker 同时处理完同一任务,后完成的那个覆盖先完成的结果"。

        :param task_id: 任务 ID。
        :param leased_at: 领取时拿到的租约时间戳;为空则不做租约校验。
        :return: 是否成功打卡(``True``=拿到所有权,``False``=已被接管)。
        """
        now = time.time()
        if leased_at is None:
            cur = self._conn.execute(
                "UPDATE tasks SET status = 'done', updated_at = ?, leased_at = NULL, "
                "lease_expires = NULL WHERE id = ? AND status = 'running'",
                (now, task_id),
            )
        else:
            cur = self._conn.execute(
                "UPDATE tasks SET status = 'done', updated_at = ?, leased_at = NULL, "
                "lease_expires = NULL WHERE id = ? AND status = 'running' AND leased_at = ?",
                (now, task_id, leased_at),
            )
        return int(cur.rowcount or 0) > 0

    def claim_by_urls(
        self, urls: list[str], limit: int = 1, worker_id: str | None = None
    ) -> list[Task]:
        """按 URL 精确领取任务(交互式会话用)。

        与 :meth:`claim` 的区别:只领取这些 URL 的 pending 任务,
        不会把队列里其它遗留任务一起捞走。

        :param urls: 目标 URL 列表。
        :param limit: 最多领取多少条。
        :param worker_id: 领取者标识(写入租约)。
        :return: 领取到的任务列表。
        """
        targets = [u for u in urls if u]
        if not targets or limit <= 0:
            return []
        now = time.time()
        expires = now + DEFAULT_LEASE_SECONDS
        collected: list[sqlite3.Row] = []
        for url in targets:
            if len(collected) >= limit:
                break
            rows = self._conn.execute(
                """
                UPDATE tasks
                SET status = 'running', worker_id = ?, leased_at = ?, lease_expires = ?, updated_at = ?
                WHERE id IN (
                    SELECT id FROM tasks
                    WHERE status = 'pending' AND url = ?
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                )
                RETURNING id, url, task_type, priority, status, retry_count, depth,
                          parent_id, payload, created_at, updated_at
                """,
                (worker_id, now, expires, now, url),
            ).fetchall()
            collected.extend(rows)
        return [self._row_to_task(r) for r in collected[:limit]]

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
                "UPDATE tasks SET status = 'pending', worker_id = NULL, leased_at = NULL, "
                "lease_expires = NULL, updated_at = ? WHERE status = 'running'",
                (now,),
            )
            return int(cur.rowcount or 0)
        total = 0
        for worker_id in worker_ids:
            cur = self._conn.execute(
                "UPDATE tasks SET status = 'pending', worker_id = NULL, leased_at = NULL, "
                "lease_expires = NULL, updated_at = ? "
                "WHERE status = 'running' AND worker_id = ?",
                (now, worker_id),
            )
            total += int(cur.rowcount or 0)
        return total

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

    # ------------------------------------------------------------------ 媒体

    def upsert_media(
        self,
        source_url: str,
        *,
        task_id: str | None = None,
        platform: str = "generic",
        media_id: str = "",
        kind: str = "video",
        title: str = "",
        status: str = "pending",
        method: str = "",
        file_path: str = "",
        file_size: int = 0,
        progress: float = 0.0,
        error: str = "",
        meta: dict[str, Any] | None = None,
    ) -> str:
        """写入/更新一条媒体下载记录(按 ``source_url + kind`` 幂等)。

        :param source_url: 来源 URL。
        :return: 记录 ID。
        """
        row = self._conn.execute(
            "SELECT id FROM media_downloads WHERE source_url = ? AND kind = ?",
            (source_url, kind),
        ).fetchone()
        now = time.time()
        record_id = str(row["id"]) if row is not None else uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT OR REPLACE INTO media_downloads (
                id, task_id, source_url, platform, media_id, kind, title, status,
                method, file_path, file_size, progress, retry_count, error, meta,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id, task_id, source_url, platform, media_id, kind, title,
                status, method, file_path, file_size, progress, 0, error,
                json.dumps(meta or {}, ensure_ascii=False), now, now,
            ),
        )
        return record_id

    def update_media(self, record_id: str, **fields: Any) -> None:
        """按字段更新一条媒体记录。

        未出现在 ``fields`` 里的列保持原值(用 ``None`` 作为"不修改"的哨兵,
        这样可以用一条固定的参数化 SQL 完成,不需要拼接语句)。

        :param record_id: 记录 ID。
        :param fields: 要更新的字段(``meta`` 传字典会自动转 JSON)。
        """
        if not fields:
            return
        meta = fields.get("meta")
        if isinstance(meta, dict):
            fields = dict(fields)
            fields["meta"] = json.dumps(meta, ensure_ascii=False)
        self._conn.execute(
            """
            UPDATE media_downloads SET
                status = COALESCE(?, status),
                method = COALESCE(?, method),
                file_path = COALESCE(?, file_path),
                file_size = COALESCE(?, file_size),
                progress = COALESCE(?, progress),
                error = COALESCE(?, error),
                title = COALESCE(?, title),
                platform = COALESCE(?, platform),
                media_id = COALESCE(?, media_id),
                retry_count = COALESCE(?, retry_count),
                meta = COALESCE(?, meta),
                updated_at = ?
            WHERE id = ?
            """,
            (
                fields.get("status"), fields.get("method"), fields.get("file_path"),
                fields.get("file_size"), fields.get("progress"), fields.get("error"),
                fields.get("title"), fields.get("platform"), fields.get("media_id"),
                fields.get("retry_count"), fields.get("meta"),
                time.time(), record_id,
            ),
        )

    def list_media(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """列出媒体下载记录。

        :param status: 只看某个状态;``None`` 看全部。
        :param limit: 最多返回条数。
        :return: 记录字典列表。
        """
        sql = "SELECT * FROM media_downloads"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["meta"] = json.loads(item.get("meta") or "{}")
            out.append(item)
        return out

    def media_stats(self) -> dict[str, int]:
        """按状态统计媒体下载记录。"""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM media_downloads GROUP BY status"
        ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

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
