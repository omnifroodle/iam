import json
from types import SimpleNamespace

import pytest

from iam.core import IamError
from iam.extract import extract, is_loopback, propose
from iam.models import USER


class FakeClient:
    """Looks like openai.OpenAI as far as extraction cares; records what it was sent."""

    def __init__(self, reply):
        self.calls = []
        outer = self

        class Completions:
            @staticmethod
            def create(**kw):
                outer.calls.append(kw)
                msg = SimpleNamespace(content=reply)
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        self.chat = SimpleNamespace(completions=Completions())


REPLY = json.dumps(
    {
        "memories": [
            {
                "type": "fact",
                "text": "Uses uv for Python projects.",
                "categories": ["python"],
                "evidence": "use uv",
            },
            {
                "type": "episode",
                "text": "Set up the iam scaffold.",
                "categories": [],
                "evidence": "",
            },
            {"type": "rumour", "text": "ignored", "categories": []},
            {"type": "fact", "text": "   ", "categories": []},
        ]
    }
)


def test_is_loopback():
    assert is_loopback("http://localhost:11434/v1") and is_loopback("http://127.0.0.1:1234/v1")
    assert not is_loopback("https://api.example.com/v1")


def test_propose_filters_bad_rows():
    out = propose(FakeClient(REPLY), "m", "content")
    assert [o["type"] for o in out] == ["fact", "episode"]


def test_propose_tolerates_prose_around_json():
    out = propose(FakeClient("Sure! " + REPLY + " Done."), "m", "content")
    assert len(out) == 2


def test_extract_proposes_with_provenance(seeded):
    src = seeded.capture(
        USER, kind="note", origin="t", content="use uv please", vault="coding", reason="r"
    )
    client = FakeClient(REPLY)
    made = extract(
        seeded,
        USER,
        src.id,
        client=client,
        base_url="http://localhost:11434/v1",
        model="llama3.1",
        reason="trying extraction",
    )
    assert len(made) == 2 and all(m.status == "proposed" for m in made)
    p = made[0].provenance
    assert p.via == "extract" and p.model == "llama3.1" and p.source_ids == [src.id]
    assert made[0].meta["evidence"] == "use uv"
    assert client.calls[0]["messages"][1]["content"] == "use uv please"
    e = seeded.audit(action="extract")[0]
    assert e.result["count"] == 2 and e.request["model"] == "llama3.1"
    assert made[0].id not in [h.memory.id for h in seeded.recall(USER, "uv python", reason="r")]


def test_local_only_vault_refuses_remote_model(seeded):
    seeded.vault_add(USER, "health", local_only=True, reason="t")
    src = seeded.capture(USER, kind="note", origin="t", content="x", vault="health", reason="r")
    with pytest.raises(IamError, match="local-only"):
        extract(
            seeded,
            USER,
            src.id,
            client=FakeClient(REPLY),
            base_url="https://api.example.com/v1",
            model="gpt",
            reason="r",
        )
    assert extract(
        seeded,
        USER,
        src.id,
        client=FakeClient(REPLY),
        base_url="http://localhost:1234/v1",
        model="local",
        reason="r",
    )
