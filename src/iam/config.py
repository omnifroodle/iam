"""Where the vault lives and how it is configured.

IAM_HOME overrides the directory (default ~/.iam). IAM_EMBEDDER overrides the embedding
model name; tests set it to "hash-256".
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .embed import DEFAULT_MODEL

DEFAULT_VAULTS = {
    "personal": "Facts about you as a person. Private by default.",
    "work": "Your job, employer, colleagues, projects.",
    "coding": "How you like code written; conventions and tooling preferences.",
}


def home() -> Path:
    return Path(os.environ.get("IAM_HOME", Path.home() / ".iam")).expanduser()


@dataclass
class Config:
    embedding_model: str = DEFAULT_MODEL
    default_vaults: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_VAULTS))

    @classmethod
    def load(cls, root: Path | None = None) -> Config:
        root = root or home()
        path = root / "config.json"
        cfg = cls()
        if path.exists():
            data = json.loads(path.read_text())
            cfg.embedding_model = data.get("embedding_model", cfg.embedding_model)
        if env := os.environ.get("IAM_EMBEDDER"):
            cfg.embedding_model = env
        return cfg

    def save(self, root: Path | None = None) -> Path:
        root = root or home()
        root.mkdir(parents=True, exist_ok=True)
        path = root / "config.json"
        path.write_text(json.dumps({"embedding_model": self.embedding_model}, indent=2) + "\n")
        return path


def db_path(root: Path | None = None) -> Path:
    return (root or home()) / "iam.db"
