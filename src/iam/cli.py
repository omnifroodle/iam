"""The user's surface. Every command is a thin call into iam.core."""

from __future__ import annotations

import functools
import json
import sys
from datetime import timedelta
from pathlib import Path

import typer

from .config import home
from .core import Iam, IamError
from .models import GRANT_MODES, MEMORY_TYPES, USER, now

app = typer.Typer(
    help="A personal memory vault for AI agents. The vault is canonical; agents are caches.",
    no_args_is_help=True,
    add_completion=False,
)
vault_app = typer.Typer(help="Vaults are the unit of access.", no_args_is_help=True)
app.add_typer(vault_app, name="vault")


def _iam() -> Iam:
    try:
        return Iam.open(via="cli")
    except Exception as e:  # noqa: BLE001
        _die(f"could not open the vault at {home()}: {e}. Run `iam init`.")


def _die(msg: str) -> None:
    typer.secho(msg, err=True, fg=typer.colors.RED)
    raise typer.Exit(1)


def _guard(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except IamError as e:
            _die(str(e))

    return run


def _parse_duration(s: str) -> timedelta:
    units = {"d": "days", "h": "hours", "m": "minutes", "w": "weeks"}
    try:
        return timedelta(**{units[s[-1]]: int(s[:-1])})
    except (KeyError, ValueError):
        _die(f"bad duration {s!r}; use e.g. 30d, 12h, 2w")


def _short(ts) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")


@app.command()
def init(embedding_model: str = typer.Option(None, help="sentence-transformers model name")):
    """Create ~/.iam (or $IAM_HOME) with the default vaults: personal, work, coding."""
    iam = Iam.init(embedding_model=embedding_model)
    typer.echo(f"vault ready at {home()}")
    for v in iam.vaults():
        typer.echo(f"  {v.name:10s} {v.description}")


@vault_app.command("list")
def vault_list():
    """List vaults."""
    for v in _iam().vaults():
        flag = " (local-only)" if v.local_only else ""
        typer.echo(f"{v.name:12s}{flag} {v.description}")


@vault_app.command("add")
@_guard
def vault_add(
    name: str,
    description: str = "",
    local_only: bool = typer.Option(False, help="never send this vault's text to a hosted model"),
    reason: str = typer.Option("", help="why this vault exists"),
):
    """Add a vault."""
    _iam().vault_add(USER, name, description=description, local_only=local_only, reason=reason)
    typer.echo(f"added vault {name}")


@app.command()
@_guard
def grant(
    client: str,
    vault: str,
    mode: str = typer.Option("read", help="|".join(GRANT_MODES)),
    expires: str = typer.Option(None, help="e.g. 30d"),
    reason: str = typer.Option(..., prompt=True),
):
    """Let a client read and/or write a vault."""
    if mode not in GRANT_MODES:
        _die(f"mode must be one of {', '.join(GRANT_MODES)}")
    exp = now() + _parse_duration(expires) if expires else None
    _iam().grant(USER, client, vault, mode, reason=reason, expires_at=exp)
    typer.echo(f"{client} may {mode} {vault}" + (f" until {_short(exp)}" if exp else ""))


@app.command()
@_guard
def revoke(client: str, vault: str, reason: str = typer.Option(..., prompt=True)):
    """Remove a client's access to a vault."""
    if _iam().revoke(USER, client, vault, reason=reason):
        typer.echo(f"revoked {client} on {vault}")
    else:
        typer.echo(f"{client} had no grant on {vault}")


@app.command()
def grants():
    """Show every grant."""
    t = now()
    for g in _iam().grants():
        state = "expired" if g.expired(t) else g.mode
        typer.echo(f"{g.client:16s} {g.vault:10s} {state:10s} {g.reason}")


@app.command()
@_guard
def add(
    text: str,
    type: str = typer.Option("fact", "--type", help="|".join(MEMORY_TYPES)),
    vault: str = typer.Option(..., help="which vault"),
    cat: list[str] = typer.Option([], "--cat"),
    supersedes: str = typer.Option(None),
    reason: str = typer.Option(..., prompt=True),
):
    """Write a memory yourself."""
    if type not in MEMORY_TYPES:
        _die(f"type must be one of {', '.join(MEMORY_TYPES)}")
    m = _iam().remember(
        USER, text, type, vault, categories=cat, supersedes=supersedes, reason=reason
    )
    typer.echo(m.id)


@app.command()
@_guard
def search(
    query: str,
    type: str = typer.Option(None, "--type"),
    vault: str = typer.Option(None),
    cat: list[str] = typer.Option([], "--cat"),
    limit: int = 10,
    reason: str = typer.Option(None),
):
    """Semantic search across the vaults you can read."""
    hits = _iam().recall(
        USER,
        query,
        types=[type] if type else None,
        categories=cat or None,
        vault=vault,
        limit=limit,
        reason=reason,
    )
    for h in hits:
        m = h.memory
        typer.echo(f"{h.score:5.2f}  {m.id}  [{m.vault}/{m.type}]  {m.text}")
    if not hits:
        typer.echo("nothing matched", err=True)


@app.command("list")
@_guard
def list_(
    type: str = typer.Option(None, "--type"),
    vault: str = typer.Option(None),
    cat: list[str] = typer.Option([], "--cat"),
    status: str = typer.Option("active"),
    limit: int = 100,
):
    """List memories, newest first."""
    for m in _iam().list_memories(
        USER,
        types=[type] if type else None,
        categories=cat or None,
        vault=vault,
        statuses=[status],
        limit=limit,
    ):
        typer.echo(f"{m.id}  {_short(m.created_at)}  [{m.vault}/{m.type}]  {m.text}")


@app.command()
@_guard
def show(id: str):
    """Show one memory with its provenance."""
    m = _iam().show(USER, id)
    typer.echo(json.dumps(m.model_dump(mode="json"), indent=2))


@app.command()
@_guard
def forget(id: str, reason: str = typer.Option(..., prompt=True)):
    """Retract a memory. It stays in the store, marked retracted, and in the audit trail."""
    _iam().forget(USER, id, reason=reason)
    typer.echo(f"retracted {id}")


@app.command()
@_guard
def accept(
    id: str,
    text: str = typer.Option(None, help="edit the text while accepting"),
    reason: str = typer.Option(..., prompt=True),
):
    """Accept a proposed memory (from extraction or an agent's capture) as active."""
    m = _iam().accept(USER, id, reason=reason, text=text)
    typer.echo(f"accepted {m.id}")


@app.command()
@_guard
def capture(
    path: Path,
    kind: str = typer.Option("file"),
    vault: str = typer.Option(...),
    reason: str = typer.Option(..., prompt=True),
):
    """Store raw context (a file) as a source for later extraction."""
    s = _iam().capture(
        USER, kind=kind, origin=str(path), content=path.read_text(), vault=vault, reason=reason
    )
    typer.echo(s.id)


@app.command()
def audit(
    client: str = typer.Option(None),
    action: str = typer.Option(None),
    since: str = typer.Option(None, help="e.g. 7d"),
    memory: str = typer.Option(None, help="everything that happened to one memory"),
    vault: str = typer.Option(None),
    limit: int = 50,
    as_json: bool = typer.Option(False, "--json"),
):
    """Who asked for what, what they were given, what they added, and why."""
    s = now() - _parse_duration(since) if since else None
    entries = _iam().audit(
        actor=client, action=action, since=s, memory_id=memory, vault=vault, limit=limit
    )
    if as_json:
        typer.echo(json.dumps([e.model_dump(mode="json") for e in entries], indent=2))
        return
    for e in entries:
        got = e.result.get("memory_ids") or []
        what = (
            e.request.get("query")
            or e.request.get("text")
            or e.request.get("id")
            or e.request.get("client")
            or e.request.get("origin")
            or ""
        )
        typer.echo(
            f"{_short(e.at)}  {e.actor:14s} {e.action:9s} {what[:60]:60s} "
            f"-> {len(got)} id(s)   {e.reason or ''}"
        )


@app.command()
def export(
    vault: list[str] = typer.Option([], "--vault"),
    include_retracted: bool = False,
    reason: str = typer.Option("export"),
):
    """Dump memories as JSONL to stdout, provenance included."""
    for d in _iam().export(
        USER, vaults=vault or None, include_retracted=include_retracted, reason=reason
    ):
        sys.stdout.write(json.dumps(d) + "\n")


@app.command("import")
@_guard
def import_(path: Path, reason: str = typer.Option(..., prompt=True)):
    """Load memories from a JSONL export. Existing ids are skipped."""
    docs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    added = _iam().import_(USER, docs, reason=reason)
    typer.echo(f"imported {len(added)} of {len(docs)}")


@app.command()
def reembed():
    """Re-embed every memory with the configured model."""
    typer.echo(f"re-embedded {_iam().reembed(USER)} memories")


if __name__ == "__main__":
    app()
