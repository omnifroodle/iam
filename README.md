# Iam

A personal memory vault for AI agents. **The vault is canonical; every agent is a cache.**

Claude, Claude Code, Codex, Muse and the rest each keep their own memory of you. Iam holds
the memories in a store you own, partitioned into vaults, and hands each agent only what it
has been granted through an MCP server. Every recall and every write is on the record: who
asked, for what, what they were given, what they added, and why.

Think 1Password, but for facts about you instead of passwords.

## Status

Early but usable. Phases 0–4 and 4b of [the plan](docs/plan.md) exist: a SQLite store, local
embeddings, vaults and grants, an append-only audit log, a CLI, an MCP server, capture of
Claude Code transcripts and memory files, extraction with a local model, review, eviction,
a Claude Code plugin that pulls the vault in at session start, and per-project scope. It runs on one
machine, on SQLite, on purpose.

## Install

```bash
uv tool install git+https://github.com/omnifroodle/iam
iam init
```

## Use

```bash
iam add --type fact --vault coding --cat python \
    "Prefers OpenAI-compatible clients with base_url over vendor SDKs." \
    --reason "Said so during the notebooks work."
iam search "what LLM client does the user like"
iam grant claude-code coding --mode read-write --reason "daily driver"
iam audit --client claude-code
```

Wire the MCP server into an agent; the client name is how grants are looked up:

```json
{ "mcpServers": { "iam": { "command": "iam-mcp", "args": ["--client", "claude-code"] } } }
```

The agent gets `whoami`, `recall`, `remember`, `log_episode`, `capture` and `forget`.
Every tool call requires a `reason`, and that reason is what you read in `iam audit`.

## Fill it from Claude Code

```bash
iam claude-code projects                                   # what Claude Code has on disk
iam claude-code import  --project ~/code/x --vault coding --reason "first import"
iam review                                                 # accept, edit or reject each one
iam claude-code capture --project ~/code/x --vault work --since 30d --reason "history"
iam extract <source-id> --model llama3.1                   # Ollama by default; proposes, never asserts
iam claude-code evict   --project ~/code/x --vault coding --reason "cache flush"
```

Point extraction at any OpenAI-compatible host once; the key stays in your environment:

```bash
iam llm --base-url http://localhost:11434/v1 --model llama3.1   # or a hosted /v1 endpoint
export IAM_LLM_API_KEY=…                                        # only if the host needs one
```

`import` turns each auto-memory file into a *proposed* memory with its origin session
attached, no model involved. `evict` does the same and then deletes the local files, so the
vault holds the only copy. `extract` runs an OpenAI-compatible model over a captured
transcript; a vault marked `local-only` refuses any endpoint that is not on this machine.

## Review page

```bash
iam serve --open        # http://127.0.0.1:8377/
```

One page: proposals waiting for review at the top, updating live as agents add them, with
accept, accept-everywhere, edit and reject; below, the vault's history drawn like
`git log --graph`, each audit row a commit with its reason as the message, global memories
on main and one branch per project. It binds 127.0.0.1 only, and every action on it is in
`iam audit` like any other.

## Claude Code plugin

The `plugin/` directory is a Claude Code plugin. It bundles the MCP server under the client
name `claude-code`, a skill that tells Claude when to read and write the vault, and a
session-start hook that runs `iam pull`, so the vaults you have granted land in context
and the audit log records exactly which memories were handed over.

```bash
claude plugin marketplace add omnifroodle/iam     # then: /plugin install iam
iam grant claude-code coding --mode read-write --reason "daily driver"
claude --plugin-dir ./plugin                      # or load it from a checkout
```

The plugin finds `iam` and `iam-mcp` in `~/.local/bin` (where `uv tool install` puts
them) even when that directory is not on the PATH Claude Code was launched with. Memories
are scoped to the project Claude Code runs in, the basename of its working directory: a
session sees that project's memories and the global ones, never another project's.

## What it cannot do

Hosted agents (ChatGPT, Muse) keep their memory server-side. Iam can own the canonical
copy and remind you to clear theirs, but it cannot clear it for you. If you want that to be
a command rather than a reminder, the people to ask are the providers.

## License

MIT.
