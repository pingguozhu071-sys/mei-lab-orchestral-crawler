"""SQLite 任务队列单测:幂等入队、并发 claim 不重复、状态机转换、断点续传。"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from meilab.core.queue import InvalidTransition, TaskQueue
from meilab.core.types import Episode, Outcome, Task, TaskStatus, TaskType


def _claim_worker(db_path: str, rounds: int, worker_id: str, out_q: "mp.Queue[list[str]]") -> None:
    """子进程 worker:反复 claim,把领到的任务 ID 放进队列。

    必须是模块级函数,Windows 的 spawn 启动方式才能 pickle 它。
    """
    q = TaskQueue(db_path)
    got: list[str] = []
    for _ in range(rounds):
        tasks = q.claim(1, worker_id=worker_id)
        if not tasks:
            break
        got.extend(t.id for t in tasks)
    out_q.put(got)
    q.close()


def test_enqueue_is_idempotent(tmp_path: Path) -> None:
    """同一 (url, task_type) 重复入队只落一行。"""
    q = TaskQueue(tmp_path / "q.db")
    t1 = Task(url="https://example.com/a", task_type=TaskType.CRAWL, priority=1)
    t2 = Task(url="https://example.com/a", task_type=TaskType.CRAWL, priority=5)
    id1 = q.enqueue(t1)
    id2 = q.enqueue(t2)
    assert id1 == id2
    rows = q.list_tasks()
    assert len(rows) == 1
    # 冲突时只抬升优先级,不新增任务
    assert rows[0].priority == 5
    assert rows[0].status is TaskStatus.PENDING
    q.close()


def test_different_task_type_creates_new_row(tmp_path: Path) -> None:
    """同 URL 不同任务类型应各自入队。"""
    q = TaskQueue(tmp_path / "q.db")
    q.enqueue(Task(url="https://example.com/x", task_type=TaskType.CRAWL))
    q.enqueue(Task(url="https://example.com/x", task_type=TaskType.JSON_API))
    assert len(q.list_tasks()) == 2
    q.close()


def test_claim_no_duplicate_under_concurrency(tmp_path: Path) -> None:
    """4 个并发进程抢 40 个任务:不重不漏。"""
    db = str(tmp_path / "q.db")
    q = TaskQueue(db)
    for i in range(40):
        q.enqueue(Task(url=f"https://example.com/{i}", priority=i % 5))
    q.close()

    ctx = mp.get_context("spawn")
    out_q: "mp.Queue[list[str]]" = ctx.Queue()
    procs = [
        ctx.Process(target=_claim_worker, args=(db, 20, f"w{i}", out_q))
        for i in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    claimed: list[str] = []
    for _ in procs:
        claimed.extend(out_q.get(timeout=10))
    assert len(claimed) == 40
    assert len(set(claimed)) == 40, "并发 claim 出现重复领取"
    q = TaskQueue(db)
    assert q.stats()[TaskStatus.PENDING.value] == 0
    assert q.stats()[TaskStatus.RUNNING.value] == 40
    q.close()


def test_state_machine_valid_transitions(tmp_path: Path) -> None:
    """pending → running → done 合法。"""
    q = TaskQueue(tmp_path / "q.db")
    tid = q.enqueue(Task(url="https://example.com/s"))
    q.transition(tid, TaskStatus.RUNNING)
    q.transition(tid, TaskStatus.DONE)
    assert q.list_tasks()[0].status is TaskStatus.DONE
    q.close()


def test_state_machine_rejects_illegal_transition(tmp_path: Path) -> None:
    """pending → done 非法,必须抛 InvalidTransition。"""
    q = TaskQueue(tmp_path / "q.db")
    tid = q.enqueue(Task(url="https://example.com/s"))
    with pytest.raises(InvalidTransition):
        q.transition(tid, TaskStatus.DONE)
    q.close()


def test_mark_failed_retry_then_dead_letter(tmp_path: Path) -> None:
    """失败先回到 failed,超过重试上限后进死信队列。"""
    q = TaskQueue(tmp_path / "q.db", max_retries=2)
    tid = q.enqueue(Task(url="https://example.com/f"))
    q.transition(tid, TaskStatus.RUNNING)
    assert q.mark_failed(tid) == TaskStatus.FAILED.value
    q.requeue(tid)
    q.transition(tid, TaskStatus.RUNNING)
    assert q.mark_failed(tid) == TaskStatus.DEAD_LETTER.value
    stats = q.stats()
    assert stats[TaskStatus.DEAD_LETTER.value] == 1
    assert stats[TaskStatus.FAILED.value] == 0
    q.close()


def test_reclaim_running_is_resume(tmp_path: Path) -> None:
    """进程崩溃后 reclaim 把 running 收回 pending。"""
    q = TaskQueue(tmp_path / "q.db")
    tid = q.enqueue(Task(url="https://example.com/r"))
    q.claim(1, worker_id="w1")
    assert q.stats()[TaskStatus.RUNNING.value] == 1
    assert q.reclaim_running() == 1
    assert q.stats()[TaskStatus.PENDING.value] == 1
    assert q.reclaim_running(worker_ids=["other"]) == 0
    q.close()


def test_episode_roundtrip(tmp_path: Path) -> None:
    """Episode 写入后可读回,JSON 字段被正确反序列化。"""
    q = TaskQueue(tmp_path / "q.db")
    tid = q.enqueue(Task(url="https://example.com/e"))
    ep = Episode(
        task_id=tid,
        url="https://example.com/e",
        outcome=Outcome.SUCCESS,
        rounds_runned=2,
        anti_bot_signals=["captcha"],
        channel="http",
        http_status=200,
        content_type="text/html",
        title="hello",
        content_hash="abc",
        content="# hello",
        raw_meta={"depth": 1},
    )
    q.save_episode(ep)
    rows = q.episodes(task_id=tid)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "success"
    assert rows[0]["anti_bot_signals"] == ["captcha"]
    assert rows[0]["raw_meta"] == {"depth": 1}
    assert q.outcome_stats()["success"] == 1
    q.close()


def test_enqueue_rejects_bad_url(tmp_path: Path) -> None:
    """非 http(s) 协议在模型层就被拒绝。"""
    with pytest.raises(ValueError):
        Task(url="file:///etc/passwd")
    q = TaskQueue(tmp_path / "q.db")
    q.close()
