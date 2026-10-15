---
name: iam
description: The user's memory vault. Use it whenever you learn something durable about the user (a preference, a convention, a decision, who they work with), when you finish a piece of work worth remembering, when you want to know what the user has told other agents, or when the user says "remember this", "what do you know about me", "forget that", or "clear your memory". Your own auto-memory is a cache of the vault, not the source.
---

# Iam: the user's memory vault

The vault is canonical. Your auto-memory directory is a cache of it. Everything you read
from or write to the vault is audited under the name `claude-code`, with the reason you
give, and the user reads those reasons. Write them for a person.

## Tools (MCP server `iam`)

- `whoami()` — which vaults you may read and write, and which project you are in (the
  basename of the working directory). Call it before your first write.
- `recall(query, reason, types?, categories?, vault?, limit?)` — semantic search over this
  project's memories and the global ones; you never see another project's. The
  session-start context already holds the standing facts; recall when you need something
  specific or older.
- `remember(text, vault, reason, type="fact", categories?, supersedes?, everywhere?)` —
  write a durable fact. One sentence that stands on its own. It applies to this project
  unless you set `everywhere: true`; do that for facts about the user in general (a
  preference, a tool, a person), not about this codebase. If it replaces an older fact,
  pass `supersedes` with that fact's id rather than writing a near-duplicate.
- `log_episode(summary, vault, reason, outcome?)` — when a piece of work ends: what you
  did and how it ended. It is filed under this project. One per piece of work, not one per
  turn.
- `capture(content, vault, reason, kind?)` — raw context you are not sure about. The user
  may extract memories from it later. Prefer this over guessing.
- `forget(id, reason)` — retract a memory you may write.

## When to write

- The user states a preference or corrects you: `remember` it, `type: fact`, in the vault
  `whoami` says you may write (usually `coding` or `work`).
- You finish something: `log_episode`. Leave `project` unset; the server knows it.
- Never write secrets, credentials, or anything the user marked private. If a vault is not
  in `whoami`, it does not exist for you; do not ask for it.

## Auto-memory is a cache

Keep using your auto-memory directory as before. When the user asks to clear it, evict it,
or "move my memory to the vault", tell them to run:

```bash
iam claude-code evict --project "$PWD" --vault coding --reason "cache flush"
```

It imports every memory file as a *proposed* memory for them to review, then deletes the
local copies. Do not delete the directory yourself; the import is what makes deletion safe.
