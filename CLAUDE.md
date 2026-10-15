# Working in this repo

Iam is a personal memory vault for AI agents. **The vault is canonical; every agent is a
cache.** Read [`docs/plan.md`](docs/plan.md) first: it holds the design, the decisions
already taken, the phase status, and the open questions. Don't re-litigate a decision
listed there; propose a change to the plan instead.

## Rules that are easy to forget

- **Every change is audited, in the same transaction.** Writes go through `iam.core.Iam`
  and nowhere else. If you add an operation, it writes its audit row inside the same
  `store.transaction()` as the change. `tests/test_core.py` has the tests that prove this;
  add to them.
- **`reason` is mandatory on writes.** It is what makes `iam audit` readable by a person.
- **Ungranted vaults are invisible.** Error messages never confirm a vault exists. Use the
  same wording for "not granted" and "does not exist".
- **Retract, never delete.** Memories and sources get a status; the audit table has
  triggers that refuse UPDATE and DELETE.
- **Private stays local.** Embeddings run on this machine. Extraction over a `local_only`
  vault refuses a non-loopback endpoint. The model client is OpenAI-compatible with a
  `base_url`; do not add a vendor SDK.
- **Keep it simple until it hurts.** SQLite and numpy. The `Store` protocol in
  `src/iam/store.py` is the seam for the Couchbase backend (phase 5); keep it small.

## Loop

```bash
uv sync --extra dev
uv run pytest -q                 # IAM_EMBEDDER=hash-256 in tests: no model download
uv run ruff format src tests && uv run ruff check src tests
IAM_HOME=/tmp/iam-scratch uv run iam init   # never point a smoke test at ~/.iam
```

Tests use the hashing embedder and a temp `IAM_HOME`; they must never touch the real
vault or the real `~/.claude` directory (`IAM_CLAUDE_HOME` overrides it).

## Layout

- `src/iam/core.py` the only way in; `store.py` SQLite; `embed.py`; `config.py`
- `src/iam/cli.py` the user's surface; `mcp_server.py` the agent's surface
- `src/iam/capture.py` Claude Code transcripts and memory files; `extract.py` model proposals
- `plugin/` the Claude Code plugin (MCP server, skill, SessionStart hook)
- `docs/plan.md` the plan; update its status line when a phase lands
