"""Document shapes. JSON-shaped on purpose, so they travel on export unchanged."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from ulid import ULID

MemoryType = Literal["fact", "episode"]
MemoryStatus = Literal["active", "superseded", "retracted", "proposed"]
GrantMode = Literal["read", "write", "read-write"]
Via = Literal["mcp", "cli", "extract", "import", "migrate"]
Action = Literal[
    "recall",
    "remember",
    "forget",
    "list",
    "show",
    "capture",
    "extract",
    "grant",
    "revoke",
    "evict",
    "export",
    "import",
    "vault",
    "migrate",
]

MEMORY_TYPES: tuple[str, ...] = ("fact", "episode")
GRANT_MODES: tuple[str, ...] = ("read", "write", "read-write")
USER = "user"  # the actor name for the person at the CLI; sees every vault


def new_id() -> str:
    return str(ULID())


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class Provenance(BaseModel):
    """Where a memory came from. Travels with the memory on export."""

    actor: str
    via: Via
    reason: str
    session: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    model: str | None = None


class Memory(BaseModel):
    id: str = Field(default_factory=new_id)
    type: MemoryType
    vault: str
    project: str | None = None  # when it applies: a short name, or None for everywhere
    categories: list[str] = Field(default_factory=list)
    text: str
    embedding_model: str | None = None
    status: MemoryStatus = "active"
    supersedes: str | None = None
    created_at: datetime = Field(default_factory=now)
    provenance: Provenance
    meta: dict[str, Any] = Field(default_factory=dict)  # type-specific: outcome, evidence, ...


class Source(BaseModel):
    """Raw context a memory may be extracted from."""

    id: str = Field(default_factory=new_id)
    kind: str
    vault: str
    project: str | None = None  # memories extracted from it default to this project
    origin: str
    content: str
    content_sha256: str
    captured_at: datetime = Field(default_factory=now)
    captured_by: str
    reason: str


class Grant(BaseModel):
    client: str
    vault: str
    mode: GrantMode
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=now)
    created_by: str
    reason: str

    def can_read(self, at: datetime) -> bool:
        return self.mode in ("read", "read-write") and not self.expired(at)

    def can_write(self, at: datetime) -> bool:
        return self.mode in ("write", "read-write") and not self.expired(at)

    def expired(self, at: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= at


class VaultInfo(BaseModel):
    name: str
    description: str = ""
    local_only: bool = False
    created_at: datetime = Field(default_factory=now)


class AuditEntry(BaseModel):
    """One row of the append-only log. Never updated, never deleted."""

    id: str = Field(default_factory=new_id)
    at: datetime = Field(default_factory=now)
    actor: str
    via: Via
    session: str | None = None
    action: Action
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class Hit(BaseModel):
    memory: Memory
    score: float
