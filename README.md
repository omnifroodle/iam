# Iam

A personal memory vault for AI agents. **The vault is canonical; every agent is a cache.**

Claude, Claude Code, Codex, Muse and the rest each keep their own memory of you. Iam holds
the memories in a store you own, partitioned into vaults, and hands each agent only what it
has been granted through an MCP server. Every recall and every write is on the record: who
asked, for what, what they were given, what they added, and why.

Think 1Password, but for facts about you instead of passwords.

## Status

Early. Phases 0–2 of [the plan](docs/plan.md) are what exists: a SQLite store, local
embeddings, grants, an append-only audit log, a CLI, and an MCP server. Capture and
extraction from agent transcripts, and the Claude Code plugin that makes the cache story
real, come next.

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

## What it cannot do

Hosted agents (ChatGPT, Muse) keep their memory server-side. Iam can own the canonical
copy and remind you to clear theirs, but it cannot clear it for you. If you want that to be
a command rather than a reminder, the people to ask are the providers.

## License

MIT.
