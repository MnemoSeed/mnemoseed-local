"""F1 -> F2/F3 -> buffer wiring: ScoringPipeline combines stripper, scorer,
score pool and the scored-turn buffer. Submit stays an O(1) append; scoring
and pooling run on the lazy drain side so /ingest stays untouched.

Verbatim contract (design/01 stage 4): EVERY conversational turn is kept —
the F2 durability verdict is an annotation on the ScoredTurn (plus stats
telemetry), never a persistence gate; the F2/F3 scoring only feeds the pool.
"""

from __future__ import annotations

from mnemoseed_local.capture.pipeline import InMemoryCapturePipeline, ScoringPipeline
from mnemoseed_local.capture.pool import PoolEventKind, ScorePool
from mnemoseed_local.capture.scorer import Durability, TurnScorer
from mnemoseed_local.schema.turn import HostId, Turn, TurnRole, TurnStep
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder

SESSION = "sess-score-p1"
PROFILE = "prof-main"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _turn(text: str, *, index: int = 0) -> Turn:
    return Turn(
        turn_index=index,
        session_id=SESSION,
        profile_id=PROFILE,
        host=HostId.GENERIC,
        started_at=0.0,
        steps=[TurnStep(role=TurnRole.USER, content=text)],
    )


def _pipeline(
    clock: _Clock | None = None,
    *,
    delegate: InMemoryCapturePipeline | None = None,
    pool: ScorePool | None = None,
) -> ScoringPipeline:
    return ScoringPipeline(
        delegate=delegate,
        scorer=TurnScorer(embedder=SyntheticEmbedder()),
        pool=pool if pool is not None else ScorePool(clock=clock if clock is not None else _Clock()),
    )


def test_submit_stays_o1_and_scoring_happens_on_drain() -> None:
    raw = InMemoryCapturePipeline()
    pipe = _pipeline(delegate=raw)
    text = "我 review 喜欢简洁"
    pipe.submit_turn(_turn(text))
    # submit only appended to the delegate buffer: raw copy intact, no scoring,
    # no pooling yet — /ingest hot path stays O(1)
    assert raw.turns(SESSION)[0].steps[0].content == text
    assert pipe.pool.stats(PROFILE) is None
    results = pipe.drain(SESSION)
    assert len(results) == 1
    assert results[0].turn.turn_index == 0
    assert results[0].importance > 0.0
    assert pipe.pool.stats(PROFILE) is not None
    assert pipe.pool.stats(PROFILE).turns_pooled == 1


def test_drain_keeps_every_turn_verbatim_and_annotates_durability() -> None:
    """The verbatim contract: a venting turn (DISPOSABLE annotation) and a
    preference turn (DURABLE annotation) are BOTH retained and BOTH credit the
    pool — the F2 verdict never gates persistence, it only annotates."""
    pipe = _pipeline()
    pipe.submit_turn(_turn("这 bug 烦死了", index=0))
    pipe.submit_turn(_turn("我 review 喜欢简洁", index=1))
    results = pipe.drain(SESSION)
    assert len(results) == 2
    assert [r.turn.turn_index for r in results] == [0, 1]
    assert results[0].durability.durability is Durability.DISPOSABLE
    assert results[1].durability.durability is Durability.DURABLE
    stats = pipe.stats
    assert stats.turns_in == 2
    assert stats.durable_flagged == 1
    assert stats.disposable_flagged == 1
    assert "venting-marker" in stats.disposable_reasons
    # both turns' S credit the pool: the scorer only feeds pool accumulation
    assert pipe.pool.stats(PROFILE).turns_pooled == 2


def test_drain_is_idempotent() -> None:
    pipe = _pipeline()
    pipe.submit_turn(_turn("我 review 喜欢简洁"))
    assert len(pipe.drain(SESSION)) == 1
    assert pipe.drain(SESSION) == []
    assert len(pipe.turns(SESSION)) == 1


def test_pool_accumulates_over_drain_and_fires_on_idle() -> None:
    clock = _Clock()
    events: list[object] = []
    pool = ScorePool(clock=clock, sink=lambda event: events.append(event))
    pipe = _pipeline(pool=pool)
    pipe.submit_turn(_turn("我 review 喜欢简洁", index=0))
    pipe.submit_turn(_turn("以后都用 pnpm 管理依赖", index=1))
    pipe.drain(SESSION)
    assert events == []  # busy: accumulated under one drain
    clock.advance(5.0)
    pipe.submit_turn(_turn("每次 code review 我都要简洁 别寒暄", index=2))
    pipe.drain(SESSION)
    assert len(events) == 1
    assert events[0].kind is PoolEventKind.DREAM_TRIGGER
    assert events[0].turn_range.start <= 2
    assert pipe.pool.stats(PROFILE).balance == 0.0
    assert pipe.stats.pool_triggers == 1


def test_stats_are_observable_and_cumulative() -> None:
    pipe = _pipeline()
    pipe.submit_turn(_turn("这 bug 烦死了", index=0))
    pipe.submit_turn(_turn("好的", index=1))
    results = pipe.drain(SESSION)
    # phatic/vented turns stay retained too (verbatim contract): their
    # DISPOSABLE verdicts surface as annotation telemetry only
    assert len(results) == 2
    stats = pipe.stats
    assert stats.turns_in == 2
    assert stats.durable_flagged == 0
    assert stats.disposable_flagged == 2
    assert sum(stats.disposable_reasons.values()) == 2
    assert pipe.pool.stats(PROFILE).turns_pooled == 2
    # compression telemetry survives (benchmark reads pipeline.stats)
    assert stats.bytes_in > 0
    assert stats.rules_hit == {}


def test_end_session_and_settled_pass_through() -> None:
    from mnemoseed_local.storage.ports import TurnRange

    pipe = _pipeline()
    pipe.end_session(SESSION, TurnRange(start=0, end=5))
    assert pipe.settled(SESSION) == TurnRange(start=0, end=5)


def test_sessions_exposed() -> None:
    pipe = _pipeline()
    pipe.submit_turn(_turn("我 review 喜欢简洁"))
    assert pipe.sessions() == (SESSION,)
