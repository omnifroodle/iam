"""The user's surface. Every command is a thin call into iam.core."""

from __future__ import annotations

import functools
import json
import sys
from datetime import timedelta
from pathlib import Path

import typer

from .config import home
from .core import Iam, IamError, project_name
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


def _where(m) -> str:
    return f"[{m.vault}/{m.type}{' @' + m.project if m.project else ''}]"


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
    project: str = typer.Option(None, help="the project it applies to (default: everywhere)"),
    cat: list[str] = typer.Option([], "--cat"),
    supersedes: str = typer.Option(None),
    reason: str = typer.Option(..., prompt=True),
):
    """Write a memory yourself."""
    if type not in MEMORY_TYPES:
        _die(f"type must be one of {', '.join(MEMORY_TYPES)}")
    m = _iam().remember(
        USER,
        text,
        type,
        vault,
        project=project,
        categories=cat,
        supersedes=supersedes,
        reason=reason,
    )
    typer.echo(m.id)


@app.command()
@_guard
def search(
    query: str,
    type: str = typer.Option(None, "--type"),
    vault: str = typer.Option(None),
    project: str = typer.Option(None, help="this project plus global (default: all)"),
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
        project=project,
        limit=limit,
        reason=reason,
    )
    for h in hits:
        m = h.memory
        typer.echo(f"{h.score:5.2f}  {m.id}  {_where(m)}  {m.text}")
    if not hits:
        typer.echo("nothing matched", err=True)


@app.command("list")
@_guard
def list_(
    type: str = typer.Option(None, "--type"),
    vault: str = typer.Option(None),
    project: str = typer.Option(None, help="this project plus global (default: all)"),
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
        project=project,
        statuses=[status],
        limit=limit,
    ):
        typer.echo(f"{m.id}  {_short(m.created_at)}  {_where(m)}  {m.text}")


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


@app.command()
@_guard
def pull(
    client: str = typer.Option(..., help="which agent is asking; its grants apply"),
    project: str = typer.Option(None, help="project name or path (default: cwd)"),
    facts: int = typer.Option(40, help="how many standing facts at most"),
    episodes: int = typer.Option(5, help="how many recent episodes for this project"),
    reason: str = typer.Option("session start"),
):
    """Print what an agent should know at session start, from the vaults it may read.

    Meant for a SessionStart hook: the output lands in the agent's context, and the audit
    log records exactly which memories it was handed.
    """
    iam = _iam()
    who = iam.whoami(client)
    if not who["read"]:
        return
    here = project_name(project or Path.cwd())
    fs = iam.list_memories(client, types=["fact"], project=here, limit=facts, reason=reason)
    eps = iam.list_memories(client, types=["episode"], project=here, limit=episodes, reason=reason)
    typer.echo(
        f"# From the user's memory vault (client: {client}; project: {here}; "
        f"vaults: {', '.join(who['read'])})"
    )
    typer.echo("Treat these as the canonical facts; your own memory is a cache of them.")
    typer.echo("Write new durable facts with the iam `remember` tool, with a reason.")
    if fs:
        typer.echo("\n## Standing facts")
        for m in fs:
            cats = f" [{', '.join(m.categories)}]" if m.categories else ""
            text = m.text.replace("\n", "\n  ")  # keep a multi-line memory inside its bullet
            scope = " (this project)" if m.project else ""
            typer.echo(f"- {text}{cats}{scope}")
    if eps:
        typer.echo("\n## Recent work")
        for m in eps:
            typer.echo(f"- {_short(m.created_at)}: {m.text}")


# ----- phase 3: capture, extract, review ------------------------------------------------------


claude_app = typer.Typer(
    help="Claude Code: capture its transcripts, import or evict its memory.", no_args_is_help=True
)
app.add_typer(claude_app, name="claude-code")


def _slug(project: str | None) -> str:
    from .capture import project_slug

    return project_slug(project or Path.cwd())


def _name(project: str | None) -> str | None:
    return project_name(Path(project or Path.cwd()).resolve())


@claude_app.command("projects")
def claude_projects():
    """List the projects Claude Code has transcripts for."""
    from .capture import list_projects, memory_files

    for slug, path in list_projects():
        n = len(list(path.glob("*.jsonl")))
        m = len(memory_files(slug))
        typer.echo(f"{slug}  ({n} sessions, {m} memory files)")


@claude_app.command("capture")
@_guard
def claude_capture(
    project: str = typer.Option(None, help="project path (default: cwd)"),
    vault: str = typer.Option(...),
    since: str = typer.Option(None, help="e.g. 7d"),
    reason: str = typer.Option(..., prompt=True),
):
    """Store each session transcript as a source, ready for `iam extract`."""
    from .capture import capture_transcripts

    s = now() - _parse_duration(since) if since else None
    made = capture_transcripts(_iam(), USER, _slug(project), vault=vault, reason=reason, since=s)
    for src in made:
        typer.echo(f"{src.id}  {src.origin}  ({len(src.content)} chars)")
    typer.echo(f"captured {len(made)} session(s)")


@claude_app.command("import")
@_guard
def claude_import(
    project: str = typer.Option(None),
    vault: str = typer.Option(...),
    reason: str = typer.Option(..., prompt=True),
):
    """Turn Claude Code's memory files into proposed memories, scoped to the project they
    belong to. Nothing is deleted."""
    from .capture import import_memory_dir

    for path, m in import_memory_dir(
        _iam(), USER, _slug(project), vault=vault, reason=reason, project=_name(project)
    ):
        typer.echo(f"{m.id if m else 'already captured':26s}  {path.name}")
    typer.echo("run `iam review` to accept them")


@claude_app.command("evict")
@_guard
def claude_evict(
    project: str = typer.Option(None),
    vault: str = typer.Option(...),
    yes: bool = typer.Option(False, "--yes", help="delete without asking"),
    reason: str = typer.Option(..., prompt=True),
):
    """Import Claude Code's memory files, then delete them. The vault holds the only copy."""
    from .capture import evict_memory_dir, memory_files

    slug = _slug(project)
    files = memory_files(slug)
    if not files:
        typer.echo("no memory files to evict")
        return
    if not yes and not typer.confirm(f"import and delete {len(files)} memory file(s) in {slug}?"):
        raise typer.Exit(1)
    imported, removed = evict_memory_dir(
        _iam(), USER, slug, vault=vault, reason=reason, project=_name(project)
    )
    typer.echo(
        f"imported {sum(1 for _, m in imported if m)} as proposed, removed {len(removed)} file(s)"
    )
    typer.echo("run `iam review` to accept them")


@app.command()
def sources(vault: str = typer.Option(None)):
    """List captured raw context."""
    for s in _iam().store.list_sources([vault] if vault else None):
        typer.echo(
            f"{s.id}  {_short(s.captured_at)}  [{s.vault}/{s.kind}]  {s.origin}  "
            f"({len(s.content)} chars)"
        )


@app.command()
@_guard
def extract(
    source_id: str,
    model: str = typer.Option(None, envvar="IAM_LLM_MODEL"),
    base_url: str = typer.Option(
        None, envvar="IAM_LLM_BASE_URL", help="OpenAI-compatible endpoint; default Ollama"
    ),
    reason: str = typer.Option("extract"),
):
    """Ask a model to propose memories from a source. Local by default; nothing is active
    until reviewed."""
    from .extract import extract as _extract
    from .extract import make_client

    if not model:
        _die("pass --model or set IAM_LLM_MODEL (e.g. llama3.1 on Ollama)")
    client, url = make_client(base_url)
    made = _extract(
        _iam(), USER, source_id, client=client, base_url=url, model=model, reason=reason
    )
    for m in made:
        typer.echo(f"{m.id}  {_where(m)}  {m.text}")
    typer.echo(f"proposed {len(made)}; run `iam review`")


@app.command()
def proposed(vault: str = typer.Option(None)):
    """List memories waiting for review."""
    for m in _iam().list_memories(USER, vault=vault, statuses=["proposed"], limit=None):
        typer.echo(f"{m.id}  {_where(m)}  {m.text[:100]}")


@app.command()
@_guard
def reject(id: str, reason: str = typer.Option(..., prompt=True)):
    """Reject a proposed memory. It is kept as retracted, like any other forget."""
    _iam().forget(USER, id, reason=reason)
    typer.echo(f"rejected {id}")


@app.command()
@_guard
def review(vault: str = typer.Option(None)):
    """Walk through proposed memories: accept, edit, reject or skip each one."""
    iam = _iam()
    todo = iam.list_memories(USER, vault=vault, statuses=["proposed"], limit=None)
    if not todo:
        typer.echo("nothing to review")
        return
    for i, m in enumerate(todo, 1):
        typer.echo("")
        typer.secho(
            f"[{i}/{len(todo)}] {m.id}  {m.vault}/{m.type}  {' '.join(m.categories)}", bold=True
        )
        typer.echo(f"  applies: {'project ' + m.project if m.project else 'everywhere'}")
        typer.echo(m.text)
        p = m.provenance
        typer.echo(
            typer.style(
                f"  via {p.via}"
                + (f" by {p.model}" if p.model else "")
                + (f"; evidence: {m.meta['evidence']}" if m.meta.get("evidence") else ""),
                dim=True,
            )
        )
        menu = "  [a]ccept / [e]dit / [r]eject / [s]kip / [q]uit"
        if m.project:
            menu = menu.replace(" / [e]dit", " / [g]lobal: accept for every project / [e]dit")
        choice = typer.prompt(menu, default="s").strip().lower()[:1]
        if choice == "a":
            iam.accept(USER, m.id, reason="accepted in review")
        elif choice == "g" and m.project:
            iam.accept(USER, m.id, reason="accepted in review, for every project", everywhere=True)
        elif choice == "e":
            text = typer.prompt("  text", default=m.text)
            iam.accept(USER, m.id, reason="edited in review", text=text)
        elif choice == "r":
            iam.forget(USER, m.id, reason=typer.prompt("  why", default="rejected in review"))
        elif choice == "q":
            break


if __name__ == "__main__":
    app()
