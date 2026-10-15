"""The agent's surface: one stdio MCP server per client.

The client name comes from the launch args, which is how every MCP host configures
servers. The project is the basename of the server's working directory, so agents never
have to name it. Every tool call is audited under that name; every tool requires a reason.
Ungranted vaults are omitted, not refused, so an agent cannot learn they exist.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .core import Iam, IamError, project_name, session_id
from .models import MEMORY_TYPES

INSTRUCTIONS = """Iam is the user's personal memory vault. It is the canonical store; your own
memory is a cache of it. Call whoami first to learn which vaults you may read and write
and which project you are in. Recall sees this project's memories and global ones;
remember writes to this project unless you set everywhere for a fact about the user.
Every call needs a short, honest reason: the user reads those reasons in the audit log.
Write a memory the moment you learn something durable about the user (remember) and log
what you did for them when a piece of work ends (log_episode). If you are unsure whether
something is worth keeping, capture it as raw context instead of asserting it."""


def build(
    iam: Iam, client: str, session: str | None = None, project: str | None = None
) -> MCPServer:
    session = session or session_id()
    here = project_name(project)
    server = MCPServer(name="iam", instructions=INSTRUCTIONS)

    def _m(m) -> dict:
        return {
            "id": m.id,
            "type": m.type,
            "vault": m.vault,
            "project": m.project,
            "categories": m.categories,
            "text": m.text,
            "created_at": m.created_at.isoformat(),
            "meta": m.meta,
        }

    @server.tool()
    def whoami() -> dict:
        """Which vaults this client may read and write, and the project it is in. Call this
        first."""
        return {**iam.whoami(client), "project": here}

    @server.tool()
    def recall(
        query: str,
        reason: str,
        types: list[str] | None = None,
        categories: list[str] | None = None,
        vault: str | None = None,
        limit: int = 8,
    ) -> dict:
        """Semantic search over the vaults you may read, within this project plus global
        memories. types: fact|episode. Returns the best matches with scores; the audit log
        records which ids you were given."""
        try:
            hits = iam.recall(
                client,
                query,
                types=types,
                categories=categories,
                vault=vault,
                project=here,
                limit=limit,
                reason=reason,
                session=session,
            )
        except IamError as e:
            return {"error": str(e)}
        return {"hits": [{"score": round(h.score, 3), **_m(h.memory)} for h in hits]}

    @server.tool()
    def remember(
        text: str,
        vault: str,
        reason: str,
        type: str = "fact",
        categories: list[str] | None = None,
        supersedes: str | None = None,
        everywhere: bool = False,
    ) -> dict:
        """Write a durable memory. type: fact (a statement about the user or their world) or
        episode (a record of work done). supersedes: the id of a memory this replaces.
        It applies to this project only, unless everywhere is true: set that for facts about
        the user in general (a preference, a tool, a person), not about this codebase."""
        if type not in MEMORY_TYPES:
            return {"error": f"type must be one of {', '.join(MEMORY_TYPES)}"}
        try:
            m = iam.remember(
                client,
                text,
                type,
                vault,
                categories=categories or [],
                supersedes=supersedes,
                project=None if everywhere else here,
                reason=reason,
                session=session,
            )
        except IamError as e:
            return {"error": str(e)}
        return _m(m)

    @server.tool()
    def log_episode(
        summary: str,
        vault: str,
        reason: str,
        project: str | None = None,
        outcome: str | None = None,
        categories: list[str] | None = None,
    ) -> dict:
        """Record what you did for the user in this session: what and how it ended.
        project defaults to the one you are in."""
        meta = {"outcome": outcome} if outcome else {}
        try:
            m = iam.remember(
                client,
                summary,
                "episode",
                vault,
                categories=categories or [],
                reason=reason,
                project=project or here,
                session=session,
                meta=meta,
            )
        except IamError as e:
            return {"error": str(e)}
        return _m(m)

    @server.tool()
    def capture(
        content: str, vault: str, reason: str, kind: str = "capture", origin: str | None = None
    ) -> dict:
        """Hand over raw context (notes, a transcript excerpt) without asserting anything.
        The user may extract memories from it later."""
        try:
            s = iam.capture(
                client,
                kind=kind,
                origin=origin or f"mcp:{client}",
                content=content,
                vault=vault,
                reason=reason,
                project=here,
                session=session,
            )
        except IamError as e:
            return {"error": str(e)}
        return {"source_id": s.id, "vault": s.vault}

    @server.tool()
    def forget(id: str, reason: str) -> dict:
        """Retract a memory you may write. It is kept, marked retracted, and stays auditable."""
        try:
            m = iam.forget(client, id, reason=reason, session=session)
        except IamError as e:
            return {"error": str(e)}
        return {"id": m.id, "status": m.status}

    return server


def main() -> None:
    p = argparse.ArgumentParser(description="Iam MCP server (stdio).")
    p.add_argument("--client", required=True, help="this agent's name; grants are looked up by it")
    p.add_argument(
        "--home", type=Path, default=None, help="vault directory (default $IAM_HOME or ~/.iam)"
    )
    p.add_argument(
        "--project", default=None, help="project name or path (default: the working directory)"
    )
    a = p.parse_args()
    iam = Iam.open(a.home, via="mcp")
    build(iam, a.client, project=a.project or Path.cwd()).run("stdio")


if __name__ == "__main__":
    main()
