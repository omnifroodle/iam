import sqlite3
from datetime import timedelta

import pytest

from iam.core import IamError
from iam.models import USER, now


def audit_count(iam):
    return iam.store.conn.execute("SELECT count(*) FROM audit").fetchone()[0]


def test_no_write_without_audit(iam):
    before = audit_count(iam)
    m = iam.remember(USER, "A fact.", "fact", "coding", reason="because")
    assert audit_count(iam) == before + 1
    e = iam.audit(memory_id=m.id)[0]
    assert e.action == "remember" and e.actor == USER and e.reason == "because"
    assert e.result["memory_ids"] == [m.id]


def test_write_requires_reason(iam):
    with pytest.raises(IamError):
        iam.remember(USER, "A fact.", "fact", "coding", reason="  ")


def test_recall_is_audited_with_ids_given(seeded):
    hits = seeded.recall(USER, "OpenAI-compatible client with base_url", reason="checking")
    assert hits and hits[0].memory.vault == "coding"
    e = seeded.audit(action="recall")[0]
    assert e.result["memory_ids"][0] == hits[0].memory.id
    assert e.request["query"].startswith("OpenAI")


def test_ungranted_vault_is_invisible(seeded):
    seeded.grant(USER, "bot", "coding", "read", reason="test")
    hits = seeded.recall("bot", "cats Tucson home", reason="snooping")
    assert all(h.memory.vault == "coding" for h in hits)
    # asking for it by name is indistinguishable from it not existing
    with pytest.raises(IamError, match="no readable vault"):
        seeded.recall("bot", "cats", vault="personal", reason="snooping")
    with pytest.raises(IamError, match="no readable vault"):
        seeded.recall("bot", "cats", vault="does-not-exist", reason="snooping")
    assert seeded.whoami("bot") == {"client": "bot", "read": ["coding"], "write": []}


def test_read_grant_cannot_write(seeded):
    seeded.grant(USER, "bot", "coding", "read", reason="test")
    with pytest.raises(IamError, match="no writable vault"):
        seeded.remember("bot", "x", "fact", "coding", reason="r")


def test_grant_expiry(seeded):
    seeded.grant(
        USER, "bot", "coding", "read-write", reason="t", expires_at=now() - timedelta(seconds=1)
    )
    assert seeded.readable("bot") == []
    seeded.grant(
        USER, "bot", "coding", "read-write", reason="t", expires_at=now() + timedelta(days=1)
    )
    assert seeded.writable("bot") == ["coding"]


def test_only_user_grants(seeded):
    with pytest.raises(IamError):
        seeded.grant("bot", "bot", "personal", "read", reason="please")


def scoped(iam):
    """One memory in each of two projects, one global, all in the same vault."""
    a = iam.remember(
        USER, "Iam tests use the hash embedder.", "fact", "coding", reason="s", project="iam"
    )
    b = iam.remember(
        USER,
        "Notebook tests hit a live cluster.",
        "fact",
        "coding",
        reason="s",
        project="couchbase_notebooks",
    )
    g = iam.remember(USER, "Writes tests before fixing a bug.", "fact", "coding", reason="s")
    return a, b, g


def test_recall_is_this_project_plus_global_never_another(iam):
    a, b, g = scoped(iam)
    iam.grant(USER, "bot", "coding", "read", reason="t")
    for actor in (USER, "bot"):
        ids = {h.memory.id for h in iam.recall(actor, "tests", project="iam", reason="r")}
        assert ids == {a.id, g.id}
        listed = {m.id for m in iam.list_memories(actor, project="iam")}
        assert listed == {a.id, g.id}
    e = iam.audit(action="recall")[0]
    assert e.request["project"] == "iam" and b.id not in e.result["memory_ids"]


def test_project_is_a_name_not_a_path(iam):
    a, b, g = scoped(iam)
    ids = {h.memory.id for h in iam.recall(USER, "tests", project="/Users/x/code/iam/", reason="r")}
    assert ids == {a.id, g.id}
    m = iam.remember(USER, "x", "fact", "coding", reason="r", project="/Users/x/code/iam")
    assert m.project == "iam"


def test_agent_without_a_project_sees_only_global(iam):
    a, b, g = scoped(iam)
    iam.grant(USER, "bot", "coding", "read", reason="t")
    assert {h.memory.id for h in iam.recall("bot", "tests", reason="r")} == {g.id}
    assert {m.id for m in iam.list_memories("bot")} == {g.id}
    # the user, at the CLI, with no project, sees every project
    assert {m.id for m in iam.list_memories(USER)} == {a.id, b.id, g.id}


def test_project_scope_never_widens_vault_grants(iam):
    a, b, g = scoped(iam)
    p = iam.remember(USER, "Private iam note.", "fact", "personal", reason="s", project="iam")
    iam.grant(USER, "bot", "coding", "read", reason="t")
    ids = {h.memory.id for h in iam.recall("bot", "iam note", project="iam", reason="r")}
    assert p.id not in ids and ids == {a.id, g.id}


def test_migrate_moves_meta_project_into_the_field_with_an_audit_row(iam, tmp_path, monkeypatch):
    from iam.models import Memory, Provenance

    # A Claude Code project dir whose transcript says where it ran: slugs can't be reversed.
    claude = tmp_path / "claude"
    slug = "-Volumes-x-couchbase-notebooks"
    (claude / "projects" / slug).mkdir(parents=True)
    (claude / "projects" / slug / "s.jsonl").write_text(
        '{"type": "user", "cwd": "/Volumes/x/couchbase_notebooks", "message": {"content": "hi"}}\n'
    )
    monkeypatch.setenv("IAM_CLAUDE_HOME", str(claude))
    prov = Provenance(actor=USER, via="cli", reason="old")
    old = [
        Memory(
            type="episode",
            vault="work",
            text="Shipped it.",
            provenance=prov,
            meta={"project": "iam", "outcome": "ok"},
        ),
        Memory(
            type="fact",
            vault="work",
            text="Imported.",
            provenance=prov,
            status="proposed",
            meta={"project": slug},
        ),
        Memory(type="fact", vault="work", text="Global.", provenance=prov),
    ]
    for m in old:
        iam.store.put_memory(m, iam.embedder.embed([m.text])[0])
    iam.store.set_data_version(0)
    before = audit_count(iam)

    moved = iam.migrate()
    assert set(moved) == {old[0].id, old[1].id}
    ep, imp, glob = (iam.store.get_memory(m.id) for m in old)
    assert ep.project == "iam" and ep.meta == {"outcome": "ok"}
    assert imp.project == "couchbase_notebooks" and imp.status == "proposed"
    assert glob.project is None
    e = iam.audit(action="migrate")[0]
    assert audit_count(iam) == before + 1 and set(e.result["memory_ids"]) == set(moved)
    assert e.via == "migrate" and e.reason
    # the column is what recall filters on, so the move must reach it
    hits = iam.recall(USER, "Shipped it", project="other", reason="r")
    assert ep.id not in {h.memory.id for h in hits}
    after = audit_count(iam)
    assert iam.migrate() == [] and audit_count(iam) == after


def test_old_database_gains_the_project_column(tmp_path):
    import sqlite3 as sq

    from iam.store import SqliteStore

    path = tmp_path / "old.db"
    c = sq.connect(path)
    c.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, type TEXT NOT NULL, vault TEXT NOT NULL,"
        " status TEXT NOT NULL, created_at TEXT NOT NULL, embedding BLOB,"
        " embedding_dim INTEGER, doc TEXT NOT NULL)"
    )
    c.close()
    store = SqliteStore(path)
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(memories)")}
    assert "project" in cols and store.data_version() == 0


def test_import_of_an_old_export_lifts_meta_project(iam):
    doc = {
        "type": "episode",
        "vault": "work",
        "text": "Old episode.",
        "provenance": {"actor": USER, "via": "cli", "reason": "r"},
        "meta": {"project": "iam"},
    }
    m = iam.import_(USER, [doc], reason="restore")[0]
    assert iam.store.get_memory(m.id).project == "iam" and "project" not in m.meta


def test_forget_retracts_and_hides(seeded):
    m = seeded.recall(USER, "Tucson cats", reason="r")[0].memory
    seeded.forget(USER, m.id, reason="moved")
    assert seeded.store.get_memory(m.id).status == "retracted"
    assert m.id not in [h.memory.id for h in seeded.recall(USER, "Tucson cats", reason="r")]
    history = [e.action for e in seeded.audit(memory_id=m.id)]
    assert history[0] == "forget" and "remember" in history and "recall" in history


def test_supersede(seeded):
    old = seeded.recall(USER, "Tucson", reason="r")[0].memory
    new = seeded.remember(
        USER, "Lives in Portland.", "fact", "personal", reason="moved", supersedes=old.id
    )
    assert seeded.store.get_memory(old.id).status == "superseded"
    assert new.supersedes == old.id
    ids = [h.memory.id for h in seeded.recall(USER, "where does the user live", reason="r")]
    assert new.id in ids and old.id not in ids


def test_audit_table_is_append_only(iam):
    iam.remember(USER, "x", "fact", "coding", reason="r")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        iam.store.conn.execute("UPDATE audit SET actor='nobody'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        iam.store.conn.execute("DELETE FROM audit")


def test_failed_write_leaves_no_audit_row(seeded):
    before = audit_count(seeded)
    with pytest.raises(IamError):
        seeded.remember(USER, "x", "fact", "personal", reason="r", supersedes="nope")
    assert audit_count(seeded) == before
    assert (
        seeded.store.find_memories(
            vaults=None, types=None, categories=None, statuses=["active"]
        ).__len__()
        == 3
    )


def test_filters(seeded):
    assert all(
        h.memory.type == "episode"
        for h in seeded.recall(USER, "notebooks", types=["episode"], reason="r")
    )
    assert [
        h.memory.vault for h in seeded.recall(USER, "anything", categories=["home"], reason="r")
    ] == ["personal"]


def test_capture_and_accept(seeded):
    s = seeded.capture(
        USER, kind="note", origin="test", content="raw text", vault="work", reason="r"
    )
    assert seeded.store.get_source(s.id).content_sha256
    p = seeded.remember(
        USER,
        "Proposed thing.",
        "fact",
        "work",
        reason="extract",
        status="proposed",
        source_ids=[s.id],
        via="extract",
        model="test-model",
    )
    assert p.id not in [h.memory.id for h in seeded.recall(USER, "proposed thing", reason="r")]
    seeded.accept(USER, p.id, reason="looks right", text="Proposed thing, edited.")
    m = seeded.store.get_memory(p.id)
    assert m.status == "active" and m.text.endswith("edited.") and m.provenance.source_ids == [s.id]
    assert p.id in [h.memory.id for h in seeded.recall(USER, "proposed thing edited", reason="r")]


def test_export_import_roundtrip(seeded, tmp_path):
    docs = seeded.export(USER, reason="backup")
    assert len(docs) == 3 and all("provenance" in d for d in docs)
    other = seeded.__class__(type(seeded.store)(tmp_path / "other.db"), seeded.embedder)
    for v in seeded.vaults():
        other.store.put_vault(v)
    added = other.import_(USER, docs, reason="restore")
    assert len(added) == 3
    assert len(other.import_(USER, docs, reason="again")) == 0
    assert other.recall(USER, "Tucson", reason="r")[0].memory.text.startswith("Lives in Tucson")
