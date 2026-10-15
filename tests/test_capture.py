import json

import pytest

from iam.capture import (
    capture_transcripts,
    evict_memory_dir,
    import_memory_dir,
    parse_memory_file,
    project_for_slug,
    project_slug,
    read_transcript,
)
from iam.models import USER

SLUG = "-tmp-proj"


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    proj = root / "projects" / SLUG
    (proj / "memory").mkdir(parents=True)
    monkeypatch.setenv("IAM_CLAUDE_HOME", str(root))
    rows = [
        {"type": "queue-operation", "operation": "enqueue"},
        {
            "type": "user",
            "timestamp": "2026-09-08T19:51:56.130Z",
            "cwd": "/tmp/proj",
            "message": {"role": "user", "content": "please use uv for this project"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-08T19:51:57.000Z",
            "cwd": "/tmp/proj",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "Switching to uv."},
                ],
            },
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]},
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        },
        {
            "type": "user",
            "isSidechain": True,
            "message": {"role": "user", "content": "subagent chatter"},
        },
        {
            "type": "user",
            "timestamp": "2026-09-08T19:52:00.000Z",
            "message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"},
        },
    ]
    (proj / "sess1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (proj / "empty.jsonl").write_text(json.dumps({"type": "system"}) + "\n")
    (proj / "memory" / "MEMORY.md").write_text("- [Uses uv](uses-uv.md) — tooling\n")
    (proj / "memory" / "uses-uv.md").write_text(
        '---\nname: uses-uv\ndescription: "Uses uv for Python projects"\n'
        "metadata:\n  type: feedback\n"
        "  originSessionId: sess1\n---\n\nMatt asked for uv over pip.\n\n**Why:** speed.\n"
    )
    return proj


def test_project_slug():
    assert (
        project_slug("/Volumes/External/code/couchbase_notebooks")
        == "-Volumes-External-code-couchbase-notebooks"
    )


def test_read_transcript_keeps_only_conversation(claude_home):
    t = read_transcript(claude_home / "sess1.jsonl")
    assert t.session_id == "sess1" and t.cwd == "/tmp/proj"
    assert t.turns == [
        ("user", "please use uv for this project"),
        ("assistant", "Switching to uv."),
    ]
    assert t.started.isoformat().startswith("2026-09-08T19:51:56")
    assert "subagent" not in t.render() and "system-reminder" not in t.render()


def test_capture_transcripts_is_idempotent(seeded, claude_home):
    made = capture_transcripts(seeded, USER, SLUG, vault="coding", reason="t")
    assert len(made) == 1 and made[0].kind == "claude-code-transcript"
    assert "please use uv" in made[0].content
    assert capture_transcripts(seeded, USER, SLUG, vault="coding", reason="t") == []
    assert seeded.audit(action="capture")[0].request["origin"].endswith(":sess1")


def test_parse_memory_file(claude_home):
    meta, body = parse_memory_file(claude_home / "memory" / "uses-uv.md")
    assert (
        meta["name"] == "uses-uv"
        and meta["type"] == "feedback"
        and meta["originSessionId"] == "sess1"
    )
    assert body.startswith("Matt asked for uv")


def test_import_memory_dir_proposes_and_is_idempotent(seeded, claude_home):
    out = import_memory_dir(seeded, USER, SLUG, vault="coding", reason="t")
    assert len(out) == 1
    path, m = out[0]
    assert path.name == "uses-uv.md" and m.status == "proposed"
    assert m.text.startswith("Uses uv for Python projects") and "feedback" in m.categories
    assert m.provenance.via == "import" and m.provenance.session == "sess1"
    assert seeded.store.get_source(m.provenance.source_ids[0]).kind == "claude-code-memory"
    # proposed memories are invisible to recall until accepted
    assert m.id not in [h.memory.id for h in seeded.recall(USER, "uv python", reason="r")]
    assert import_memory_dir(seeded, USER, SLUG, vault="coding", reason="t") == [(path, None)]
    seeded.accept(USER, m.id, reason="ok")
    assert m.id in [h.memory.id for h in seeded.recall(USER, "uv python projects", reason="r")]


def test_evict_removes_files_after_import(seeded, claude_home):
    imported, removed = evict_memory_dir(seeded, USER, SLUG, vault="coding", reason="cache")
    assert [p.name for p in removed] == ["uses-uv.md", "MEMORY.md"]
    assert not (claude_home / "memory" / "uses-uv.md").exists()
    assert seeded.store.get_memory(imported[0][1].id).status == "proposed"
    e = seeded.audit(action="evict")[0]
    assert e.result["memory_ids"] == [imported[0][1].id] and len(e.result["removed"]) == 2


def test_project_for_slug_asks_a_transcript(claude_home):
    assert project_for_slug(SLUG) == "proj"  # cwd /tmp/proj, from sess1.jsonl
    assert project_for_slug("-Volumes-x-no-such-thing") == "thing"


def test_capture_and_import_set_the_project(seeded, claude_home):
    src = capture_transcripts(seeded, USER, SLUG, vault="coding", reason="t")[0]
    assert src.project == "proj"
    _, m = import_memory_dir(seeded, USER, SLUG, vault="coding", reason="t", project="iam")[0]
    assert m.project == "iam" and "project" not in m.meta and m.meta["slug"] == SLUG
    assert seeded.audit(action="remember")[0].request["project"] == "iam"
