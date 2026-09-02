"""Regression for journaled truncated delta with 236 overflow staying journaled forever.

Production had a snapshot journaled at the merge boundary with 19 consumed and
236 overflow, empty triples, reflect_done. Every daemon boot replayed it as
"merge deferred for default (boot replay, not counted): journaled truncated delta
with 236 overflow chunks stays journaled" and never resolved.
"""

from __future__ import annotations

from pathlib import Path

from mnemoseed_local.dream.pipeline import DreamPipeline
from mnemoseed_local.dream.reflect import (
    ReflectionResult,
    ReflectOutcome,
    _result_to_payload,
    result_from_payload,
)
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk, SnapshotPhase, resume_boundary
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import TurnRange

_PROFILE = "default"
_RANGE = TurnRange(0, 2)


def _stamp(chunk_id: str) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=_PROFILE,
        text=f"text for {chunk_id}",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id="s1", source="manual"),
        turn_start=0,
        turn_end=1,
    )


def _snapshot_with_journaled_result(
    snapshot_id: str,
    result: ReflectionResult,
    tmp_path: Path,
) -> Snapshot:
    from mnemoseed_local.dream.snapshot import write_snapshot_file

    snap = Snapshot(
        snapshot_id=snapshot_id,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        chunks=tuple(
            SnapshotChunk.from_stamp(_stamp(cid))
            for cid in (result.consumed_chunk_ids + result.overflow_chunk_ids)
        ),
        created_at=1000.0,
        phases=frozenset({SnapshotPhase.SNAPSHOT_DONE.value, SnapshotPhase.REFLECT_DONE.value}),
        reflect_result=_result_to_payload(result),
    )
    # also write to disk so _consumed_ids path works via load
    write_snapshot_file(tmp_path / "dreams", snap)
    # reload to ensure phases/payload correct
    from mnemoseed_local.dream.snapshot import load_snapshot_file

    loaded = load_snapshot_file(tmp_path / "dreams" / f"{snapshot_id}.json")
    assert loaded is not None
    return loaded


class _SpyMerger:
    def __init__(self) -> None:
        self.calls: list[tuple[Snapshot, ReflectionResult]] = []

    def merge(self, snapshot: Snapshot, result: ReflectionResult):
        self.calls.append((snapshot, result))
        from mnemoseed_local.dream.merge import MergeOutcome

        return MergeOutcome(ok=True, committed=True)


class _NoopTrigger:
    def on_dream_failed(self, profile_id: str) -> None:
        pass

    def on_merge_committed(self, profile_id: str) -> None:
        pass


class _ConsolidatingMerger(_SpyMerger):
    """A merger that fires the safe-clear seam on commit, mirroring the
    production merge -> purge wiring, so a regression can assert the covered
    chunks are never marked consolidated when no commit should fire."""

    def __init__(self, snapshotter) -> None:
        super().__init__()
        self._snapshotter = snapshotter

    def merge(self, snapshot: Snapshot, result: ReflectionResult):
        self.calls.append((snapshot, result))
        from mnemoseed_local.dream.merge import MergeOutcome

        self._snapshotter.purge_snapshot(
            snapshot.profile_id,
            snapshot.turn_range,
            consumed_chunk_ids=result.consumed_chunk_ids,
        )
        return MergeOutcome(ok=True, committed=True)


class _StubReflector:
    """Return a caller-supplied reflect outcome at the reflect boundary."""

    def __init__(self, outcome: ReflectOutcome) -> None:
        self.outcome = outcome

    def reflect(self, snapshot):  # type: ignore[no-untyped-def]
        return self.outcome


class _StubCombineReflector:
    """Return a caller-supplied outcome at the combine boundary (vote lane)."""

    def __init__(self, outcome: ReflectOutcome) -> None:
        self.outcome = outcome

    def combine(self, snapshot):  # type: ignore[no-untyped-def]
        return self.outcome


class _RecordingStore:
    """Records every consolidated-mark so a test can assert no chunk was
    cleared when a dream should defers instead of committing."""

    def __init__(self, rows) -> None:
        self._rows = rows
        self.marked: list[str] = []

    def snapshot_read(self, f):  # type: ignore[no-untyped-def]
        return self._rows

    def mark_consolidated(self, chunk_ids):  # type: ignore[no-untyped-def]
        self.marked.extend(chunk_ids)


def _pipeline(
    tmp_path: Path,
    merger: _SpyMerger,
    *,
    reflector: object | None = None,
    store: object | None = None,
    snapshotter: object | None = None,
) -> DreamPipeline:
    from mnemoseed_local.dream.snapshot import FileSnapshotter

    class _EmptyMeta:
        def record_dream_run(self, run):  # type: ignore[no-untyped-def]
            return ""

    if snapshotter is None:
        snapshotter = FileSnapshotter(
            store=store if store is not None else _RecordingStore([]),  # type: ignore[arg-type]
            meta=_EmptyMeta(),  # type: ignore[arg-type]
            directory=tmp_path / "dreams",
        )

    return DreamPipeline(
        trigger=_NoopTrigger(),  # type: ignore[arg-type]
        snapshotter=snapshotter,  # type: ignore[arg-type]
        reflector=reflector
        if reflector is not None
        else _StubReflector(outcome=ReflectOutcome(ok=True, result=None)),  # type: ignore[arg-type]
        merger=merger,  # type: ignore[arg-type]
    )


def _legacy_snapshot_with_journaled_result(
    snapshot_id: str,
    result: ReflectionResult,
    tmp_path: Path,
) -> Snapshot:
    """Craft a legacy-shape journal entry that lacks the ``batched`` key (pre-fix)."""
    from mnemoseed_local.dream.snapshot import load_snapshot_file, write_snapshot_file

    payload = _result_to_payload(result)
    payload.pop("batched", None)
    snap = Snapshot(
        snapshot_id=snapshot_id,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        chunks=tuple(
            SnapshotChunk.from_stamp(_stamp(cid))
            for cid in (result.consumed_chunk_ids + result.overflow_chunk_ids)
        ),
        created_at=1000.0,
        phases=frozenset({SnapshotPhase.SNAPSHOT_DONE.value, SnapshotPhase.REFLECT_DONE.value}),
        reflect_result=payload,
    )
    write_snapshot_file(tmp_path / "dreams", snap)
    loaded = load_snapshot_file(tmp_path / "dreams" / f"{snapshot_id}.json")
    assert loaded is not None
    assert loaded.reflect_result is not None
    assert "batched" not in loaded.reflect_result
    return loaded


def test_boot_replay_journaled_truncated_delta_with_236_overflow_eventually_commits(
    tmp_path: Path, caplog
) -> None:
    """The production journal entry with 19 consumed / 236 overflow must not stay
    journaled forever on boot replay. It should commit the covered range via
    the allow-list safe-clear so the overflow tail can be picked up later."""
    import logging

    overflow = tuple(f"ov-{i:04d}" for i in range(236))
    consumed = tuple(f"c-{i:04d}" for i in range(19))
    result = ReflectionResult(
        snapshot_id="snap-236",
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(),
        overflow_chunk_ids=overflow,
        consumed_chunk_ids=consumed,
    )
    snap = _legacy_snapshot_with_journaled_result("snap-236", result, tmp_path)
    merger = _SpyMerger()
    pipeline = _pipeline(tmp_path, merger)

    # Boot replay: counts_toward_parking=False, the path that previously deferred forever
    caplog.set_level(logging.WARNING)
    pipeline.run(snap, counts_toward_parking=False)

    assert len(merger.calls) == 1, "boot replay must commit the covered range instead of deferring forever"
    _, merged_result = merger.calls[0]
    assert merged_result.consumed_chunk_ids == consumed
    assert merged_result.overflow_chunk_ids == overflow
    log_text = caplog.text
    assert "stale journal recovery" in log_text
    assert "batched dream" not in log_text
    assert "stay pending" in log_text


def test_batched_flag_survives_journal_and_merge_boundary_commits(tmp_path: Path, caplog) -> None:
    """A batched reflect that produced an empty verdict must survive the journal
    seam so the merge boundary (boot replay) commits instead of deferring."""
    import logging

    overflow = tuple(f"ov-{i}" for i in range(5))
    consumed = tuple(f"c-{i}" for i in range(3))
    result = ReflectionResult(
        snapshot_id="snap-batched",
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(),
        overflow_chunk_ids=overflow,
        consumed_chunk_ids=consumed,
        batched=True,
    )
    payload = _result_to_payload(result)
    assert payload.get("batched") is True, "payload must carry batched marker"
    recovered = result_from_payload(payload)
    assert recovered is not None
    assert recovered.batched is True, "recovered result must preserve batched"

    snap = _snapshot_with_journaled_result("snap-batched", result, tmp_path)
    merger = _SpyMerger()
    pipeline = _pipeline(tmp_path, merger)
    caplog.set_level(logging.WARNING)
    pipeline.run(snap, counts_toward_parking=False)
    assert len(merger.calls) == 1, "batched empty verdict at merge boundary must commit"
    assert "batched dream" in caplog.text
    assert "stay pending" in caplog.text
    assert "stale journal recovery" not in caplog.text


def test_boot_replay_without_consumed_still_defers(tmp_path: Path) -> None:
    """A journaled truncated delta with no consumed ids (all overflow) still defers
    at boot replay — there is nothing to commit via allow-list."""
    overflow = tuple(f"ov-{i}" for i in range(10))
    result = ReflectionResult(
        snapshot_id="snap-all-overflow",
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(),
        overflow_chunk_ids=overflow,
        consumed_chunk_ids=(),
    )
    snap = _snapshot_with_journaled_result("snap-all-overflow", result, tmp_path)
    merger = _SpyMerger()
    pipeline = _pipeline(tmp_path, merger)
    pipeline.run(snap, counts_toward_parking=False)
    assert len(merger.calls) == 0, "all-overflow with no consumed must still defer"


def test_boot_replay_non_batched_truncated_empty_fresh_defers(tmp_path: Path, caplog) -> None:
    """Fresh non-batched truncated empty must NOT auto-commit on boot replay.

    Post-fix payload carries an explicit ``batched`` key (False). Even with
    consumed ids present on boot replay (counts_toward_parking=False) the
    pipeline must defer and leave the journal inspectable, not silently
    commit via the migration recovery path."""
    import logging

    overflow = tuple(f"ov-{i}" for i in range(5))
    consumed = tuple(f"c-{i}" for i in range(3))
    result = ReflectionResult(
        snapshot_id="snap-fresh-non-batched",
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(),
        overflow_chunk_ids=overflow,
        consumed_chunk_ids=consumed,
        batched=False,
    )
    snap = _snapshot_with_journaled_result("snap-fresh-non-batched", result, tmp_path)
    # Fresh shape must carry the batched key so the migration gate can distinguish it.
    assert snap.reflect_result is not None
    assert "batched" in snap.reflect_result
    assert snap.reflect_result["batched"] is False
    merger = _SpyMerger()
    pipeline = _pipeline(tmp_path, merger)
    caplog.set_level(logging.WARNING)
    pipeline.run(snap, counts_toward_parking=False)
    assert len(merger.calls) == 0, "fresh non-batched truncated empty must defer, not commit"
    log_text = caplog.text
    assert "merge deferred" in log_text
    assert "boot replay" in log_text
    assert "stale journal recovery" not in log_text
    assert "batched dream" not in log_text


def _fresh_reflect_snapshot(snapshot_id: str, chunk_ids, tmp_path: Path) -> Snapshot:
    """A just-captured snapshot at the reflect boundary: only SNAPSHOT_DONE, no
    ``reflect_result`` in memory (reflect persists to disk only). Resume at
    ``"reflect"`` with ``reflect_result`` None — the pre-persist shape the QA
    blocker turned on."""
    from mnemoseed_local.dream.snapshot import load_snapshot_file, write_snapshot_file

    snap = Snapshot(
        snapshot_id=snapshot_id,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(_stamp(cid)) for cid in chunk_ids),
        created_at=1000.0,
        phases=frozenset({SnapshotPhase.SNAPSHOT_DONE.value}),
    )
    write_snapshot_file(tmp_path / "dreams", snap)
    loaded = load_snapshot_file(tmp_path / "dreams" / f"{snapshot_id}.json")
    assert loaded is not None
    assert loaded.reflect_result is None
    assert resume_boundary(loaded) == "reflect"
    return loaded


def test_boot_recovery_fresh_reflect_truncated_non_batched_defers_not_legacy(tmp_path: Path, caplog) -> None:
    """A FRESH reflect-boundary snapshot recovered at boot (``reflect_result`` is
    None in-memory) whose truncated delta yielded a non-batched empty verdict must
    DEFER — it must NOT take the legacy migration recovery, must not call the
    merger (zero graph writes), and must not mark any chunk consolidated
    (D1 / FR-2.5 never-drop). Legacy migration is boundary-scoped, so a fresh
    boot-reflect must not impersonate a stale journal."""
    import logging

    from mnemoseed_local.dream.snapshot import FileSnapshotter

    overflow = tuple(f"ov-{i}" for i in range(5))
    consumed = tuple(f"c-{i}" for i in range(3))
    snap = _fresh_reflect_snapshot("snap-fresh-reflect", consumed + overflow, tmp_path)

    rows = tuple(SnapshotChunk.from_stamp(_stamp(c)) for c in (consumed + overflow))
    store = _RecordingStore(rows)

    class _EmptyMeta:
        def record_dream_run(self, run):  # type: ignore[no-untyped-def]
            return ""

    snapshotter = FileSnapshotter(store=store, meta=_EmptyMeta(), directory=tmp_path / "dreams")
    snapshotter.adopt(snap)

    result = ReflectionResult(
        snapshot_id="snap-fresh-reflect",
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(),
        overflow_chunk_ids=overflow,
        consumed_chunk_ids=consumed,
        batched=False,
    )
    outcome = ReflectOutcome(ok=True, result=result, batched=False)
    merger = _ConsolidatingMerger(snapshotter)
    pipeline = _pipeline(
        tmp_path,
        merger,
        reflector=_StubReflector(outcome),
        snapshotter=snapshotter,
    )

    caplog.set_level(logging.WARNING)
    pipeline.run(snap, counts_toward_parking=False)

    assert merger.calls == [], "fresh non-batched truncated empty must NOT commit (no legacy migration)"
    assert store.marked == [], "no chunk may be marked consolidated for a deferred dream"
    log_text = caplog.text
    assert "merge deferred" in log_text
    assert "boot replay" in log_text
    assert "stale journal recovery" not in log_text
    assert "batched dream" not in log_text
    assert resume_boundary(snap) == "reflect", "snapshot must stay journaled at the reflect boundary"


def _vote_seat_combine_snapshot(snapshot_id: str, chunk_ids, tmp_path: Path) -> Snapshot:
    """A snapshot at the combine boundary (REFLECT_A + REFLECT_B done, combine
    not yet run): ``resume_boundary == "combine"`` and no persisted
    ``reflect_result`` (vote payloads are carried in ``vote_results``)."""
    from mnemoseed_local.dream.snapshot import load_snapshot_file, write_snapshot_file

    snap = Snapshot(
        snapshot_id=snapshot_id,
        profile_id=_PROFILE,
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(_stamp(cid)) for cid in chunk_ids),
        created_at=1000.0,
        phases=frozenset(
            {
                SnapshotPhase.SNAPSHOT_DONE.value,
                SnapshotPhase.REFLECT_A_DONE.value,
                SnapshotPhase.REFLECT_B_DONE.value,
            }
        ),
    )
    write_snapshot_file(tmp_path / "dreams", snap)
    loaded = load_snapshot_file(tmp_path / "dreams" / f"{snapshot_id}.json")
    assert loaded is not None
    assert resume_boundary(loaded) == "combine"
    return loaded


def test_combine_boundary_batched_flag_propagates_to_merge(tmp_path: Path, caplog) -> None:
    """The ``result.batched`` flag must reach ``_merge`` even when the seam does
    NOT forward ``empty_verdict_commitable`` (the combine boundary calls
    ``_merge`` bare). A boot-replayed combined verdict that is batched + empty
    must commit via the batched path rather than defer — closing the M1
    test-oracle gap where only the merge-boundary forwarding elevated the flag
    and masked the ``or result.batched`` branch."""
    import logging

    overflow = tuple(f"ov-{i}" for i in range(5))
    consumed = tuple(f"c-{i}" for i in range(3))
    snap = _vote_seat_combine_snapshot("snap-combine-batched", consumed + overflow, tmp_path)

    combined = ReflectionResult(
        snapshot_id="snap-combine-batched",
        profile_id=_PROFILE,
        turn_range=_RANGE,
        prompt_version="v1",
        triples=(),
        overflow_chunk_ids=overflow,
        consumed_chunk_ids=consumed,
        batched=True,
    )
    outcome = ReflectOutcome(ok=True, result=combined, batched=True)
    merger = _SpyMerger()
    pipeline = _pipeline(tmp_path, merger, reflector=_StubCombineReflector(outcome))

    caplog.set_level(logging.WARNING)
    pipeline.run(snap, counts_toward_parking=False)

    assert len(merger.calls) == 1, "batched empty verdict at combine boundary must commit"
    _, merged_result = merger.calls[0]
    assert merged_result.batched is True
    log_text = caplog.text
    assert "batched dream" in log_text
    assert "stay pending" in log_text
    assert "stale journal recovery" not in log_text
    assert "merge deferred" not in log_text
