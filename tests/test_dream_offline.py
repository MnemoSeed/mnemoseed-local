from __future__ import annotations

from test_dream_schedule import _chunk, _config, _FakeStores, _RecordingSeam, _scheduler

from mnemoseed_local.capture.pool import PoolEventKind


def test_scheduler_defers_without_draining_when_provider_is_unready() -> None:
    sink = _RecordingSeam([])
    scheduler = _scheduler(
        _FakeStores(
            [_chunk("p", turn=(0, 9), ingested_at=0.0)],
            profiles=["p"],
            balances={"p": 12.0},
        ),
        _config(hard_deadline_sec=1e9),
        trigger=sink,
        clock=lambda: 10_000.0,
        ready=lambda: False,
    )

    assert scheduler.tick() == []
    assert scheduler._meta.pool_state("p").balance == 12.0
    assert sink.sink == []


def test_scheduler_emits_the_same_pending_window_after_provider_recovers() -> None:
    sink = _RecordingSeam([])
    ready_values = iter((False, True))
    scheduler = _scheduler(
        _FakeStores(
            [_chunk("p", turn=(0, 9), ingested_at=0.0)],
            profiles=["p"],
            balances={"p": 12.0},
        ),
        _config(hard_deadline_sec=1e9),
        trigger=sink,
        clock=lambda: 10_000.0,
        ready=lambda: next(ready_values),
    )

    assert scheduler.tick() == []
    assert scheduler._meta.pool_state("p").balance == 12.0
    assert len(scheduler.tick()) == 1
    assert scheduler._meta.pool_state("p").balance == 0.0
    assert len(sink.sink) == 1


def test_forced_cap_recovery_fires_once_after_provider_recovers_without_new_credit() -> None:
    sink = _RecordingSeam([])
    ready_values = iter((False, True, True))
    scheduler = _scheduler(
        _FakeStores(
            [_chunk("p", turn=(0, 9), ingested_at=10_000.0)],
            profiles=["p"],
            balances={"p": 50.0},
        ),
        _config(floor_pool_points=1e9, hard_deadline_sec=1e9),
        trigger=sink,
        clock=lambda: 10_000.0,
        ready=lambda: next(ready_values),
    )

    assert scheduler.tick() == []
    assert scheduler.tick() != []
    assert scheduler.tick() == []
    assert len(sink.sink) == 1
    assert sink.sink[0].kind is PoolEventKind.FORCED_CONSOLIDATION
    assert scheduler._meta.pool_state("p").balance == 0.0
    sink = _RecordingSeam([])
    now = [10_000.0]
    ready_values = iter((True, False, True, True, True))
    stores = _FakeStores(
        [_chunk("p", turn=(0, 9), ingested_at=0.0)],
        profiles=["p"],
        balances={"p": 12.0},
    )
    meta = stores.meta
    scheduler = _scheduler(
        stores,
        _config(hard_deadline_sec=1e9),
        trigger=sink,
        clock=lambda: now[0],
        ready=lambda: next(ready_values),
    )

    assert len(scheduler.tick()) == 1
    scheduler.report_outcome("p", sink.sink[0].turn_range, False, "provider offline")
    assert scheduler.tick() == []
    for _ in range(3):
        now[0] += 1
        scheduler.tick()
    assert not any(entry.action == "dream_retry_give_up" for entry in meta.audit)
