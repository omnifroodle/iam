"""Pull raw context out of agents and into the vault.

Claude Code keeps two things per project under ~/.claude/projects/<slug>/: session
transcripts (*.jsonl) and an auto-memory directory (memory/*.md). Transcripts become
sources for extraction. Memory files are already memories in all but name, so they are
imported directly as *proposed* memories, no model involved, and the user accepts them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .core import Iam
from .models import Memory, Source

MEMORY_INDEX = "MEMORY.md"


def claude_root() -> Path:
    return Path(os.environ.get("IAM_CLAUDE_HOME", Path.home() / ".claude")).expanduser()


def project_slug(path: Path | str) -> str:
    """Claude Code names a project directory after its absolute path, non-alphanumerics as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def list_projects() -> list[tuple[str, Path]]:
    root = claude_root() / "projects"
    if not root.exists():
        return []
    return sorted((p.name, p) for p in root.iterdir() if p.is_dir())


def project_dir(slug: str) -> Path:
    return claude_root() / "projects" / slug


def project_for_slug(slug: str) -> str:
    """The project name behind a slug. A slug cannot be reversed ('_' and '/' both became
    '-'), so ask a transcript where it ran; failing that, the slug's last segment."""
    for p in transcripts(slug):
        if cwd := read_transcript(p).cwd:
            return Path(cwd).name
    return slug.rstrip("-").rsplit("-", 1)[-1] or slug


# ----- transcripts ------------------------------------------------------------------------------


@dataclass
class Transcript:
    session_id: str
    path: Path
    cwd: str | None = None
    started: datetime | None = None
    ended: datetime | None = None
    turns: list[tuple[str, str]] = field(default_factory=list)  # (role, text)

    def render(self) -> str:
        head = [f"session: {self.session_id}", f"cwd: {self.cwd or '?'}"]
        if self.started:
            head.append(f"from: {self.started.isoformat()}")
        if self.ended:
            head.append(f"to: {self.ended.isoformat()}")
        body = [f"{role}: {text}" for role, text in self.turns]
        return "\n".join(head) + "\n\n" + "\n\n".join(body)


def _text_of(content) -> str:
    """Conversation text only: user prompts and assistant prose. Tool traffic is noise."""
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content or []:
        if block.get("type") == "text" and block.get("text"):
            parts.append(block["text"].strip())
    return "\n".join(p for p in parts if p)


def read_transcript(path: Path) -> Transcript:
    t = Transcript(session_id=path.stem, path=path)
    with path.open() as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") not in ("user", "assistant") or d.get("isSidechain"):
                continue
            text = _text_of((d.get("message") or {}).get("content"))
            if not text or text.startswith("<"):  # system-reminder / command wrappers
                continue
            t.cwd = t.cwd or d.get("cwd")
            ts = d.get("timestamp")
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
            if when:
                t.started = t.started or when
                t.ended = when
            t.turns.append((d["type"], text))
    return t


def transcripts(slug: str, since: datetime | None = None) -> list[Path]:
    d = project_dir(slug)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.jsonl")):
        if since and datetime.fromtimestamp(p.stat().st_mtime, tz=since.tzinfo) < since:
            continue
        out.append(p)
    return out


def capture_transcripts(
    iam: Iam, actor: str, slug: str, *, vault: str, reason: str, since: datetime | None = None
) -> list[Source]:
    """One source per session with any conversation in it. Unchanged sessions are skipped.
    Each source's project is the basename of the directory the session ran in."""
    seen = {(s.origin, s.content_sha256) for s in iam.store.list_sources()}
    import hashlib

    out = []
    for p in transcripts(slug, since):
        t = read_transcript(p)
        if not t.turns:
            continue
        content = t.render()
        origin = f"claude-code:{slug}:{t.session_id}"
        if (origin, hashlib.sha256(content.encode()).hexdigest()) in seen:
            continue
        out.append(
            iam.capture(
                actor,
                kind="claude-code-transcript",
                origin=origin,
                content=content,
                vault=vault,
                reason=reason,
                project=Path(t.cwd).name if t.cwd else project_for_slug(slug),
            )
        )
    return out


# ----- auto-memory files -------------------------------------------------------------------------


def memory_files(slug: str) -> list[Path]:
    d = project_dir(slug) / "memory"
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.md") if p.name != MEMORY_INDEX)


_FM = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


def parse_memory_file(path: Path) -> tuple[dict[str, str], str]:
    """A tiny frontmatter reader: top-level `key: value` lines only; nested keys flattened."""
    raw = path.read_text()
    m = _FM.match(raw)
    if not m:
        return {}, raw.strip()
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"')
    return meta, raw[m.end() :].strip()


def import_memory_dir(
    iam: Iam, actor: str, slug: str, *, vault: str, reason: str, project: str | None = None
) -> list[tuple[Path, Memory | None]]:
    """Each memory file becomes a captured source plus one proposed fact for the user to accept.
    The fact is scoped to the project the memory dir belongs to (default: the slug's).

    A file whose content was already captured is skipped, so this is safe to re-run.
    """
    project = project or project_for_slug(slug)
    seen = {s.content_sha256 for s in iam.store.list_sources()}
    import hashlib

    out: list[tuple[Path, Memory | None]] = []
    for p in memory_files(slug):
        raw = p.read_text()
        if hashlib.sha256(raw.encode()).hexdigest() in seen:
            out.append((p, None))
            continue
        meta, body = parse_memory_file(p)
        src = iam.capture(
            actor,
            kind="claude-code-memory",
            origin=f"claude-code:{slug}:memory/{p.name}",
            content=raw,
            vault=vault,
            reason=reason,
            project=project,
        )
        text = body if body else meta.get("description", "")
        if desc := meta.get("description"):
            if desc not in text:
                text = f"{desc}\n\n{text}"
        cats = [c for c in (meta.get("type"), "claude-code") if c]
        m = iam.remember(
            actor,
            text,
            "fact",
            vault,
            categories=cats,
            reason=reason,
            project=project,
            status="proposed",
            via="import",
            source_ids=[src.id],
            session=meta.get("originSessionId"),
            meta={"name": meta.get("name", p.stem), "file": p.name, "slug": slug},
        )
        out.append((p, m))
    return out


def evict_memory_dir(
    iam: Iam, actor: str, slug: str, *, vault: str, reason: str, project: str | None = None
) -> tuple[list[tuple[Path, Memory | None]], list[Path]]:
    """Import, then delete. The vault now holds the only copy; the agent will rebuild its cache."""
    project = project or project_for_slug(slug)
    imported = import_memory_dir(iam, actor, slug, vault=vault, reason=reason, project=project)
    removed = []
    for p, _ in imported:
        p.unlink()
        removed.append(p)
    index = project_dir(slug) / "memory" / MEMORY_INDEX
    if index.exists():
        index.unlink()
        removed.append(index)
    iam._log(  # noqa: SLF001 - eviction is a vault event, recorded like the rest
        actor,
        "evict",
        request={"agent": "claude-code", "slug": slug, "project": project, "vault": vault},
        result={
            "memory_ids": [m.id for _, m in imported if m],
            "removed": [str(p) for p in removed],
        },
        reason=reason,
        session=None,
    )
    return imported, removed
