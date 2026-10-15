"""Where the vault lives and how it is configured.

IAM_HOME overrides the directory (default ~/.iam). IAM_EMBEDDER overrides the embedding
model name; tests set it to "hash-256".

The model host for extraction is any OpenAI-compatible endpoint. config.json holds its
base URL and model name; IAM_LLM_BASE_URL and IAM_LLM_MODEL override them. The key is
never written to disk by Iam: it comes from IAM_LLM_API_KEY only.
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
    llm_base_url: str | None = None
    llm_model: str | None = None
    default_vaults: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_VAULTS))

    @classmethod
    def load(cls, root: Path | None = None, *, env: bool = True) -> Config:
        """env=False reads the file alone, for code that is about to save it."""
        root = root or home()
        path = root / "config.json"
        cfg = cls()
        if path.exists():
            data = json.loads(path.read_text())
            cfg.embedding_model = data.get("embedding_model", cfg.embedding_model)
            llm = data.get("llm") or {}
            cfg.llm_base_url, cfg.llm_model = llm.get("base_url"), llm.get("model")
        if not env:
            return cfg
        if name := os.environ.get("IAM_EMBEDDER"):
            cfg.embedding_model = name
        cfg.llm_base_url = os.environ.get("IAM_LLM_BASE_URL") or cfg.llm_base_url
        cfg.llm_model = os.environ.get("IAM_LLM_MODEL") or cfg.llm_model
        return cfg

    def save(self, root: Path | None = None) -> Path:
        root = root or home()
        root.mkdir(parents=True, exist_ok=True)
        path = root / "config.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data["embedding_model"] = self.embedding_model
        llm = {"base_url": self.llm_base_url, "model": self.llm_model}
        if any(llm.values()):
            data["llm"] = {k: v for k, v in llm.items() if v}
        path.write_text(json.dumps(data, indent=2) + "\n")
        return path


def db_path(root: Path | None = None) -> Path:
    return (root or home()) / "iam.db"
