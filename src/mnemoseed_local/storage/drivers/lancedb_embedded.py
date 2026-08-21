"""Embedded vector store over a local LanceDB directory (embedded default, prd-08 FR-8.3).

Implements the full VectorStore surface (prd-08 appendix B.1) on a lazy
pyarrow/LanceDB table named "chunks". The schema carries every appendix A.1
field: verbatim text, dense and structured sparse vectors, session/turn bounds,
the cues/provenance/score structs, and the usage counters.

Search is hybrid: a dense ANN prefilter (cosine) followed by a sparse
dot-product re-rank so the stored sparse vectors actually participate in
ranking. Metadata filters (profile_id, decay floor, ingestion window, session,
turn window, entities, consolidation flag) are pushed into the LanceDB SQL
WHERE clause. Reads of a past committed version back a snapshot_read. Writes
merge with a chunk_id primary key (upsert), consolidations and weight updates
are batched column updates, and purge_range removes an overlapping turn window
for one session in one delete.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections.abc import Sequence
from typing import Any

import pyarrow as pa
from lancedb import connect
from lancedb.query import ColumnOrdering

from mnemoseed_local.config import CONFIG_DIR
from mnemoseed_local.schema.stamp import (
    ChunkStamp,
    CognitiveTier,
    Cues,
    EmotionCue,
    Provenance,
    ProvenanceEvent,
)
from mnemoseed_local.storage.ports import (
    Capability,
    ChunkFilter,
    DriverInfo,
    Page,
    PageResult,
    SearchHit,
    SparseVector,
    WeightUpdate,
)
from mnemoseed_local.storage.registry import VECTOR_DRIVERS, register

_CAPABILITIES = frozenset(
    {
        Capability.VECTOR_HYBRID_SEARCH,
        Capability.VECTOR_METADATA_FILTER,
        Capability.VECTOR_SNAPSHOT,
    }
)

_DEFAULT_TABLE = "chunks"
_DEFAULT_URI = CONFIG_DIR / "chunks.lance"
_DENSE_FUSION_WEIGHT = 0.5

_NEAR_DUP_PREFILTER_K = 50
_NEAR_DUP_WIDEN_MARGIN = 0.05
_NEAR_DUP_WIDEN_FACTOR = 4
_NEAR_DUP_WIDEN_STEP = 50

_widening_count = 0


def near_duplicate_widenings() -> int:
    """Total near-duplicate prefilter widenings since import; approximate
    under concurrent probes (unlocked counter, diagnostics only)."""
    return _widening_count


def _escape(value: object) -> str:
    """SQL string/number literal for the LanceDB WHERE clause."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


@register(VECTOR_DRIVERS)
class LanceDbEmbeddedStore:
    """Vector store over a local LanceDB directory."""

    info = DriverInfo(
        name="lancedb_embedded",
        capabilities=_CAPABILITIES,
        description="local LanceDB chunks table, hybrid dense+sparse search, snapshots",
    )

    def __init__(
        self,
        uri: str | os.PathLike[str] | None = None,
        table_name: str = _DEFAULT_TABLE,
        dimensions: int = 1024,
        **kwargs: Any,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.params: dict[str, Any] = kwargs
        self.dimensions = dimensions
        self.table_name = table_name
        self._uri = str(os.path.expanduser(str(uri))) if uri is not None else str(_DEFAULT_URI)
        self._db = connect(self._uri)
        # One store-level lock serializes every mutation. LanceDB serializes
        # writers internally but concurrent commits still collide on the
        # latest_version_hint.json file (Windows: os error 5) and drain lance's
        # single background loop; a single write lock turns that collision storm
        # into an ordered queue. Reads are snapshot-based and safe lock-free.
        self._write_lock = threading.Lock()
        self._ensure_table()

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    # ------------------------------------------------------------- writes

    def upsert_chunk(
        self,
        chunk: ChunkStamp,
        dense: Sequence[float],
        sparse: SparseVector | None = None,
    ) -> None:
        with self._write_lock:
            row = self._to_row(chunk, dense, sparse)
            existing = self._find_row(chunk.chunk_id)
            if existing is not None:
                # B2.7 rules merge: a re-upsert of the same chunk_id carries the
                # row's usage counters forward and merges the rules_json (union,
                # ttl_turns takes the larger) instead of overwriting the row.
                row = self._merge_upsert_row(existing, row)
            self._table.merge_insert(
                "chunk_id"
            ).when_matched_update_all().when_not_matched_insert_all().execute([row])

    def _find_row(self, chunk_id: str) -> dict[str, Any] | None:
        rows = self._table.search().where(f"chunk_id = {_escape(chunk_id)}").limit(1).to_list()
        return rows[0] if rows else None

    def _merge_upsert_row(self, existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        """Fold a matched upsert onto the existing row: the incoming values win
        for the fields it models, while the usage counters and the reconcile
        flag (absent from ChunkStamp) are carried forward, and the rules merge
        by identity."""
        merged = dict(incoming)
        for key in ("hit_count", "reinforce_count", "last_hit_at", "needs_reconcile"):
            if existing.get(key) is not None:
                merged[key] = existing[key]
        merged["rules_json"] = self._merge_rules_json(existing.get("rules_json"), incoming.get("rules_json"))
        return merged

    @staticmethod
    def _merge_rules_json(old_json: object, new_json: object) -> object:
        """Union two rules_json payloads by rule identity; same-identity rules
        keep the larger ttl_turns. Either side empty falls through to the other."""
        old: list[dict[str, Any]] = json.loads(old_json) if isinstance(old_json, str) else []
        new: list[dict[str, Any]] = json.loads(new_json) if isinstance(new_json, str) else []
        if not old:
            return new_json
        if not new:
            return old_json
        merged: list[dict[str, Any]] = []
        index: dict[tuple[str, str, str, str | None], int] = {}
        for rule in [*old, *new]:
            key = (
                str(rule.get("kind", "")),
                json.dumps(rule.get("value"), sort_keys=True, ensure_ascii=False),
                str(rule.get("scope", "session")),
                rule.get("session_id"),
            )
            if key in index:
                merged[index[key]]["ttl_turns"] = max(
                    merged[index[key]].get("ttl_turns", 0), rule.get("ttl_turns", 0)
                )
            else:
                index[key] = len(merged)
                merged.append(dict(rule))
        return json.dumps(merged, separators=(",", ":"), ensure_ascii=False)

    def upsert_chunks(
        self,
        entries: Sequence[tuple[ChunkStamp, Sequence[float], SparseVector | None]],
    ) -> None:
        """Bulk upsert of many chunks in ONE merge commit (B6 drain batch write).

        The capture drain collects a whole session's new chunks and flushes them
        together — a single lock acquisition and LanceDB commit instead of one
        per turn. Empty entry list is a no-op.
        """
        rows = [self._to_row(chunk, dense, sparse) for chunk, dense, sparse in entries]
        if not rows:
            return
        with self._write_lock:
            self._table.merge_insert(
                "chunk_id"
            ).when_matched_update_all().when_not_matched_insert_all().execute(rows)

    def delete_chunk(self, chunk_id: str) -> None:
        with self._write_lock:
            self._table.delete(f"chunk_id = {_escape(chunk_id)}")

    def mark_consolidated(self, chunk_ids: Sequence[str]) -> None:
        ids = list(chunk_ids)
        if not ids:
            return
        clause = ", ".join(_escape(cid) for cid in ids)
        with self._write_lock:
            self._table.update(where=f"chunk_id IN ({clause})", values={"consolidated": True})

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        where = (
            f"session_id = {_escape(session_id)} "
            "AND turn_start IS NOT NULL AND turn_end IS NOT NULL "
            f"AND turn_start <= {_escape(turn_end)} AND turn_end >= {_escape(turn_start)}"
        )
        with self._write_lock:
            count = int(self._table.count_rows(filter=where))
            self._table.delete(where)
        return count

    def update_weights(self, updates: Sequence[WeightUpdate]) -> None:
        for update in updates:
            values: dict[str, Any] = {}
            if update.decay_weight is not None:
                values["decay_weight"] = float(update.decay_weight)
            if update.last_reinforced is not None:
                values["last_reinforced"] = float(update.last_reinforced)
            if update.reinforce_count is not None:
                values["reinforce_count"] = int(update.reinforce_count)
            if values:
                with self._write_lock:
                    self._table.update(where=f"chunk_id = {_escape(update.chunk_id)}", values=values)

    def update_chunk_state(
        self,
        chunk_ids: Sequence[str],
        hit_increment: int | None = None,
        needs_reconcile: bool | None = None,
    ) -> None:
        """Port update_chunk_state; one batched SQL-expression update. The
        counter increment references the row; static pieces (last_hit_at,
        needs_reconcile) go in as SQL literals because LanceDB rejects passing
        `values` and `values_sql` in the same call."""
        ids = list(chunk_ids)
        if not ids:
            return
        if hit_increment is None and needs_reconcile is None:
            return
        clause = ", ".join(_escape(cid) for cid in ids)
        where = f"chunk_id IN ({clause})"
        updates_sql: dict[str, str] = {}
        if hit_increment is not None and hit_increment != 0:
            updates_sql["hit_count"] = f"hit_count + {int(hit_increment)}"
            if hit_increment > 0:
                updates_sql["last_hit_at"] = repr(time.time())
        if needs_reconcile is not None:
            updates_sql["needs_reconcile"] = "true" if needs_reconcile else "false"
        if updates_sql:
            with self._write_lock:
                self._table.update(where=where, values_sql=updates_sql)

    # ------------------------------------------------------------- reads

    def get_chunk(self, chunk_id: str) -> ChunkStamp | None:
        rows = self._table.search().where(f"chunk_id = {_escape(chunk_id)}").limit(1).to_list()
        return self._to_stamp(rows[0]) if rows else None

    def search(
        self,
        dense: Sequence[float],
        sparse: SparseVector | None,
        filter: ChunkFilter,
        top_k: int,
    ) -> list[SearchHit]:
        where = self._filter_sql(filter)
        candidate_k = max(top_k * 4, top_k + 50)
        rows = (
            self._table.search(list(dense), vector_column_name="vector_dense")
            .where(where)
            .metric("cosine")
            .limit(candidate_k)
            .to_list()
        )
        hits: list[SearchHit] = []
        for row in rows:
            dense_sim = 1.0 - float(row["_distance"])
            sparse_sim = self._sparse_similarity(sparse, row) if sparse is not None else 0.0
            score = (
                _DENSE_FUSION_WEIGHT * dense_sim + (1.0 - _DENSE_FUSION_WEIGHT) * sparse_sim
                if sparse is not None
                else dense_sim
            )
            hits.append(SearchHit(chunk=self._to_stamp(row), similarity=float(score)))
        hits.sort(key=lambda hit: hit.similarity, reverse=True)
        return hits[:top_k]

    def near_duplicate(
        self, vector: Sequence[float], threshold: float, profile_id: str | None = None
    ) -> list[ChunkStamp]:
        """Capped top-K scan + exact cosine re-score probe; returns the stamps.
        Thin wrapper over ``near_duplicate_ranked`` (the shared probe)."""
        return [chunk for chunk, _ in self.near_duplicate_ranked(vector, threshold, profile_id)]

    def near_duplicate_ranked(
        self, vector: Sequence[float], threshold: float, profile_id: str | None = None
    ) -> list[tuple[ChunkStamp, float]]:
        """Capped top-K scan + exact cosine re-score probe, returning
        ``(chunk, similarity)`` pairs sorted by similarity desc then chunk_id
        asc. profile_id scopes the probe to one profile (D5 isolation); when
        omitted the whole table is probed. The top-K cap widens while its K-th
        candidate's dense similarity stays within MARGIN of the threshold, so a
        true duplicate ranked just beyond the initial K is still caught.
        Residual envelope: a duplicate whose top-K rank lands beyond the widened
        K, or whose dense similarity sits below the threshold, is missed — the
        sparse signature never rescues the probe. Each match is reconstructed
        from the probe rows themselves — never re-read through ``get_chunk``
        (B6 round-trip elimination: no per-match table re-read on the drain
        hot path).
        """
        global _widening_count
        query = [float(value) for value in vector]
        query_norm = math.sqrt(sum(value * value for value in query)) or 1.0
        where = self._filter_sql_all(ChunkFilter(profile_id=profile_id or "*"))
        k = _NEAR_DUP_PREFILTER_K
        while True:
            rows = (
                self._table.search(query, vector_column_name="vector_dense")
                .where(where)
                .metric("cosine")
                .limit(k)
                .to_list()
            )
            if len(rows) < k:
                break
            kth_sim = 1.0 - float(rows[-1]["_distance"])
            if kth_sim < threshold - _NEAR_DUP_WIDEN_MARGIN:
                break
            _widening_count += 1
            k = max(k * _NEAR_DUP_WIDEN_FACTOR, k + _NEAR_DUP_WIDEN_STEP)
        matches: list[tuple[float, str]] = []
        for row in rows:
            similarity = self._dense_similarity(query, query_norm, row)
            if similarity >= threshold:
                matches.append((similarity, str(row["chunk_id"])))
        if not matches:
            return []
        matches.sort(key=lambda entry: (-entry[0], entry[1]))
        rows_by_id = {str(row["chunk_id"]): row for row in rows}
        return [(self._to_stamp(rows_by_id[chunk_id]), similarity) for similarity, chunk_id in matches]

    def snapshot_read(self, filter: ChunkFilter) -> list[ChunkStamp]:
        version = self._table.version
        snapshot = self._db.open_table(self.table_name, version=version)
        where = self._filter_sql(filter)
        total = snapshot.count_rows(filter=where)
        rows: list[dict[str, Any]] = []
        step = 500
        for offset in range(0, total, step):
            batch = snapshot.search().where(where).limit(min(step, total - offset)).offset(offset).to_list()
            rows.extend(batch)
        return [self._to_stamp(row) for row in rows]

    def list_chunks(self, filter: ChunkFilter, page: Page) -> PageResult[ChunkStamp]:
        where = self._filter_sql(filter)
        total = self._table.count_rows(filter=where)
        rows = (
            self._table.search()
            .where(where)
            .order_by([ColumnOrdering(column_name="ingested_at", ascending=False)])
            .limit(page.limit)
            .offset(page.offset)
            .to_list()
        )
        return PageResult(
            items=[self._to_stamp(row) for row in rows],
            total=total,
            offset=page.offset,
            limit=page.limit,
        )

    async def close(self) -> None:
        """Release the connection (no-op; LanceDB owns no client handle)."""

    # ------------------------------------------------------------ schema

    def _schema(self) -> pa.Schema:
        sparse = pa.struct(
            [
                pa.field("indices", pa.list_(pa.int64())),
                pa.field("values", pa.list_(pa.float32())),
            ]
        )
        cues = pa.struct(
            [
                pa.field("project", pa.string()),
                pa.field("host", pa.string()),
                pa.field("task", pa.string()),
                pa.field("tools_used", pa.list_(pa.string())),
                pa.field("time_bucket", pa.string()),
                pa.field("emotion_valence", pa.float32()),
                pa.field("entities", pa.list_(pa.string())),
            ]
        )
        event = pa.struct(
            [
                pa.field("at", pa.float64()),
                pa.field("action", pa.string()),
                pa.field("actor", pa.string()),
                pa.field("detail", pa.string()),
            ]
        )
        provenance = pa.struct(
            [
                pa.field("asserted_by", pa.string()),
                pa.field("agent_id", pa.string()),
                pa.field("source", pa.string()),
                pa.field("source_ref", pa.string()),
                pa.field("confidence", pa.float32()),
                pa.field("asserted_at", pa.float64()),
                pa.field("history", pa.list_(event)),
            ]
        )
        score = pa.struct(
            [
                pa.field("emotion", pa.float32()),
                pa.field("novelty", pa.float32()),
                pa.field("causal", pa.float32()),
                pa.field("total", pa.float32()),
            ]
        )
        return pa.schema(
            [
                pa.field("chunk_id", pa.string()),
                pa.field("profile_id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector_dense", pa.list_(pa.float32(), self.dimensions)),
                pa.field("vector_sparse", sparse),
                pa.field("session_id", pa.string()),
                pa.field("turn_start", pa.int64()),
                pa.field("turn_end", pa.int64()),
                pa.field("anima_id", pa.string()),
                pa.field("cognitive_tier", pa.int64()),
                pa.field("model_id", pa.string()),
                pa.field("persona_id", pa.string()),
                pa.field("cues", cues),
                pa.field("provenance", provenance),
                pa.field("score", score),
                pa.field("decay_weight", pa.float32()),
                pa.field("last_reinforced", pa.float64()),
                pa.field("consolidated", pa.bool_()),
                pa.field("ingested_at", pa.float64()),
                pa.field("peripheral_gaps", pa.bool_()),
                pa.field("needs_reconcile", pa.bool_()),
                pa.field("hit_count", pa.int64()),
                pa.field("last_hit_at", pa.float64()),
                pa.field("reinforce_count", pa.int64()),
                # denormalized filter index (internal, prd-08 A.4)
                pa.field("entities_filter", pa.string()),
                # B2.7 Scheme 2-lite: verbatim standing-constraint JSON
                pa.field("rules_json", pa.string()),
            ]
        )

    def _existing_tables(self) -> list[str]:
        """Table names in this database.

        lancedb < 0.3x returned a plain list from list_tables(); newer
        versions return a response object with a ``tables`` attribute. Normalise
        both so the exists-check drives open-vs-create correctly.
        """
        listing = self._db.list_tables()
        if isinstance(listing, (list, tuple)):
            return [str(name) for name in listing]
        tables = getattr(listing, "tables", None)
        return [str(name) for name in tables] if tables is not None else []

    def _ensure_table(self) -> None:
        if self.table_name in self._existing_tables():
            self._table = self._db.open_table(self.table_name)
        else:
            self._table = self._db.create_table(self.table_name, schema=self._schema())

    # ------------------------------------------------------------- mapping

    def _to_row(
        self,
        chunk: ChunkStamp,
        dense: Sequence[float],
        sparse: SparseVector | None,
    ) -> dict[str, Any]:
        cues = chunk.cues
        emotion = cues.emotion
        provenance = chunk.provenance
        return {
            "chunk_id": chunk.chunk_id,
            "profile_id": chunk.profile_id,
            "text": chunk.text,
            "vector_dense": [float(value) for value in dense],
            "vector_sparse": (
                {"indices": [int(i) for i in sparse.indices], "values": [float(v) for v in sparse.values]}
                if sparse is not None
                else {"indices": [], "values": []}
            ),
            "session_id": provenance.session_id,
            "turn_start": chunk.turn_start,
            "turn_end": chunk.turn_end,
            "anima_id": chunk.persona_id,
            "cognitive_tier": int(chunk.cognitive_tier),
            "model_id": chunk.model_id,
            "persona_id": chunk.persona_id,
            "cues": {
                "project": cues.project,
                "host": cues.host,
                "task": cues.task,
                "tools_used": [str(tool) for tool in cues.tools_used],
                "time_bucket": cues.time_bucket,
                "emotion_valence": (
                    float(emotion.valence) if emotion and emotion.valence is not None else None
                ),
                "entities": [str(entity) for entity in cues.entities],
            },
            "provenance": {
                "asserted_by": provenance.asserted_by,
                "agent_id": provenance.agent_id,
                "source": provenance.source,
                "source_ref": provenance.session_id,
                "confidence": float(provenance.confidence),
                "asserted_at": float(provenance.asserted_at),
                "history": [self._event_to_row(event) for event in provenance.history],
            },
            "score": {
                "emotion": 0.0,
                "novelty": 0.0,
                "causal": 0.0,
                "total": float(chunk.score),
            },
            "decay_weight": float(chunk.decay_weight),
            "last_reinforced": float(
                chunk.last_reinforced if chunk.last_reinforced is not None else chunk.ingested_at
            ),
            "consolidated": bool(chunk.consolidated),
            "ingested_at": float(chunk.ingested_at),
            "peripheral_gaps": bool(emotion.peripheral_gaps) if emotion else False,
            "needs_reconcile": False,
            "hit_count": 0,
            "last_hit_at": None,
            "reinforce_count": 0,
            "entities_filter": ",".join(cues.entities),
            "rules_json": (
                json.dumps(chunk.rules, separators=(",", ":"), ensure_ascii=False) if chunk.rules else None
            ),
        }

    def _to_stamp(self, row: dict[str, Any]) -> ChunkStamp:
        cues_row = row["cues"] or {}
        prov_row = row["provenance"] or {}
        score_row = row["score"] or {}
        valence_raw = cues_row.get("emotion_valence")
        valence = float(valence_raw) if isinstance(valence_raw, (int, float)) else None
        peripheral_gaps = bool(row["peripheral_gaps"])
        has_emotion = valence is not None or peripheral_gaps
        return ChunkStamp(
            chunk_id=str(row["chunk_id"]),
            profile_id=str(row["profile_id"]),
            text=str(row["text"]),
            cognitive_tier=CognitiveTier(int(row["cognitive_tier"])),
            model_id=str(row["model_id"]),
            persona_id=row.get("persona_id"),
            cues=Cues(
                project=cues_row.get("project"),
                host=cues_row.get("host"),
                task=cues_row.get("task"),
                tools_used=[str(tool) for tool in (cues_row.get("tools_used") or [])],
                time_bucket=cues_row.get("time_bucket"),
                entities=[str(entity) for entity in (cues_row.get("entities") or [])],
                emotion=(
                    EmotionCue(valence=valence, peripheral_gaps=peripheral_gaps) if has_emotion else None
                ),
            ),
            provenance=Provenance(
                asserted_by=str(prov_row.get("asserted_by", "")),
                agent_id=prov_row.get("agent_id"),
                session_id=prov_row.get("source_ref"),
                source=str(prov_row.get("source", "")),
                confidence=float(prov_row.get("confidence", 0.5)),
                asserted_at=float(prov_row.get("asserted_at", 0.0)),
                history=[
                    ProvenanceEvent(**self._event_from_row(event))
                    for event in (prov_row.get("history") or [])
                ],
            ),
            decay_weight=float(row["decay_weight"]),
            last_reinforced=float(row["last_reinforced"]) if row.get("last_reinforced") is not None else None,
            score=float(score_row.get("total", 0.0)),
            consolidated=bool(row["consolidated"]),
            ingested_at=float(row["ingested_at"]),
            turn_start=int(row["turn_start"]) if row.get("turn_start") is not None else None,
            turn_end=int(row["turn_end"]) if row.get("turn_end") is not None else None,
            rules=self._parse_rules(row.get("rules_json")),
        )

    @staticmethod
    def _parse_rules(raw: object) -> list[dict[str, Any]]:
        if not isinstance(raw, str) or not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []

    # ------------------------------------------------------------- helpers

    def _event_to_row(self, event: ProvenanceEvent) -> dict[str, Any]:
        return {
            "at": float(event.at),
            "action": event.action,
            "actor": event.actor,
            "detail": json.dumps(event.detail, separators=(",", ":"), default=str),
        }

    def _event_from_row(self, event: dict[str, Any]) -> dict[str, Any]:
        detail_raw = event.get("detail") or "{}"
        try:
            detail = json.loads(detail_raw)
        except (ValueError, TypeError):
            detail = {}
        return {
            "at": float(event.get("at", 0.0)),
            "action": str(event.get("action", "")),
            "actor": str(event.get("actor", "")),
            "detail": detail,
        }

    def _filter_sql(self, filter: ChunkFilter) -> str:
        """SQL WHERE clause scoped to one profile (search/list/snapshot)."""
        parts = [f"profile_id = {_escape(filter.profile_id)}"]
        parts.extend(self._filter_extra_sql(filter))
        return " AND ".join(parts)

    def _filter_sql_all(self, filter: ChunkFilter) -> str:
        """SQL WHERE clause covering every profile (near-duplicate probe)."""
        parts: list[str] = []
        if filter.profile_id not in (None, "", "*"):
            parts.append(f"profile_id = {_escape(filter.profile_id)}")
        parts.extend(self._filter_extra_sql(filter))
        return " AND ".join(parts) if parts else "true"

    def _filter_extra_sql(self, filter: ChunkFilter) -> list[str]:
        parts: list[str] = []
        if filter.min_decay > 0.0:
            parts.append(f"decay_weight >= {_escape(filter.min_decay)}")
        if filter.ingested_after is not None:
            parts.append(f"ingested_at >= {_escape(filter.ingested_after)}")
        if filter.ingested_before is not None:
            parts.append(f"ingested_at <= {_escape(filter.ingested_before)}")
        if filter.session_id is not None:
            parts.append(f"session_id = {_escape(filter.session_id)}")
        if filter.turn_start is not None:
            parts.append(f"turn_start IS NOT NULL AND turn_start >= {_escape(filter.turn_start)}")
        if filter.turn_end is not None:
            parts.append(f"turn_end IS NOT NULL AND turn_end <= {_escape(filter.turn_end)}")
        if filter.entities:
            contains = []
            for entity in filter.entities:
                escaped = _escape(entity)
                contains.append(
                    f"(entities_filter = {escaped} OR entities_filter LIKE {_escape(entity + ',%')} "
                    f"OR entities_filter LIKE {_escape('%,' + entity)} "
                    f"OR entities_filter LIKE {_escape('%,' + entity + ',%')})"
                )
            group = " OR ".join(contains)
            if filter.entities_allow_missing:
                # Recall-surface tolerance (D2): an empty stored filter means
                # "no entity evidence", never a contradiction.
                group = "entities_filter = '' OR " + group
            parts.append("(" + group + ")")
        if filter.consolidated is not None:
            parts.append("consolidated = " + ("true" if filter.consolidated else "false"))
        if filter.needs_reconcile is not None:
            parts.append("needs_reconcile = " + ("true" if filter.needs_reconcile else "false"))
        if filter.rules_not_null:
            parts.append("rules_json IS NOT NULL AND rules_json <> ''")
        return parts

    def _dense_similarity(self, query: Sequence[float], query_norm: float, row: dict[str, Any]) -> float:
        row_vector = row["vector_dense"]
        row_norm = math.sqrt(sum(value * value for value in row_vector)) or 1.0
        dot = sum(q * v for q, v in zip(query, row_vector, strict=False))
        return float(dot / (query_norm * row_norm))

    def _sparse_similarity(self, query: SparseVector, row: dict[str, Any]) -> float:
        stored = row.get("vector_sparse") or {}
        indices = stored.get("indices") or []
        values = stored.get("values") or []
        if not indices or not values:
            return 0.0
        left = dict(zip(query.indices, query.values, strict=False))
        right = dict(zip((int(i) for i in indices), (float(v) for v in values), strict=False))
        query_norm = math.sqrt(sum(v * v for v in query.values)) or 1.0
        stored_norm = math.sqrt(sum(v * v for v in right.values())) or 1.0
        dot = sum(weight * right[index] for index, weight in left.items() if index in right)
        return float(dot / (query_norm * stored_norm))
