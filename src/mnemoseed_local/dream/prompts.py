"""Reflection de-biasing prompt template (PRD-02 T3; FR-2.2, design/02 §5).

The template renders the reflect model's system instruction: extract neutral
entity triples from the snapshot chunks, strip emotional/tone/persona artifacts,
never store speaking style, judge per-triple source tier, enforce the Tier-3
anti-backflow rule, and restrict preference-type extraction to user-originated
chunks (FR-2.12). Rendering is deterministic text parameterized by snapshot
content only: identical snapshots produce identical prompts.

The chunk block grammar is also the contract the (deterministic, offline)
StubReflectLLM parses, so the prompt and the harness stub can never drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk

PROMPT_VERSION = "v1"


def origin_of(chunk: SnapshotChunk) -> str:
    """Which side produced the chunk's text: ``"user"`` (verbatim user input)
    or ``"agent"`` (model/persona-rendered output). FR-2.12's evidence boundary
    keys off this label. The stamp carries no explicit source-role field, so
    the label derives from ``persona_id`` being set or ``asserted_by`` being a
    model id."""
    stamp = chunk.to_stamp()
    if stamp.persona_id is not None or stamp.provenance.asserted_by != "user":
        return "agent"
    return "user"


@dataclass(frozen=True)
class ReflectPrompt:
    """The rendered, versioned prompt handed to the ReflectLLM seam."""

    version: str
    system: str
    user: str


@dataclass(frozen=True)
class ChunkBlock:
    """One parsed chunk block from the user prompt (the stub's input grammar)."""

    chunk_id: str
    tier: int
    origin: str
    text: str


_SYSTEM_TEMPLATE = """\
You are the reflection pass of a cross-model neutral memory engine.

Extract factual entity triples (subject | predicate | object) from the
conversation chunks below. The chunks carry metadata: chunk_id, cognitive
tier, origin (user or agent), and turn range.

De-biasing contract (mandatory):
1. Strip personal color before extracting: remove emotional or flavor words,
   tone markers, interjections, and emoji from every triple component.
2. Speak-style is never a fact: never emit a triple whose predicate or object
   describes HOW someone speaks, their mannerisms, honorifics, or tone.
   Agent-rendered mannerisms are NOT user facts.
3. Judgment is per-triple: record the source cognitive tier(s) from the
   chunks that evidence the triple.
4. Anti-backflow: a triple evidenced by tier-3 chunks must never route to the
   main graph. Mark it "isolated" (low-value noise) or "salvage" (potentially
   valuable), never "core".
5. Preference-type triples (predicates like prefer/like/want/love/value) may
   only be extracted from origin=user chunks. Never adopt agent output as
   preference evidence.

Output ONLY a JSON array of objects, one per triple, with these exact fields:
subject, predicate, object, tiers, chunk_ids, confidence, route, preference.
route is one of: core | isolated | salvage.
Do not output any other text, explanation, or markdown.
"""

_USER_HEADER = "Reflect over these chunks (deterministic order, oldest turn first):\n\n"


def build_reflect_prompt(snapshot: Snapshot) -> ReflectPrompt:
    """Render the de-biasing prompt from a snapshot. Deterministic: chunk order
    is fixed by (turn_start, turn_end, chunk_id), never by capture order."""
    return ReflectPrompt(
        version=PROMPT_VERSION,
        system=_SYSTEM_TEMPLATE,
        user=_USER_HEADER + _render_chunk_blocks(snapshot),
    )


def _render_chunk_blocks(snapshot: Snapshot) -> str:
    return render_chunk_blocks(snapshot.chunks)


def ordered_chunks(chunks: Iterable[SnapshotChunk]) -> list[SnapshotChunk]:
    """Deterministic chunk order shared by the full render and delta packing:
    (turn_start, turn_end, chunk_id), never capture order."""
    return sorted(chunks, key=_chunk_sort_key)


def render_chunk_block(chunk: SnapshotChunk) -> str:
    """Render the chunk-block grammar for one chunk. The delta packer needs the
    per-chunk text to count its tokens before deciding whether it fits."""
    return _render_block(chunk)


def render_chunk_blocks(chunks: Iterable[SnapshotChunk]) -> str:
    """Render the deterministic chunk-block grammar for a subset of chunks."""
    return "".join(_render_block(c) for c in ordered_chunks(chunks))


def build_cache_prefix(graph_digest: str = "") -> str:
    """The byte-stable prompt-cache prefix: system template plus the user header.

    ``graph_digest`` (a profile-stable string supplied by the delta packer's
    optional digest provider) renders as a labeled section; an empty digest
    renders no section, so the prefix is a fixed constant across dreams of a
    profile. Per-dream data (timestamps, snapshot ids, chunk ids) must never
    appear here — it lives in the delta segment.
    """
    prefix = _SYSTEM_TEMPLATE + "\n\n" + _USER_HEADER
    if graph_digest:
        prefix += f"\nKnown graph digest:\n{graph_digest}\n"
    return prefix


def _chunk_sort_key(chunk: SnapshotChunk) -> tuple[int, int, str]:
    return (
        chunk.turn_start if chunk.turn_start is not None else -1,
        chunk.turn_end if chunk.turn_end is not None else -1,
        chunk.chunk_id,
    )


def _render_block(chunk: SnapshotChunk) -> str:
    stamp = chunk.to_stamp()
    origin = origin_of(chunk)
    if stamp.turn_start is not None:
        turn = f"{stamp.turn_start}-{stamp.turn_end}"
    else:
        turn = "?"
    return (
        "<chunk>\n"
        f"chunk_id: {chunk.chunk_id}\n"
        f"tier: {int(stamp.cognitive_tier)}\n"
        f"origin: {origin}\n"
        f"turn: {turn}\n"
        "text:\n"
        f"{stamp.text}\n"
        "</chunk>\n"
    )


_BLOCK_RE = re.compile(
    r"<chunk>\n"
    r"chunk_id: (?P<id>\S+)\n"
    r"tier: (?P<tier>\d+)\n"
    r"origin: (?P<origin>[a-z]+)\n"
    r"turn: (?P<turn>[\d-]+)\n"
    r"text:\n"
    r"(?P<text>.*?)</chunk>",
    re.DOTALL,
)


def parse_chunk_blocks(user_text: str) -> list[ChunkBlock]:
    """Parse the deterministic chunk block grammar back out of user text."""
    return [
        ChunkBlock(chunk_id=m["id"], tier=int(m["tier"]), origin=m["origin"], text=m["text"])
        for m in _BLOCK_RE.finditer(user_text)
    ]
