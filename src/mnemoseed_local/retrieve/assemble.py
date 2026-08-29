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

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any

from mnemoseed_local.dream.delta import estimate_tokens
from mnemoseed_local.retrieve.hybrid import Candidate, HybridRecall
from mnemoseed_local.schema.graph import GraphNode
from mnemoseed_local.schema.stamp import ChunkStamp, is_explicit_pin
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
    # Read-side deterministic contradiction probe (prefer-to-under-flag): two
    # same-entity in-effect statements are flagged only when the probe finds
    # a lexically-relevant value/assertion divergence — never for raw character
    # overlap. ``read_conflict_min_frame`` is the token-set Jaccard floor on
    # content tokens (entity + stopwords removed) below which the facts are
    # complementary, not contradictory; ``read_conflict_token_sim`` is the
    # minimum character-similarity a differing token needs to count as a
    # same-word edit (typo/tense/small date correction) that is agreement, not
    # a contradiction.
    read_conflict_min_frame: float = 0.25
    read_conflict_token_sim: float = 0.55

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
        if not 0.0 <= self.read_conflict_min_frame < 1.0:
            raise ValueError("read_conflict_min_frame must satisfy 0 <= min < 1")
        if not 0.0 < self.read_conflict_token_sim <= 1.0:
            raise ValueError("read_conflict_token_sim must be in (0, 1]")


# ---------------------------------------------------------------- output types


class EntryFlag(StrEnum):
    """Per-entry explicit markers (serialized by the T4 consumers)."""

    PENDING_CONSOLIDATION = "pending_consolidation"
    CONFLICT_PAIR = "conflict_pair"
    CONFLICT_OMITTED = "conflict_omitted"
    FRESH_EVIDENCE = "fresh_evidence"
    RESCUED = "rescued"  # design/09 §3.5: admitted through the cue-driven rescue band
    READ_CONFLICT = "read_conflict"  # raised by the read path; peer via read_conflict_id


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
    # Assertion-time fact from the graph version chain (when the claim took
    # effect); chunks carry None — it is not a session attribution.
    valid_from: float | None = None
    # Inert provenance labels served read-only (origin attribution + encoding
    # host); graph entries and unlabeled chunks carry None.
    origin_agent: str | None = None
    host: str | None = None
    # Inert provenance used by the trust surface (read-only): who asserted the
    # memory ("user" / model id) and the storage conflict flag. Chunks carry
    # their values; graph entries and unlabeled chunks keep None/False.
    asserted_by: str | None = None
    needs_reconcile: bool = False
    # R2 pin discriminant: the genuine provenance source label (per §2.1 the
    # pin test is `provenance_source == "memory.remember"`) and its derived
    # boolean. Never invented for graph entries — sourced from the real stamp.
    provenance_source: str | None = None
    explicit_pin: bool = False


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

        read_pairs = self._read_conflict_pairs(admitted, config)
        for node_a, node_b in read_pairs:
            graph_store.set_read_conflict(node_a, node_b)
        read_conflict_ids = {node_id for pair in read_pairs for node_id in pair}

        entries = tuple(
            self._entry(admission, marked, config, read_conflict_ids)
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
        read_conflict_ids: set[str],
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
        if candidate.id in read_conflict_ids:
            flags.append(EntryFlag.READ_CONFLICT)
        text = self._entry_text(candidate)
        provenance: dict[str, Any] = {}
        if candidate.kind == "chunk" and isinstance(candidate.item, ChunkStamp):
            provenance = {
                "session_id": candidate.item.provenance.session_id,
                "ingested_at": candidate.item.ingested_at,
                "origin_agent": candidate.item.origin_agent,
                "host": candidate.item.cues.host,
                "asserted_by": candidate.item.provenance.asserted_by,
                "needs_reconcile": candidate.item.needs_reconcile,
            }
        elif isinstance(candidate.item, GraphNode):
            provenance = {
                "valid_from": candidate.item.valid_from,
                "asserted_by": candidate.item.provenance.asserted_by,
            }
        provenance["provenance_source"] = candidate.item.provenance.source
        provenance["explicit_pin"] = is_explicit_pin(candidate.item.provenance.source)
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

    def _read_conflict_pairs(
        self,
        admitted: Sequence[_Admission],
        config: AssembleConfig,
    ) -> list[tuple[str, str]]:
        """Pair same-entity in-effect graph statements the deterministic probe
        judges as looking contradictory.

        The read path raises the annotation but never decides which side is
        correct and never rewrites a statement. Under-flag posture: unrelated
        (complementary) facts and near-identical agreement — a tense change,
        a typo, a minor date correction — are never paired. Deterministic by
        (a, b) node id order.
        """
        in_effect: list[GraphNode] = []
        for admission in admitted:
            candidate = admission.candidate
            if candidate.kind != "graph":
                continue
            node = candidate.item
            if not isinstance(node, GraphNode) or node.valid_to is not None:
                continue
            if not node.entities or not Assembler._node_statement(node):
                continue
            in_effect.append(node)
        pairs: list[tuple[str, str]] = []
        paired: set[str] = set()
        for a, b in _pairwise(in_effect):
            if a.node_id in paired or b.node_id in paired:
                continue
            a_entities = {self._fold(e) for e in a.entities}
            b_entities = {self._fold(e) for e in b.entities}
            if not (a_entities & b_entities):
                continue
            if not self._is_read_conflict(
                Assembler._node_statement(a),
                Assembler._node_statement(b),
                a_entities,
                config,
            ):
                continue
            pairs.append((a.node_id, b.node_id))
            paired.add(a.node_id)
            paired.add(b.node_id)
        return pairs

    def _is_read_conflict(
        self,
        statement_a: str,
        statement_b: str,
        entities: set[str],
        config: AssembleConfig,
    ) -> bool:
        """Deterministic, model-free contradiction probe over two statements.

        Decision order (each branch returns): (1) an explicit-negation polarity
        flip over a shared content token is a direct contradiction; (2) a pure
        agreement edit — every differing token is a character-similar same-word
        edit (typo, inflection, small date correction) or one side refines the
        other — is NEVER a contradiction; (3) otherwise, only a shared assertion
        frame (at least two shared content tokens and a content-token Jaccard at
        or above the floor) with lexically distinct value tokens is a divergence
        worth flagging — a lone shared subject-mention is complementary, not
        contradictory.
        """
        content_a, neg_a = _statement_tokens(statement_a, entities)
        content_b, neg_b = _statement_tokens(statement_b, entities)
        if neg_a != neg_b and content_a & content_b:
            return True
        if _is_agreement_edit(content_a, content_b, config.read_conflict_token_sim):
            return False
        shared = content_a & content_b
        if len(shared) < 2:
            return False
        return _token_jaccard(content_a, content_b) >= config.read_conflict_min_frame

    @staticmethod
    def _entry_text(candidate: Candidate) -> str:
        """The assembled text this entry exposes (token accounting source)."""
        if candidate.kind == "chunk":
            item = candidate.item
            return item.text if isinstance(item, ChunkStamp) else ""
        node = candidate.item
        if not isinstance(node, GraphNode):
            return ""
        return Assembler._node_statement(node)

    @staticmethod
    def _node_statement(node: GraphNode) -> str:
        """The assertion text carried on a graph node (its entry text)."""
        for key in ("statement", "rule", "summary", "name", "trigger_condition", "action", "domain"):
            value = node.props.get(key)
            if isinstance(value, str) and value:
                return value
        if node.entities:
            return f"{node.node_type.value}: {', '.join(node.entities)}"
        return node.node_type.value


def _pairwise(nodes: Sequence[GraphNode]) -> Iterator[tuple[GraphNode, GraphNode]]:
    """Ordered unique node pairs, deterministically sorted by node id."""
    ordered = sorted(nodes, key=lambda node: node.node_id)
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            yield ordered[i], ordered[j]


# ------------------------------------------------------------ read-conflict probe

# Content tokens are the assertion's lexical substance; function words and
# negation markers are removed. The shared subject mention (usually the node's
# entity) is retained: together with a predicate it forms the shared frame the
# ≥2-token gate keys on, so dropping it would hide a same-frame value
# divergence behind a single bare predicate token.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "and",
        "or",
        "but",
        "does",
        "do",
        "did",
        "done",
        "it",
        "this",
        "that",
        "these",
        "those",
        "he",
        "she",
        "they",
        "them",
        "his",
        "her",
        "its",
        "their",
        "our",
        "we",
        "you",
        "i",
        "me",
        "my",
        "your",
        "as",
        "by",
        "from",
        "into",
        "about",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
    }
)

_NEGATION = frozenset({"no", "not", "never", "none", "nor", "nobody", "nothing", "without"})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _statement_tokens(statement: str, entities: set[str]) -> tuple[set[str], bool]:
    """Tokenize a statement into its content token set and its negation flag.

    Stopwords and negation markers drop out of the content surface; negation is
    read off the raw token stream so polarity can flip without the marker being
    swallowed. The shared subject mention (which is typically the node's
    registered entity) is KEPT in the content surface so a single predicate
    still forms a shared frame with it — dropping the subject would collapse a
    genuine same-frame value divergence down to one bare predicate token and hide
    the contradiction. A subject mention whose name collides with a stopword
    (e.g. the entity "Will") is force-kept by peeling those entity tokens out of
    the drop set when they appear in the raw surface; only the subject survives,
    other stopwords still drop so a lone shared subject stays complementary.
    """
    raw = _TOKEN_RE.findall(statement.casefold())
    negated = any(token in _NEGATION for token in raw)
    subject_mentions = {token for token in entities if token in raw}
    drop = (_STOPWORDS | _NEGATION) - subject_mentions
    content = {token for token in raw if len(token) > 1 and token not in drop}
    return content, negated


def _token_jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _is_agreement_edit(a: set[str], b: set[str], token_sim: float) -> bool:
    """True when the two statements differ only in the small-edit sense.

    Identical sets, a one-side refinement, or every differing token having a
    character-similar distinct partner on the other side (a typo, an inflection,
    a small date correction) all read as agreement, not contradiction.
    """
    diff_a = a - b
    diff_b = b - a
    if not diff_a and not diff_b:
        return True
    if not diff_a or not diff_b:
        return True  # one statement refines/elaborates the other
    if len(diff_a) != len(diff_b):
        return False
    avail: set[str] = set(diff_b)
    for token in sorted(diff_a):
        best = max(avail, key=lambda other: SequenceMatcher(None, token, other).ratio())
        if SequenceMatcher(None, token, best).ratio() < token_sim:
            return False
        avail.remove(best)
    return True
