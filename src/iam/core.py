"""The only way in. Grants are checked here, audit rows are written here, in the same
transaction as the change they record. The CLI and the MCP server are thin skins over this.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .config import Config, db_path, home
from .embed import Embedder, get_embedder
from .models import (
    USER,
    AuditEntry,
    Grant,
    Hit,
    Memory,
    Provenance,
    Source,
    VaultInfo,
    new_id,
    now,
)
from .store import SqliteStore, Store


class IamError(Exception):
    """A refusal the caller should see. Never reveals whether an ungranted vault exists."""


ALL_STATUSES = ["active", "superseded", "proposed", "retracted"]
DATA_VERSION = 1  # 1: episodes' meta.project moved into the project column


def project_name(value: str | Path | None) -> str | None:
    """A project is a short name, not a path: the basename of the working directory."""
    if value is None:
        return None
    s = str(value).strip().rstrip("/")
    if not s:
        return None
    if s.startswith("-") and "/" not in s:  # a Claude Code slug from before 4b
        from .capture import project_for_slug

        return project_for_slug(s)
    return Path(s).name if "/" in s else s


class Iam:
    def __init__(self, store: Store, embedder: Embedder, *, via: str = "cli"):
        self.store = store
        self.embedder = embedder
        self.via = via

    @classmethod
    def open(cls, root: Path | None = None, *, via: str = "cli") -> Iam:
        root = root or home()
        cfg = Config.load(root)
        iam = cls(SqliteStore(db_path(root)), get_embedder(cfg.embedding_model), via=via)
        iam.migrate()
        return iam

    @classmethod
    def init(cls, root: Path | None = None, *, embedding_model: str | None = None) -> Iam:
        root = root or home()
        cfg = Config.load(root, env=False)  # so IAM_LLM_* in the shell never lands in the file
        cfg.embedding_model = (
            embedding_model or os.environ.get("IAM_EMBEDDER") or cfg.embedding_model
        )
        cfg.save(root)
        iam = cls.open(root)
        for name, desc in cfg.default_vaults.items():
            if iam.store.get_vault(name) is None:
                iam.store.put_vault(VaultInfo(name=name, description=desc))
        return iam

    # ----- access -------------------------------------------------------------------------

    def readable(self, actor: str) -> list[str]:
        if actor == USER:
            return [v.name for v in self.store.list_vaults()]
        t = now()
        return sorted(g.vault for g in self.store.grants_for(actor) if g.can_read(t))

    def writable(self, actor: str) -> list[str]:
        if actor == USER:
            return [v.name for v in self.store.list_vaults()]
        t = now()
        return sorted(g.vault for g in self.store.grants_for(actor) if g.can_write(t))

    def whoami(self, actor: str) -> dict[str, list[str]]:
        return {"client": actor, "read": self.readable(actor), "write": self.writable(actor)}

    def _require_vault(self, name: str) -> VaultInfo:
        v = self.store.get_vault(name)
        if v is None:
            raise IamError(f"no vault named {name!r}")
        return v

    def _scope(self, actor: str, vault: str | None) -> list[str]:
        """Vaults a recall may touch. An ungranted vault looks exactly like a missing one."""
        allowed = self.readable(actor)
        if vault is None:
            return allowed
        if vault not in allowed:
            raise IamError(f"no readable vault named {vault!r}")
        return [vault]

    def _project_scope(self, actor: str, project: str | None) -> str | None:
        """None means every project, and only the user gets that. An agent with no project
        sees global memories only, never another project's."""
        project = project_name(project)
        if project is None and actor != USER:
            return ""  # matches no project, so only the global (NULL) rows survive
        return project

    # ----- audit ----------------------------------------------------------------------------

    def _log(
        self,
        actor: str,
        action: str,
        *,
        request: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        reason: str | None,
        session: str | None,
        via: str | None = None,
    ) -> AuditEntry:
        e = AuditEntry(
            actor=actor,
            via=via or self.via,  # type: ignore[arg-type]
            session=session,
            action=action,  # type: ignore[arg-type]
            request=request or {},
            result=result or {},
            reason=reason,
        )
        self.store.append_audit(e)
        return e

    def audit(self, **filters) -> list[AuditEntry]:
        """Reading the log is not itself logged; the user's view of the vault is not an event."""
        return self.store.query_audit(**filters)

    def review_queue(self, actor: str, *, vault: str | None = None) -> list[Memory]:
        """Proposals waiting for the user, oldest first. Like the audit log, this is the user's
        own view and is not logged: the review page polls it, and acting on it is logged."""
        self._user_only(actor)
        rows = self.store.find_memories(
            vaults=[vault] if vault else None, types=None, categories=None, statuses=["proposed"]
        )
        return [m for m, _ in reversed(rows)]

    def history(self, actor: str, *, limit: int = 300) -> list[tuple[AuditEntry, Memory | None]]:
        """The audit log, newest first, each row with the memory it changed as it is now."""
        self._user_only(actor)
        out = []
        for e in self.store.query_audit(limit=limit):
            mid = e.result.get("memory_id") or e.request.get("id")
            if (
                not mid
                and e.action not in ("recall", "list", "export")
                and e.result.get("memory_ids")
            ):
                mid = e.result["memory_ids"][0] if len(e.result["memory_ids"]) == 1 else None
            out.append((e, self.store.get_memory(mid) if mid else None))
        return out

    def migrate(self) -> list[str]:
        """One-off data moves for a vault made by an older Iam, each audited like any change."""
        if self.store.data_version() >= DATA_VERSION:
            return []
        moved = []
        with self.store.transaction():
            if self.store.data_version() >= DATA_VERSION:
                return []  # another process got here first
            rows = self.store.find_memories(
                vaults=None, types=None, categories=None, statuses=ALL_STATUSES
            )
            for m, vec in rows:
                if _lift_project(m):
                    self.store.put_memory(m, vec)
                    moved.append(m.id)
            if moved:
                self._log(
                    USER,
                    "migrate",
                    request={"change": "meta.project -> project"},
                    result={"memory_ids": moved, "count": len(moved)},
                    reason="Iam 4b: project became a field; moved meta.project into it",
                    session=None,
                    via="migrate",
                )
            self.store.set_data_version(DATA_VERSION)
        return moved

    # ----- read ------------------------------------------------------------------------------

    def recall(
        self,
        actor: str,
        query: str,
        *,
        types: Iterable[str] | None = None,
        categories: Iterable[str] | None = None,
        vault: str | None = None,
        project: str | None = None,
        limit: int = 10,
        reason: str | None = None,
        session: str | None = None,
    ) -> list[Hit]:
        """project: this project's memories plus global ones; None means no project filter."""
        scope = self._scope(actor, vault)
        project = self._project_scope(actor, project)
        types = list(types) if types else None
        categories = list(categories) if categories else None
        cands = self.store.find_memories(
            vaults=scope, types=types, categories=categories, statuses=["active"], project=project
        )
        hits: list[Hit] = []
        if cands:
            q = self.embedder.embed([query])[0]
            scored = []
            for m, vec in cands:
                if vec is None or vec.shape != q.shape:
                    continue  # embedded by another model; re-embed to make it searchable
                scored.append((float(np.dot(q, vec)), m))
            scored.sort(key=lambda t: t[0], reverse=True)
            hits = [Hit(memory=m, score=s) for s, m in scored[:limit]]
        self._log(
            actor,
            "recall",
            request={
                "query": query,
                "types": types,
                "categories": categories,
                "vault": vault,
                "project": project,
                "limit": limit,
            },
            result={"memory_ids": [h.memory.id for h in hits], "count": len(hits), "vaults": scope},
            reason=reason,
            session=session,
        )
        return hits

    def list_memories(
        self,
        actor: str,
        *,
        types=None,
        categories=None,
        vault=None,
        project: str | None = None,
        statuses=("active",),
        limit: int | None = 100,
        reason=None,
        session=None,
    ) -> list[Memory]:
        scope = self._scope(actor, vault)
        project = self._project_scope(actor, project)
        rows = self.store.find_memories(
            vaults=scope,
            types=types,
            categories=categories,
            statuses=list(statuses),
            project=project,
            limit=limit,
        )
        mems = [m for m, _ in rows]
        self._log(
            actor,
            "list",
            request={
                "types": types,
                "categories": categories,
                "vault": vault,
                "project": project,
                "statuses": list(statuses),
            },
            result={"memory_ids": [m.id for m in mems], "count": len(mems), "vaults": scope},
            reason=reason,
            session=session,
        )
        return mems

    def show(self, actor: str, id: str, *, reason=None, session=None) -> Memory:
        m = self.store.get_memory(id)
        if m is None or m.vault not in self.readable(actor):
            raise IamError(f"no memory {id!r}")
        self._log(
            actor,
            "show",
            request={"id": id},
            result={"memory_ids": [id], "vault": m.vault},
            reason=reason,
            session=session,
        )
        return m

    # ----- write -------------------------------------------------------------------------------

    def remember(
        self,
        actor: str,
        text: str,
        type: str,
        vault: str,
        *,
        reason: str,
        project: str | None = None,
        categories: Iterable[str] = (),
        supersedes: str | None = None,
        source_ids: Iterable[str] = (),
        session: str | None = None,
        meta: dict[str, Any] | None = None,
        status: str = "active",
        model: str | None = None,
        via: str | None = None,
    ) -> Memory:
        if not reason or not reason.strip():
            raise IamError("a reason is required to write a memory")
        if not text or not text.strip():
            raise IamError("a memory needs text")
        if vault not in self.writable(actor):
            raise IamError(f"no writable vault named {vault!r}")
        self._require_vault(vault)
        text = text.strip()
        prov = Provenance(
            actor=actor,
            via=via or self.via,
            reason=reason.strip(),  # type: ignore[arg-type]
            session=session,
            source_ids=list(source_ids),
            model=model,
        )
        m = Memory(
            type=type,
            vault=vault,
            project=project_name(project),
            categories=sorted({c.strip() for c in categories if c.strip()}),  # type: ignore[arg-type]
            text=text,
            embedding_model=self.embedder.name,
            status=status,  # type: ignore[arg-type]
            supersedes=supersedes,
            provenance=prov,
            meta=meta or {},
        )
        vec = self.embedder.embed([text])[0]
        with self.store.transaction():
            if supersedes:
                old = self.store.get_memory(supersedes)
                if old is None or old.vault not in self.writable(actor):
                    raise IamError(f"cannot supersede {supersedes!r}")
                old.status = "superseded"
                self.store.put_memory(old, self._embedding_of(old.id))
            self.store.put_memory(m, vec)
            self._log(
                actor,
                "remember",
                request={
                    "type": type,
                    "vault": vault,
                    "project": m.project,
                    "categories": m.categories,
                    "supersedes": supersedes,
                    "text": text,
                },
                result={"memory_id": m.id, "memory_ids": [m.id], "vault": vault, "status": status},
                reason=reason,
                session=session,
            )
        return m

    def forget(self, actor: str, id: str, *, reason: str, session: str | None = None) -> Memory:
        if not reason or not reason.strip():
            raise IamError("a reason is required to retract a memory")
        m = self.store.get_memory(id)
        if m is None or m.vault not in self.writable(actor):
            raise IamError(f"no memory {id!r} you can retract")
        with self.store.transaction():
            m.status = "retracted"
            self.store.put_memory(m, self._embedding_of(id))
            self._log(
                actor,
                "forget",
                request={"id": id},
                result={"memory_ids": [id], "vault": m.vault},
                reason=reason,
                session=session,
            )
        return m

    def accept(
        self,
        actor: str,
        id: str,
        *,
        reason: str,
        text: str | None = None,
        everywhere: bool = False,
        session: str | None = None,
    ) -> Memory:
        """Turn a proposed memory into an active one. Editing the text re-embeds it;
        everywhere drops its project, for a fact that turned out not to be project-specific."""
        m = self.store.get_memory(id)
        if m is None or m.status != "proposed" or m.vault not in self.writable(actor):
            raise IamError(f"no proposed memory {id!r}")
        if text is not None and text.strip() and text.strip() != m.text:
            m.text = text.strip()
            vec = self.embedder.embed([m.text])[0]
            m.embedding_model = self.embedder.name
        else:
            vec = self._embedding_of(id)
        if everywhere:
            m.project = None
        with self.store.transaction():
            m.status = "active"
            self.store.put_memory(m, vec)
            self._log(
                actor,
                "remember",
                request={"id": id, "accepted": True, "text": m.text, "project": m.project},
                result={"memory_id": id, "memory_ids": [id], "vault": m.vault, "status": "active"},
                reason=reason,
                session=session,
            )
        return m

    def _embedding_of(self, id: str) -> np.ndarray | None:
        row = self.store.conn.execute("SELECT embedding FROM memories WHERE id=?", (id,)).fetchone()  # type: ignore[attr-defined]
        return np.frombuffer(row[0], dtype=np.float32) if row and row[0] is not None else None

    # ----- sources ----------------------------------------------------------------------------

    def capture(
        self,
        actor: str,
        *,
        kind: str,
        origin: str,
        content: str,
        vault: str,
        reason: str,
        project: str | None = None,
        session: str | None = None,
    ) -> Source:
        if vault not in self.writable(actor):
            raise IamError(f"no writable vault named {vault!r}")
        s = Source(
            kind=kind,
            vault=vault,
            project=project_name(project),
            origin=origin,
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            captured_by=actor,
            reason=reason,
        )
        with self.store.transaction():
            self.store.put_source(s)
            self._log(
                actor,
                "capture",
                request={
                    "kind": kind,
                    "origin": origin,
                    "vault": vault,
                    "project": s.project,
                    "bytes": len(content),
                },
                result={"source_id": s.id, "vault": vault},
                reason=reason,
                session=session,
            )
        return s

    # ----- vaults and grants (user only) ----------------------------------------------------------

    def _user_only(self, actor: str) -> None:
        if actor != USER:
            raise IamError("only the user can do that")

    def vault_add(
        self,
        actor: str,
        name: str,
        *,
        description: str = "",
        local_only: bool = False,
        reason: str = "",
    ) -> VaultInfo:
        self._user_only(actor)
        v = VaultInfo(name=name, description=description, local_only=local_only)
        with self.store.transaction():
            self.store.put_vault(v)
            self._log(
                actor,
                "vault",
                request={"name": name, "local_only": local_only},
                result={"vault": name},
                reason=reason or None,
                session=None,
            )
        return v

    def vaults(self) -> list[VaultInfo]:
        return self.store.list_vaults()

    def grant(
        self,
        actor: str,
        client: str,
        vault: str,
        mode: str,
        *,
        reason: str,
        expires_at: datetime | None = None,
    ) -> Grant:
        self._user_only(actor)
        self._require_vault(vault)
        g = Grant(
            client=client,
            vault=vault,
            mode=mode,
            expires_at=expires_at,  # type: ignore[arg-type]
            created_by=actor,
            reason=reason,
        )
        with self.store.transaction():
            self.store.put_grant(g)
            self._log(
                actor,
                "grant",
                request={"client": client, "vault": vault, "mode": mode, "expires_at": expires_at},
                result={"vault": vault},
                reason=reason,
                session=None,
            )
        return g

    def revoke(self, actor: str, client: str, vault: str, *, reason: str) -> bool:
        self._user_only(actor)
        with self.store.transaction():
            removed = self.store.delete_grant(client, vault)
            self._log(
                actor,
                "revoke",
                request={"client": client, "vault": vault},
                result={"removed": removed, "vault": vault},
                reason=reason,
                session=None,
            )
        return removed

    def grants(self) -> list[Grant]:
        return self.store.list_grants()

    # ----- portability ----------------------------------------------------------------------------

    def export(
        self,
        actor: str,
        *,
        vaults: Iterable[str] | None = None,
        include_retracted: bool = False,
        reason: str = "",
    ) -> list[dict[str, Any]]:
        self._user_only(actor)
        statuses = ["active", "superseded", "proposed"] + (
            ["retracted"] if include_retracted else []
        )
        vaults = list(vaults) if vaults else None
        rows = self.store.find_memories(
            vaults=vaults, types=None, categories=None, statuses=statuses
        )
        docs = [m.model_dump(mode="json") for m, _ in rows]
        self._log(
            actor,
            "export",
            request={"vaults": vaults, "include_retracted": include_retracted},
            result={"count": len(docs), "memory_ids": [d["id"] for d in docs]},
            reason=reason or None,
            session=None,
        )
        return docs

    def import_(self, actor: str, docs: Iterable[dict[str, Any]], *, reason: str) -> list[Memory]:
        self._user_only(actor)
        out = []
        with self.store.transaction():
            for d in docs:
                m = Memory.model_validate(d)
                _lift_project(m)  # an export from before 4b
                self._require_vault(m.vault)
                if self.store.get_memory(m.id):
                    continue
                vec = self.embedder.embed([m.text])[0]
                m.embedding_model = self.embedder.name
                self.store.put_memory(m, vec)
                out.append(m)
            self._log(
                actor,
                "import",
                request={"count": len(out)},
                result={"memory_ids": [m.id for m in out], "count": len(out)},
                reason=reason,
                session=None,
            )
        return out

    def reembed(self, actor: str) -> int:
        """Re-embed every memory with the current model. Not a memory event; not logged per row."""
        self._user_only(actor)
        rows = self.store.find_memories(
            vaults=None, types=None, categories=None, statuses=ALL_STATUSES
        )
        if not rows:
            return 0
        vecs = self.embedder.embed([m.text for m, _ in rows])
        with self.store.transaction():
            for (m, _), v in zip(rows, vecs, strict=True):
                m.embedding_model = self.embedder.name
                self.store.put_memory(m, v)
        return len(rows)


def _lift_project(m: Memory) -> bool:
    """Before 4b an episode's project rode in meta. Move it into the field; True if it moved."""
    if "project" not in m.meta:
        return False
    old = m.meta.pop("project")
    m.project = m.project or project_name(old)
    return True


def session_id() -> str:
    return new_id()
