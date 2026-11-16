---
name: mnemoseed
description: Use when the session touches past work, past decisions, or user preferences - when the user references content from an earlier session, when what happened before would change what to do now, or when a decision on an existing topic is about to be made again. Query, pin, and resume the local mnemoseed memory instead of guessing or re-asking.
---

# MnemoSeed memory

MnemoSeed Local is the user's local memory layer for coding agents. It captures
sessions automatically and serves them back through the `mnemoseed` MCP tools
below. The capture hook already injects relevant memory into context on its
own — these tools are for deliberately querying, pinning, and replacing memory.

## Verbs

Verbs: `recall`, `remember`, `capture`, `supersede`, `dream_once`, `recent_sessions`, `session_windows`

- `recall` — search memory for past decisions, preferences, and prior work on a topic. Required `query`; optional `top_k` caps the result count.
- `remember` — store one fact verbatim as a pin. Call it when the user explicitly asks to keep or pin that fact: this verb is the pin path, and a statement the user merely makes is a `capture` instead.
- `capture` — record a durable decision, preference, or constraint the user stated, quoting their own wording verbatim. Captures are not pins: use it in a surface that has no capture hook, and never record your own guesses, drafts, or reasoning.
- `supersede` — record that one belief replaces another (`superseded_node_id`, `successor_node_id`). A deliberate act only; never fire it automatically.
- `dream_once` — launch exactly one dream consolidation cycle and return its launched/state payload.
- `recent_sessions` — fetch recent sessions' verbatim tails to re-anchor on where the previous conversation ended.
- `session_windows` — fetch per-session activity time windows (first/latest ISO-8601 UTC, chunk counts, active flags); use them to attribute an unfamiliar workspace artifact (a file mtime) to the session that produced it.

### recent_sessions and resume_query

`resume_query` is required: pass the user's current prompt raw (max 500
chars). The daemon extracts anchor identifiers from it and returns only
sessions whose user content matches, so a scoped resume lands on the right
conversation. As a root caller pass `self_parent_id=None` to filter child
sessions out.

CAUTION: the returned groups may be other conversations. `active=true` means
that conversation is still in progress — do not adopt or continue its work
unless the user explicitly asks. For dormant sessions, resume only what the
user asks this session to continue.

## When to recall proactively

- At the start of a session whose context is unfamiliar, recall once on the topic before acting.
- When the user references a past decision, prior work, or a stated preference.
- Before re-deciding an existing topic, recall what was already decided.

Never call recall unconditionally every turn. One recall per topic is enough.

When a memory block is already in context, that block is the memory channel for
the session: use it rather than recalling the same topic again.

## When to capture

A surface without the capture hook (a chat window, for example) writes nothing
by itself. There, a durable decision, preference, or constraint the user states
is worth a `capture` call in the user's own words. One capture per statement is
enough, and an assistant's own speculation never belongs in memory.

## Empty results

An empty result means there is no relevant memory, nothing more. Say so
plainly and continue with the task.

## Recognizing pinned memories

A pin is a fact the user explicitly asked to keep; everything else was
captured automatically. The two surfaces below are read in their own places.

### In MCP tool results

Each returned entry carries `provenance_source` and `explicit_pin`. The entry
is a pin exactly when `provenance_source == "memory.remember"` (equivalently
`explicit_pin` is `true`). Captured entries carry a non-pin source label (for
example `claude_code-chat`); the captured signal is `explicit_pin` being `false`.

### In injected context

In the automatic memory injection rendered by host hooks, a pinned line ends
with the suffix ` ⟵ pinned`. That suffix is decoration only, never part of
the stored text. Captured lines carry no marker — absence is the captured
signal.
