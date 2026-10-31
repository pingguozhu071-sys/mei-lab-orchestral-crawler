"""可观测与可恢复:stats.jsonl / checkpoint.json / deadletter.jsonl。

三个文件各管一件事:

* **stats.jsonl** —— 每完成一个任务追加一行(事件流,便于事后聚合);
* **checkpoint.json** —— 当前进度快照(可恢复的最小状态);
* **deadletter.jsonl** —— 批量落库失败的兜底记录,避免"静默丢失"。

写入一律用"先写临时文件再原子改名"或"追加 + flush",保证进程被杀也不丢已写内容。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StatsWriter:
    """事件流写入器(``stats.jsonl``)。"""

    path: Path
    enabled: bool = True
    _handle: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """确保目录存在。"""
        self.path = Path(self.path)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        """追加一条事件。

        :param event: 事件名。
        :param fields: 附加字段。
        """
        if not self.enabled:
            return
        payload = {"event": event, "ts": round(time.time(), 3), **fields}
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 统计写不进去不该拖垮主流程

    def read_all(self, limit: int = 1000) -> list[dict[str, Any]]:
        """读回全部事件(排障用)。

        :param limit: 最多返回条数。
        :return: 事件列表。
        """
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            return []
        return out


@dataclass
class Checkpoint:
    """进度快照(``checkpoint.json``)。"""

    path: Path
    enabled: bool = True
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """加载已有快照。"""
        self.path = Path(self.path)
        if self.enabled and self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}
        elif self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def update(self, **fields: Any) -> None:
        """更新快照字段并落盘(原子改名)。

        :param fields: 要更新的字段。
        """
        if not self.enabled:
            return
        self.data.update(fields)
        self.data["updated_at"] = round(time.time(), 3)
        self.save()

    def save(self) -> None:
        """把快照写到磁盘(临时文件 + 原子改名)。"""
        if not self.enabled:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except OSError:
            pass


@dataclass
class DeadLetter:
    """死信记录(``deadletter.jsonl``)。"""

    path: Path
    enabled: bool = True

    def __post_init__(self) -> None:
        """确保目录存在。"""
        self.path = Path(self.path)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, payload: Any, reason: str = "") -> None:
        """写一条死信记录。

        :param kind: 记录类型(如 ``bulk_write_failed``)。
        :param payload: 出问题的数据。
        :param reason: 原因。
        """
        if not self.enabled:
            return
        try:
            item = payload if isinstance(payload, dict) else {"data": payload}
        except Exception:
            item = {"data": repr(payload)}
        line = json.dumps(
            {"kind": kind, "reason": reason, "ts": round(time.time(), 3), "payload": item},
            ensure_ascii=False,
            default=str,
        )
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def count(self) -> int:
        """统计死信条数。"""
        if not self.path.is_file():
            return 0
        try:
            return len(self.path.read_text(encoding="utf-8").splitlines())
        except OSError:
            return 0


class Observability:
    """把三个写入器打包在一起(引擎持有它)。"""

    def __init__(self, root: str | os.PathLike[str], enabled: bool = True) -> None:
        """初始化。

        :param root: 输出根目录(三个文件都放这里)。
        :param enabled: 是否启用。
        """
        base = Path(root)
        self.stats = StatsWriter(base / "stats.jsonl", enabled=enabled)
        self.checkpoint = Checkpoint(base / "checkpoint.json", enabled=enabled)
        self.deadletter = DeadLetter(base / "deadletter.jsonl", enabled=enabled)

    def snapshot(self, **fields: Any) -> None:
        """更新一次进度快照。"""
        self.checkpoint.update(**fields)
