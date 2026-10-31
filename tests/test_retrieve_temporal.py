"""PRD-281: read-side temporal applicability — explicit expired-window demotion.

The pure evaluator turns a chunk's verbatim text into a binary ranking signal:
``expired_window`` only when the frozen v1 grammar parses exactly one explicit
expiry instant that lies at or before the reading instant; anything ambiguous,
conflicting, quoted, URL-borne, or unparsed is unknown and never demotes
(fail-open). The case-table rows exercise the tier at the hybrid/assemble
serving surfaces: expired candidates rank behind ALL candidates without an
expired window (before the rescue tier per D04), are never excluded, and age
(ingested_at/decay) never touches the flag.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from mnemoseed_local.retrieve.assemble import AssembleConfig, Assembler
from mnemoseed_local.retrieve.cues import ExtractedCues, Intent
from mnemoseed_local.retrieve.hybrid import Candidate, HybridRecall, HybridRetriever, ScoreBreakdown
from mnemoseed_local.retrieve.temporal import TemporalVerdict, evaluate_temporal
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers.lancedb_embedded import LanceDbEmbeddedStore
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.registry import GRAPH_DRIVERS, META_DRIVERS, VECTOR_DRIVERS, register

_DIM = 64
_PROFILE = "alice"
# The PRD review instant: every reported expiry (2026-09-08/09) lies weeks past it.
_NOW = datetime(2026, 10, 31, tzinfo=UTC).timestamp()


def _epoch(*args: int) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


@dataclass
class _Stack:
    vector: LanceDbEmbeddedStore
    graph: SqliteGraphDriver
    meta: SqliteMetaDriver
    embed: SyntheticEmbedder


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not VECTOR_DRIVERS.contains("lancedb_embedded"):
        register(VECTOR_DRIVERS)(LanceDbEmbeddedStore)
    if not GRAPH_DRIVERS.contains("sqlite_graph"):
        register(GRAPH_DRIVERS)(SqliteGraphDriver)
    if not META_DRIVERS.contains("sqlite_meta"):
        register(META_DRIVERS)(SqliteMetaDriver)
    yield


@pytest.fixture
def stack(tmp_path):
    db = _Stack(
        vector=LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=_DIM),
        graph=SqliteGraphDriver(path=tmp_path / "graph.db"),
        meta=SqliteMetaDriver(path=tmp_path / "meta.db"),
        embed=SyntheticEmbedder(dimension=_DIM),
    )
    yield db
    asyncio.run(db.vector.close())
    asyncio.run(db.graph.close())
    asyncio.run(db.meta.close())


def _chunk(
    chunk_id: str,
    text: str,
    *,
    decay: float = 1.0,
    ingested_at: float = 1.0,
    entities: tuple[str, ...] = ("Ops",),
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id="p1",
        cues=Cues(entities=list(entities)),
        provenance=Provenance(
            asserted_by="test-model",
            session_id="s1",
            source="manual",
            confidence=0.8,
            asserted_at=100.0,
        ),
        decay_weight=decay,
        score=0.5,
        consolidated=False,
        ingested_at=ingested_at,
        turn_start=1,
        turn_end=2,
    )


def _write(stack: _Stack, stamp: ChunkStamp) -> None:
    result = stack.embed.embed(stamp.text)
    stack.vector.upsert_chunk(stamp, result.dense, result.sparse)


def _query_cues(entities: tuple[str, ...] = ("Ops",)) -> ExtractedCues:
    return ExtractedCues(cues=Cues(entities=list(entities)), intent=Intent.RECALL)


def _recall(stack: _Stack, text: str, cues: ExtractedCues) -> HybridRecall:
    return HybridRetriever().recall(
        text,
        cues,
        profile_id=_PROFILE,
        vector_store=stack.vector,
        graph_store=stack.graph,
        embedder=stack.embed,
        now=_NOW,
    )


def _chunk_candidates(result: HybridRecall) -> list[Candidate]:
    return [c for c in result.candidates if c.kind == "chunk"]


def _pool_candidate(
    chunk_id: str,
    text: str,
    *,
    score: float,
    expired_window: bool = False,
) -> Candidate:
    breakdown = ScoreBreakdown(
        semantic=score,
        cue_overlap=0.0,
        decay_weight=0.0,
        graph_centrality=0.0,
        cooccurrence=0.0,
        total=score,
    )
    return Candidate(
        kind="chunk",
        id=chunk_id,
        source="vector",
        item=_chunk(chunk_id, text),
        score=score,
        breakdown=breakdown,
        expired_window=expired_window,
    )


def _assemble(stack: _Stack, recall: HybridRecall, config: AssembleConfig | None = None):
    return Assembler(config).assemble(
        recall,
        profile_id=_PROFILE,
        meta_store=stack.meta,
        vector_store=stack.vector,
        graph_store=stack.graph,
    )


# ------------------------------------------------------------ frozen grammar: EN markers

_EN_EXPIRED_TEXTS = [
    "expires 2026-09-08",
    "expiry 2026-09-08",
    "expiration 2026-09-08",
    "valid until 2026-09-08",
    "valid through 2026-09-08",
    "valid before 2026-09-08",
    "good until 2026-09-08",
    "available until 2026-09-08",
    "effective until 2026-09-08",
    "until 2026-09-08",
    "before 2026-09-08",
]


@pytest.mark.parametrize("text", _EN_EXPIRED_TEXTS)
def test_en_marker_governs_the_date_token(text: str) -> None:
    assert evaluate_temporal(text, _NOW) == TemporalVerdict(True, False)


@pytest.mark.parametrize(
    "text",
    ["EXPIRES 2026-09-08", "Valid Until 2026-09-08", "UNTIL 2026-09-08", "Before 2026-09-08"],
)
def test_en_markers_match_casefolded(text: str) -> None:
    assert evaluate_temporal(text, _NOW) == TemporalVerdict(True, False)


# ------------------------------------------------------------ frozen grammar: ZH markers

_ZH_EXPIRED_TEXTS = [
    "有效期至2026-09-08",
    "有效期到2026-09-08",
    "有效期：2026-09-08",
    "有效期: 2026-09-08",
    "截止2026-09-08",
    "截止至2026-09-08",
    "截止日期2026-09-08",
    "到期日2026-09-08",
    "失效时间2026-09-08",
    "过期时间2026-09-08",
    "2026-09-08前有效",
    "2026-09-08之前有效",
]


@pytest.mark.parametrize("text", _ZH_EXPIRED_TEXTS)
def test_zh_marker_governs_the_date_token(text: str) -> None:
    assert evaluate_temporal(text, _NOW) == TemporalVerdict(True, False)


def test_zh_suffix_marker_requires_the_date_directly_before() -> None:
    # "前有效版本" is a version label, not an expiry: without the date attached
    # to the suffix marker the clause stays unknown.
    assert evaluate_temporal("2026-09-08的记录里提到前有效版本", _NOW) == TemporalVerdict(False, True)
    assert evaluate_temporal("迁移在2026-09-08之前有效地完成了", _NOW) == TemporalVerdict(False, True)


# ------------------------------------------------------------ frozen grammar: cleanliness


@pytest.mark.parametrize(
    "text",
    [
        'banner said "expires 2026-09-08" during the launch',
        "banner said 'expires 2026-09-08' during the launch",
        "config note `expires 2026-09-08` kept for history",
        "横幅写着「有效期至2026-09-08」",
        "横幅写着『有效期至2026-09-08』",
    ],
)
def test_row7_quoted_span_dates_never_create_expiry(text: str) -> None:
    assert evaluate_temporal(text, _NOW) == TemporalVerdict(False, True)


def test_row7_url_token_dates_never_create_expiry() -> None:
    assert evaluate_temporal(
        "see https://ops.internal/notes/expires-2026-09-08 for detail", _NOW
    ) == TemporalVerdict(False, True)


# ------------------------------------------------------------ frozen grammar: date forms


def test_instant_with_z_offset_respects_the_exact_instant() -> None:
    bound = _epoch(2026, 9, 8, 12, 0, 0)
    assert evaluate_temporal("until 2026-09-08T12:00:00Z", bound - 0.001) == TemporalVerdict(False, False)
    assert evaluate_temporal("until 2026-09-08T12:00:00Z", bound) == TemporalVerdict(True, False)


def test_instant_with_explicit_offset_is_normalized_to_utc() -> None:
    # 2026-09-08T23:59:00+08:00 == 2026-09-08T15:59:00Z
    assert evaluate_temporal(
        "until 2026-09-08T23:59:00+08:00", _epoch(2026, 9, 8, 15, 59, 0)
    ) == TemporalVerdict(True, False)
    assert evaluate_temporal(
        "until 2026-09-08T23:59:00+08:00", _epoch(2026, 9, 8, 15, 58, 59)
    ) == TemporalVerdict(False, False)


def test_date_only_normalizes_to_end_of_day_utc() -> None:
    assert evaluate_temporal("until 2026-09-08", _epoch(2026, 9, 8, 23, 59, 58)) == TemporalVerdict(
        False, False
    )
    assert evaluate_temporal("until 2026-09-08", _epoch(2026, 9, 8, 23, 59, 59)) == TemporalVerdict(
        True, False
    )
    assert evaluate_temporal("until 2026-09-08", _epoch(2026, 9, 9, 0, 0, 0)) == TemporalVerdict(True, False)


def test_row5_timezone_boundary_forms() -> None:
    at_16z = _epoch(2026, 9, 8, 16, 0, 0)
    # +08:00 bound normalizes to 15:59Z: expired at 16:00Z
    assert evaluate_temporal("until 2026-09-08T23:59:00+08:00", at_16z).expired_window is True
    # Z bound is 23:59Z: still valid at 16:00Z
    assert evaluate_temporal("until 2026-09-08T23:59:00Z", at_16z).expired_window is False
    # timezone-less instant stays unknown and never demotes
    assert evaluate_temporal("until 2026-09-08T23:59:00", at_16z) == TemporalVerdict(False, True)


def test_row6_ambiguous_dates_stay_unknown() -> None:
    for text in ("until 09/08", "有效期至9月8日", "expires Sep 8", "有效期至2026/09/08"):
        assert evaluate_temporal(text, _NOW) == TemporalVerdict(False, True), text


def test_space_separated_timezone_less_instant_stays_unknown() -> None:
    # A date followed by a bare space-separated time is a timezone-less instant,
    # not a calendar day: it must not degrade to the date-only EOD form.
    assert evaluate_temporal("until 2026-09-08 12:00", _NOW) == TemporalVerdict(False, True)


def test_invalid_calendar_tokens_stay_unknown() -> None:
    assert evaluate_temporal("expires 2026-13-45T10:00:00Z", _NOW) == TemporalVerdict(False, True)


def test_zh_suffix_marker_with_full_instant_stays_unknown() -> None:
    # The suffix form attaches to the bare calendar date only; an instant
    # followed by the marker has no marker-governed date and stays unknown.
    assert evaluate_temporal("2026-09-08T12:00:00Z前有效", _NOW) == TemporalVerdict(False, True)


# ------------------------------------------------------------ frozen grammar: unknown rules


def test_row8_conflicting_instants_stay_unknown() -> None:
    verdict = evaluate_temporal("temporary until 2026-09-08。另外有效期至2026-09-09。", _NOW)
    assert verdict == TemporalVerdict(False, True)


def test_repeated_identical_instant_is_not_a_conflict() -> None:
    verdict = evaluate_temporal("until 2026-09-08; see also the note repeating until 2026-09-08", _NOW)
    assert verdict == TemporalVerdict(True, False)


def test_marker_with_future_bound_is_parsed_but_not_expired() -> None:
    assert evaluate_temporal("valid until 2027-01-01", _NOW) == TemporalVerdict(False, False)


def test_date_without_marker_stays_unknown() -> None:
    assert evaluate_temporal("the meeting on 2026-09-08 was productive", _NOW) == TemporalVerdict(False, True)


def test_valid_from_alone_stays_unknown() -> None:
    # v1 has no not-yet-effective semantics: a start-only expression never demotes.
    assert evaluate_temporal("valid from 2026-10-01", _NOW) == TemporalVerdict(False, True)


def test_missing_now_fails_open() -> None:
    assert evaluate_temporal("until 2026-09-08", None) == TemporalVerdict(False, True)


# ------------------------------------------------------------ case row 11: exact boundary


def test_row11_reading_instant_equal_to_the_bound_is_expired() -> None:
    bound = _epoch(2026, 9, 8, 12, 0, 0)
    assert evaluate_temporal("expires 2026-09-08T12:00:00Z", bound) == TemporalVerdict(True, False)
    assert evaluate_temporal("expires 2026-09-08T12:00:00Z", bound - 1e-6) == TemporalVerdict(False, False)


# ------------------------------------------------------------ case rows on the serving surfaces


def test_row1_reported_shape_a_ranks_after_all_fresh_candidates(stack) -> None:
    """Reported shape A (reconstructed): a temporary-use constraint chunk with
    the explicit past expiry `until 2026-09-08` shares the ops vocabulary with
    the query; it must rank behind every candidate without an expired window
    yet stay in the pool (tier, not exclusion)."""
    expired = "user: use the shared ops runbook for the cutover, temporary constraint until 2026-09-08"
    fresh = "user: the shared ops runbook for the cutover lives in the wiki"
    _write(stack, _chunk("c-expired-a", expired, decay=1.0))
    _write(stack, _chunk("c-fresh", fresh, decay=0.9))

    result = _recall(stack, "ops runbook", _query_cues())

    by_id = {c.id: c for c in _chunk_candidates(result)}
    assert set(by_id) == {"c-expired-a", "c-fresh"}
    assert by_id["c-expired-a"].expired_window is True
    assert by_id["c-fresh"].expired_window is False
    assert _chunk_candidates(result)[-1].id == "c-expired-a"


def test_row2_reported_shape_b_ranks_after_all_fresh_candidates(stack) -> None:
    """Reported shape B (reconstructed): same shape, expiry `until 2026-09-09`."""
    expired = "user: use the shared ops runbook for the cutover, temporary constraint until 2026-09-09"
    fresh = "user: the shared ops runbook for the cutover lives in the wiki"
    _write(stack, _chunk("c-expired-b", expired, decay=1.0))
    _write(stack, _chunk("c-fresh", fresh, decay=0.9))

    result = _recall(stack, "ops runbook", _query_cues())

    by_id = {c.id: c for c in _chunk_candidates(result)}
    assert by_id["c-expired-b"].expired_window is True
    assert _chunk_candidates(result)[-1].id == "c-expired-b"


def test_row3_still_valid_old_decision_is_not_demoted(stack) -> None:
    """A still-valid old decision (explicit future bound) and an open-ended old
    chunk rank purely by the fused score among themselves; only the expired
    constraint drops behind the tier."""
    _write(stack, _chunk("c-expired", "user: ops runbook temporary constraint until 2026-09-08", decay=1.0))
    _write(stack, _chunk("c-still-valid", "user: ops runbook decision valid until 2027-01-01", decay=0.9))
    _write(
        stack,
        _chunk(
            "c-open-old",
            "user: ops runbook adoption decision from the early days",
            decay=0.8,
            ingested_at=1.0,
        ),
    )

    result = _recall(stack, "ops runbook", _query_cues())

    candidates = _chunk_candidates(result)
    by_id = {c.id: c for c in candidates}
    assert set(by_id) == {"c-expired", "c-still-valid", "c-open-old"}
    assert by_id["c-expired"].expired_window is True
    assert by_id["c-still-valid"].expired_window is False
    assert by_id["c-open-old"].expired_window is False
    non_expired = [c for c in candidates if not c.expired_window]
    assert [c.id for c in non_expired] == [
        c.id for c in sorted(non_expired, key=lambda c: (-c.score, c.kind, c.id))
    ]
    assert candidates[-1].id == "c-expired"


def test_row4_expired_chunk_is_served_when_it_is_the_sole_hit(stack) -> None:
    """Historical reachability: the pool's only relevant hit carries an expired
    window and is still admitted and returned (non-exclusion)."""
    recall = HybridRecall(
        candidates=[
            _pool_candidate(
                "c-hist", "user: ops cutover constraint until 2026-09-08", score=1.0, expired_window=True
            )
        ],
        vector_hits=1,
        graph_hits=0,
    )
    result = _assemble(stack, recall)
    assert [entry.id for entry in result.entries] == ["c-hist"]
    assert result.dropped_count == 0


def test_row4_expired_chunk_squeezed_out_by_top_k_counts_honestly(stack) -> None:
    """When top_k squeezes the expired candidate out, it is an honest counted
    drop — never silently excluded and never ranked above fresh candidates."""
    recall = HybridRecall(
        candidates=[
            _pool_candidate("c-a", "user: ops runbook fact one", score=2.0),
            _pool_candidate("c-b", "user: ops runbook fact two", score=1.5),
            _pool_candidate(
                "c-exp", "user: ops cutover constraint until 2026-09-08", score=3.0, expired_window=True
            ),
        ],
        vector_hits=3,
        graph_hits=0,
    )
    result = _assemble(stack, recall, AssembleConfig(top_k=2))
    assert [entry.id for entry in result.entries] == ["c-a", "c-b"]
    assert result.dropped_count == 1


def test_row7_quoted_expiry_chunk_keeps_its_normal_rank(stack) -> None:
    """A date inside a quoted banner span is reference existence, not a validity
    window: the chunk is not flagged and keeps its fused-score rank."""
    _write(
        stack,
        _chunk("c-quoted", 'user: the ops banner said "expires 2026-09-08" during the launch', decay=0.9),
    )
    result = _recall(stack, "ops banner", _query_cues())
    quoted = next(c for c in _chunk_candidates(result) if c.id == "c-quoted")
    assert quoted.expired_window is False


def test_row8_conflicting_expiry_chunk_keeps_its_normal_rank(stack) -> None:
    """Two different instants in one chunk conflict into unknown: fail-open,
    the chunk is not flagged and keeps its fused-score rank."""
    _write(
        stack,
        _chunk("c-conflict", "user: ops cutover until 2026-09-08，有效期至2026-09-09", decay=0.9),
    )
    result = _recall(stack, "ops cutover", _query_cues())
    conflict = next(c for c in _chunk_candidates(result) if c.id == "c-conflict")
    assert conflict.expired_window is False


def test_row10_age_controls_nothing(stack) -> None:
    """D04 age negative: two chunks with identical texts but different
    ingested_at and decay carry the same flag — with and without an expiry
    marker, age never touches the verdict."""
    plain = "user: the shared ops runbook is the source of truth"
    marked = "user: the shared ops runbook is the source of truth, temporary until 2026-09-08"
    _write(stack, _chunk("c-old-plain", plain, decay=0.9, ingested_at=1.0))
    _write(stack, _chunk("c-new-plain", plain, decay=0.5, ingested_at=_NOW - 86400))
    _write(stack, _chunk("c-old-marked", marked, decay=0.9, ingested_at=1.0))
    _write(stack, _chunk("c-new-marked", marked, decay=0.5, ingested_at=_NOW - 86400))

    result = _recall(stack, "ops runbook", _query_cues())

    flags = {c.id: c.expired_window for c in _chunk_candidates(result)}
    assert flags["c-old-plain"] is False
    assert flags["c-new-plain"] is False
    assert flags["c-old-marked"] is True
    assert flags["c-new-marked"] is True
