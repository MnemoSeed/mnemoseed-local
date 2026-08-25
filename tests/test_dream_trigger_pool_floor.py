"""Score-pool dream floor: S components -> pool -> scheduler (FR-1.4 / FR-2.1).

End-to-end over the REAL scorer and pool: every durable capture turn credits
its F3 importance (arousal / novelty / causal components, 0..10 scale) into the
per-profile ScorePool, which mirrors the balance into the MetaStore row; the
DreamScheduler reads that balance and fires the floor rule once the pool
reaches ``floor_pool_points`` AND the profile has been idle for
``idle_min_sec``. A fired dream consumes the pool (drain), so the same credits
never trigger twice — re-firing requires re-earned points over a fresh window.

The score components matter: an emotion-bearing turn carries a higher arousal
component and credits more points, so emotional arrivals push the floor faster
than low-arousal ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.capture.pool import ScorePool
from mnemoseed_local.capture.scorer import Durability, TurnScorer
from mnemoseed_local.config import Config, DreamConfig
from mnemoseed_local.dream import DreamScheduler, DreamTrigger, NullSnapshotter
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.schema.turn import HostId, Turn, TurnRole, TurnStep
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import (
    Capability,
    ChunkFilter,
    Page,
    PageResult,
    TurnRange,
)


class _Clock:
    """Deterministic injected clock shared by the pool and the scheduler."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeVector:
    """VectorStore-shaped double holding the written (unconsolidated) chunks."""

    def __init__(self) -> None:
        self.chunks: list[ChunkStamp] = []

    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    def list_chunks(self, filter: ChunkFilter, page: Page) -> PageResult[ChunkStamp]:
        del page
        items = [c for c in self.chunks if c.profile_id == filter.profile_id]
        if filter.consolidated is not None:
            items = [c for c in items if c.consolidated is filter.consolidated]
        if filter.turn_start is not None:
            items = [c for c in items if c.turn_start is not None and c.turn_start >= filter.turn_start]
        return PageResult(items=items, total=len(items), offset=0, limit=len(items))


class _Stores:
    """Minimal stores bundle: the real meta driver plus the fake vector."""

    def __init__(self, vector: _FakeVector, meta: SqliteMetaDriver) -> None:
        self.vector = vector
        self.meta = meta


def _config(**overrides) -> Config:
    return Config(dream=DreamConfig(**overrides))


def _chunk(profile: str, turn_index: int, ingested_at: float) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=f"{profile}-t{turn_index}",
        profile_id=profile,
        text=f"turn {turn_index}",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="model-x",
        cues=Cues(),
        provenance=Provenance(asserted_by="model-x", source="test"),
        ingested_at=ingested_at,
        turn_start=turn_index,
        turn_end=turn_index,
    )


def _capture_turn(
    scorer: TurnScorer,
    pool: ScorePool,
    vector: _FakeVector,
    clock: _Clock,
    text: str,
    profile: str,
    index: int,
):
    """Score one durable turn, credit the pool, and file its chunk — exactly
    the ScoringPipeline / WritingPipeline wiring (FR-1.6 / FR-1.9)."""
    turn = Turn(
        turn_index=index,
        session_id=f"s-{profile}",
        profile_id=profile,
        host=HostId.GENERIC,
        started_at=float(clock.t),
        steps=[TurnStep(role=TurnRole.USER, content=text)],
    )
    scored = scorer.score_turn(turn)
    assert scored.durability.durability is Durability.DURABLE, scored.durability.reasons
    pool.add_points(profile, scored.importance, TurnRange(index, index))
    vector.chunks.append(_chunk(profile, index, float(clock.t)))
    return scored


# ---------------------------------------------------------------- score components drive the floor


def test_low_arousal_events_trickle_the_pool_slowly(tmp_path: Path) -> None:
    """Emotion-free durable turns contribute less S per event, so the same
    number of events grows the pool slower and the floor fires later."""
    clock = _Clock()
    meta = SqliteMetaDriver(path=str(tmp_path / "meta.db"))
    # the pool itself stays inert here (huge idle window): the SCHEDULER is the
    # only trigger evaluator under test
    pool = ScorePool(clock=clock, backend=meta, idle_window_sec=1e9)
    vector = _FakeVector()
    scorer = TurnScorer(embedder=SyntheticEmbedder())
    # two emotionally flat durable turns (decision marker, no emotion word)
    _capture_turn(scorer, pool, vector, clock, "以后都用 pnpm 管理项目", "calm", 0)
    _capture_turn(scorer, pool, vector, clock, "以后都用 uv 管理项目", "calm", 1)
    # two arousal-bearing durable turns (喜欢 / 太棒)
    _capture_turn(scorer, pool, vector, clock, "我 review 喜欢简洁", "excited", 0)
    _capture_turn(scorer, pool, vector, clock, "这个方案太棒了 我决定采用", "excited", 1)
    calm_balance = meta.pool_state("calm").balance
    excited_balance = meta.pool_state("excited").balance
    assert calm_balance < excited_balance  # low arousal: less S per event
    clock.advance(1000.0)  # idle window elapses for both profiles
    scheduler = DreamScheduler(_Stores(vector, meta), _config(), clock=clock, drain=meta.pool_drain)
    assert scheduler.eligibility("calm") is None  # trickled slowly: still below the floor
    eligible = scheduler.eligibility("excited")
    assert eligible is not None  # the same event count reached the floor
    assert eligible.reason == "floor_idle"
    assert eligible.pool_points == pytest.approx(excited_balance)


def test_emotion_modulated_arrivals_push_the_floor_faster(tmp_path: Path) -> None:
    """The S arousal component rides the emotion cue: identical structure with
    an emotion word credits more per event, so fewer events clear the floor."""
    clock = _Clock()
    meta = SqliteMetaDriver(path=str(tmp_path / "meta.db"))
    pool = ScorePool(clock=clock, backend=meta, idle_window_sec=1e9)
    vector = _FakeVector()
    scorer = TurnScorer(embedder=SyntheticEmbedder())
    flat_texts = ["这个方案还行 我决定采用", "这个流程还行 我以后都用它"]
    vivid_texts = ["这个方案太棒了 我决定采用", "这个流程很兴奋 我以后都用它"]
    flat0 = _capture_turn(scorer, pool, vector, clock, flat_texts[0], "flat", 0)
    vivid0 = _capture_turn(scorer, pool, vector, clock, vivid_texts[0], "vivid", 0)
    _capture_turn(scorer, pool, vector, clock, flat_texts[1], "flat", 1)
    _capture_turn(scorer, pool, vector, clock, vivid_texts[1], "vivid", 1)
    # the emotion word is the only difference: arousal lifts S and the credit
    assert vivid0.components.arousal > flat0.components.arousal
    assert vivid0.importance > flat0.importance
    assert meta.pool_state("vivid").balance > meta.pool_state("flat").balance
    clock.advance(1000.0)
    scheduler = DreamScheduler(_Stores(vector, meta), _config(), clock=clock, drain=meta.pool_drain)
    # two events: the emotion-bearing pool cleared the floor, the flat one not
    assert scheduler.eligibility("vivid") is not None
    assert scheduler.eligibility("flat") is None
    # one more flat event earns the floor for the neutral profile too
    _capture_turn(scorer, pool, vector, clock, "那个方案还行 我决定采用", "flat", 2)
    clock.advance(1000.0)
    flat_eligible = scheduler.eligibility("flat")
    assert flat_eligible is not None
    assert flat_eligible.reason == "floor_idle"


# ---------------------------------------------------------------- fire / drain / re-earn cycle


def test_floor_fires_at_threshold_and_refires_only_after_reearning(tmp_path: Path) -> None:
    """The fired dream consumes the pool (drain), so the same points never
    re-trigger; re-firing requires the pool to earn toward the floor again,
    against the NEW pending window."""
    clock = _Clock()
    meta = SqliteMetaDriver(path=str(tmp_path / "meta.db"))
    pool = ScorePool(clock=clock, backend=meta)
    vector = _FakeVector()
    scorer = TurnScorer(embedder=SyntheticEmbedder())
    total = 0.0
    for index in range(2):
        scored = _capture_turn(scorer, pool, vector, clock, "我 review 喜欢简洁", "p", index)
        total += scored.importance
    assert total == pytest.approx(13.6)
    clock.advance(1000.0)
    trigger = DreamTrigger(snapshotter=NullSnapshotter(), auto_trigger=False)
    scheduler = DreamScheduler(
        _Stores(vector, meta), _config(), trigger=trigger, clock=clock, drain=pool.drain
    )
    emitted = scheduler.tick()
    assert len(emitted) == 1
    assert emitted[0].reason == "floor_idle"
    assert emitted[0].pool_points == pytest.approx(13.6)
    # the scheduler's emission drained the gauge into the lifetime ledger; the
    # merge marks the covered chunks consolidated (the watermark advance's
    # persisted effect on the vector side)
    state = meta.pool_state("p")
    assert state.balance == pytest.approx(0.0)
    assert state.filed_points_total == pytest.approx(total)
    for chunk in vector.chunks:
        if chunk.turn_start in (0, 1):
            chunk.consolidated = True
    # drained + consolidated: the same credits never re-trigger (deadline not
    # yet binding, and nothing is pending)
    assert scheduler.eligibility("p") is None
    assert scheduler.tick() == []
    # re-earned points over a NEW window re-arm the floor
    _capture_turn(scorer, pool, vector, clock, "这个方案太棒了 我决定采用", "p", 2)
    _capture_turn(scorer, pool, vector, clock, "这个流程很兴奋 我以后都用它", "p", 3)
    clock.advance(1000.0)
    re_emitted = scheduler.tick()
    assert len(re_emitted) == 1
    assert re_emitted[0].turn_range == TurnRange(2, 3)  # fresh window, never the old one
    assert trigger.status("p").pending_manual == 2
    # the second fire drained again: the lifetime ledger holds every point
    # exactly once and the gauge is empty
    state = meta.pool_state("p")
    assert state.balance == pytest.approx(0.0)
    assert state.filed_points_total > total
    # consumed again: the old window never re-arms the floor
    assert scheduler.eligibility("p") is None
    assert scheduler.tick() == []


def test_double_scheduler_fires_file_every_point_exactly_once(tmp_path: Path) -> None:
    """N scheduler-fired dreams leave the gauge empty and the lifetime ledger
    at exactly the credited sum: the same points never trigger twice, and the
    live capture pool cannot resurrect drained points on later credits."""
    clock = _Clock()
    meta = SqliteMetaDriver(path=str(tmp_path / "meta.db"))
    pool = ScorePool(clock=clock, backend=meta, idle_window_sec=1e9)
    vector = _FakeVector()
    scorer = TurnScorer(embedder=SyntheticEmbedder())
    trigger = DreamTrigger(snapshotter=NullSnapshotter(), auto_trigger=False)
    scheduler = DreamScheduler(
        _Stores(vector, meta), _config(), trigger=trigger, clock=clock, drain=pool.drain
    )
    total = 0.0
    for window in range(2):
        for index in range(2):
            scored = _capture_turn(scorer, pool, vector, clock, "我 review 喜欢简洁", "p", window * 2 + index)
            total += scored.importance
        clock.advance(1000.0)
        emitted = scheduler.tick()
        assert len(emitted) == 1
        # the dream commits: covered chunks leave the pending read
        for chunk in vector.chunks:
            chunk.consolidated = True

    state = meta.pool_state("p")
    assert state.balance == pytest.approx(0.0)
    assert state.filed_points_total == pytest.approx(total)
    # a third tick cannot re-fire the consumed points
    clock.advance(1000.0)
    assert scheduler.tick() == []


def test_restart_seeds_the_gauge_from_pending_not_lifetime(tmp_path: Path) -> None:
    """Restart oracle: boot seeds the trigger gauge from the persisted balance
    (the true pending amount) — after fired dreams the gauge boots at 0, never
    the lifetime total, and a mid-accrual pending amount survives."""
    meta = SqliteMetaDriver(path=str(tmp_path / "meta.db"))
    meta.pool_credit("p", 13.6, TurnRange(start=0, end=1))
    meta.advance_watermark("p", TurnRange(start=0, end=1))
    filed = meta.pool_drain("p", TurnRange(start=0, end=1))  # a dream fired
    assert filed == pytest.approx(13.6)
    meta.pool_credit("p", 4.2, TurnRange(start=2, end=3))  # mid-accrual pending

    # the daemon boot loop reads exactly this surface (stores.meta.pool_states())
    states = meta.pool_states()
    state = states["p"]
    assert state.balance == pytest.approx(4.2)  # the boot seed is pending-only
    assert state.filed_points_total == pytest.approx(13.6)  # lifetime kept aside
