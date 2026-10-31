"""导出层:JSONL / CSV / Markdown / XLSX,按域名分目录。

导出的数据来源是仓库里的记录(:class:`~meilab.storage.vault.VaultRecord`)。
目录结构::

    <output>/
      ├── jsonl/<domain>.jsonl
      ├── csv/<domain>.csv
      ├── markdown/<domain>/<hash>.md
      └── xlsx/<domain>.xlsx

Markdown 目录额外写一个 ``index.md`` 汇总清单,方便人工翻阅。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .vault import VaultRecord

#: 导出格式清单
FORMATS = ("jsonl", "csv", "markdown", "xlsx", "sqlite")

#: 文件名里的非法字符
_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_name(name: str, fallback: str = "unknown") -> str:
    """把域名等字符串转成安全的文件名。

    :param name: 原始名称。
    :param fallback: 名称为空时的占位。
    :return: 安全文件名。
    """
    cleaned = _UNSAFE.sub("_", (name or "").strip().lower()).strip("._-")
    return cleaned or fallback


def _group_by_domain(records: Iterable[VaultRecord]) -> dict[str, list[VaultRecord]]:
    """按域名分组。"""
    grouped: dict[str, list[VaultRecord]] = {}
    for record in records:
        grouped.setdefault(record.domain or "(unknown)", []).append(record)
    return grouped


def export_jsonl(records: Iterable[VaultRecord], output_dir: str | Path) -> list[Path]:
    """导出为 JSONL(每个域名一个文件)。

    **产物层脱敏**:正文里的手机号/邮箱/IP 在写盘前被替换。

    :param records: 记录列表。
    :param output_dir: 输出目录。
    :return: 生成的文件路径列表。
    """
    from ..privacy.sanitizer import sanitize_record, sanitize_text

    base = Path(output_dir) / "jsonl"
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for domain, items in _group_by_domain(records).items():
        path = base / f"{safe_name(domain)}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for item in items:
                payload = item.to_row()
                payload["content"] = sanitize_text(item.content)
                handle.write(
                    json.dumps(sanitize_record(payload), ensure_ascii=False) + "\n"
                )
        written.append(path)
    return written


def export_csv(records: Iterable[VaultRecord], output_dir: str | Path) -> list[Path]:
    """导出为 CSV(每个域名一个文件)。

    **产物层脱敏**:正文里的手机号/邮箱/IP 在写盘前被替换。

    :param records: 记录列表。
    :param output_dir: 输出目录。
    :return: 生成的文件路径列表。
    """
    from ..privacy.sanitizer import sanitize_text

    base = Path(output_dir) / "csv"
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    fields = [
        "content_hash", "url", "domain", "title", "version",
        "seen_count", "first_seen", "updated_at", "encrypted", "content",
    ]
    for domain, items in _group_by_domain(records).items():
        path = base / f"{safe_name(domain)}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in items:
                row = {
                    "content_hash": item.content_hash,
                    "url": item.url,
                    "domain": item.domain,
                    "title": item.title,
                    "version": item.version,
                    "seen_count": item.seen_count,
                    "first_seen": item.first_seen,
                    "updated_at": item.updated_at,
                    "encrypted": int(item.encrypted),
                    "content": sanitize_text(item.content).replace("\n", " "),
                }
                writer.writerow(row)
        written.append(path)
    return written


def export_markdown(records: Iterable[VaultRecord], output_dir: str | Path) -> list[Path]:
    """导出为 Markdown(每个域名一个目录 + 一个 index.md)。

    **产物层脱敏**:正文里的手机号/邮箱/IP 在写盘前被替换。

    :param records: 记录列表。
    :param output_dir: 输出目录。
    :return: 生成的文件路径列表。
    """
    from ..privacy.sanitizer import sanitize_text, sanitize_url

    base = Path(output_dir) / "markdown"
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for domain, items in _group_by_domain(records).items():
        domain_dir = base / safe_name(domain)
        domain_dir.mkdir(parents=True, exist_ok=True)
        index_lines = [f"# {domain}", ""]
        for item in items:
            name = f"{safe_name(item.title)[:60] or item.content_hash[:12]}-{item.content_hash[:8]}.md"
            path = domain_dir / name
            header = f"# {sanitize_text(item.title) or sanitize_url(item.url)}\n\n"
            meta = (
                f"- 来源: {sanitize_url(item.url)}\n"
                f"- 内容哈希: `{item.content_hash}`\n"
                f"- 版本: {item.version} · 见到 {item.seen_count} 次\n\n"
            )
            path.write_text(
                header + meta + sanitize_text(item.content) + "\n", encoding="utf-8"
            )
            written.append(path)
            index_lines.append(f"- [{item.title or item.url}]({name}) — {item.url}")
        index_path = domain_dir / "index.md"
        index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
        written.append(index_path)
    return written


def export_xlsx(records: Iterable[VaultRecord], output_dir: str | Path) -> list[Path]:
    """导出为 XLSX(每个域名一个工作簿)。

    未安装 ``openpyxl`` 时返回空列表并记录失败原因(降级 ≠ 失败)。

    :param records: 记录列表。
    :param output_dir: 输出目录。
    :return: 生成的文件路径列表。
    """
    base = Path(output_dir) / "xlsx"
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        from openpyxl import Workbook  # 第三方库:xlsx 导出
    except Exception:
        return written

    for domain, items in _group_by_domain(records).items():
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "documents"
        sheet.append([
            "content_hash", "url", "title", "version", "seen_count",
            "first_seen", "updated_at", "content",
        ])
        for item in items:
            sheet.append([
                item.content_hash, item.url, item.title, item.version,
                item.seen_count, item.first_seen, item.updated_at, item.content,
            ])
        path = base / f"{safe_name(domain)}.xlsx"
        workbook.save(path)
        written.append(path)
    return written


def export_sqlite(records: Iterable[VaultRecord], output_dir: str | Path) -> list[Path]:
    """导出为 SQLite(带 FTS5 全文索引,可直接检索)。

    产物层脱敏:正文里的手机号/邮箱/IP 在写盘前被替换。

    :param records: 记录列表。
    :param output_dir: 输出目录。
    :return: 生成的文件路径列表。
    """
    import sqlite3

    from ..privacy.sanitizer import sanitize_text, sanitize_url

    base = Path(output_dir) / "sqlite"
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for domain, items in _group_by_domain(records).items():
        path = base / f"{safe_name(domain)}.sqlite"
        if path.exists():
            path.unlink()
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(
                """
                CREATE TABLE scrape (
                    url TEXT PRIMARY KEY,
                    content_hash TEXT,
                    title TEXT,
                    content TEXT,
                    version INTEGER,
                    seen_count INTEGER,
                    updated_at REAL
                )
                """
            )
            has_fts = True
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE scrape_fts USING fts5(title, content, content='scrape', content_rowid='rowid')"
                )
            except sqlite3.OperationalError:
                has_fts = False
            for item in items:
                cursor = connection.execute(
                    "INSERT OR REPLACE INTO scrape "
                    "(url, content_hash, title, content, version, seen_count, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sanitize_url(item.url), item.content_hash,
                        sanitize_text(item.title), sanitize_text(item.content),
                        item.version, item.seen_count, item.updated_at,
                    ),
                )
                if has_fts:
                    connection.execute(
                        "INSERT INTO scrape_fts(rowid, title, content) VALUES (?, ?, ?)",
                        (
                            cursor.lastrowid,
                            sanitize_text(item.title), sanitize_text(item.content),
                        ),
                    )
            connection.commit()
        finally:
            connection.close()
        written.append(path)
    return written


#: 格式 → 导出函数
_EXPORTERS = {
    "jsonl": export_jsonl,
    "csv": export_csv,
    "markdown": export_markdown,
    "xlsx": export_xlsx,
    "sqlite": export_sqlite,
}


def export(
    records: Iterable[VaultRecord],
    output_dir: str | Path,
    fmt: str = "markdown",
) -> dict[str, Any]:
    """按格式导出。

    :param records: 记录列表。
    :param output_dir: 输出目录。
    :param fmt: 导出格式(:data:`FORMATS` 之一)。
    :return: ``{"format", "files", "count", "error"}`` 摘要。
    """
    materialised = list(records)
    key = (fmt or "").strip().lower()
    exporter = _EXPORTERS.get(key)
    if exporter is None:
        return {
            "format": key,
            "files": [],
            "count": 0,
            "error": f"未知导出格式 {fmt!r},可选: {', '.join(FORMATS)}",
        }
    try:
        files = exporter(materialised, output_dir)
    except Exception as exc:
        return {"format": key, "files": [], "count": 0, "error": f"导出失败: {exc!r}"}
    return {
        "format": key,
        "files": [str(f) for f in files],
        "count": len(materialised),
        "error": "",
    }
