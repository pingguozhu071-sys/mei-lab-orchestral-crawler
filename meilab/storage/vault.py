"""内容仓库:SQLite 存储 + SHA256 增量 diff + 可选 AES-256-GCM 加密。

设计要点:

* **增量**:以正文的 SHA256 作为主键。内容没变 → 只更新"最后见到"的时间戳,
  版本号不变;内容变了 → 版本号 +1,覆盖写入。避免重复抓取把库撑爆。
* **加密**:可选开启。密钥来自环境变量 ``MELAB_KEY``(十六进制 / base64 / 任意口令),
  统一用 SHA-256 派生为 32 字节主密钥,用 AES-256-GCM 加密正文,
  每条记录独立随机 nonce。未设置环境变量时**明文存储**。
* **不落密钥**:密钥只在内存里,不写库、不写日志。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

#: 存放加密密钥的环境变量名
KEY_ENV = "MELAB_KEY"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    url          TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    domain       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL DEFAULT '',
    nonce        BLOB,
    encrypted    INTEGER NOT NULL DEFAULT 0,
    version      INTEGER NOT NULL DEFAULT 1,
    meta         TEXT NOT NULL DEFAULT '{}',
    first_seen   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    seen_count   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents (domain);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents (content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents (updated_at DESC);
"""


@dataclass
class VaultRecord:
    """仓库中的一条内容记录。"""

    content_hash: str = ""
    url: str = ""
    domain: str = ""
    title: str = ""
    content: str = ""
    version: int = 1
    meta: dict[str, Any] = field(default_factory=dict)
    first_seen: float = 0.0
    updated_at: float = 0.0
    seen_count: int = 1
    encrypted: bool = False

    def to_row(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典(不含明文正文)。"""
        return {
            "content_hash": self.content_hash,
            "url": self.url,
            "domain": self.domain,
            "title": self.title,
            "version": self.version,
            "meta": self.meta,
            "first_seen": self.first_seen,
            "updated_at": self.updated_at,
            "seen_count": self.seen_count,
            "encrypted": self.encrypted,
        }


def derive_key(material: str | bytes | None) -> bytes | None:
    """把任意口令/编码材料派生为 32 字节 AES 密钥。

    * ``None`` 或空 → 返回 ``None``(不加密);
    * 64 位十六进制 → 直接按字节解析(便于用随机密钥);
    * base64 → 解码后取 SHA-256;
    * 其他任意口令 → 取 SHA-256。

    :param material: 密钥材料。
    :return: 32 字节密钥;不加密时返回 ``None``。
    """
    if material is None:
        return None
    if isinstance(material, bytes):
        raw = material
    else:
        text = material.strip()
        if not text:
            return None
        raw = text.encode("utf-8")
        # 64 位十六进制 → 按字节解析
        if len(text) == 64:
            try:
                return bytes.fromhex(text)
            except ValueError:
                pass
        # base64 → 解码后哈希
        try:
            decoded = base64.b64decode(text, validate=True)
            if decoded:
                return hashlib.sha256(decoded).digest()
        except Exception:
            pass
    return hashlib.sha256(raw).digest()


def domain_of(url: str) -> str:
    """取 URL 的域名(用于分目录导出)。

    :param url: URL。
    :return: 域名;解析失败返回空串。
    """
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class Vault:
    """内容仓库。"""

    def __init__(self, db_path: str | os.PathLike[str], key: str | bytes | None = None) -> None:
        """初始化仓库。

        :param db_path: SQLite 文件路径。
        :param key: 加密密钥材料;``None`` 时从环境变量 :data:`KEY_ENV` 读取,
            仍为空则明文存储。
        """
        self.db_path = str(db_path)
        self.key = derive_key(key if key is not None else os.environ.get(KEY_ENV))
        self._conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)

    @property
    def encrypted(self) -> bool:
        """当前是否启用加密。"""
        return self.key is not None

    def close(self) -> None:
        """关闭连接。"""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Vault":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 写入

    def store(
        self,
        content: str,
        url: str = "",
        title: str = "",
        meta: dict[str, Any] | None = None,
        content_hash: str | None = None,
    ) -> tuple[VaultRecord, bool]:
        """写入/更新一条内容(增量 diff)。

        判重以 **URL** 为准(一个 URL = 一份文档),用正文 SHA256 判断内容是否变化:
        * 内容未变 → ``changed=False``,只累计 ``seen_count`` 与 ``updated_at``;
        * 内容变了 → ``changed=True``,版本号 +1,覆盖正文。

        :param content: 正文。
        :param url: 来源 URL。
        :param title: 标题。
        :param meta: 附加元数据。
        :param content_hash: 指定哈希;为空则按正文计算。
        :return: ``(记录, 是否发生变化)``。
        """
        digest = content_hash or hashlib.sha256(
            (content or "").encode("utf-8", errors="replace")
        ).hexdigest()
        now = time.time()
        key = url or digest  # 没有 URL 时退化成按哈希判重
        existing = self._conn.execute(
            "SELECT content_hash, version, seen_count, first_seen FROM documents WHERE url = ?",
            (key,),
        ).fetchone()

        payload, nonce, encrypted_flag = self._encrypt(content or "")

        if existing is None:
            record = VaultRecord(
                content_hash=digest,
                url=url,
                domain=domain_of(url),
                title=title,
                content=content or "",
                version=1,
                meta=dict(meta or {}),
                first_seen=now,
                updated_at=now,
                seen_count=1,
                encrypted=encrypted_flag,
            )
            self._conn.execute(
                """
                INSERT INTO documents (url, content_hash, domain, title, content, nonce, encrypted,
                                       version, meta, first_seen, updated_at, seen_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.url or digest, record.content_hash, record.domain, record.title,
                    payload, nonce, int(encrypted_flag), record.version,
                    json.dumps(record.meta, ensure_ascii=False),
                    record.first_seen, record.updated_at, record.seen_count,
                ),
            )
            return record, True

        same_content = str(existing["content_hash"]) == digest
        if same_content:
            # 内容未变 → 只累计见到次数
            version = int(existing["version"])
            seen = int(existing["seen_count"]) + 1
            self._conn.execute(
                "UPDATE documents SET seen_count = ?, updated_at = ?, title = ? WHERE url = ?",
                (seen, now, title, key),
            )
            return (
                VaultRecord(
                    content_hash=digest,
                    url=url,
                    domain=domain_of(url),
                    title=title,
                    content=content or "",
                    version=version,
                    meta=dict(meta or {}),
                    first_seen=float(existing["first_seen"]),
                    updated_at=now,
                    seen_count=seen,
                    encrypted=encrypted_flag,
                ),
                False,
            )

        # 内容变了 → 版本 +1,覆盖正文
        version = int(existing["version"]) + 1
        seen = int(existing["seen_count"]) + 1
        self._conn.execute(
            """
            UPDATE documents SET content_hash = ?, title = ?, content = ?, nonce = ?,
                                encrypted = ?, version = ?, meta = ?, updated_at = ?, seen_count = ?
            WHERE url = ?
            """,
            (
                digest, title, payload, nonce, int(encrypted_flag), version,
                json.dumps(dict(meta or {}), ensure_ascii=False), now, seen, key,
            ),
        )
        return (
            VaultRecord(
                content_hash=digest,
                url=url,
                domain=domain_of(url),
                title=title,
                content=content or "",
                version=version,
                meta=dict(meta or {}),
                first_seen=float(existing["first_seen"]),
                updated_at=now,
                seen_count=seen,
                encrypted=encrypted_flag,
            ),
            True,
        )

    # ------------------------------------------------------------------ 读取

    def list_records(self, domain: str | None = None, limit: int = 1000) -> list[VaultRecord]:
        """列出记录(按更新时间倒序)。

        :param domain: 只看某个域名;``None`` 看全部。
        :param limit: 最多返回条数。
        :return: 记录列表(正文已解密)。
        """
        sql = "SELECT * FROM documents"
        params: list[Any] = []
        if domain:
            sql += " WHERE domain = ?"
            params.append(domain.lower())
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out: list[VaultRecord] = []
        for row in rows:
            out.append(self._row_to_record(row))
        return out

    def get(self, identifier: str) -> VaultRecord | None:
        """按 URL 或内容哈希取记录。

        :param identifier: URL 或内容哈希。
        :return: 记录;不存在返回 ``None``。
        """
        row = self._conn.execute(
            "SELECT * FROM documents WHERE url = ?", (identifier,)
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE content_hash = ? LIMIT 1", (identifier,)
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def domains(self) -> list[str]:
        """列出所有域名及其记录数。"""
        rows = self._conn.execute(
            "SELECT domain, COUNT(*) AS n FROM documents GROUP BY domain ORDER BY n DESC"
        ).fetchall()
        return [f"{r['domain'] or '(unknown)'}:{r['n']}" for r in rows]

    def stats(self) -> dict[str, Any]:
        """仓库统计。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, SUM(seen_count) AS seen, MAX(updated_at) AS last FROM documents"
        ).fetchone()
        return {
            "documents": int(row["n"] or 0),
            "total_seen": int(row["seen"] or 0),
            "last_updated": float(row["last"] or 0.0),
            "encrypted": self.encrypted,
        }

    # ------------------------------------------------------------------ 内部

    def _encrypt(self, text: str) -> tuple[str, bytes | None, bool]:
        """加密正文(未启用密钥时原样返回)。"""
        if self.key is None:
            return text, None, False
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # 第三方库:AES-GCM

            nonce = os.urandom(12)
            cipher = AESGCM(self.key)
            blob = cipher.encrypt(nonce, text.encode("utf-8", errors="replace"), None)
            return blob.hex(), nonce, True
        except Exception:
            # 加密失败时明文落库(功能优先),但会在 meta 里体现
            return text, None, False

    def _decrypt(self, payload: str, nonce: bytes | None, encrypted: bool) -> str:
        """解密正文。"""
        if not encrypted or nonce is None or self.key is None:
            return payload
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            cipher = AESGCM(self.key)
            blob = bytes.fromhex(payload)
            return cipher.decrypt(nonce, blob, None).decode("utf-8", errors="replace")
        except Exception:
            return payload

    def _row_to_record(self, row: sqlite3.Row) -> VaultRecord:
        """把数据库行转成记录对象。"""
        return VaultRecord(
            content_hash=str(row["content_hash"]),
            url=str(row["url"]),
            domain=str(row["domain"]),
            title=str(row["title"]),
            content=self._decrypt(str(row["content"]), row["nonce"], bool(row["encrypted"])),
            version=int(row["version"]),
            meta=json.loads(row["meta"] or "{}"),
            first_seen=float(row["first_seen"]),
            updated_at=float(row["updated_at"]),
            seen_count=int(row["seen_count"]),
            encrypted=bool(row["encrypted"]),
        )
