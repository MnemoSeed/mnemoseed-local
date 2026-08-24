"""Context assembly: budget gate, conflict pairing, and the Freshness Guard.

Takes the ranked candidate pool from the hybrid retriever and produces the
final context package (PRD-03 FR-3.5 / FR-3.6 / FR-3.8 / FR-3.13):

1. Budget gate (FR-3.5): admit candidates in rank order while the entry count
   is at most ``top_k`` and the assembled token estimate stays at most
   ``budget_tokens``. Token accounting reuses ``dream.delta.estimate_tokens``
   over each entry's assembled text. An over-budget tail is dropped and every
   drop is reported through ``dropped_count`` — never silent.
2. Conflict pairing (FR-3.6): graph candidates flagged ``conflict_flag`` are
   admitted as an atomic group — every pool member sharing the same
   ``conflict_group`` returns together with the ``conflict_pair`` marker.
   Pair members are resolved inside the ranked pool (the two sides of a
   conflict normally share a cue entity and enter the pool on the same track);
   a pair is never split silently. When only one side of a conflict reaches the
   pool, or the whole group does not fit the remaining budget/top-k, the
   highest-ranked member is still admitted with an explicit ``conflict_omitted``
   marker if it fits alone and the omitted siblings count toward
   ``dropped_count``; the pair marker is reserved for returns where every side
   is present.
3. Freshness Guard (FR-3.8): after a provisional selection, probe the vector
   store for chunks ingested after the profile's consolidation watermark whose
   entities overlap the selected graph candidates' entities. On a hit the
   affected graph candidates become pending-consolidation: their fused score
   is demoted by ``freshness_demotion`` before the next cut, up to
   ``evidence_cap`` truncated original snippets are attached, and the flag is
   persisted to the graph store so the dream engine consolidates the pair.
   The selection-cut loop re-runs until membership stabilizes, so a demotion
   that changes the top-k membership is honored.
4. Honest empty (FR-3.13): zero qualifying candidates produce the explicit
   empty ``AssembledContext`` with ``dropped_count`` and a coverage
   self-report — never padded with junk.

Watermark semantics: ``MetaStore.pool_state`` returns the watermark as the
turn range consolidated by the last dream run, and chunks carry their
``turn_start``/``turn_end`` capture window. A chunk is therefore "new since
the last consolidation" exactly when ``turn_start > watermark.end``; the probe
filter maps the guard's prose predicate (``ingested_at > watermark``) onto the
real stored types, which carry turn numbers rather than timestamps.

Deterministic: no clocks except reading stored timestamps, no randomness, no
network. Ties break by (kind, id) exactly like the hybrid retriever.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from mnemoseed_local.dream.delta import estimate_tokens
from mnemoseed_local.retrieve.hybrid import Candidate, HybridRecall
from mnemoseed_local.schema.graph import GraphNode
from mnemoseed_local.schema.stamp import ChunkStamp
from mnemoseed_local.storage.ports import (
    ChunkFilter,
    GraphFlag,
    GraphStore,
    MetaStore,
    Page,
    TurnRange,
    VectorStore,
)

# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class AssembleConfig:
    """Tunable bounds for the assembly step.

    Defaults follow design item 1 on retrieval discipline: top-k of 5 and a
    800-token budget (both overridable). Freshness demotion matches design/02
    section 9 (x0.8 rerank demotion); at most 2 truncated evidence snippets
    per pending graph candidate.
    """

    top_k: int = 5
    budget_tokens: int = 800
    evidence_cap: int = 2
    evidence_max_chars: int = 400
    freshness_demotion: float = 0.8

    def __post_init__(self) -> None:
        if self.top_k <= 0:
            raise ValueError("top_k must be positive")
        if self.budget_tokens <= 0:
            raise ValueError("budget_tokens must be positive")
        if self.evidence_cap < 0:
            raise ValueError("evidence_cap must be non-negative")
        if self.evidence_max_chars <= 0:
            raise ValueError("evidence_max_chars must be positive")
        if not 0.0 < self.freshness_demotion <= 1.0:
            raise ValueError("freshness_demotion must be in (0, 1]")


# ---------------------------------------------------------------- output types


class EntryFlag(StrEnum):
    """Per-entry explicit markers (serialized by the T4 consumers)."""

    PENDING_CONSOLIDATION = "pending_consolidation"
    CONFLICT_PAIR = "conflict_pair"
    CONFLICT_OMITTED = "conflict_omitted"
    FRESH_EVIDENCE = "fresh_evidence"
    RESCUED = "rescued"  # design/09 §3.5: admitted through the cue-driven rescue band


@dataclass(frozen=True)
class AssembledEntry:
    """One returned memory: assembled text plus explicit flags and evidence."""

    kind: str
    id: str
    source: str
    text: str
    score: float
    tokens: int
    flags: tuple[EntryFlag, ...]
    conflict_group: str | None = None
    recent_evidence: tuple[str, ...] = ()
    session_id: str | None = None
    ingested_at: float | None = None


@dataclass(frozen=True)
class CoverageReport:
    """Honest self-report of what was searched (FR-3.13, metamemory).

    ``watermark`` is the consolidation boundary read for the guard;
    ``fresh_evidence_chunks`` counts the unconsolidated fragments the probe
    saw overlapping the returned entities; ``pending_marked`` counts the graph
    candidates the guard flagged for consolidation.
    """

    vector_hits: int
    graph_hits: int
    pool_size: int
    profile_chunks: int
    watermark: TurnRange | None = None
    fresh_evidence_chunks: int = 0
    pending_marked: int = 0


@dataclass(frozen=True)
class AssembledContext:
    """The final context package T4 tools and the daemon serialize."""

    entries: tuple[AssembledEntry, ...]
    dropped_count: int
    budget_tokens: int
    tokens_used: int
    coverage: CoverageReport


@dataclass(frozen=True)
class _Admission:
    """One candidate admitted by the budget gate, with its transient marking."""

    candidate: Candidate
    score: float
    flags: tuple[EntryFlag, ...]
    conflict_group: str | None
    order: int


# ---------------------------------------------------------------- assembler


class Assembler:
    """Deterministic budget gate + conflict pairing + Freshness Guard."""

    def __init__(self, config: AssembleConfig | None = None) -> None:
        self._config = config if config is not None else AssembleConfig()

    @property
    def config(self) -> AssembleConfig:
        return self._config

    def assemble(
        self,
        recall: HybridRecall,
        *,
        profile_id: str,
        meta_store: MetaStore,
        vector_store: VectorStore,
        graph_store: GraphStore,
    ) -> AssembledContext:
        """Assemble one profile's ranked pool into the final context package."""
        config = self._config
        pool = recall.candidates
        by_id = {candidate.id: candidate for candidate in pool}
        tokens = {candidate.id: estimate_tokens(self._entry_text(candidate)) for candidate in pool}
        effective = {candidate.id: candidate.score for candidate in pool}

        state = meta_store.pool_state(profile_id)
        watermark = state.watermark
        coverage = CoverageReport(
            vector_hits=recall.vector_hits,
            graph_hits=recall.graph_hits,
            pool_size=len(pool),
            profile_chunks=self._profile_chunk_count(vector_store, profile_id),
            watermark=watermark,
        )

        admitted, dropped = self._select(pool, effective, tokens, config)
        marked: dict[str, tuple[str, ...]] = {}
        fresh_ids: set[str] = set()
        for _pass in range(len(pool) + 1):
            admitted_ids = {admission.candidate.id for admission in admitted}
            unprobed = [
                candidate.id
                for candidate in pool
                if candidate.id in admitted_ids
                and candidate.kind == "graph"
                and candidate.id not in marked
                and tuple(self._item_entities(candidate))
            ]
            if not unprobed:
                break
            marks, found = self._probe(
                profile_id,
                watermark,
                admitted_ids,
                by_id,
                vector_store,
                config,
            )
            fresh_ids.update(found)
            if not marks:
                break
            marked.update(marks)
            for candidate_id in marks:
                effective[candidate_id] = by_id[candidate_id].score * config.freshness_demotion
            new_admitted, new_dropped = self._select(pool, effective, tokens, config)
            if {admission.candidate.id for admission in new_admitted} == admitted_ids:
                admitted, dropped = new_admitted, new_dropped
                break
            admitted, dropped = new_admitted, new_dropped

        if marked:
            graph_store.set_flags(list(marked), [GraphFlag.PENDING_CONSOLIDATION])

        entries = tuple(
            self._entry(admission, marked, config)
            for admission in sorted(
                admitted,
                key=lambda item: (item.order, item.candidate.kind, item.candidate.id),
            )
        )
        return AssembledContext(
            entries=entries,
            dropped_count=dropped,
            budget_tokens=config.budget_tokens,
            tokens_used=sum(entry.tokens for entry in entries),
            coverage=replace(coverage, fresh_evidence_chunks=len(fresh_ids), pending_marked=len(marked)),
        )

    # ----------------------------------------------------------- budget gate

    def _select(
        self,
        pool: Sequence[Candidate],
        effective: Mapping[str, float],
        tokens: Mapping[str, int],
        config: AssembleConfig,
    ) -> tuple[list[_Admission], int]:
        """Walk the ranked pool, admitting under top-k/budget and pairing
        conflict groups atomically. Admission re-applies the retriever's rank
        discipline (design/09 §3.5: rescued candidates trail normal ones
        regardless of fused score) so rendering order and top-k truncation
        inherit it at the serving surface. Returns the admissions and the
        count of candidates rejected by the gate (never silent)."""
        ordered = sorted(
            pool,
            key=lambda candidate: (
                candidate.rescued,
                -effective[candidate.id],
                candidate.kind,
                candidate.id,
            ),
        )
        kept: list[_Admission] = []
        used = 0
        consumed: set[str] = set()
        dropped = 0
        order_seq = 0
        for candidate in ordered:
            if candidate.id in consumed:
                continue
            group = self._group_of(candidate)
            if group is None:
                cost = tokens[candidate.id]
                if len(kept) < config.top_k and used + cost <= config.budget_tokens:
                    kept.append(_Admission(candidate, effective[candidate.id], (), None, order_seq))
                    order_seq += 1
                    used += cost
                else:
                    dropped += 1
                continue
            members = [item for item in ordered if item.id not in consumed and self._group_of(item) == group]
            for member in members:
                consumed.add(member.id)
            group_cost = sum(tokens[member.id] for member in members)
            if (
                len(members) > 1
                and len(kept) + len(members) <= config.top_k
                and used + group_cost <= config.budget_tokens
            ):
                for member in members:
                    kept.append(
                        _Admission(
                            member,
                            effective[member.id],
                            (EntryFlag.CONFLICT_PAIR,),
                            group,
                            order_seq,
                        )
                    )
                order_seq += 1
                used += group_cost
            else:
                # lone survivor (only one side in the pool) or too-large group:
                # the top member is admitted alone and mislabeling it a pair is
                # avoided by the explicit omission marker.
                top = members[0]
                cost = tokens[top.id]
                if len(kept) + 1 <= config.top_k and used + cost <= config.budget_tokens:
                    kept.append(
                        _Admission(
                            top,
                            effective[top.id],
                            (EntryFlag.CONFLICT_OMITTED,),
                            group,
                            order_seq,
                        )
                    )
                    order_seq += 1
                    used += cost
                    dropped += len(members) - 1
                else:
                    dropped += len(members)
        return kept, dropped

    # -------------------------------------------------------- freshness guard

    def _probe(
        self,
        profile_id: str,
        watermark: TurnRange | None,
        selected_ids: set[str],
        by_id: Mapping[str, Candidate],
        vector_store: VectorStore,
        config: AssembleConfig,
    ) -> tuple[dict[str, tuple[str, ...]], set[str]]:
        """Probe the vector store for UNCONSOLIDATED fragments whose entities
        overlap the selected graph candidates. Returns per-candidate truncated
        evidence and the (deduplicated) fresh fragment ids observed.

        The probe is scoped to ``consolidated=False`` (design/03 §4): chunks
        the dream merge already marked consolidated are the fact's retained
        evidence scene, never "fresh unconsolidated evidence" — a merged chunk
        must not re-arm pending_consolidation or ride back in as recent
        evidence.
        """
        if watermark is None:
            return {}, set()
        entities: list[str] = []
        seen: set[str] = set()
        for candidate_id in sorted(selected_ids):
            for entity in self._item_entities(by_id[candidate_id]):
                if entity not in seen:
                    seen.add(entity)
                    entities.append(entity)
        if not entities:
            return {}, set()
        fresh = vector_store.snapshot_read(
            ChunkFilter(
                profile_id=profile_id,
                turn_start=watermark.end + 1,
                entities=tuple(entities),
                consolidated=False,
            )
        )
        marks: dict[str, tuple[str, ...]] = {}
        for candidate_id in selected_ids:
            candidate = by_id[candidate_id]
            if candidate.kind != "graph":
                continue
            node = candidate.item
            if not isinstance(node, GraphNode):
                continue
            own = {self._fold(entity) for entity in node.entities}
            if not own:
                continue
            hits = [chunk for chunk in fresh if own & {self._fold(e) for e in chunk.cues.entities}]
            if hits:
                hits.sort(key=lambda chunk: (-chunk.ingested_at, chunk.chunk_id))
                marks[candidate_id] = tuple(
                    chunk.text[: config.evidence_max_chars] for chunk in hits[: config.evidence_cap]
                )
        fresh_ids = {chunk.chunk_id for chunk in fresh}
        return marks, fresh_ids

    # ------------------------------------------------------------- plumbing

    def _entry(
        self,
        admission: _Admission,
        marked: Mapping[str, tuple[str, ...]],
        config: AssembleConfig,
    ) -> AssembledEntry:
        candidate = admission.candidate
        flags: list[EntryFlag] = []
        if candidate.rescued:
            flags.append(EntryFlag.RESCUED)
        if candidate.kind == "graph":
            node = candidate.item
            if isinstance(node, GraphNode) and node.pending_consolidation:
                flags.append(EntryFlag.PENDING_CONSOLIDATION)
        if candidate.id in marked and EntryFlag.PENDING_CONSOLIDATION not in flags:
            flags.append(EntryFlag.PENDING_CONSOLIDATION)
        evidence = tuple(marked.get(candidate.id, ()))
        if evidence:
            flags.append(EntryFlag.FRESH_EVIDENCE)
        for flag in admission.flags:
            if flag not in flags:
                flags.append(flag)
        text = self._entry_text(candidate)
        provenance: dict[str, Any] = {}
        if candidate.kind == "chunk" and isinstance(candidate.item, ChunkStamp):
            provenance = {
                "session_id": candidate.item.provenance.session_id,
                "ingested_at": candidate.item.ingested_at,
            }
        return AssembledEntry(
            kind=candidate.kind,
            id=candidate.id,
            source=candidate.source,
            text=text,
            score=admission.score,
            tokens=estimate_tokens(text),
            flags=tuple(flags),
            conflict_group=admission.conflict_group,
            recent_evidence=evidence,
            **provenance,
        )

    def _profile_chunk_count(self, vector_store: VectorStore, profile_id: str) -> int:
        page = vector_store.list_chunks(ChunkFilter(profile_id=profile_id), Page(offset=0, limit=1))
        return page.total

    @staticmethod
    def _fold(value: str) -> str:
        return value.casefold()

    @staticmethod
    def _group_of(candidate: Candidate) -> str | None:
        """Conflict group for a graph candidate, or None when not paired."""
        if candidate.kind != "graph":
            return None
        node = candidate.item
        if not isinstance(node, GraphNode) or not node.conflict_flag or node.conflict_group is None:
            return None
        return node.conflict_group

    @staticmethod
    def _item_entities(candidate: Candidate) -> tuple[str, ...]:
        item = candidate.item
        if candidate.kind == "chunk":
            if isinstance(item, ChunkStamp):
                return tuple(item.cues.entities)
            return ()
        if isinstance(item, GraphNode):
            return tuple(item.entities)
        return ()

    @staticmethod
    def _entry_text(candidate: Candidate) -> str:
        """The assembled text this entry exposes (token accounting source)."""
        if candidate.kind == "chunk":
            item = candidate.item
            return item.text if isinstance(item, ChunkStamp) else ""
        node = candidate.item
        if not isinstance(node, GraphNode):
            return ""
        for key in ("statement", "rule", "summary", "name", "trigger_condition", "action", "domain"):
            value = node.props.get(key)
            if isinstance(value, str) and value:
                return value
        if node.entities:
            return f"{node.node_type.value}: {', '.join(node.entities)}"
        return node.node_type.value
