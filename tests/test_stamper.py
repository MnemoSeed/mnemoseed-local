"""Stamp writer (M1-T4): FR-1.6 stamp assembly, FR-1.8 near-duplicate dual
branch, FR-1.9 importance_hint threading, peripheral_gaps transmission.

All behaviours go through StampWriter.write(ScoredTurn, WriteContext) against
an injected fake VectorStore / ScorePool / clock — no live storage, no models.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from mnemoseed_local.capture.pool import ScorePool
from mnemoseed_local.capture.scorer import (
    Durability,
    DurabilityResult,
    ScoreComponents,
    ScoredTurn,
    TurnScorer,
)
from mnemoseed_local.capture.stamper import (
    ConsistencyVerdict,
    NearDuplicateChecker,
    StampWriter,
    WriteContext,
    WriteOutcomeKind,
)
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, EmotionCue, Provenance
from mnemoseed_local.schema.turn import HostId, Turn, TurnRole, TurnStep
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import SparseVector, WeightUpdate

SESSION = "sess-stamp-1"
PROFILE = "prof-main"
ALICE = "prof-alice"
BOB = "prof-bob"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeVectorStore:
    """Scripted VectorStore: near_duplicate_ranked returns whatever the test
    plans, with similarities derived from the strong (0.9) vs band (0.85)
    scripting — mirroring the D5 contract (probes are profile-scoped, so
    scripted matches are returned only for the probing profile)."""

    def __init__(self) -> None:
        self.chunks: dict[str, ChunkStamp] = {}
        self.upserts: list[ChunkStamp] = []
        self.upsert_chunks_calls: int = 0
        self.script: dict[float, list[ChunkStamp]] = {}
        self.probe_thresholds: list[float] = []
        self.probe_profiles: list[str] = []
        self.weight_updates: dict[str, WeightUpdate] = {}
        self.reconcile_flags: set[str] = set()

    def near_duplicate(self, vector: Sequence[float], threshold: float, profile_id: str) -> list[ChunkStamp]:
        return [chunk for chunk, _ in self.near_duplicate_ranked(vector, threshold, profile_id)]

    def near_duplicate_ranked(
        self, vector: Sequence[float], threshold: float, profile_id: str
    ) -> list[tuple[ChunkStamp, float]]:
        self.probe_thresholds.append(threshold)
        self.probe_profiles.append(profile_id)
        strong_ids = {chunk.chunk_id for chunk in self.script.get(0.9, []) if chunk.profile_id == profile_id}
        band = [chunk for chunk in self.script.get(threshold, []) if chunk.profile_id == profile_id]
        return [(chunk, 0.95 if chunk.chunk_id in strong_ids else 0.85) for chunk in band]

    def upsert_chunk(
        self,
        chunk: ChunkStamp,
        dense: Sequence[float],
        sparse: SparseVector | None = None,
    ) -> None:
        self.chunks[chunk.chunk_id] = chunk
        self.upserts.append(chunk)

    def upsert_chunks(
        self, entries: Sequence[tuple[ChunkStamp, Sequence[float], SparseVector | None]]
    ) -> None:
        self.upsert_chunks_calls += 1
        for chunk, dense, sparse in entries:
            self.upsert_chunk(chunk, dense, sparse)

    def update_weights(self, updates: Sequence[WeightUpdate]) -> None:
        for update in updates:
            self.weight_updates[update.chunk_id] = update

    def update_chunk_state(
        self,
        chunk_ids: Sequence[str],
        hit_increment: int | None = None,
        needs_reconcile: bool | None = None,
    ) -> None:
        del hit_increment  # unused in this fake
        if needs_reconcile:
            self.reconcile_flags.update(chunk_ids)


def _existing(
    text: str, *, chunk_id: str = "chunk-old", decay: float = 0.7, profile_id: str = PROFILE
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile_id,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(project="demo"),
        provenance=Provenance(asserted_by="test-model", source="manual"),
        decay_weight=decay,
    )


def _turn(text: str, *, index: int = 0, profile: str = PROFILE) -> Turn:
    return Turn(
        turn_index=index,
        session_id=SESSION,
        profile_id=profile,
        host=HostId.GENERIC,
        model_id="claude-sonnet-5",
        started_at=0.0,
        steps=[TurnStep(role=TurnRole.USER, content=text)],
    )


def _scored(
    text: str,
    *,
    emotion: EmotionCue | None = None,
    importance: float = 5.0,
    profile: str = PROFILE,
) -> ScoredTurn:
    return ScoredTurn(
        turn=_turn(text, profile=profile),
        importance=importance,
        components=ScoreComponents(arousal=5.0, novelty=5.0, causal_chain=2.0),
        durability=DurabilityResult(
            durability=Durability.DURABLE,
            confidence=0.85,
            reasons=["pref-marker"],
        ),
        emotion=emotion,
        causal_reasons=[],
        features={},
    )


def _writer(store: _FakeVectorStore, clock: _Clock, pool: ScorePool | None = None) -> StampWriter:
    return StampWriter(store=store, embedder=SyntheticEmbedder(), clock=clock, pool=pool)


# ------------------------------------------------------------ stamp assembly


def test_writer_assembles_complete_stamp() -> None:
    store = _FakeVectorStore()
    clock = _Clock()
    ctx = WriteContext(
        profile_id=PROFILE,
        agent_label="soul-default",
        cognitive_tier=CognitiveTier.TIER_2,
        project="MnemoSeed",
        host="cursor",
        task="fix-ci",
        time_bucket="weekday-morning",
        entities=("pipelines",),
        tools_used=("bash", "pytest"),
    )
    scored = _scored("以后都用 pnpm", emotion=EmotionCue(valence=-0.2, arousal=0.5, peripheral_gaps=True))
    outcome = _writer(store, clock).write(scored, ctx)

    assert outcome.kind is WriteOutcomeKind.NEW_CHUNK
    stamp = store.chunks[outcome.chunk_id]
    assert stamp.chunk_id == outcome.chunk_id
    assert stamp.profile_id == PROFILE
    assert stamp.text == "user: 以后都用 pnpm"
    assert stamp.cognitive_tier is CognitiveTier.TIER_2
    assert stamp.model_id == "claude-sonnet-5"
    assert stamp.persona_id == "soul-default"
    assert stamp.cues.project == "MnemoSeed"
    assert stamp.cues.host == "cursor"
    assert stamp.cues.task == "fix-ci"
    assert stamp.cues.time_bucket == "weekday-morning"
    assert stamp.cues.entities == ["pipelines"]
    assert stamp.cues.tools_used == ["bash", "pytest"]
    assert stamp.cues.emotion == EmotionCue(valence=-0.2, arousal=0.5, peripheral_gaps=True)
    assert stamp.provenance.asserted_by == "claude-sonnet-5"
    assert stamp.provenance.session_id == SESSION
    assert stamp.provenance.source == "generic-chat"
    assert stamp.provenance.confidence == pytest.approx(0.85)
    assert stamp.provenance.history[0].action == "created"
    assert stamp.score == pytest.approx(5.0)
    assert stamp.decay_weight == pytest.approx(1.0)
    assert stamp.ingested_at == pytest.approx(1000.0)
    assert stamp.turn_start == 0
    assert stamp.turn_end == 0


def test_written_chunk_carries_the_turn_window() -> None:
    # The stamp-writer fills the turn bounds from the turn so purge_range can
    # later target a funnel-written chunk by its turn window.
    store = _FakeVectorStore()
    scored = _scored("以后都用 pnpm")
    later = ScoredTurn(
        turn=Turn(
            turn_index=42,
            session_id=SESSION,
            profile_id=PROFILE,
            host=HostId.GENERIC,
            model_id="claude-sonnet-5",
            started_at=0.0,
            steps=[TurnStep(role=TurnRole.USER, content="以后都用 pnpm")],
        ),
        importance=scored.importance,
        components=scored.components,
        durability=scored.durability,
        emotion=scored.emotion,
        causal_reasons=[],
        features={},
    )
    outcome = _writer(store, _Clock()).write(later, WriteContext(profile_id=PROFILE))
    stamp = store.chunks[outcome.chunk_id]
    assert stamp.turn_start == 42
    assert stamp.turn_end == 42


def test_stamp_text_joins_user_and_assistant_lines() -> None:
    store = _FakeVectorStore()
    turn = Turn(
        turn_index=0,
        session_id=SESSION,
        profile_id=PROFILE,
        host=HostId.GENERIC,
        model_id="claude-sonnet-5",
        started_at=0.0,
        steps=[
            TurnStep(role=TurnRole.USER, content="你喜欢什么风格"),
            TurnStep(role=TurnRole.ASSISTANT, content="简洁直接"),
        ],
    )
    scored = ScoredTurn(
        turn=turn,
        importance=4.0,
        components=ScoreComponents(arousal=3.0, novelty=4.0, causal_chain=2.0),
        durability=DurabilityResult(durability=Durability.DURABLE, confidence=0.8, reasons=["pref-marker"]),
        emotion=None,
        causal_reasons=[],
        features={},
    )
    outcome = _writer(store, _Clock()).write(scored, WriteContext(profile_id=PROFILE))
    assert store.chunks[outcome.chunk_id].text == "user: 你喜欢什么风格\nassistant: 简洁直接"


def test_stamp_text_excludes_tool_steps() -> None:
    """Verbatim contract with TOOL steps present: tool output is excluded at
    stamp assembly (never captured), only USER + ASSISTANT lines join the text.
    The tool NAMES travel separately as cues — Option C fills them, the text
    channel stays untouched."""
    store = _FakeVectorStore()
    turn = Turn(
        turn_index=0,
        session_id=SESSION,
        profile_id=PROFILE,
        host=HostId.GENERIC,
        model_id="claude-sonnet-5",
        started_at=0.0,
        steps=[
            TurnStep(role=TurnRole.USER, content="跑一遍测试"),
            TurnStep(role=TurnRole.TOOL, content="tool stdout here", tool_name="bash"),
            TurnStep(role=TurnRole.ASSISTANT, content="全绿"),
        ],
    )
    scored = ScoredTurn(
        turn=turn,
        importance=4.0,
        components=ScoreComponents(arousal=3.0, novelty=4.0, causal_chain=2.0),
        durability=DurabilityResult(durability=Durability.DURABLE, confidence=0.8, reasons=["pref-marker"]),
        emotion=None,
        causal_reasons=[],
        features={},
    )
    outcome = _writer(store, _Clock()).write(scored, WriteContext(profile_id=PROFILE, tools_used=("bash",)))
    stamp = store.chunks[outcome.chunk_id]
    assert stamp.text == "user: 跑一遍测试\nassistant: 全绿"
    assert "tool stdout here" not in stamp.text
    assert stamp.cues.tools_used == ["bash"]


def test_no_model_falls_back_to_user_asserted() -> None:
    store = _FakeVectorStore()
    scored = _scored("以后都用 pnpm")
    # _scored carries model_id; simulate a model-less turn explicitly
    stripped_model = ScoredTurn(
        turn=Turn(
            turn_index=0,
            session_id=SESSION,
            profile_id=PROFILE,
            host=HostId.GENERIC,
            started_at=0.0,
            steps=[TurnStep(role=TurnRole.USER, content="以后都用 pnpm")],
        ),
        importance=5.0,
        components=scored.components,
        durability=scored.durability,
        emotion=scored.emotion,
        causal_reasons=[],
        features={},
    )
    outcome = _writer(store, _Clock()).write(stripped_model, WriteContext(profile_id=PROFILE))
    stamp = store.chunks[outcome.chunk_id]
    assert stamp.model_id == "unknown"
    assert stamp.provenance.asserted_by == "user"


def test_importance_hint_reaches_score_on_written_chunk() -> None:
    store = _FakeVectorStore()
    scored = _scored("以后都用 pnpm", importance=10.0)
    outcome = _writer(store, _Clock()).write(scored, WriteContext(profile_id=PROFILE))
    assert store.chunks[outcome.chunk_id].score == pytest.approx(10.0)


# --------------------------------------------------------- origin attribution


def test_writer_stamps_origin_agent_without_touching_persona() -> None:
    """origin_agent rides its own inert stamp column verbatim; persona_id
    stays the soul carrier (agent_label only) — the two never merge."""
    store = _FakeVectorStore()
    ctx = WriteContext(profile_id=PROFILE, agent_label="soul-default", origin_agent="build")
    outcome = _writer(store, _Clock()).write(_scored("以后都用 pnpm"), ctx)
    stamp = store.chunks[outcome.chunk_id]
    assert stamp.origin_agent == "build"
    assert stamp.persona_id == "soul-default"


def test_writer_leaves_origin_agent_null_when_context_has_none() -> None:
    store = _FakeVectorStore()
    outcome = _writer(store, _Clock()).write(_scored("以后都用 pnpm"), WriteContext(profile_id=PROFILE))
    assert store.chunks[outcome.chunk_id].origin_agent is None


def test_origin_agent_alone_never_fills_persona_id() -> None:
    store = _FakeVectorStore()
    ctx = WriteContext(profile_id=PROFILE, origin_agent="build")
    outcome = _writer(store, _Clock()).write(_scored("以后都用 pnpm"), ctx)
    stamp = store.chunks[outcome.chunk_id]
    assert stamp.origin_agent == "build"
    assert stamp.persona_id is None


# ------------------------------------------------------- near-duplicate branch


def test_similar_consistent_turn_reinforces_in_place() -> None:
    store = _FakeVectorStore()
    existing = _existing("我以前用 tabs 写代码")
    store.script[0.9] = [existing]
    store.script[0.85] = [existing]
    outcome = _writer(store, _Clock()).write(
        _scored("我以前用 tabs 写代码"), WriteContext(profile_id=PROFILE)
    )

    assert outcome.kind is WriteOutcomeKind.REINFORCED
    assert outcome.chunk_id == "chunk-old"
    assert store.upserts == []  # no new chunk written
    update = store.weight_updates["chunk-old"]
    assert update.last_reinforced == pytest.approx(1000.0)
    assert update.decay_weight == pytest.approx(0.8)  # min(1.0, 0.7 + 0.1)
    assert update.reinforce_count is None


def test_reinforcement_bounces_decay_to_one_from_high_base() -> None:
    store = _FakeVectorStore()
    existing = _existing("我以前用 tabs 写代码", decay=0.99)
    store.script[0.9] = [existing]
    store.script[0.85] = [existing]
    _writer(store, _Clock()).write(_scored("我以前用 tabs 写代码"), WriteContext(profile_id=PROFILE))
    assert store.weight_updates["chunk-old"].decay_weight == pytest.approx(1.0)


def test_conflict_band_flags_reconcile_and_credits_prediction_error() -> None:
    store = _FakeVectorStore()
    clock = _Clock()
    pool = ScorePool(clock=clock)
    existing = _existing("我用 tabs 写代码")
    store.script[0.9] = []
    store.script[0.85] = [existing]
    outcome = _writer(store, clock, pool).write(
        _scored("以后都不用 tabs，统一用空格"),
        WriteContext(profile_id=PROFILE),
    )

    assert outcome.kind is WriteOutcomeKind.NEEDS_RECONCILE
    assert outcome.chunk_id == "chunk-old"
    assert "chunk-old" in store.reconcile_flags
    assert store.upserts == []
    assert pool.stats(PROFILE).points_added == pytest.approx(2.0)


def test_high_similarity_but_conflicting_content_flags_reconcile() -> None:
    store = _FakeVectorStore()
    existing = _existing("我用 tabs 写代码")
    store.script[0.9] = [existing]
    store.script[0.85] = [existing]
    outcome = _writer(store, _Clock()).write(
        _scored("以后都改用空格"),
        WriteContext(profile_id=PROFILE),
    )
    assert outcome.kind is WriteOutcomeKind.NEEDS_RECONCILE
    assert "chunk-old" in store.reconcile_flags
    assert store.upserts == []


def test_band_without_conflict_writes_fresh_chunk() -> None:
    store = _FakeVectorStore()
    store.script[0.9] = []
    store.script[0.85] = [_existing("我用 tabs 写代码")]
    outcome = _writer(store, _Clock()).write(_scored("我用 tabs 写代码"), WriteContext(profile_id=PROFILE))
    assert outcome.kind is WriteOutcomeKind.NEW_CHUNK
    assert len(store.upserts) == 1
    assert store.upserts[0].text == "user: 我用 tabs 写代码"


def test_no_near_duplicate_writes_new_chunk() -> None:
    store = _FakeVectorStore()
    outcome = _writer(store, _Clock()).write(_scored("我用 tabs 写代码"), WriteContext(profile_id=PROFILE))
    assert outcome.kind is WriteOutcomeKind.NEW_CHUNK
    assert len(store.upserts) == 1
    assert store.probe_thresholds == [0.85]  # B6 single probe at the conflict threshold


def test_write_many_batches_new_chunk_upserts_into_one_call() -> None:
    """B6 batch write: write_many collects every new-chunk and flushes them in a
    single upsert_chunks call — one store commit instead of N per-turn ones."""
    store = _FakeVectorStore()
    items = [(_scored(f"以后都用 pnpm{i}"), WriteContext(profile_id=PROFILE)) for i in range(3)]

    outcomes = _writer(store, _Clock()).write_many(items)

    assert [o.kind for o in outcomes] == [WriteOutcomeKind.NEW_CHUNK] * 3
    assert store.upsert_chunks_calls == 1
    assert len(store.upserts) == 3
    # one probe per turn (single ranked probe), all at the conflict threshold
    assert store.probe_thresholds == [0.85, 0.85, 0.85]


def test_write_many_stamps_batch_chunks_with_monotonic_ingested_at() -> None:
    """B6 batch write: one drain reads the clock once, but each new chunk's
    ingested_at is constructively monotonic (now + i * 1ms, items order) so the
    ms-quantized focal scan and the recent tail keep their ordering — the
    pre-B6 per-write clock semantics are reproduced as-is."""
    store = _FakeVectorStore()
    items = [(_scored(f"以后都用 pnpm{i}"), WriteContext(profile_id=PROFILE)) for i in range(3)]

    _writer(store, _Clock()).write_many(items)

    stamps = [store.upserts[i].ingested_at for i in range(3)]
    assert stamps == [1000.0, 1000.001, 1000.002]
    quantized = {round(stamp, 3) for stamp in stamps}
    assert quantized == {1000.0, 1000.001, 1000.002}, (
        "ms-quantized stamps must stay distinct: a smaller epsilon would collapse back to a tie"
    )


# ------------------------------------------------------------- consistency v1


def test_supersession_marker_flags_conflict() -> None:
    checker = NearDuplicateChecker()
    assert checker.check("以后都改用空格", "我用 tabs 写代码") is ConsistencyVerdict.CONFLICT
    assert checker.check("以后都不用 tabs，统一用空格", "我用 tabs 写代码") is ConsistencyVerdict.CONFLICT
    assert checker.check("I switched to vscode from vim", "i use vim") is ConsistencyVerdict.CONFLICT


def test_quoted_value_flip_flags_conflict() -> None:
    checker = NearDuplicateChecker()
    assert checker.check('"python": "3.12"', '"python": "3.11"') is ConsistencyVerdict.CONFLICT
    assert checker.check('"python": "3.12"', '"python": "3.12"') is ConsistencyVerdict.CONSISTENT


def test_unsure_cases_treated_as_consistent() -> None:
    checker = NearDuplicateChecker()
    # same value restated, no revoke/replace
    assert checker.check("以后都用空格", "项目统一用空格") is ConsistencyVerdict.CONSISTENT
    # value flip with no revoke/replace marker: conservative -> consistent
    assert checker.check("我周六去上海", "我周六去北京") is ConsistencyVerdict.CONSISTENT
    # old text does not affirm a practice
    assert checker.check("以后都改用空格", "项目今天有点忙") is ConsistencyVerdict.CONSISTENT


def test_plain_restatement_is_consistent() -> None:
    checker = NearDuplicateChecker()
    assert checker.check("我用 tabs 写代码", "我用 tabs 写代码") is ConsistencyVerdict.CONSISTENT


# ----------------------------------------------------------------- red lines


def test_high_arousal_durable_turn_writes_peripheral_gaps() -> None:
    store = _FakeVectorStore()
    scored = TurnScorer(embedder=SyntheticEmbedder()).score_turn(_turn("我崩溃了 以后都用 pnpm"))
    assert scored.emotion is not None
    assert scored.emotion.peripheral_gaps is True  # arousal >= gaps_arousal threshold
    outcome = _writer(store, _Clock()).write(scored, WriteContext(profile_id=PROFILE))
    written = store.chunks[outcome.chunk_id]
    assert written.cues.emotion is not None
    assert written.cues.emotion.peripheral_gaps is True


def test_low_arousal_turn_writes_peripheral_gaps_false() -> None:
    store = _FakeVectorStore()
    scored = TurnScorer(embedder=SyntheticEmbedder()).score_turn(_turn("我 review 喜欢简洁"))
    assert scored.emotion is not None
    assert scored.emotion.peripheral_gaps is False
    outcome = _writer(store, _Clock()).write(scored, WriteContext(profile_id=PROFILE))
    written = store.chunks[outcome.chunk_id]
    assert written.cues.emotion is not None
    assert written.cues.emotion.peripheral_gaps is False


# ------------------------------------------------------ D5 profile isolation


def test_cross_profile_identical_text_never_reinforces_other_profile() -> None:
    store = _FakeVectorStore()
    alice = _existing("以前用 tabs 写代码", chunk_id="chunk-alice", profile_id=ALICE)
    store.script[0.9] = [alice]
    store.script[0.85] = [alice]
    # BOB posts the identical text; his profile has no prior content, so the
    # near-duplicate probe must not scan ALICE's chunks
    outcome = _writer(store, _Clock()).write(
        _scored("以前用 tabs 写代码", profile=BOB), WriteContext(profile_id=BOB)
    )
    assert outcome.kind is WriteOutcomeKind.NEW_CHUNK
    assert outcome.chunk_id != "chunk-alice"
    assert store.chunks[outcome.chunk_id].profile_id == BOB
    assert store.probe_profiles == [BOB]
    assert "chunk-alice" not in store.weight_updates


def test_cross_profile_conflict_never_flags_other_profile_chunk() -> None:
    store = _FakeVectorStore()
    pool = ScorePool(clock=_Clock())
    alice = _existing("我用 tabs 写代码", chunk_id="chunk-alice", profile_id=ALICE)
    store.script[0.9] = []
    store.script[0.85] = [alice]
    # BOB's conflicting text revokes ALICE's practice, but the conflict must
    # stay inside BOB's profile band: no reconcile flag on ALICE's chunk and
    # no prediction-error credit to anyone
    outcome = _writer(store, _Clock(), pool).write(
        _scored("以后都不用 tabs，统一用空格", profile=BOB), WriteContext(profile_id=BOB)
    )
    assert outcome.kind is WriteOutcomeKind.NEW_CHUNK
    assert outcome.chunk_id != "chunk-alice"
    assert "chunk-alice" not in store.reconcile_flags
    assert pool.stats(BOB) is None
