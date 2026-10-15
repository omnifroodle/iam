"""Embedders. Local by default; a hashing embedder for tests so nothing is downloaded."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HASH = "hash-256"


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (n, dim) float32 array of unit vectors."""
        ...


def _normalise(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


class HashEmbedder:
    """Bag-of-words hashed into a fixed dim. Deterministic, dependency-free, test-only quality."""

    name = HASH
    dim = 256

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in re.findall(r"[a-z0-9]+", text.lower()):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "big")
                out[i, h % self.dim] += 1.0
        return _normalise(out)


class LocalEmbedder:
    """sentence-transformers, loaded on first use. Runs on this machine; nothing leaves."""

    def __init__(self, name: str = DEFAULT_MODEL):
        self.name = name
        self._model = None

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            _quiet_hf()
            from sentence_transformers import SentenceTransformer

            try:  # cached already: no network, no chatter
                self._model = SentenceTransformer(self.name, local_files_only=True)
            except Exception:  # noqa: BLE001
                self._model = SentenceTransformer(self.name)
        vecs = self._model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return np.asarray(vecs, dtype=np.float32)


def _quiet_hf() -> None:
    """A CLI call should not print download bars and token warnings over its own output."""
    import logging
    import os

    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    for name in ("huggingface_hub", "transformers", "sentence_transformers"):
        logging.getLogger(name).setLevel(logging.ERROR)
    try:
        from transformers.utils import logging as tlog

        tlog.set_verbosity_error()
        tlog.disable_progress_bar()
    except Exception:  # noqa: BLE001
        pass


def get_embedder(name: str) -> Embedder:
    if name == HASH:
        return HashEmbedder()
    return LocalEmbedder(name)
