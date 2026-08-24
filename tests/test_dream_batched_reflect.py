"""Batched reflection (#99): oversized backlogs drain instead of parking forever.

DeltaPacker.plan_batches slices a snapshot into whole-chunk batches under a
per-batch token cap; ReflectOrchestrator._run_seat runs up to a bounded number
of LLM calls per dream (total still under the delta ceiling), folds mentions
globally, and reports uncovered chunks as honest overflow so merge can commit
the covered part (allow-list safe-clear) while the rest stays journaled.
Batching is opt-in (batch_max_tokens=None keeps the legacy single-pack path
byte-identical)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.dream import StubReflectLLM
from mnemoseed_local.dream.delta import DeltaPacker, estimate_tokens
from mnemoseed_local.dream.prompts import render_chunk_block
from mnemoseed_local.dream.reflect import (
    _MAX_REFLECT_BATCHES_PER_DREAM,
    ReflectOrchestrator,
    ReflectOutcome,
)
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import TurnRange

_PROFILE = "alice"
_RANGE = TurnRange(0, 8)


class _CountingLLM:
    """Delegates to the deterministic stub and counts chat calls."""

    def __init__(self, inner=None, *, fail_from: int | None = None) -> None:
        self.inner = inner if inner is not None else StubReflectLLM()
        self.calls = 0
        self.fail_from = fail_from

    def chat(self, *, system: str, user: str) -> str:
        self.calls += 1
        if self.fail_from is not None and self.calls >= self.fail_from:
            raise RuntimeError("reflect model unreachable")
        return self.inner.chat(system=system, user=user)


def _stamp(chunk_id: str, text: str, turn: int = 0) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id="s1", source="manual"),
        turn_start=turn,
        turn_end=turn,
    )


def _snap(*chunks: ChunkStamp) -> Snapshot:
    return Snapshot(
        snapshot_id="snap-batch",
        profile_id=_PROFILE,
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in chunks),
        created_at=1000.0,
        phases=frozenset({"snapshot_done"}),
    )


def _extractable(chunk_id: str, turn: int) -> ChunkStamp:
    return _stamp(chunk_id, f"I prefer dark mode {chunk_id}", turn)


def _one_batch_cap(snapshot: Snapshot) -> int:
    """A cap that fits exactly one chunk block (so every chunk lands in its
    own batch), derived from the real renderer + estimator."""
    blocks = [estimate_tokens(render_chunk_block(c)) for c in snapshot.chunks]
    return min(blocks) + 1


# ---------------------------------------------------------------- plan_batches


def test_plan_batches_covers_every_chunk_exactly_once_in_order() -> None:
    stamps = [_extractable(f"c{i}", i) for i in range(5)]
    snapshot = _snap(*stamps)
    requests = DeltaPacker().plan_batches(snapshot, batch_max_tokens=_one_batch_cap(snapshot))
    assert len(requests) == 5, "cap fits one block: one chunk per batch"
    packed = [cid for req in requests for cid in req.packed_chunk_ids]
    assert sorted(packed) == sorted(c.chunk_id for c in snapshot.chunks)
    assert packed == ["c0", "c1", "c2", "c3", "c4"], "ordered_chunks order, never capture order"
    assert all(req.overflow_chunk_ids == () for req in requests)
    assert all(req.delta for req in requests)


def test_plan_batches_packs_multiple_chunks_per_batch_when_they_fit() -> None:
    stamps = [_extractable(f"c{i}", i) for i in range(4)]
    snapshot = _snap(*stamps)
    single = estimate_tokens(render_chunk_block(SnapshotChunk.from_stamp(stamps[0])))
    requests = DeltaPacker().plan_batches(snapshot, batch_max_tokens=single * 2 + 10)
    assert len(requests) == 2, "two blocks fit side by side under the doubled cap"
    packed = [cid for req in requests for cid in req.packed_chunk_ids]
    assert sorted(packed) == ["c0", "c1", "c2", "c3"]


def test_plan_batches_oversized_single_chunk_gets_its_own_batch() -> None:
    big = _stamp("big", "我特别喜欢" * 200)  # far over any small cap, never split mid-text
    small = _extractable("c1", 1)
    snapshot = _snap(big, small)
    requests = DeltaPacker().plan_batches(_snap(big, small), batch_max_tokens=50)
    del snapshot
    assert [len(req.packed_chunk_ids) for req in requests] == [1, 1]
    assert requests[0].packed_chunk_ids == ("big",)
    assert requests[1].packed_chunk_ids == ("c1",)


def test_plan_batches_empty_snapshot_yields_no_batches() -> None:
    assert DeltaPacker().plan_batches(_snap(), batch_max_tokens=100) == []


def test_plan_batches_rejects_non_positive_cap() -> None:
    with pytest.raises(ValueError):
        DeltaPacker().plan_batches(_snap(_extractable("c0", 0)), batch_max_tokens=0)


def test_plan_batches_clamps_cap_to_the_packers_ceiling() -> None:
    stamps = [_extractable(f"c{i}", i) for i in range(4)]
    requests = DeltaPacker().plan_batches(_snap(*stamps), batch_max_tokens=500_000)
    assert all(req.budget_tokens <= 32_000 for req in requests), (
        "pack binds at the ceiling; the plan must not promise more"
    )
    packed = [cid for req in requests for cid in req.packed_chunk_ids]
    assert sorted(packed) == ["c0", "c1", "c2", "c3"], "clamping never drops chunks"


# ---------------------------------------------------------------- batched _run_seat


def _orchestrate(
    snapshot: Snapshot,
    directory: Path,
    llm: _CountingLLM,
    *,
    batch_max_tokens: int | None,
) -> ReflectOutcome:
    return ReflectOrchestrator(
        llm=llm,
        directory=directory,
        sleep=lambda _: None,
        batch_max_tokens=batch_max_tokens,
    ).reflect(snapshot)


def test_batched_reflect_aggregates_triples_across_batches(tmp_path: Path) -> None:
    snapshot = _snap(*[_extractable(f"c{i}", i) for i in range(3)])
    llm = _CountingLLM()
    outcome = _orchestrate(snapshot, tmp_path, llm, batch_max_tokens=_one_batch_cap(snapshot))
    assert outcome.ok and outcome.result is not None
    assert llm.calls == 3, "one LLM call per batch"
    objects = {t.object for t in outcome.result.triples}
    for i in range(3):
        assert f"dark mode c{i}" in objects, f"extraction from batch {i} survived aggregation"
    assert sorted(outcome.result.consumed_chunk_ids) == ["c0", "c1", "c2"]
    assert outcome.result.overflow_chunk_ids == ()
    on_disk = load_journal(tmp_path)
    assert on_disk is not None and "reflect_done" in on_disk.phases


def load_journal(directory: Path) -> Snapshot | None:
    from mnemoseed_local.dream.snapshot import load_snapshot_file

    return load_snapshot_file(directory / "snap-batch.json")


def test_batched_reflect_caps_llm_calls_and_reports_rest_as_overflow(tmp_path: Path) -> None:
    snapshot = _snap(*[_extractable(f"c{i}", i) for i in range(6)])
    llm = _CountingLLM()
    outcome = _orchestrate(snapshot, tmp_path, llm, batch_max_tokens=_one_batch_cap(snapshot))
    assert outcome.ok and outcome.result is not None
    assert llm.calls == _MAX_REFLECT_BATCHES_PER_DREAM, "per-dream LLM budget bounded"
    consumed = set(outcome.result.consumed_chunk_ids)
    overflow = set(outcome.result.overflow_chunk_ids)
    assert consumed | overflow == {"c0", "c1", "c2", "c3", "c4", "c5"}, "never-drop: nothing vanishes"
    assert consumed.isdisjoint(overflow)
    assert overflow == {"c4", "c5"}, "the un-run tail stays honest overflow"


def test_batched_reflect_mid_batch_failure_degrades_and_stays_journaled(tmp_path: Path) -> None:
    snapshot = _snap(*[_extractable(f"c{i}", i) for i in range(3)])
    llm = _CountingLLM(fail_from=2)  # batch 1 succeeds, batch 2 raises, retries exhausted
    outcome = _orchestrate(snapshot, tmp_path, llm, batch_max_tokens=_one_batch_cap(snapshot))
    assert not outcome.ok and outcome.result is None
    assert "reflect model unreachable" in (outcome.error or "")
    on_disk = load_journal(tmp_path)
    assert on_disk is None or "reflect_done" not in on_disk.phases, (
        "no progress marker may land on a degraded batched run"
    )


def test_batching_disabled_keeps_legacy_single_call_path(tmp_path: Path) -> None:
    snapshot = _snap(*[_extractable(f"c{i}", i) for i in range(3)])
    llm = _CountingLLM()
    outcome = _orchestrate(snapshot, tmp_path, llm, batch_max_tokens=None)
    assert outcome.ok
    assert llm.calls == 1, "opt-in off: byte-identical legacy single-pack path"


def test_zero_batch_cap_through_the_seat_is_treated_as_disabled(tmp_path: Path) -> None:
    """Direct construction with 0 must not raise out of reflect() (the module
    contract is never-a-raise); it degrades to the legacy single-call path."""
    snapshot = _snap(*[_extractable(f"c{i}", i) for i in range(3)])
    llm = _CountingLLM()
    outcome = _orchestrate(snapshot, tmp_path, llm, batch_max_tokens=0)
    assert outcome.ok
    assert llm.calls == 1


def test_oversized_solo_chunk_defers_without_an_empty_user_call(tmp_path: Path) -> None:
    """D1 parity: a chunk whose block alone exceeds the packer budget clips to
    an empty delta inside pack(). The seat must NOT call the LLM with an empty
    user turn (that wedge sits outside merge-boundary parking); the chunk
    stays honest uncovered overflow while the rest still drains."""

    class _NoEmptyUserLLM(_CountingLLM):
        def chat(self, *, system: str, user: str) -> str:
            assert user, "empty delta must never reach the LLM"
            return super().chat(system=system, user=user)

    huge = _stamp("huge", "我特别喜欢" * 14000)  # block alone far over the 32k ceiling
    small = _extractable("c0", 0)
    snapshot = _snap(huge, small)
    # cap above the ceiling so both chunks land in one planned batch each and
    # the huge one clips inside pack() exactly like production would
    llm = _NoEmptyUserLLM()
    outcome = _orchestrate(snapshot, tmp_path, llm, batch_max_tokens=500_000)
    assert outcome.ok and outcome.result is not None
    assert llm.calls >= 1
    assert outcome.result.overflow_chunk_ids == ("huge",), "oversized solo chunk stays journaled"
    assert outcome.result.consumed_chunk_ids == ("c0",)
    objects = {t.object for t in outcome.result.triples}
    assert "dark mode c0" in objects


def test_batched_reflect_all_empty_batches_still_commits_all_noise(tmp_path: Path) -> None:
    snapshot = _snap(
        _stamp("n0", "嘻嘻嘻", 0),
        _stamp("n1", "啦啦啦", 1),
    )
    llm = _CountingLLM()
    outcome = _orchestrate(snapshot, tmp_path, llm, batch_max_tokens=_one_batch_cap(snapshot))
    assert outcome.ok and outcome.result is not None
    assert outcome.result.triples == ()
    assert outcome.result.overflow_chunk_ids == (), "fully covered noise commits like the legacy path"
