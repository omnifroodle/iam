"""Propose memories from a raw source with a model. Nothing becomes active until a person
accepts it; everything proposed carries the model's name and the source it came from.

The model is reached through an OpenAI-compatible endpoint, so the same code runs against
Ollama or LM Studio on this machine or a hosted provider. A vault marked local-only refuses
any endpoint that is not loopback.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol
from urllib.parse import urlparse

from .core import Iam, IamError
from .models import MEMORY_TYPES, Memory

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama

PROMPT = """You are extracting durable memories about a person from a transcript of their
work with an AI agent. Return JSON: {"memories": [{"type": "fact" | "episode",
"text": "...", "categories": ["..."], "evidence": "short quote from the source"}]}.

A fact is something durable about the person or their world: preferences, conventions,
who they work with, tools they use, decisions they have made. Write it as a standalone
sentence that will still make sense out of context.
An episode is a record of work done: what, where (project or repo), how it ended.
Skip anything transient, anything about the agent rather than the person, and anything
you are not confident of. Fewer good memories beat many weak ones. Return at most 15.
"""


class ChatClient(Protocol):
    """The slice of an OpenAI-compatible client that extraction uses."""

    class chat:  # noqa: N801
        class completions:  # noqa: N801
            @staticmethod
            def create(**kwargs: Any) -> Any: ...


def is_loopback(base_url: str) -> bool:
    host = urlparse(base_url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def make_client(base_url: str | None = None, api_key: str | None = None):
    from openai import OpenAI

    base_url = base_url or os.environ.get("IAM_LLM_BASE_URL", DEFAULT_BASE_URL)
    api_key = api_key or os.environ.get("IAM_LLM_API_KEY", "not-needed")
    return OpenAI(base_url=base_url, api_key=api_key), base_url


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def propose(client, model: str, content: str) -> list[dict[str, Any]]:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": content},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start : end + 1]) if start >= 0 else {}
    out = []
    for m in data.get("memories", []):
        text = (m.get("text") or "").strip()
        if not text or m.get("type") not in MEMORY_TYPES:
            continue
        out.append(
            {
                "type": m["type"],
                "text": text,
                "categories": [str(c) for c in (m.get("categories") or [])][:6],
                "evidence": (m.get("evidence") or "")[:300],
            }
        )
    return out


def extract(
    iam: Iam,
    actor: str,
    source_id: str,
    *,
    client,
    base_url: str,
    model: str,
    reason: str,
    chunk_chars: int = 60_000,
) -> list[Memory]:
    src = iam.store.get_source(source_id)
    if src is None or src.vault not in iam.writable(actor):
        raise IamError(f"no source {source_id!r}")
    vault = iam.store.get_vault(src.vault)
    if vault and vault.local_only and not is_loopback(base_url):
        raise IamError(f"vault {src.vault!r} is local-only; {base_url} is not on this machine")
    proposals: list[dict[str, Any]] = []
    for chunk in _chunks(src.content, chunk_chars):
        proposals += propose(client, model, chunk)
    made = []
    for p in proposals:
        made.append(
            iam.remember(
                actor,
                p["text"],
                p["type"],
                src.vault,
                categories=p["categories"],
                reason=reason,
                status="proposed",
                via="extract",
                model=model,
                source_ids=[src.id],
                meta={"evidence": p["evidence"]} if p["evidence"] else {},
            )
        )
    iam._log(  # noqa: SLF001 - one row for the extraction run, on top of one per proposal
        actor,
        "extract",
        request={"source_id": src.id, "vault": src.vault, "model": model, "base_url": base_url},
        result={"memory_ids": [m.id for m in made], "count": len(made), "vault": src.vault},
        reason=reason,
        session=None,
    )
    return made
