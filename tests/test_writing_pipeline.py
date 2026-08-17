"""WritingPipeline wiring (FR-1.6/FR-1.8/FR-1.9): durable scored turns are
written to the VectorStore drain-side; submit stays an O(1) append through the
inner ScoringPipeline. Outcomes land in stats.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import pytest

from mnemoseed_local.capture.pipeline import ScoringPipeline, WritingPipeline
from mnemoseed_local.capture.pool import ScorePool
from mnemoseed_local.capture.scorer import TurnScorer
from mnemoseed_local.capture.stamper import StampWriter, WriteContext, WriteOutcomeKind
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.schema.turn import HostId, Turn, TurnRole, TurnStep
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import SparseVector, WeightUpdate

SESSION = "sess-write-1"
SESSION_B = "sess-write-2"
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
    """Scripted VectorStore: near_duplicate returns whatever the test plans.

    ``near_duplicate`` mirrors the D5-contract: the probe is profile-scoped, so
    scripted matches are returned only for the probing profile (cross-profile
    contamination would surface as a missing NEW_CHUNK / wrong outcome).
    """

    def __init__(self) -> None:
        self.chunks: dict[str, ChunkStamp] = {}
        self.upserts: list[ChunkStamp] = []
        self.script: dict[float, list[ChunkStamp]] = {}
        self.probe_thresholds: list[float] = []
        self.probe_profiles: list[str] = []
        self.weight_updates: dict[str, WeightUpdate] = {}
        self.reconcile_flags: set[str] = set()

    def near_duplicate(self, vector: Sequence[float], threshold: float, profile_id: str) -> list[ChunkStamp]:
        self.probe_thresholds.append(threshold)
        self.probe_profiles.append(profile_id)
        return [chunk for chunk in self.script.get(threshold, []) if chunk.profile_id == profile_id]

    def upsert_chunk(
        self,
        chunk: ChunkStamp,
        dense: Sequence[float],
        sparse: SparseVector | None = None,
    ) -> None:
        self.chunks[chunk.chunk_id] = chunk
        self.upserts.append(chunk)

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


def _turn(
    text: str, *, index: int = 0, hint: float | None = None, profile: str = PROFILE, session: str = SESSION
) -> Turn:
    return Turn(
        turn_index=index,
        session_id=session,
        profile_id=profile,
        host=HostId.GENERIC,
        model_id="claude-sonnet-5",
        started_at=0.0,
        importance_hint=hint,
        steps=[TurnStep(role=TurnRole.USER, content=text)],
    )


def _writing_pipeline(
    store: _FakeVectorStore,
    clock: _Clock,
    *,
    pool: ScorePool | None = None,
    context=None,
    writer: StampWriter | None = None,
) -> WritingPipeline:
    inner = ScoringPipeline(
        scorer=TurnScorer(embedder=SyntheticEmbedder()),
        pool=pool if pool is not None else ScorePool(clock=clock),
    )
    if writer is None:
        writer = StampWriter(store=store, embedder=SyntheticEmbedder(), clock=clock, pool=inner.pool)
    return WritingPipeline(store, inner, writer=writer, context=context, clock=clock)


def test_submit_stays_o1_write_happens_on_drain() -> None:
    store = _FakeVectorStore()
    pipe = _writing_pipeline(store, _Clock())
    pipe.submit_turn(_turn("我 review 喜欢简洁"))
    assert store.upserts == []  # nothing written on the HTTP path
    outcomes = pipe.drain(SESSION)
    assert len(outcomes) == 1
    assert outcomes[0].kind is WriteOutcomeKind.NEW_CHUNK
    assert len(store.upserts) == 1


def test_default_clock_stamps_are_epoch_domain() -> None:
    """D3 (live-drain finding): persisted stamp timestamps — ``ingested_at``,
    ``provenance.asserted_at``, provenance event times — are epoch fields: the
    decay sweeper, ingest-time windows and provenance audit all read epochs.
    A monotonic default clock mixes domains: the chunk's baseline lands at
    ~boot-uptime seconds, which the next decay sweep misreads as ~57 years of
    staleness and silently zeroes (observed live: capture -> boot sweep ->
    decay_weight 1.0 -> 0.0).
    """
    store = _FakeVectorStore()
    inner = ScoringPipeline(
        scorer=TurnScorer(embedder=SyntheticEmbedder()), pool=ScorePool(clock=time.monotonic)
    )
    # NO clock injection — the production serving shape (daemon wiring).
    pipe = WritingPipeline(store, inner)
    pipe.submit_turn(_turn("我 review 喜欢简洁"))

    assert [o.kind for o in pipe.drain(SESSION)] == [WriteOutcomeKind.NEW_CHUNK]

    stamp = store.upserts[0]
    assert abs(stamp.ingested_at - time.time()) < 300
    assert abs(stamp.provenance.asserted_at - time.time()) < 300
    assert abs(stamp.provenance.history[0].at - time.time()) < 300


def test_stats_track_outcome_kinds() -> None:
    store = _FakeVectorStore()
    pipe = _writing_pipeline(
        store,
        _Clock(),
        context=lambda turn: WriteContext(
            profile_id=turn.profile_id, project="MnemoSeed", agent_label="soul-x"
        ),
    )
    pipe.submit_turn(_turn("我 review 喜欢简洁"))  # no prior content -> NEW_CHUNK
    # the fake probes the script at drain time, so the no-hit turn drains first,
    # then the script drives the reinforcing turn's probe
    assert [o.kind for o in pipe.drain(SESSION)] == [WriteOutcomeKind.NEW_CHUNK]

    store.script[0.9] = [_existing("以前也说过喜欢简洁")]
    store.script[0.85] = [_existing("以前也说过喜欢简洁")]
    pipe.submit_turn(_turn("以后都用 pnpm"))  # consistent strong hit -> REINFORCED
    outcomes = pipe.drain(SESSION)
    assert [o.kind for o in outcomes] == [WriteOutcomeKind.REINFORCED]

    assert pipe.stats.turns_written == 2
    assert pipe.stats.new_chunks == 1
    assert pipe.stats.reinforced == 1
    assert pipe.stats.needs_reconcile == 0
    # the write context reached the stamp writer
    chunk = store.upserts[0]
    assert chunk.cues.project == "MnemoSeed"
    assert chunk.persona_id == "soul-x"


def test_stats_track_reconcile_outcome() -> None:
    store = _FakeVectorStore()
    store.script[0.9] = []
    store.script[0.85] = [_existing("我用 tabs 写代码")]
    pipe = _writing_pipeline(store, _Clock())
    pipe.submit_turn(_turn("以后都改用空格"))
    outcomes = pipe.drain(SESSION)
    assert outcomes[0].kind is WriteOutcomeKind.NEEDS_RECONCILE
    assert pipe.stats.needs_reconcile == 1
    assert "chunk-old" in store.reconcile_flags


def test_end_session_and_settled_pass_through() -> None:
    from mnemoseed_local.storage.ports import TurnRange

    store = _FakeVectorStore()
    pipe = _writing_pipeline(store, _Clock())
    pipe.end_session(SESSION, TurnRange(start=0, end=2))
    assert pipe.settled(SESSION) == TurnRange(start=0, end=2)


def test_sessions_exposed() -> None:
    store = _FakeVectorStore()
    pipe = _writing_pipeline(store, _Clock())
    pipe.submit_turn(_turn("我 review 喜欢简洁"))
    assert pipe.sessions() == (SESSION,)


def test_cross_profile_identical_text_writes_fresh_chunk_for_second_profile() -> None:
    store = _FakeVectorStore()
    alice = _existing("以后都用 pnpm", chunk_id="chunk-alice", profile_id=ALICE)
    store.script[0.9] = [alice]
    store.script[0.85] = [alice]
    pipe = _writing_pipeline(store, _Clock())

    # ALICE's own re-assertion reinforces her chunk (her probe is scoped to her)
    pipe.submit_turn(_turn("以后都用 pnpm", profile=ALICE))
    assert [o.kind for o in pipe.drain(SESSION)] == [WriteOutcomeKind.REINFORCED]

    # BOB posts the same text; his profile has no prior content -> NEW_CHUNK
    # with his own chunk persisted, ALICE's chunk untouched
    pipe.submit_turn(_turn("以后都用 pnpm", profile=BOB, session=SESSION_B))
    bob_outcomes = pipe.drain(SESSION_B)
    assert [o.kind for o in bob_outcomes] == [WriteOutcomeKind.NEW_CHUNK]
    assert store.upserts[-1].profile_id == BOB
    assert store.probe_profiles == [ALICE, ALICE, BOB, BOB]
    # the only weight update is ALICE's own reinforcement; BOB's write settled
    # into his own fresh chunk without touching ALICE's decay/last_reinforced
    assert list(store.weight_updates) == ["chunk-alice"]
    assert store.weight_updates["chunk-alice"].decay_weight == pytest.approx(0.8)
