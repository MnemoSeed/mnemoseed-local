"""Dream schedule trigger rules (FR-2.1 / FR-2.4 trigger side, local trim).

The schedule is score-pool based (design/01 + PRD-02): every durable capture
turn credits its S importance into the profile's ScorePool, which mirrors the
balance into the per-profile MetaStore row; the scheduler reads that balance
through ``MetaStore.pool_state`` on every tick:

- floor+idle: a profile is eligible when its pool balance >=
  ``floor_pool_points`` (10.0) AND the profile has been idle (no capture
  activity) for >= ``idle_min_sec`` (900s);
- hard deadline: a profile is forced once the OLDEST pending verbatim chunk
  has waited >= ``hard_deadline_sec`` (default 24h); skipped when nothing is
  pending.

A fired dream consumes the pool (drain): the persisted balance resets to 0, so
the same points never trigger twice — re-firing requires the pool to earn
toward the floor again. The hard deadline is the post-drain backstop.

Both rules are CONFIG keys ([dream] table, configwrite registry) and are
re-read on every tick, so a ``config set`` hot-applies to the next tick.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind, ScorePool
from mnemoseed_local.config import Config, ConfigError, DreamConfig, load_config
from mnemoseed_local.configwrite.service import CONFIG_KEY_REGISTRY, ConfigWriteService
from mnemoseed_local.dream import (
    DREAM_RETRY_BASE_S,
    DREAM_RETRY_CAP_S,
    DREAM_RETRY_MAX,
    DREAM_RETRY_MULT,
    DreamEligibility,
    DreamScheduler,
)
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import (
    AuditEntry,
    Capability,
    ChunkFilter,
    Page,
    PageResult,
    PoolState,
    TurnRange,
)

# ---------------------------------------------------------------- fakes


class _FakeVector:
    """VectorStore-shaped double: list_chunks over an in-memory chunk list."""

    def __init__(self, chunks: list[ChunkStamp] | None = None) -> None:
        self.chunks = list(chunks or [])

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


class _FakeMeta:
    """MetaStore-shaped double that is ALSO the pool's backend seam — exactly
    the daemon wiring, where stores.meta binds both roles."""

    def __init__(
        self,
        profiles: Sequence[str] = (),
        balances: dict[str, float] | None = None,
    ) -> None:
        self.profiles = list(profiles)
        self._states: dict[str, PoolState] = {}
        for profile in self.profiles:
            self._states[profile] = PoolState(balance=float(balances.get(profile, 0.0)) if balances else 0.0)
        self.audit: list[AuditEntry] = []

    def list_profiles(self) -> list[object]:
        return [type("P", (), {"profile_id": p})() for p in self.profiles]

    # ---- PoolBackend seam (mirrors SqliteMetaDriver semantics)

    def pool_add(self, profile_id: str, points: float, turn_range: TurnRange) -> None:
        del turn_range
        state = self._states.get(profile_id, PoolState(balance=0.0))
        self._states[profile_id] = PoolState(balance=state.balance + points)

    def pool_credit(self, profile_id: str, balance: float, turn_range: TurnRange) -> None:
        self._states[profile_id] = PoolState(balance=balance, watermark=turn_range)

    # ---- MetaStore read seams

    def pool_state(self, profile_id: str) -> PoolState:
        return self._states.get(profile_id, PoolState(balance=0.0))

    def pool_states(self) -> dict[str, PoolState]:
        return dict(self._states)

    def audit_append(self, entry: AuditEntry) -> None:
        self.audit.append(entry)


class _RecordingSeam:
    """DreamTrigger-shaped recording sink for scheduler emissions."""

    def __init__(self, sink: list[PoolEvent]) -> None:
        self.sink = sink

    def handle_event(self, event: PoolEvent) -> None:
        self.sink.append(event)


class _FakeStores:
    def __init__(
        self,
        chunks: list[ChunkStamp] | None = None,
        profiles: Sequence[str] = (),
        balances: dict[str, float] | None = None,
        meta: _FakeMeta | None = None,
    ) -> None:
        self.vector = _FakeVector(chunks)
        self.meta = meta if meta is not None else _FakeMeta(profiles, balances)


class _Clock:
    """Deterministic injected clock (mutable, for the live pool + scheduler)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ---------------------------------------------------------------- fixtures


def _chunk(
    profile: str,
    *,
    turn: tuple[int, int],
    ingested_at: float,
    consolidated: bool = False,
) -> ChunkStamp:
    start, end = turn
    return ChunkStamp(
        chunk_id=f"{profile}-t{start}-{end}",
        profile_id=profile,
        text=f"turn {start}",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="model-x",
        cues=Cues(),
        provenance=Provenance(asserted_by="model-x", source="test"),
        ingested_at=ingested_at,
        turn_start=start,
        turn_end=end,
        consolidated=consolidated,
    )


def _config(**dream_overrides) -> Config:
    return Config(dream=DreamConfig(**dream_overrides))


def _eligible(scheduler: DreamScheduler, profile: str = "p") -> DreamEligibility | None:
    return scheduler.eligibility(profile)


# ---------------------------------------------------------------- floor + idle rule


def test_below_floor_is_not_eligible() -> None:
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(9)]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 9.0}),
        _config(hard_deadline_sec=1e9),  # deadline disabled: only the floor can fire
        clock=lambda: 100_000.0,  # idle huge, but the pool is below the floor
    )
    assert _eligible(scheduler) is None


def test_floor_met_but_not_idle_is_not_eligible() -> None:
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 12.0}),
        _config(),
        clock=lambda: 500.0,  # last activity at 9.0 -> idle 491s < 900
    )
    assert _eligible(scheduler) is None


def test_floor_and_idle_met_is_eligible() -> None:
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 12.0}),
        _config(),
        clock=lambda: 10_000.0,
    )
    eligible = _eligible(scheduler)
    assert eligible is not None
    assert eligible.reason == "floor_idle"
    assert eligible.pool_points == pytest.approx(12.0)
    assert eligible.turn_range == TurnRange(0, 9)


def test_floor_counts_pool_points_not_chunks() -> None:
    """The floor is points-based: two high-value turns can reach it while many
    low-value turns cannot. Turn/window counts never enter the rule."""
    chunks = [
        _chunk("p", turn=(0, 0), ingested_at=1.0),
        _chunk("p", turn=(1, 1), ingested_at=2.0),
    ]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 12.0}),
        _config(hard_deadline_sec=1e9),
        clock=lambda: 100_000.0,
    )
    eligible = _eligible(scheduler)
    assert eligible is not None
    assert eligible.reason == "floor_idle"
    assert eligible.pool_points == pytest.approx(12.0)
    assert eligible.turn_range == TurnRange(0, 1)


def test_consolidated_chunks_do_not_count_as_pending() -> None:
    pending = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    old = [_chunk("p", turn=(100 + i, 100 + i), ingested_at=float(i), consolidated=True) for i in range(50)]
    scheduler = DreamScheduler(
        _FakeStores([*pending, *old], profiles=["p"], balances={"p": 12.0}),
        _config(),
        clock=lambda: 10_000.0,
    )
    eligible = _eligible(scheduler)
    assert eligible is not None
    assert eligible.pool_points == pytest.approx(12.0)
    assert eligible.turn_range == TurnRange(0, 9)


def test_idle_measures_from_latest_capture_activity() -> None:
    # latest activity is a CONSOLIDATED chunk at t=9500; pending chunks are old
    pending = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    recent = [_chunk("p", turn=(50, 50), ingested_at=9_500.0, consolidated=True)]
    scheduler = DreamScheduler(
        _FakeStores([*pending, *recent], profiles=["p"], balances={"p": 12.0}),
        _config(),
        clock=lambda: 10_000.0,
    )
    assert _eligible(scheduler) is None  # idle = 500s < 900


def test_no_pending_chunks_is_never_eligible() -> None:
    scheduler = DreamScheduler(
        _FakeStores([], profiles=["p"], balances={"p": 12.0}),
        _config(),
        clock=lambda: 1e9,
    )
    assert _eligible(scheduler) is None


def test_drained_pool_never_re_triggers_the_floor() -> None:
    """A dream consumed the pool: balance 0 means the same credits can never
    re-trigger the floor, regardless of idle or pending chunks."""
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 0.0}),
        _config(hard_deadline_sec=1e9),
        clock=lambda: 100_000.0,
    )
    assert _eligible(scheduler) is None


# ---------------------------------------------------------------- hard deadline rule


def test_hard_deadline_forces_below_floor() -> None:
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(3)]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 3.0}),
        _config(),
        clock=lambda: 100_000.0,  # oldest chunk waited ~100k s > 24h
    )
    eligible = _eligible(scheduler)
    assert eligible is not None
    assert eligible.reason == "hard_deadline"
    assert eligible.pool_points == pytest.approx(3.0)
    assert eligible.turn_range == TurnRange(0, 2)


def test_hard_deadline_forces_even_when_idle_below_window() -> None:
    chunks = [_chunk("p", turn=(0, 0), ingested_at=0.0)]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 3.0}),
        _config(),
        clock=lambda: 90_000.0,  # 25h since the only pending chunk
    )
    eligible = _eligible(scheduler)
    assert eligible is not None
    assert eligible.reason == "hard_deadline"
    assert eligible.first_chunk_at == 0.0


def test_hard_deadline_counts_from_oldest_pending_chunk() -> None:
    # oldest pending chunk is ~23.9h old (just under the 24h deadline): not due
    chunks = [
        _chunk("p", turn=(0, 0), ingested_at=100.0),
        _chunk("p", turn=(1, 1), ingested_at=90_000.0),
    ]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 3.0}),
        _config(),
        clock=lambda: 100.0 + 86_399.0,  # oldest waited 86_399s < 86400
    )
    assert _eligible(scheduler) is None
    # one second later the deadline binds, regardless of the fresh chunk
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 3.0}),
        _config(),
        clock=lambda: 100.0 + 86_401.0,
    )
    eligible = _eligible(scheduler)
    assert eligible is not None
    assert eligible.reason == "hard_deadline"


def test_deadline_not_hit_within_window() -> None:
    chunks = [_chunk("p", turn=(0, 0), ingested_at=100.0)]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 3.0}),
        _config(),
        clock=lambda: 100.0 + 86_399.0,  # just under 24h
    )
    assert _eligible(scheduler) is None


def test_deadline_skip_when_zero_pending() -> None:
    # every chunk consolidated: zero pending, the deadline is skipped
    chunks = [_chunk("p", turn=(0, 0), ingested_at=0.0, consolidated=True)]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 0.0}),
        _config(),
        clock=lambda: 1_000_000.0,
    )
    assert _eligible(scheduler) is None


def test_drained_pool_still_allows_hard_deadline() -> None:
    """A dream consumed the pool (balance 0), but stale unconsolidated chunks
    still hit the 24h deadline: the floor respects the drain, the deadline is
    the backstop that does not."""
    chunks = [_chunk("p", turn=(0, 0), ingested_at=0.0)]
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 0.0}),
        _config(),
        clock=lambda: 90_000.0,
    )
    eligible = _eligible(scheduler)
    assert eligible is not None
    assert eligible.reason == "hard_deadline"
    assert eligible.pool_points == pytest.approx(0.0)


# ---------------------------------------------------------------- config keys (hot-apply)


def test_schedule_keys_are_registry_keys() -> None:
    for key in ("dream.floor_pool_points", "dream.idle_min_sec", "dream.hard_deadline_sec"):
        assert key in CONFIG_KEY_REGISTRY


def test_schedule_keys_parse_and_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.dream.floor_pool_points == 10.0
    assert cfg.dream.idle_min_sec == 900.0
    assert cfg.dream.hard_deadline_sec == 86400.0


def test_schedule_keys_parse_from_file(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n'
        "[dream]\n"
        "floor_pool_points = 3.5\n"
        "idle_min_sec = 60.0\n"
        "hard_deadline_sec = 7200.0\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.dream.floor_pool_points == 3.5
    assert cfg.dream.idle_min_sec == 60.0
    assert cfg.dream.hard_deadline_sec == 7200.0


def test_schedule_keys_bad_values_name_the_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    for body, key in (
        ("[dream]\nfloor_pool_points = 0\n", "dream.floor_pool_points"),
        ('[dream]\nfloor_pool_points = "ten"\n', "dream.floor_pool_points"),
        ("[dream]\nfloor_pool_points = -1\n", "dream.floor_pool_points"),
        ('[dream]\nidle_min_sec = "soon"\n', "dream.idle_min_sec"),
        ("[dream]\nhard_deadline_sec = -1\n", "dream.hard_deadline_sec"),
    ):
        path = tmp_path / "config.toml"
        path.write_text('preset = "embedded"\n' + body, encoding="utf-8")
        with pytest.raises(ConfigError, match=rf"config\[{key}\]"):
            load_config(path)


def test_configwrite_set_schedule_keys_live_apply_and_patch(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('preset = "embedded"\n[dream]\n', encoding="utf-8")
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1.0)
    result = service.set("dream.floor_pool_points", 4.0, actor="cli")
    assert result["ok"] is True
    assert result["restart_required"] is False  # hot-apply
    service.set("dream.idle_min_sec", 300.0, actor="cli")
    service.set("dream.hard_deadline_sec", 43200.0, actor="cli")
    assert service._config.dream.floor_pool_points == 4.0
    assert service._config.dream.idle_min_sec == 300.0
    assert service._config.dream.hard_deadline_sec == 43200.0
    assert load_config(path).dream.floor_pool_points == 4.0
    assert load_config(path).dream.idle_min_sec == 300.0
    assert load_config(path).dream.hard_deadline_sec == 43200.0


def test_scheduler_re_reads_config_each_tick() -> None:
    """Hot-apply: a configwrite change alters the NEXT tick's decision."""
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(3)]
    stores = _FakeStores(chunks, profiles=["p"], balances={"p": 3.5})
    config = Config()  # defaults: floor 10.0 -> not eligible
    scheduler = DreamScheduler(stores, config, clock=lambda: 100_000.0)
    # deadline disabled by the hot-apply step below; first check the floor only
    config.dream = DreamConfig(hard_deadline_sec=1e9)
    assert _eligible(scheduler) is None
    # hot-apply via the config object (the registry mutates the same object)
    config.dream = DreamConfig(floor_pool_points=3.0, idle_min_sec=0.0, hard_deadline_sec=1e9)
    eligible = _eligible(scheduler)
    assert eligible is not None
    assert eligible.reason == "floor_idle"


# ---------------------------------------------------------------- tick emission


def test_tick_emits_pending_manual_events_and_dedups_window() -> None:
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    stores = _FakeStores(chunks, profiles=["p"], balances={"p": 12.0})
    scheduler = DreamScheduler(stores, Config(), clock=lambda: 10_000.0)
    emitted = scheduler.tick()
    assert len(emitted) == 1
    assert emitted[0].reason == "floor_idle"
    # same window: no duplicate emission
    assert scheduler.tick() == []
    # a new pending turn changes the window -> a fresh emission
    stores.vector.chunks.append(_chunk("p", turn=(10, 10), ingested_at=9_000.0))
    emitted_again = scheduler.tick()
    assert len(emitted_again) == 1
    assert emitted_again[0].turn_range == TurnRange(0, 10)


def test_tick_hands_events_to_trigger_when_bound() -> None:
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    from mnemoseed_local.dream import DreamTrigger, NullSnapshotter

    trigger = DreamTrigger(snapshotter=NullSnapshotter(), auto_trigger=False)
    scheduler = DreamScheduler(
        _FakeStores(chunks, profiles=["p"], balances={"p": 12.0}),
        Config(),
        trigger=trigger,
        clock=lambda: 10_000.0,
    )
    scheduler.tick()
    status = trigger.status("p")
    assert status.pending_manual == 1
    assert status.last_event is not None
    assert status.last_event.kind is PoolEventKind.DREAM_TRIGGER
    assert status.last_event.turn_range == TurnRange(0, 9)
    assert status.last_event.balance == pytest.approx(12.0)  # carries the pool points


# ---------------------------------------------------------------- live-daemon thresholds


def test_pool_self_fire_uses_config_idle_window_and_drain_stops_double_service() -> None:
    """Live-daemon string: the capture pool is constructed from the config keys
    (dream_threshold <- floor_pool_points, idle_window_sec <- idle_min_sec), so
    it self-fires only at the configured 900s idle window — never at a fixed 5s
    — and the drain means the scheduler never double-services the same window."""
    config = _config()  # floor 10.0, idle 900.0
    clock = _Clock(start=1000.0)
    meta = _FakeMeta(profiles=["p"])
    sink_events: list[PoolEvent] = []
    pool = ScorePool(
        clock=clock,
        backend=meta,
        sink=sink_events.append,
        dream_threshold=config.dream.floor_pool_points,
        idle_window_sec=config.dream.idle_min_sec,
    )
    # reach the floor while the profile stays busier than the configured window
    for index in range(3):
        clock.advance(200.0)  # idle since the last credit: 200s < 900s
        pool.add_points("p", 4.0, TurnRange(index, index))
    assert sink_events == []  # balance 12 >= floor, but idle < idle_min_sec
    clock.advance(600.0)  # idle 600s: a fixed 5s window would fire here
    assert pool.evaluate() == ()
    clock.advance(400.0)  # idle 1000s >= 900s: the pool fires and drains
    fired = pool.evaluate()
    assert len(fired) == 1
    assert fired[0].kind is PoolEventKind.DREAM_TRIGGER
    assert sink_events == [fired[0]]
    assert meta.pool_state("p").balance == pytest.approx(0.0)
    # the scheduler shares the same config and the drained balance: the same
    # window is never double-serviced
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(1_000.0 + i * 200.0)) for i in range(3)]
    scheduler = DreamScheduler(_FakeStores(chunks, profiles=["p"], meta=meta), config, clock=clock)
    assert scheduler.eligibility("p") is None
    assert scheduler.tick() == []


# ---------------------------------------------------------------- failure backoff retry (A2.5 T1)


def test_failed_dream_retries_with_backoff_then_stops_and_audits() -> None:
    """AC2a: after a reflect failure the scheduler re-emits the SAME window on
    an exponential backoff — the fired fingerprint is never a permanent block
    and the retry never re-scores the pool. After DREAM_RETRY_MAX consecutive
    failures it stops and records a give-up audit event; pending chunks are
    never lost and the drained pool is never re-drained."""
    clock = _Clock(start=1000.0)
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    meta = _FakeMeta(profiles=["p"], balances={"p": 12.0})
    stores = _FakeStores(chunks, profiles=["p"], meta=meta)
    emitted: list[PoolEvent] = []
    scheduler = DreamScheduler(stores, _config(), trigger=_RecordingSeam(emitted), clock=clock)

    # initial fire: floor + idle, balance 12.0
    first = scheduler.tick()
    assert len(first) == 1
    assert first[0].reason == "floor_idle"
    assert len(emitted) == 1
    # the dream's pool fire drained the balance (persisted row) before reflect;
    # the drained pool can never re-fire the floor on its own
    meta.pool_credit("p", 0.0, TurnRange(0, 9))
    assert scheduler.tick() == []

    # reflect fails (the pipeline reports it back); after the BASE interval the
    # SAME window re-emits with a drained balance — no re-scoring, no re-drain
    scheduler.report_outcome("p", TurnRange(0, 9), False, "llm unavailable")
    clock.advance(DREAM_RETRY_BASE_S)
    retry1 = scheduler.tick()
    assert len(retry1) == 1
    assert retry1[0].turn_range == TurnRange(0, 9)
    assert retry1[0].pool_points == pytest.approx(0.0)
    assert len(emitted) == 2
    assert meta.pool_state("p").balance == pytest.approx(0.0)
    assert [c for c in chunks if not c.consolidated] == chunks  # pending unchanged

    # second consecutive failure: the backoff doubles (BASE * MULT)
    scheduler.report_outcome("p", TurnRange(0, 9), False, "llm unavailable")
    clock.advance(DREAM_RETRY_BASE_S * DREAM_RETRY_MULT)
    retry2 = scheduler.tick()
    assert len(retry2) == 1
    assert len(emitted) == 3

    # third consecutive failure reaches MAX: no further retries, the give-up is
    # recorded on the existing audit channel, pending stays, pool stays drained
    scheduler.report_outcome("p", TurnRange(0, 9), False, "llm unavailable")
    clock.advance(DREAM_RETRY_CAP_S)
    assert scheduler.tick() == []
    assert scheduler.tick() == []
    assert len(emitted) == 3
    give_ups = [entry for entry in meta.audit if entry.action == "dream_retry_give_up"]
    assert len(give_ups) == 1
    assert give_ups[0].detail["attempts"] == DREAM_RETRY_MAX
    assert give_ups[0].detail["turn_range"] == {"start": 0, "end": 9}
    assert all(not c.consolidated for c in chunks)
    assert meta.pool_state("p").balance == pytest.approx(0.0)


def test_success_after_failure_resets_the_backoff_streak() -> None:
    """AC2b: one successful dream clears the retry streak; the next failure
    starts a FRESH attempt budget — it never inherits the old failure count."""
    clock = _Clock(start=1000.0)
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    meta = _FakeMeta(profiles=["p"], balances={"p": 12.0})
    stores = _FakeStores(chunks, profiles=["p"], meta=meta)
    emitted: list[PoolEvent] = []
    scheduler = DreamScheduler(stores, _config(), trigger=_RecordingSeam(emitted), clock=clock)

    assert len(scheduler.tick()) == 1
    meta.pool_credit("p", 0.0, TurnRange(0, 9))
    scheduler.report_outcome("p", TurnRange(0, 9), False, "llm unavailable")
    clock.advance(DREAM_RETRY_BASE_S)
    assert len(scheduler.tick()) == 1  # first backoff retry re-emitted
    assert len(emitted) == 2

    # the retried dream SUCCEEDS: the streak resets, so nothing re-emits even
    # long past every backoff interval
    scheduler.report_outcome("p", TurnRange(0, 9), True, None)
    clock.advance(DREAM_RETRY_CAP_S)
    assert scheduler.tick() == []

    # a fresh failure starts a fresh streak: the FIRST retry lands at BASE
    # again (an inherited count would require 2*BASE and never fire here)
    scheduler.report_outcome("p", TurnRange(0, 9), False, "llm unavailable")
    clock.advance(DREAM_RETRY_BASE_S)
    fresh = scheduler.tick()
    assert len(fresh) == 1
    assert fresh[0].turn_range == TurnRange(0, 9)
    assert len(emitted) == 3


def test_retry_waits_for_outcome_while_worker_is_busy() -> None:
    """AC2c: once a retry is re-emitted, the scheduler waits for its outcome —
    while the worker is busy (the dream has not failed or succeeded yet), ticks
    never stack another emission for the same profile. The next re-fire happens
    only after the next failure report."""
    clock = _Clock(start=1000.0)
    chunks = [_chunk("p", turn=(i, i), ingested_at=float(i)) for i in range(10)]
    meta = _FakeMeta(profiles=["p"], balances={"p": 12.0})
    stores = _FakeStores(chunks, profiles=["p"], meta=meta)
    emitted: list[PoolEvent] = []
    scheduler = DreamScheduler(stores, _config(), trigger=_RecordingSeam(emitted), clock=clock)

    assert len(scheduler.tick()) == 1
    meta.pool_credit("p", 0.0, TurnRange(0, 9))
    scheduler.report_outcome("p", TurnRange(0, 9), False, "llm unavailable")
    clock.advance(DREAM_RETRY_BASE_S)
    assert len(scheduler.tick()) == 1  # retry re-emitted; its outcome is pending
    assert len(emitted) == 2

    # the worker stays busy with that dream: the clock sails far past every
    # backoff interval, yet not one tick stacks another emission
    clock.advance(DREAM_RETRY_CAP_S * 8)
    for _ in range(5):
        assert scheduler.tick() == []
    assert len(emitted) == 2

    # the busy dream finally fails: exactly one re-fire lands after the doubled
    # backoff, and no further emission follows without a new failure
    scheduler.report_outcome("p", TurnRange(0, 9), False, "llm unavailable")
    clock.advance(DREAM_RETRY_BASE_S * DREAM_RETRY_MULT)
    assert len(scheduler.tick()) == 1
    assert len(emitted) == 3
    clock.advance(DREAM_RETRY_CAP_S)
    assert scheduler.tick() == []
    assert len(emitted) == 3
