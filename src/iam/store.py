"""The store. One small interface; SQLite today, Couchbase later.

Each table holds the full document as JSON plus the columns needed to filter. The audit
table is append-only and the database itself enforces it with triggers, so no code path,
including ours, can rewrite history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from .models import AuditEntry, Grant, Memory, Source, VaultInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS vaults (
    name TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, vault TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, embedding BLOB, embedding_dim INTEGER, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memories_vault ON memories(vault, status, type);
CREATE TABLE IF NOT EXISTS memory_categories (
    memory_id TEXT NOT NULL REFERENCES memories(id), category TEXT NOT NULL,
    PRIMARY KEY (memory_id, category));
CREATE INDEX IF NOT EXISTS memory_categories_cat ON memory_categories(category);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, vault TEXT NOT NULL, captured_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants (
    client TEXT NOT NULL, vault TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (client, vault));
CREATE TABLE IF NOT EXISTS audit (
    id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_actor ON audit(actor, at);
CREATE INDEX IF NOT EXISTS audit_at ON audit(at);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
"""


class Store(Protocol):
    def transaction(self) -> Iterator[None]: ...
    def put_vault(self, v: VaultInfo) -> None: ...
    def get_vault(self, name: str) -> VaultInfo | None: ...
    def list_vaults(self) -> list[VaultInfo]: ...
    def put_memory(self, m: Memory, embedding: np.ndarray | None) -> None: ...
    def get_memory(self, id: str) -> Memory | None: ...
    def find_memories(
        self,
        *,
        vaults: Iterable[str] | None,
        types: Iterable[str] | None,
        categories: Iterable[str] | None,
        statuses: Iterable[str],
        limit: int | None = None,
    ) -> list[tuple[Memory, np.ndarray | None]]: ...
    def put_source(self, s: Source) -> None: ...
    def get_source(self, id: str) -> Source | None: ...
    def list_sources(self, vaults: Iterable[str] | None = None) -> list[Source]: ...
    def put_grant(self, g: Grant) -> None: ...
    def delete_grant(self, client: str, vault: str) -> bool: ...
    def grants_for(self, client: str) -> list[Grant]: ...
    def list_grants(self) -> list[Grant]: ...
    def append_audit(self, e: AuditEntry) -> None: ...
    def query_audit(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        since: datetime | None = None,
        memory_id: str | None = None,
        vault: str | None = None,
        limit: int = 200,
    ) -> list[AuditEntry]: ...


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _dump(model) -> str:
    return model.model_dump_json()


class SqliteStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # MCP hosts call sync tools from worker threads; one connection, serialised by a lock.
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth == 0:
                self.conn.execute("BEGIN")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("COMMIT")

    # vaults
    def put_vault(self, v: VaultInfo) -> None:
        self.conn.execute("INSERT OR REPLACE INTO vaults VALUES (?, ?)", (v.name, _dump(v)))

    def get_vault(self, name: str) -> VaultInfo | None:
        row = self.conn.execute("SELECT doc FROM vaults WHERE name=?", (name,)).fetchone()
        return VaultInfo.model_validate_json(row[0]) if row else None

    def list_vaults(self) -> list[VaultInfo]:
        rows = self.conn.execute("SELECT doc FROM vaults ORDER BY name").fetchall()
        return [VaultInfo.model_validate_json(r[0]) for r in rows]

    # memories
    def put_memory(self, m: Memory, embedding: np.ndarray | None) -> None:
        blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
        dim = int(embedding.shape[0]) if embedding is not None else None
        with self.transaction():
            self.conn.execute(
                "INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?,?)",
                (m.id, m.type, m.vault, m.status, m.created_at.isoformat(), blob, dim, _dump(m)),
            )
            self.conn.execute("DELETE FROM memory_categories WHERE memory_id=?", (m.id,))
            self.conn.executemany(
                "INSERT INTO memory_categories VALUES (?, ?)", [(m.id, c) for c in m.categories]
            )

    def get_memory(self, id: str) -> Memory | None:
        row = self.conn.execute("SELECT doc FROM memories WHERE id=?", (id,)).fetchone()
        return Memory.model_validate_json(row[0]) if row else None

    def find_memories(
        self, *, vaults=None, types=None, categories=None, statuses, limit=None
    ) -> list[tuple[Memory, np.ndarray | None]]:
        statuses = list(statuses)
        where, params = [f"status IN ({_marks(len(statuses))})"], list(statuses)
        if vaults is not None:
            vaults = list(vaults)
            if not vaults:
                return []
            where.append(f"vault IN ({_marks(len(vaults))})")
            params += vaults
        if types:
            types = list(types)
            where.append(f"type IN ({_marks(len(types))})")
            params += types
        if categories:
            categories = list(categories)
            where.append(
                "id IN (SELECT memory_id FROM memory_categories "
                f"WHERE category IN ({_marks(len(categories))}))"
            )
            params += categories
        sql = (
            "SELECT doc, embedding, embedding_dim FROM memories "
            f"WHERE {' AND '.join(where)} ORDER BY created_at DESC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for doc, blob, _dim in self.conn.execute(sql, params):
            vec = np.frombuffer(blob, dtype=np.float32) if blob is not None else None
            out.append((Memory.model_validate_json(doc), vec))
        return out

    # sources
    def put_source(self, s: Source) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?)",
            (s.id, s.kind, s.vault, s.captured_at.isoformat(), s.content_sha256, _dump(s)),
        )

    def get_source(self, id: str) -> Source | None:
        row = self.conn.execute("SELECT doc FROM sources WHERE id=?", (id,)).fetchone()
        return Source.model_validate_json(row[0]) if row else None

    def list_sources(self, vaults=None) -> list[Source]:
        if vaults is not None:
            vaults = list(vaults)
            if not vaults:
                return []
            rows = self.conn.execute(
                f"SELECT doc FROM sources WHERE vault IN ({_marks(len(vaults))}) "
                "ORDER BY captured_at DESC",
                vaults,
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT doc FROM sources ORDER BY captured_at DESC").fetchall()
        return [Source.model_validate_json(r[0]) for r in rows]

    # grants
    def put_grant(self, g: Grant) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO grants VALUES (?,?,?)", (g.client, g.vault, _dump(g))
        )

    def delete_grant(self, client: str, vault: str) -> bool:
        cur = self.conn.execute("DELETE FROM grants WHERE client=? AND vault=?", (client, vault))
        return cur.rowcount > 0

    def grants_for(self, client: str) -> list[Grant]:
        rows = self.conn.execute("SELECT doc FROM grants WHERE client=?", (client,)).fetchall()
        return [Grant.model_validate_json(r[0]) for r in rows]

    def list_grants(self) -> list[Grant]:
        rows = self.conn.execute("SELECT doc FROM grants ORDER BY client, vault").fetchall()
        return [Grant.model_validate_json(r[0]) for r in rows]

    # audit
    def append_audit(self, e: AuditEntry) -> None:
        self.conn.execute(
            "INSERT INTO audit VALUES (?,?,?,?,?)",
            (e.id, e.at.isoformat(), e.actor, e.action, _dump(e)),
        )

    def query_audit(
        self, *, actor=None, action=None, since=None, memory_id=None, vault=None, limit=200
    ) -> list[AuditEntry]:
        where: list[str] = ["1=1"]
        params: list[str] = []
        if actor:
            where.append("actor=?")
            params.append(actor)
        if action:
            where.append("action=?")
            params.append(action)
        if since:
            where.append("at>=?")
            params.append(since.isoformat())
        rows = self.conn.execute(
            f"SELECT doc FROM audit WHERE {' AND '.join(where)} ORDER BY at DESC, id DESC", params
        ).fetchall()
        entries = [AuditEntry.model_validate_json(r[0]) for r in rows]
        if memory_id:
            entries = [e for e in entries if memory_id in _ids(e)]
        if vault:
            entries = [
                e
                for e in entries
                if e.request.get("vault") == vault
                or vault in (e.request.get("vaults") or [])
                or e.result.get("vault") == vault
            ]
        return entries[:limit]


def _ids(e: AuditEntry) -> set[str]:
    ids = set(e.result.get("memory_ids") or [])
    for k in ("memory_id", "id", "supersedes"):
        v = e.result.get(k) or e.request.get(k)
        if v:
            ids.add(v)
    return ids


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
