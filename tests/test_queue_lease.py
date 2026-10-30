"""租约 / CAS / 回收 相关单测(离线)。"""

from __future__ import annotations

import time
from pathlib import Path

from meilab.core.queue import TaskQueue
from meilab.core.types import Task


def test_claim_writes_lease(tmp_path: Path) -> None:
    """领取任务要写租约( leased_at / lease_expires )与 worker_id。"""
    q = TaskQueue(tmp_path / "q.db")
    q.enqueue(Task(url="https://example.com/a"))
    tasks = q.claim(limit=1, worker_id="w1")
    assert len(tasks) == 1
    row = q.connection.execute(
        "SELECT worker_id, leased_at, lease_expires FROM tasks WHERE id = ?",
        (tasks[0].id,),
    ).fetchone()
    assert row["worker_id"] == "w1"
    assert row["leased_at"] is not None
    assert row["lease_expires"] > row["leased_at"]
    q.close()


def test_heartbeat_extends_lease(tmp_path: Path) -> None:
    """心跳能续租。"""
    q = TaskQueue(tmp_path / "q.db")
    q.enqueue(Task(url="https://example.com/a"))
    task = q.claim(limit=1, worker_id="w1")[0]
    before = q.connection.execute(
        "SELECT lease_expires FROM tasks WHERE id = ?", (task.id,)
    ).fetchone()[0]
    assert q.heartbeat(task.id, lease_seconds=600) is True
    after = q.connection.execute(
        "SELECT lease_expires FROM tasks WHERE id = ?", (task.id,)
    ).fetchone()[0]
    assert after > before
    q.close()


def test_expired_lease_is_reclaimed(tmp_path: Path) -> None:
    """租约过期的 running 任务会被回收成 pending(Worker 崩溃自愈)。"""
    q = TaskQueue(tmp_path / "q.db")
    q.enqueue(Task(url="https://example.com/a"))
    task = q.claim(limit=1, worker_id="w1")[0]
    # 模拟租约过期:把 lease_expires 改到过去
    q.connection.execute(
        "UPDATE tasks SET lease_expires = ? WHERE id = ?", (time.time() - 10, task.id)
    )
    assert q.reclaim_expired() == 1
    assert q.stats()["pending"] == 1
    assert q.stats()["running"] == 0
    q.close()


def test_unexpired_lease_is_kept(tmp_path: Path) -> None:
    """租约未到期的任务不会被回收。"""
    q = TaskQueue(tmp_path / "q.db")
    q.enqueue(Task(url="https://example.com/a"))
    q.claim(limit=1, worker_id="w1")
    assert q.reclaim_expired() == 0
    assert q.stats()["running"] == 1
    q.close()


def test_cas_blocks_double_completion(tmp_path: Path) -> None:
    """CAS:只有租约持有者能打卡,后来者拿不到所有权。"""
    q = TaskQueue(tmp_path / "q.db")
    q.enqueue(Task(url="https://example.com/a"))
    task = q.claim(limit=1, worker_id="w1")[0]
    leased_at = q.connection.execute(
        "SELECT leased_at FROM tasks WHERE id = ?", (task.id,)
    ).fetchone()[0]

    # 模拟:租约被回收后被别人重新领走(leased_at 变了)
    q.connection.execute(
        "UPDATE tasks SET leased_at = ? WHERE id = ?", (leased_at + 999, task.id)
    )
    assert q.mark_done_checked(task.id, leased_at=leased_at) is False
    assert q.stats()["running"] == 1  # 没有被错误标记为 done

    # 当前持有者可以打卡
    current = q.connection.execute(
        "SELECT leased_at FROM tasks WHERE id = ?", (task.id,)
    ).fetchone()[0]
    assert q.mark_done_checked(task.id, leased_at=current) is True
    assert q.stats()["done"] == 1
    q.close()


def test_cas_without_lease_still_requires_running(tmp_path: Path) -> None:
    """不校验租约时,仍要求任务处于 running 才能打卡。"""
    q = TaskQueue(tmp_path / "q.db")
    q.enqueue(Task(url="https://example.com/a"))
    task = q.claim(limit=1)[0]
    assert q.mark_done_checked(task.id) is True
    assert q.mark_done_checked(task.id) is False  # 已完成,不能再打
    q.close()


def test_schema_version_is_tracked(tmp_path: Path) -> None:
    """schema 版本号会被记录并推进。"""
    q = TaskQueue(tmp_path / "q.db")
    version = q.connection.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 2
    q.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """重复打开同一个库不会重复 ALTER 报错。"""
    path = tmp_path / "q.db"
    q = TaskQueue(path)
    q.close()
    q2 = TaskQueue(path)
    q2.close()
    q3 = TaskQueue(path)
    q3.close()
