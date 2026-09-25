from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
from mnemoseed_local.daemon.app import DreamSubmission, DreamWorker
from mnemoseed_local.dream import DreamTrigger, SnapshotResult
from mnemoseed_local.storage.ports import TurnRange


class _Snapshotter:
    def __init__(self) -> None:
        self.requests = 0

    def request(self, profile_id: str, turn_range: TurnRange) -> SnapshotResult:
        del profile_id, turn_range
        self.requests += 1
        return SnapshotResult(snapshot=None, ok=True)


def _event() -> PoolEvent:
    return PoolEvent(
        kind=PoolEventKind.DREAM_TRIGGER,
        profile_id="p",
        turn_range=TurnRange(0, 1),
        balance=10.0,
        fired_at=1.0,
    )


def test_provider_deferred_event_is_released_after_recovery() -> None:
    snapshotter = _Snapshotter()
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=True)
    trigger.defer_provider_event(_event())

    assert trigger.status("p").pending_provider == 1
    trigger.release_provider_events()
    assert trigger.status("p").pending_provider == 0
    assert snapshotter.requests == 1


@pytest.mark.asyncio
async def test_manual_dream_is_not_hidden_or_queued_while_provider_unready() -> None:
    snapshotter = _Snapshotter()
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=False)
    trigger.handle_event(_event())
    worker = DreamWorker(
        trigger,
        ready=lambda: False,
        provider_status=lambda: "Ollama API unavailable",
    )
    worker.start()

    result = await worker.submit_dream_once("p")
    assert result == DreamSubmission(launched=False, reason="Ollama API unavailable")
    assert snapshotter.requests == 0
    assert trigger.status("p").pending_manual == 1
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_final_guard_rejects_a_racing_manual_job() -> None:
    snapshotter = _Snapshotter()
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=False)
    trigger.handle_event(_event())
    ready_values = iter((True, False))
    worker = DreamWorker(
        trigger,
        ready=lambda: next(ready_values),
        provider_status=lambda: "Ollama API disappeared",
    )
    worker.start()

    result = await asyncio.wait_for(worker.submit_dream_once("p"), timeout=2.0)
    assert result == DreamSubmission(launched=False, reason="Ollama API disappeared")
    assert snapshotter.requests == 0
    await worker.stop()


@pytest.mark.asyncio
async def test_manual_rejection_reasons_are_immutable_across_interleaved_requests() -> None:
    snapshotter = _Snapshotter()
    trigger = DreamTrigger(snapshotter=snapshotter, auto_trigger=False)
    trigger.handle_event(_event())
    readiness = iter((True, True, False, False, True, True))
    provider_reasons = iter(("Ollama API disappeared for first", "Ollama API unavailable for second"))
    worker = DreamWorker(
        trigger,
        ready=lambda: next(readiness),
        provider_status=lambda: next(provider_reasons),
    )

    first_request = asyncio.create_task(worker.submit_dream_once("p"))
    second_request = asyncio.create_task(worker.submit_dream_once("p"))
    await asyncio.sleep(0)
    worker.start()
    first, second = await asyncio.gather(first_request, second_request)

    assert first == DreamSubmission(False, "Ollama API disappeared for first")
    assert second == DreamSubmission(False, "Ollama API unavailable for second")
    with pytest.raises(FrozenInstanceError):
        first.reason = "mutated"  # type: ignore[misc]

    successful = await worker.submit_dream_once("p")
    assert successful == DreamSubmission(True, None)
    assert snapshotter.requests == 1
    await worker.stop()
