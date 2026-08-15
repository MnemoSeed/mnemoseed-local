"""Shared support for the driver-agnostic contract suite (prd-08 FR-8.5 / AC-3).

One parametrized stack backs every contract test, so the same behavioral
assertions run against the embedded driver family: lancedb_embedded +
sqlite_graph + sqlite_meta + the deterministic synthetic embedder (prd-08 D7 —
no network and no model, so the suite stays offline).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mnemoseed_local.schema.graph import Edge, GraphNode, NodeType, RelType
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.lancedb_embedded import _escape as _lance_escape
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import SparseVector

PROFILE = "alice"
DIMENSION = 64

_PREF_PROPS: dict[str, Any] = {
    "domain": "coding",
    "statement": "dark mode",
    "valence": 0.8,
    "prior_width": 0.3,
    "trait_anchor": "anima-1",
    "evidence_chain": [{"event": "created", "at": 123.0}],
}


@dataclass
class ContractStack:
    """One fully-wired driver family under contract test."""

    backend: str
    vector: Any
    graph: Any
    meta: Any
    embed: SyntheticEmbedder
    dimension: int = DIMENSION
    profile: str = PROFILE

    def text_vector(self, text: str) -> list[float]:
        return self.embed.embed(text).dense

    async def close(self) -> None:
        for store in (self.vector, self.graph, self.meta):
            closer = getattr(store, "close", None)
            if closer is not None:
                await closer()


def build_embedded(tmp_path: Path) -> ContractStack:
    """Embedded stack: lancedb chunks + sqlite graph + sqlite meta + synthetic."""
    return ContractStack(
        backend="embedded",
        vector=LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=DIMENSION),
        graph=SqliteGraphDriver(path=tmp_path / "cortex.db"),
        meta=SqliteMetaDriver(path=tmp_path / "meta.db"),
        embed=SyntheticEmbedder(dimension=DIMENSION),
    )


# ---------------------------------------------------------------- stamp makers


def make_stamp(
    chunk_id: str,
    text: str,
    *,
    profile_id: str = PROFILE,
    session: str | None = "s1",
    score: float = 0.0,
    decay: float = 1.0,
    entities: tuple[str, ...] = (),
    consolidated: bool = False,
    ingested_at: float = 1.0,
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile_id,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="contract-model",
        persona_id="p1",
        cues=Cues(
            project="contract-suite",
            tools_used=["pytest"],
            time_bucket="diurnal",
            entities=list(entities),
        ),
        provenance=Provenance(
            asserted_by="contract-model",
            session_id=session,
            source="manual",
            confidence=0.8,
            asserted_at=100.0,
        ),
        decay_weight=decay,
        score=score,
        consolidated=consolidated,
        ingested_at=ingested_at,
    )


def make_prov(**over: object) -> Provenance:
    base: dict[str, object] = dict(asserted_by="contract-agent", source="session://s-contract")
    base.update(over)
    return Provenance(**base)


def make_pref(**over: object) -> GraphNode:
    base: dict[str, object] = dict(
        profile_id=PROFILE,
        node_type=NodeType.PREFERENCE,
        entities=["ui"],
        props=dict(_PREF_PROPS),
        provenance=make_prov(),
        valid_from=time.time() - 100.0,
    )
    base.update(over)
    return GraphNode(**base)


def make_edge(
    src: str,
    dst: str,
    *,
    rel: RelType = RelType.EVIDENCED_BY,
    profile_id: str = PROFILE,
    weight: float = 1.0,
    created_at: float | None = None,
) -> Edge:
    """Edge helper (profile scopes traversal, not node membership)."""
    return Edge(
        src=src,
        dst=dst,
        rel=rel,
        profile_id=profile_id,
        weight=weight,
        created_at=time.time() if created_at is None else created_at,
    )


def make_intention(over: dict[str, Any] | None = None, **kw: object) -> GraphNode:
    props: dict[str, Any] = {"trigger_condition": "when", "action": "act", "status": "pending"}
    if over:
        props.update(over)
    return GraphNode(
        profile_id=PROFILE,
        node_type=NodeType.INTENTION,
        props=props,
        provenance=make_prov(),
        **kw,
    )


# ---------------------------------------------------------------- raw-layer helpers

# Turn bounds and usage counters are stored per-row but are deliberately not
# settable/readable through the public ports, so contract tests reach the raw
# row on the driver. Identical semantics are what is under test here, not the
# bytes of the backend.


def write_turn_chunk(
    stack: ContractStack,
    chunk_id: str,
    text: str,
    session: str,
    start: int,
    end: int,
    *,
    profile_id: str = PROFILE,
    dense: list[float] | None = None,
    sparse: SparseVector | None = None,
) -> None:
    """Insert a chunk whose session/turn bounds survive the write path."""
    stamp = make_stamp(chunk_id, text, profile_id=profile_id, session=session)
    if dense is None:
        dense = list(stack.text_vector(text))
    row = stack.vector._to_row(stamp, dense, sparse)
    row["session_id"] = session
    row["turn_start"] = int(start)
    row["turn_end"] = int(end)
    stack.vector._table.merge_insert("chunk_id").when_not_matched_insert_all().execute([row])


def raw_chunk(stack: ContractStack, chunk_id: str) -> dict[str, Any]:
    """Raw chunks row for usage counters / turn bounds ({} when absent)."""
    rows = stack.vector._table.search().where(f"chunk_id = {_lance_escape(chunk_id)}").limit(1).to_list()
    return rows[0] if rows else {}


def raw_meta_row(stack: ContractStack, table: str, where_column: str, value: Any) -> dict[str, Any]:
    """Raw meta row for columns the ports do not expose (token revocation)."""
    row = stack.meta._conn.execute(f"SELECT * FROM {table} WHERE {where_column} = ?", (value,)).fetchone()
    return dict(row) if row is not None else {}


def run(coro: Any) -> Any:
    """Run an async closer in a plain-test context."""
    return asyncio.run(coro)
