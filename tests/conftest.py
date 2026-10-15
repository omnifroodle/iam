import pytest

from iam.core import Iam
from iam.embed import HashEmbedder
from iam.models import USER, VaultInfo
from iam.store import SqliteStore


@pytest.fixture
def iam(tmp_path, monkeypatch):
    monkeypatch.setenv("IAM_HOME", str(tmp_path))
    monkeypatch.setenv("IAM_EMBEDDER", "hash-256")
    store = SqliteStore(tmp_path / "iam.db")
    for name in ("personal", "work", "coding"):
        store.put_vault(VaultInfo(name=name))
    return Iam(store, HashEmbedder(), via="cli")


@pytest.fixture
def seeded(iam):
    iam.remember(
        USER,
        "Prefers OpenAI-compatible clients with base_url over vendor SDKs.",
        "fact",
        "coding",
        categories=["python", "llm"],
        reason="seed",
    )
    iam.remember(
        USER,
        "Lives in Tucson with two cats.",
        "fact",
        "personal",
        categories=["home"],
        reason="seed",
    )
    iam.remember(
        USER,
        "Re-shipped flows/01 in couchbase_notebooks; the prompt fix held.",
        "episode",
        "work",
        categories=["couchbase"],
        reason="seed",
        project="couchbase_notebooks",
    )
    return iam
