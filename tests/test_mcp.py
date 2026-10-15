import json

import pytest

from iam.mcp_server import build
from iam.models import USER


async def call(server, name, **args):
    result = await server.call_tool(name, args)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_mcp_tools_respect_grants_and_audit(seeded):
    seeded.via = "mcp"
    seeded.grant(USER, "claude-code", "coding", "read-write", reason="t")
    server = build(seeded, "claude-code", session="sess-1")

    who = await call(server, "whoami")
    assert who["read"] == ["coding"] and who["write"] == ["coding"]

    got = await call(server, "recall", query="Tucson cats", reason="looking for home facts")
    assert all(h["vault"] == "coding" for h in got["hits"])

    denied = await call(server, "remember", text="x", vault="personal", reason="r")
    assert "error" in denied and "personal" in denied["error"]

    ok = await call(
        server,
        "remember",
        text="Uses uv for Python projects.",
        vault="coding",
        reason="user said so",
        categories=["python"],
    )
    assert ok["vault"] == "coding"
    ep = await call(
        server,
        "log_episode",
        summary="Scaffolded iam.",
        vault="coding",
        reason="r",
        project="iam",
        outcome="tests pass",
    )
    assert ep["type"] == "episode" and ep["project"] == "iam"

    entries = seeded.audit(actor="claude-code")
    assert {e.action for e in entries} >= {"recall", "remember"}
    assert all(e.session == "sess-1" and e.via == "mcp" for e in entries)
    assert seeded.store.get_memory(ok["id"]).provenance.actor == "claude-code"


@pytest.mark.anyio
async def test_mcp_scopes_to_its_project(seeded):
    seeded.via = "mcp"
    seeded.grant(USER, "claude-code", "work", "read-write", reason="t")
    other = seeded.remember(USER, "Iam work note.", "fact", "work", reason="s", project="iam")
    server = build(seeded, "claude-code", session="s", project="/Volumes/x/couchbase_notebooks")

    assert (await call(server, "whoami"))["project"] == "couchbase_notebooks"
    got = await call(server, "recall", query="note notebooks", reason="r")
    ids = {h["id"] for h in got["hits"]}
    assert other.id not in ids and any(h["project"] == "couchbase_notebooks" for h in got["hits"])

    here = await call(server, "remember", text="Uses a live cluster.", vault="work", reason="r")
    glob = await call(
        server, "remember", text="Likes short PRs.", vault="work", reason="r", everywhere=True
    )
    ep = await call(server, "log_episode", summary="Did a thing.", vault="work", reason="r")
    assert here["project"] == ep["project"] == "couchbase_notebooks" and glob["project"] is None
    src = await call(server, "capture", content="raw", vault="work", reason="r")
    assert seeded.store.get_source(src["source_id"]).project == "couchbase_notebooks"
