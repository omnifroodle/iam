# Iam — a personal memory vault for AI agents

The agents you use (Claude, Claude Code, Codex, Muse, …) each keep their own memory of you.
Iam inverts that: **the vault is canonical, every agent is a cache.** Memories live in a
store you own, partitioned so an agent sees only what it has been granted, and every read
and write is on the record.

Think 1Password, but for facts about you instead of passwords.

## Principles

1. **The vault is canonical; agents are caches.** Anything an agent remembers locally can be
   evicted and rebuilt from the vault. The vault is never rebuilt from an agent.
2. **Nothing moves without a record.** Every recall, write, retraction, grant and eviction
   leaves an audit row: who asked, for what, what they were given, what they added, and why.
   This ships in the first release, not later — it is the product's claim.
3. **Private stays local.** Embeddings are computed locally. Extraction over a vault marked
   `local-only` never calls a hosted model.
4. **One write path.** The MCP server and the CLI both call the same core API, and the
   audit row is written in the same transaction as the change. There is no way to write a
   memory without an audit row, including from our own tooling.
5. **Keep it simple until it hurts.** SQLite and numpy today. The store sits behind one
   small interface so something else can replace it later without touching the rest, and
   nothing else is planned until one machine stops being enough.

## Vocabulary

| Term | Meaning |
|---|---|
| **memory** | One unit of recall: a fact or an episode. Has a type, a vault, categories, text, an embedding and provenance. |
| **source** | Raw context a memory may be extracted from: a Claude Code transcript, an agent's local memory file, a ChatGPT export, a pasted note. Kept so provenance can point at it. |
| **vault** | The access partition. A memory is in exactly one vault. Initial set: `personal`, `work`, `coding`. |
| **category** | Free-form tags for filtering within or across vaults (`python`, `family`, `music`). Many per memory. |
| **client** | An agent identity: `claude-code`, `claude-desktop`, `codex`, or `user` for the CLI. |
| **grant** | client × vault × mode (`read`, `write`, `read-write`) × optional expiry. No grant, no access, and no hint that the vault exists. |
| **audit** | Append-only log of every action against the vault. |

## Memory types

Two to start. Types are a closed set; categories are the open one.

- **fact** — an atomic, durable statement. "Prefers OpenAI-compatible clients over vendor
  SDKs." "Lives in Tucson." A fact can `supersede` an earlier fact; the old one is kept,
  marked superseded, and stays in the audit trail.
- **episode** — a record of work done, usually by an agent: what, where (project, repo),
  when, outcome. "2026-10-12: re-shipped notebook 01 in my-notebooks; the prompt fix held
  across 3 runs." This is the "context history" of agent work.

Likely later: `preference` if facts get crowded, `procedure` for how-to knowledge. Add a
type only when filtering by category stops being enough.

## Data model

JSON documents, deliberately shaped so a document store could hold them unchanged if one
is ever needed. IDs are ULIDs (sortable, no coordination).

```jsonc
// memories
{
  "id": "01J...",
  "type": "fact",                       // fact | episode
  "vault": "coding",
  "categories": ["python", "llm"],
  "text": "Prefers OpenAI-compatible clients with base_url over vendor SDKs.",
  "embedding": [/* float32 */],         // stored as blob in SQLite
  "embedding_model": "bge-small-en-v1.5",
  "status": "active",                   // active | superseded | retracted
  "supersedes": null,                   // id of the memory this replaces
  "created_at": "2026-10-15T18:02:11Z",
  "provenance": {
    "actor": "claude-code",             // client id, or "user"
    "via": "mcp",                       // mcp | cli | extract | import
    "reason": "User corrected me when I reached for the anthropic SDK.",
    "session": "52c61a0a-…",            // agent session id when known
    "source_ids": ["01J…"],             // sources this was derived from
    "model": null                       // extraction model, when via=extract
  }
}

// sources (raw context)
{
  "id": "01J...", "kind": "claude-code-transcript",   // | claude-code-memory | chatgpt-export | file | capture
  "vault": "coding", "origin": "~/.claude/projects/…/52c6….jsonl",
  "content_sha256": "…", "content": "…",              // or a path, for large ones
  "captured_at": "…", "captured_by": "user", "reason": "weekly capture"
}

// grants
{ "client": "claude-code", "vault": "coding", "mode": "read-write",
  "expires_at": null, "created_at": "…", "created_by": "user", "reason": "daily driver" }

// audit (append-only; never updated, never deleted)
{
  "id": "01J...", "at": "…",
  "actor": "claude-code", "via": "mcp", "session": "52c6…",
  "action": "recall",                   // recall | remember | forget | list | capture | extract | grant | revoke | evict | export | import
  "request": { "query": "how does the user like LLM clients", "types": ["fact"], "vault": "coding", "limit": 5 },
  "result": { "memory_ids": ["01J…", "01J…"], "count": 2 },
  "reason": "Starting a session in my-notebooks; checking conventions."
}
```

**Provenance is the memory's own record of where it came from.** Audit is the vault's
record of what happened. They overlap on purpose: provenance travels with the memory on
export, audit stays with the vault.

### What the audit log must answer, from the first release

- *Who asked for what?* — `actor`, `action`, `request`, with the natural-language `reason`
  the caller supplied. `reason` is **required** on every MCP write and every recall. An agent
  that cannot say why it wants something does not get it.
- *What were they given?* — `result.memory_ids` on every recall and list. Reconstructable:
  "show me everything claude-code has ever seen from `personal`."
- *What was added, by whom, when, why?* — one `remember` row per write, with the new id,
  the actor, and the reason; the memory's provenance carries the same.
- *What has happened to this memory?* — `iam audit --memory <id>`: created, recalled N
  times by whom, superseded, retracted.

Append-only is enforced by the store (no update or delete path exists for the table).
A hash chain over rows is a cheap later addition if tamper-evidence matters; not v0.

## Components

```
            ┌──────────────┐      ┌──────────────┐
  agents ──▶│  iam-mcp     │      │  iam (CLI)   │◀── you
            │  (stdio, one │      │              │
            │  per client) │      │              │
            └──────┬───────┘      └──────┬───────┘
                   └───────┬─────────────┘
                     ┌─────▼─────┐
                     │ iam.core  │  grants · audit · embed · search · extract
                     └─────┬─────┘
                     ┌─────▼─────┐
                     │ Store     │  SQLite + numpy
                     └───────────┘
```

### `iam.core` — the only way in

- `recall(actor, query, *, types, categories, vault, limit, reason)` — filters in SQL
  (vault ∩ grants, type, categories, status=active), cosine over the survivors in numpy,
  audit row with the returned ids. Personal scale: brute force is fine past 100k memories.
- `remember(actor, text, type, vault, categories, reason, *, supersedes, source_ids, session)`
  — grant check, embed, insert memory + audit row in one transaction.
- `forget(actor, id, reason)` — sets `retracted`; nothing is ever physically deleted.
- `capture(actor, kind, origin, content, vault, reason)` — store a source.
- `extract(actor, source_id, model)` — propose memories from a source (see below).
- `grant / revoke / audit / export / import`.

### Store interface

About ten methods: `put_memory`, `get_memory`, `find_memories(filters) -> candidates with
embeddings`, `put_source`, `get_source`, `grants_for(client)`, `put_grant`, `append_audit`,
`query_audit(filters)`, `all_for_export()`. The SQLite implementation is the whole of v0,
and the interface exists so that stays true for as long as possible.

### `iam-mcp` — the agent surface

Python `mcp` package (2.x), stdio transport, one process per client. Identity comes from
the launch args, which is how each client's config already works:

```json
{ "mcpServers": { "iam": { "command": "iam-mcp", "args": ["--client", "claude-code"] } } }
```

Tools, all logged, all requiring `reason`:

| tool | does |
|---|---|
| `whoami()` | the vaults this client can read and write. Lets an agent behave without guessing. |
| `recall(query, types?, categories?, vault?, limit?, reason)` | search within granted vaults. Ungranted vaults are omitted, not redacted. |
| `remember(text, type, vault, categories?, reason, supersedes?)` | write a memory. |
| `log_episode(summary, project?, outcome?, reason)` | sugar for `remember(type="episode")` with the session attached. |
| `capture(content, kind, reason)` | hand over raw context for later extraction, without asserting anything. |
| `forget(id, reason)` | retract. |

The agent never sees the audit log or other clients' grants. That is the user's view.

### `iam` — the user surface

```
iam init                                   create ~/.iam/iam.db, default vaults
iam vault list|add
iam grant <client> <vault> [--mode rw] [--expires 30d] --reason "…"
iam revoke <client> <vault>
iam add --type fact --vault coding --cat python "…" --reason "…"
iam search "…" [--type] [--vault] [--cat]
iam list|show|forget <id>
iam capture claude-code [--project <slug>] [--since 7d]     transcripts + memory dir -> sources
iam capture file <path> --kind chatgpt-export --vault personal
iam extract <source-id> [--model …]        propose memories -> status=proposed
iam review                                 accept / reject / edit proposals
iam audit [--client] [--since] [--action] [--memory <id>] [--vault]
iam evict claude-code [--project <slug>]   push local memory to the vault, then clear it
iam export [--vault] > vault.jsonl         the portability promise, with provenance
iam import vault.jsonl
```

### Extraction

Memories *should* arrive through the MCP, written by the agent at the moment it learned
something, with its reason. But agents forget to, and existing history is already sitting
in transcripts. So:

1. `iam capture` reads raw context into `sources`. First parser: Claude Code transcripts
   (`~/.claude/projects/<slug>/*.jsonl`; only `type in {user, assistant}` rows carry
   conversation, the rest is tooling noise) and the per-project `memory/*.md` files.
2. `iam extract` runs a model over a source with a fixed prompt that returns candidate
   facts and episodes as JSON, each with the span it came from. Candidates are stored as
   memories with `status: proposed` and full provenance (`via: extract`, `model`,
   `source_ids`). Proposed memories are never returned by `recall`.
3. `iam review` is where the human accepts, edits or rejects. Acceptance is a `remember`
   with `actor: user`, so it is audited like any other write.

The model is called through an OpenAI-compatible client with a `base_url`, so the same
code runs against a local model (Ollama, LM Studio) or a hosted one. A vault marked
`local-only` refuses any `base_url` that is not loopback. Default to local.

### Claude Code as the first cached agent

A small plugin (skill + hooks), once the MCP works:

- **pull** (SessionStart): `recall` the project's and the user's standing facts into
  context, logged like any other recall.
- **push** (Stop): `log_episode` for the session, and `capture` the project memory dir.
- **evict** (`iam evict claude-code`): after a push, clear
  `~/.claude/projects/<slug>/memory/` so the only copy is the vault's.

Hosted agents (ChatGPT, Muse) keep memory server-side. For them the vault owns the
canonical copy and eviction is a reminder, not a command. Say so plainly in the README:
a reminder is still worth sending, and users who want it to be a command are the ones who
will push providers to expose real memory controls.

## Phases

Status as of 2026-10-15: phases 0–4 built; phase 5 not started.

Each phase ends runnable and tested. Nothing in a later phase is needed to use an earlier one.

**0. Scaffold** — `uv` project, package `iam`, `pyproject.toml`, ruff, pytest, this plan,
README with the one-paragraph pitch and the hosted-agent caveat.

**1. Core + CLI** — models (pydantic), SQLite store, grants, audit, local embeddings,
`init / vault / grant / add / search / list / show / forget / audit / export / import`.
Tests prove: no write without an audit row; recall never returns an ungranted vault;
`audit --memory` reconstructs a memory's history.

**2. MCP server** — `iam-mcp` with the six tools, wired into Claude Code and Claude
Desktop. Smoke test: a session recalls, remembers with a reason, and `iam audit` shows it.

**3. Capture, extract, review** — Claude Code transcript and memory-dir parsers,
extraction prompt, proposed status, `review`. First real population of the vault from
existing history.

**4. Claude Code plugin** — pull / push / evict. The cache story becomes real.

**5. A second device** — only when one machine stops being enough. Whatever store that
needs, sync comes from the store, not from Iam. Not planned; listed so the `Store` seam has
a reason to stay small.

## Decisions taken (push back now, not in phase 3)

- **Python 3.11+, `uv`.** Matches the notebooks repo and the `mcp` SDK; local embeddings
  via `sentence-transformers`, the same model family the notebooks use.
- **SQLite, one file, `~/.iam/iam.db`.** Brute-force cosine in numpy after SQL filtering.
  No second backend until a real need shows up.
- **Client identity by launch argument.** Simple and matches how every MCP host configures
  servers. A client that lies about its name gets that name's grants; the threat model is
  "agent over-reaches", not "hostile local process", and the audit log is the defence.
- **`reason` is mandatory** on MCP writes and recalls, optional on CLI reads, mandatory on
  CLI writes. Cheap to type, and it is what makes the audit log readable by a human.
- **Retract, never delete.** Memories and sources are soft-deleted; audit is append-only.
  `iam export` of a retracted memory is possible but off by default.
- **Ungranted vaults are invisible**, not "access denied". An agent should not learn that a
  `health` vault exists.
- **Vendor-neutral model client** (OpenAI-compatible + `base_url`) for extraction, local by
  default. The vault must not depend on any one provider.
- **Two memory types to start.** Categories carry the rest until a type earns its keep.

## Stretch: secrets agents can use but never see

Not in the MVP. It is a different threat model: the vault partitions facts between agents,
whereas a secret must stay confidential from the agent *using* it. The agent authors the
command, so once a value is substituted it can be echoed, encoded or sent anywhere; only
egress control or allowlisted command templates close that, and 1Password's `op run` /
`op inject` already cover the substitution half well. The honest position is that Iam
holds facts, a password manager holds secrets, and they compose.

Reserved so it drops in later without reshaping anything:

- a `secret` memory type: never embedded, never returned by `recall`, referenced by handle;
- a grant mode `use`, distinct from `read`: may substitute, may not view;
- `iam run --with <handle> -- <cmd>`: injects at exec and writes an audit row like any
  other action;
- a Claude Code tool-output redaction hook that strips known secret values before the
  model sees them. Cheap, catches the accidental `cat .env`, promises nothing against
  deliberate exfiltration. The one piece worth doing early if the itch is real.

## Open questions

- Does `recall` deserve a hybrid (BM25 + vector) path early? Personal memories are short
  and specific; exact names and project slugs matter. Probably yes in phase 1 if SQLite
  FTS5 makes it cheap; measure first.
- Episode granularity: one per session, or one per piece of work? Start with one per
  session and let `push` learn to split.
- Should `recall` results carry a short "why this matched" for the audit log? Nice for the
  human reading `audit`, not needed to ship.
