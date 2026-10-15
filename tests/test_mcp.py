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
    assert ep["type"] == "episode" and ep["meta"]["project"] == "iam"

    entries = seeded.audit(actor="claude-code")
    assert {e.action for e in entries} >= {"recall", "remember"}
    assert all(e.session == "sess-1" and e.via == "mcp" for e in entries)
    assert seeded.store.get_memory(ok["id"]).provenance.actor == "claude-code"
