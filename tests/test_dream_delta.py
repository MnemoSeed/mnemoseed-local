"""Dream delta packing + prompt-cache partition (PRD-02 T5; FR-2.5 / NFR-2.2).

Testable behaviors asserted through the public surface:

- ``estimate_tokens`` is a deterministic local token estimator (chars/4 for
  non-CJK + one token per CJK char), pinned on known strings.
- Delta packing: chunks are packed whole (never split mid-text) in deterministic
  order under a token budget (default 10000); chunks that do not fit are reported
  as overflow, never silently dropped; the cache prefix never counts against the
  delta budget.
- Cache prefix: byte-stable across dreams of the same profile; per-dream data
  (snapshot ids, chunk ids, timestamps) never leaks in; an optional injected
  graph-digest provider slots into the prefix while the null default renders no
  digest section.
- Orchestrator integration: the reflect pass consumes the packed request instead
  of the raw full-snapshot render; overflow is reported and never an error;
  under no budget pressure the packed delta IS the full snapshot render.
- Token ledger metering (T3b): a successful reflect records the packed delta
  plus provider-reported output tokens into the current UTC month; the ledger
  is observability-only and never gates the reflect boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
from mnemoseed_local.dream import (
    DEFAULT_DELTA_BUDGET_TOKENS,
    DELTA_BUDGET_CEILING_TOKENS,
    DELTA_BUDGET_FLOOR_TOKENS,
    DeltaPacker,
    DeltaReport,
    DreamState,
    DreamTrigger,
    GraphDigest,
    NullGraphDigest,
    NullSnapshotter,
    ReflectionResult,
    ReflectOrchestrator,
    ReflectOutcome,
    StubReflectLLM,
    TokenLedger,
    build_cache_prefix,
    estimate_tokens,
    render_chunk_blocks,
    resolve_delta_budget,
    resume_boundary,
)
from mnemoseed_local.dream.merge import MergeOutcome, Merger
from mnemoseed_local.dream.pipeline import DreamPipeline
from mnemoseed_local.dream.snapshot import FileSnapshotter, Snapshot, SnapshotChunk
from mnemoseed_local.llm.types import ChatResult, Usage
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import AuditEntry, TurnRange

_RANGE = TurnRange(0, 10)


# ---------------------------------------------------------------- helpers


def _stamp(
    chunk_id: str,
    text: str,
    *,
    turn_start: int = 0,
    turn_end: int = 1,
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id="alice",
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id="s1", source="manual"),
        turn_start=turn_start,
        turn_end=turn_end,
    )


def _snap(
    *stamps: ChunkStamp,
    snapshot_id: str = "snap-p1",
    profile_id: str = "alice",
    created_at: float = 1000.0,
    phases: frozenset[str] = frozenset({"snapshot_done"}),
) -> Snapshot:
    return Snapshot(
        snapshot_id=snapshot_id,
        profile_id=profile_id,
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in stamps),
        created_at=created_at,
        phases=phases,
    )


class _RecordingLLM(StubReflectLLM):
    """Stub that also records the (system, user) segments of each chat call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def chat(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        return super().chat(system=system, user=user)


class _FixedDigest:
    """Graph-digest double returning a fixed, profile-independent string."""

    def __init__(self, value: str) -> None:
        self._value = value
        self.calls = 0

    def digest(self, profile_id: str) -> str:
        del profile_id
        self.calls += 1
        return self._value


def _packed_tokens(text: str) -> int:
    return estimate_tokens(text)


# ---------------------------------------------------------------- token estimator


def test_estimate_tokens_pinned_strings() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("hello") == 2  # 5 non-CJK chars -> ceil(5/4)
    assert estimate_tokens("hello world") == 3  # 11 non-CJK chars -> ceil(11/4)
    assert estimate_tokens("你好世界") == 4  # one token per CJK char
    assert estimate_tokens("hello你好") == 4  # ceil(5/4) + 2 CJK


def test_estimate_tokens_deterministic_across_calls() -> None:
    text = "I prefer dark mode and vim, 深色模式是最好的。"
    assert estimate_tokens(text) == estimate_tokens(text)


def test_estimate_tokens_english_accuracy_bounds() -> None:
    """Documented envelope: a BPE tokenizer averages roughly 3-5 chars/token
    for English prose; the chars/4 estimator must land inside that band."""
    text = "a" * 4000
    estimate = estimate_tokens(text)
    assert 4000 // 6 <= estimate <= 4000 // 3
    assert estimate == 1000


def test_estimate_tokens_mixed_cjk_and_ascii() -> None:
    # "你好" = 2 CJK tokens; " world" = 6 non-CJK chars -> ceil(6/4) = 2
    assert estimate_tokens("你好 world") == 4


# ---------------------------------------------------------------- delta packing


def test_delta_pack_all_chunks_when_within_budget() -> None:
    snap = _snap(
        _stamp("c1", "a" * 8, turn_start=2, turn_end=3),
        _stamp("c2", "b" * 8, turn_start=0, turn_end=1),
        _stamp("c3", "c" * 8, turn_start=4, turn_end=5),
    )
    request = DeltaPacker().pack(snap)
    assert request.packed_chunk_ids == ("c2", "c1", "c3")  # deterministic turn order
    assert request.overflow_chunk_ids == ()
    assert request.delta == render_chunk_blocks(snap.chunks)
    assert request.delta_tokens == _packed_tokens(request.delta)


def test_delta_pack_is_deterministic_same_input_same_request() -> None:
    snap = _snap(_stamp("c1", "a" * 40), _stamp("c2", "b" * 40))
    assert DeltaPacker().pack(snap) == DeltaPacker().pack(snap)


def test_delta_order_is_deterministic_regardless_of_input_order() -> None:
    a = _stamp("c1", "a" * 16, turn_start=2, turn_end=3)
    b = _stamp("c2", "b" * 16, turn_start=0, turn_end=1)
    forward = _snap(a, b)
    reversed_snap = _snap(b, a)
    assert DeltaPacker().pack(forward) == DeltaPacker().pack(reversed_snap)


def test_delta_overflow_reported_never_dropped() -> None:
    """Overflow chunk ids are part of the result; every chunk is either packed
    or reported, never silently dropped."""
    snap = _snap(
        *(_stamp(f"c{i}", "z" * 100, turn_start=i, turn_end=i) for i in range(4)),
    )  # each rendered block is ~42 delta tokens
    request = DeltaPacker(budget_tokens=90).pack(snap)
    assert request.packed_chunk_ids == ("c0", "c1")
    assert request.overflow_chunk_ids == ("c2", "c3")
    assert set(request.packed_chunk_ids + request.overflow_chunk_ids) == {"c0", "c1", "c2", "c3"}
    assert request.delta_tokens <= 90


def test_delta_never_splits_a_chunk() -> None:
    snap = _snap(_stamp("c1", "a" * 200, turn_start=0, turn_end=1))  # ~69 block tokens
    request = DeltaPacker(budget_tokens=10).pack(snap)
    assert request.overflow_chunk_ids == ("c1",)
    assert request.delta == ""
    assert request.delta_tokens == 0


# ---------------------------------------------------------------- dynamic budget (FR-2.5)


def test_resolve_delta_budget_floor_midpoint_ceiling() -> None:
    """budget = clamp(backlog_tokens, 5k, 32k) exactly as designed (design/02 §6):

    the floor keeps micro-backlogs above the pathological empty-delta edge, and
    the ceiling bounds the single-dream cloud cost to ~$0.0045 (NFR-2.2)."""
    assert resolve_delta_budget(0) == DELTA_BUDGET_FLOOR_TOKENS
    assert resolve_delta_budget(100) == DELTA_BUDGET_FLOOR_TOKENS
    assert resolve_delta_budget(4999) == DELTA_BUDGET_FLOOR_TOKENS
    assert resolve_delta_budget(5000) == 5000
    assert resolve_delta_budget(20000) == 20000
    assert resolve_delta_budget(32000) == 32000
    assert resolve_delta_budget(50000) == DELTA_BUDGET_CEILING_TOKENS


def test_delta_pack_dynamic_resolves_to_backlog_inside_the_band() -> None:
    """Inside the band the no-arg packer spends exactly the measured backlog:
    no feedback loop, no persisted state, deterministic per input."""
    snap = _snap(
        *(_stamp(f"c{i}", "z" * 500, turn_start=i, turn_end=i) for i in range(40)),
    )
    render = render_chunk_blocks(snap.chunks)
    backlog = estimate_tokens(render)
    assert backlog > DELTA_BUDGET_FLOOR_TOKENS  # ~5.7k, just inside the band
    request = DeltaPacker().pack(snap)
    assert request.budget_tokens == backlog
    assert request.overflow_chunk_ids == ()
    assert request.delta_tokens == backlog


def test_delta_pack_dynamic_below_floor_uses_floor_budget() -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode", turn_start=0, turn_end=1))
    request = DeltaPacker().pack(snap)
    assert request.budget_tokens == DELTA_BUDGET_FLOOR_TOKENS
    assert request.overflow_chunk_ids == ()
    assert request.delta_tokens > 0


def test_delta_report_carries_resolved_budget() -> None:
    """The resolved budget rides on the report (and the request): the budget
    value is observable per dream, not a silent internal (design/02 §6)."""
    snap = _snap(_stamp("c1", "I prefer dark mode", turn_start=0, turn_end=1))
    packer = DeltaPacker()
    request = packer.pack(snap)
    report = packer.report(request)
    assert report.budget_tokens == request.budget_tokens == DELTA_BUDGET_FLOOR_TOKENS


def test_delta_pack_explicit_budget_arg_still_wins() -> None:
    """A caller-supplied budget still binds (regression fence): the dynamic
    resolution applies ONLY to the no-arg default; every historic caller that
    passed an explicit budget keeps today's exact behavior."""
    snap = _snap(
        *(_stamp(f"c{i}", "z" * 100, turn_start=i, turn_end=i) for i in range(20)),
    )
    dynamic = DeltaPacker().pack(snap)
    assert dynamic.budget_tokens == resolve_delta_budget(estimate_tokens(render_chunk_blocks(snap.chunks)))
    assert dynamic.overflow_chunk_ids == ()  # ~2.3k tokens pack fully
    explicit = DeltaPacker(budget_tokens=300).pack(snap)
    assert explicit.budget_tokens == 300
    assert explicit.overflow_chunk_ids  # explicit cap binds, the backlog truncates


def test_delta_prefix_excluded_from_budget() -> None:
    """System instruction + stable context never count against the delta budget:
    a very large cache prefix must not push chunks into overflow (FR-2.5)."""
    chunks = _snap(*(_stamp(f"c{i}", "z" * 40, turn_start=i, turn_end=i) for i in range(3)))
    digest = _FixedDigest("g" * 20000)  # ~5000 cached prefix tokens on its own
    request = DeltaPacker(budget_tokens=100, graph_digest=digest).pack(chunks)
    assert request.prefix_tokens >= 5000
    assert request.overflow_chunk_ids == ()
    assert request.packed_chunk_ids == ("c0", "c1", "c2")


def test_delta_budget_dynamic_resolves_from_backlog_and_caps_at_ceiling() -> None:
    """FR-2.5 / NFR-2.2: the no-arg packer resolves its budget from the pending
    backlog — 44 chunks rendering to ~11k tokens land inside the 5k..32k band,
    so the resolved budget EQUALS the measured backlog and nothing overflows."""
    snap = _snap(
        *(_stamp(f"c{i}", "z" * 1000, turn_start=i, turn_end=i) for i in range(44)),
    )
    assert _packed_tokens(render_chunk_blocks(snap.chunks)) > 11000
    request = DeltaPacker().pack(snap)
    assert request.budget_tokens == request.delta_tokens  # resolved budget == backlog
    assert request.overflow_chunk_ids == ()
    # NFR-2.2 cap proof: a ~38k-token backlog is clamped to the 32k ceiling and
    # the excess is reported as overflow, never sent to the cloud.
    oversized = _snap(
        *(_stamp(f"c{i}", "z" * 5000, turn_start=i, turn_end=i) for i in range(30)),
    )
    assert _packed_tokens(render_chunk_blocks(oversized.chunks)) > 32000
    capped = DeltaPacker().pack(oversized)
    assert capped.budget_tokens == DELTA_BUDGET_CEILING_TOKENS
    assert capped.delta_tokens <= DELTA_BUDGET_CEILING_TOKENS
    assert capped.overflow_chunk_ids  # a later dream picks these up
    assert len(capped.packed_chunk_ids) + len(capped.overflow_chunk_ids) == 30


# ---------------------------------------------------------------- cache prefix


def test_cache_prefix_byte_stable_across_dreams_of_same_profile() -> None:
    dream_a = _snap(_stamp("c1", "I prefer dark mode"), snapshot_id="snap-a", created_at=1.0)
    dream_b = _snap(_stamp("c9", "I like coffee"), snapshot_id="snap-b", created_at=999.0)
    packer = DeltaPacker()
    assert packer.pack(dream_a).cache_prefix == packer.pack(dream_b).cache_prefix
    assert packer.pack(dream_a).cache_prefix == build_cache_prefix("")


def test_cache_prefix_excludes_per_dream_data() -> None:
    snap = _snap(_stamp("c7", "I prefer dark mode"), snapshot_id="snap-secret", created_at=1234.0)
    prefix = DeltaPacker().pack(snap).cache_prefix
    assert "snap-secret" not in prefix
    assert "c7" not in prefix
    assert "1234" not in prefix
    assert "1000.0" not in prefix


def test_cache_prefix_includes_injected_graph_digest() -> None:
    digest = _FixedDigest("digest-abc")
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    packer = DeltaPacker(graph_digest=digest)
    prefix = packer.pack(snap).cache_prefix
    assert digest.calls == 1
    assert "digest-abc" in prefix
    assert "Known graph digest" in prefix
    # still byte-stable across dreams when the digest is stable
    other = _snap(_stamp("c9", "zzz"), snapshot_id="snap-other", created_at=5.0)
    assert packer.pack(other).cache_prefix == prefix


def test_null_graph_digest_renders_no_section() -> None:
    assert NullGraphDigest().digest("alice") == ""
    assert DeltaPacker(graph_digest=NullGraphDigest()).pack(
        _snap(_stamp("c1", "hi"))
    ).cache_prefix == build_cache_prefix("")


def test_graph_digest_protocol_seam_is_satisfied_by_duck_typed_provider() -> None:
    provider: GraphDigest = _FixedDigest("stable")
    assert provider.digest("alice") == "stable"


# ---------------------------------------------------------------- delta report


def test_delta_report_tracks_delta_prefix_overflow() -> None:
    snap = _snap(
        *(_stamp(f"c{i}", "z" * 100, turn_start=i, turn_end=i) for i in range(30)),
    )
    packer = DeltaPacker(budget_tokens=200)
    request = packer.pack(snap)
    report = packer.report(request)
    assert report.delta_tokens == request.delta_tokens
    assert report.prefix_tokens == request.prefix_tokens
    assert report.overflow_count == len(request.overflow_chunk_ids)
    assert report.delta_tokens <= 200
    assert report.overflow_count > 0
    assert report.prefix_tokens == estimate_tokens(request.cache_prefix)


# ---------------------------------------------------------------- orchestrator integration


def test_orchestrator_default_preserves_full_render_behavior(tmp_path: Path) -> None:
    """No budget pressure: the deltas the LLM sees ARE the full snapshot render,
    split as (stable cache prefix -> system, chunk blocks -> user)."""
    snap = _snap(
        _stamp("c1", "I prefer dark mode", turn_start=2, turn_end=3),
        _stamp("c2", "I like coffee", turn_start=0, turn_end=1),
    )
    llm = _RecordingLLM()
    outcome = ReflectOrchestrator(llm=llm, directory=tmp_path, packer=DeltaPacker()).reflect(snap)
    assert outcome.ok
    assert outcome.result is not None
    assert len(outcome.result.triples) == 2
    assert len(llm.calls) == 1
    assert llm.calls[0][0] == build_cache_prefix("")
    assert llm.calls[0][1] == render_chunk_blocks(snap.chunks)
    assert outcome.report is not None
    assert outcome.report.overflow_count == 0


def test_orchestrator_outcome_carries_delta_report(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    packer = DeltaPacker()
    outcome = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path, packer=packer).reflect(snap)
    assert outcome.ok
    assert outcome.report is not None
    assert isinstance(outcome.report, DeltaReport)
    assert outcome.report.delta_tokens > 0
    assert outcome.report.delta_tokens == estimate_tokens(render_chunk_blocks(snap.chunks))


def test_orchestrator_overflow_reflects_packed_subset_only(tmp_path: Path) -> None:
    """Chunks beyond the budget are deferred (reported as overflow), never an
    error, and never reflected this pass."""
    coffee = (_stamp(f"c{i}", "I prefer coffee", turn_start=i, turn_end=i) for i in range(2, 12))
    snap = _snap(_stamp("c1", "I prefer dark mode", turn_start=0, turn_end=1), *coffee)
    packer = DeltaPacker(budget_tokens=40)
    request = packer.pack(snap)
    assert request.overflow_chunk_ids  # coffee chunks beyond the cap are deferred

    outcome = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path, packer=packer).reflect(snap)
    assert outcome.ok  # overflow is never an error
    assert outcome.report is not None
    assert outcome.report.overflow_count == len(request.overflow_chunk_ids)
    for triple in outcome.result.triples or ():
        for cid in triple.chunk_ids:
            assert cid in request.packed_chunk_ids


def test_orchestrator_marker_gate_skips_packing(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"), phases=frozenset({"snapshot_done", "reflect_done"}))
    llm = _RecordingLLM()
    outcome = ReflectOrchestrator(llm=llm, directory=tmp_path).reflect(snap)
    assert outcome.skipped is True
    assert outcome.report is None  # skipped dreams never call the cloud
    assert llm.calls == []


def test_orchestrator_empty_snapshot_reports_zero_delta(tmp_path: Path) -> None:
    snap = _snap()
    packer = DeltaPacker()
    outcome = ReflectOrchestrator(
        llm=StubReflectLLM(),
        directory=tmp_path,
        packer=packer,
        on_done=lambda profile_id: None,
    ).reflect(snap)
    assert outcome.ok
    assert outcome.result is not None
    assert outcome.result.triples == ()
    assert outcome.report is not None
    assert outcome.report.delta_tokens == 0
    assert outcome.report.overflow_count == 0


# ---------------------------------------------------------------- D1 data-loss defenses
#
# The delta layer must never be the reason a source chunk is lost. Without a
# guard, a snapshot whose chunks all exceed the budget produced an empty delta,
# the LLM was still called, zero triples merged, and the safe-clear purged the
# source rows the model never saw. Two defense lines break that chain:
#
# 1. Orchestrator (reflect boundary): when nothing packed but overflow exists,
#    the dream is deferred -- no cloud call, no REFLECT_DONE, the snapshot stays
#    journaled so a later dream (bigger budget / manual run) picks the overflow
#    up.
# 2. Pipeline (merge boundary): a result that is empty BECAUSE the delta was
#    truncated (``overflow_chunk_ids`` non-empty) is never handed to the merger,
#    so the commit callback cannot fire the purge. A genuinely empty result with
#    NO overflow (all-noise session) still merges and safe-clears normally.


class _VectorFake:
    """VectorStore/GraphStore-shaped double: snapshot_read + purge_range plus
    the merger's idempotent-write seams (exercised only when triples exist)."""

    def __init__(self, chunks: list[ChunkStamp] | None = None) -> None:
        self.chunks = list(chunks or [])
        self.purged: list[tuple[str, int, int]] = []
        self.marked: list[str] = []  # consumed-ids-scoped safe-clear (mark path)

    def capabilities(self) -> frozenset[object]:
        return frozenset()

    def snapshot_read(self, filter: object) -> list[ChunkStamp]:
        return [c for c in self.chunks if c.profile_id == getattr(filter, "profile_id", None)]

    def delete_chunk(self, chunk_id: str) -> None:
        self.chunks = [c for c in self.chunks if c.chunk_id != chunk_id]

    def mark_consolidated(self, chunk_ids: object) -> None:
        ids = set(chunk_ids)
        self.marked.extend(chunk_ids)
        self.chunks = [
            c.model_copy(update={"consolidated": True}) if c.chunk_id in ids else c for c in self.chunks
        ]

    def purge_range(self, session_id: str, turn_start: int, turn_end: int) -> int:
        self.purged.append((session_id, turn_start, turn_end))
        before = len(self.chunks)
        self.chunks = [
            c
            for c in self.chunks
            if not (
                c.provenance.session_id == session_id
                and c.turn_start is not None
                and c.turn_end is not None
                and c.turn_start <= turn_end
                and c.turn_end >= turn_start
            )
        ]
        return before - len(self.chunks)

    def find_same_predicate(self, subject: str, predicate: str, profile_id: str) -> list[Any]:
        del subject, predicate, profile_id
        return []

    def upsert_node(self, node: Any) -> None:
        del node


class _MetaFake:
    """MetaStore-shaped double: the FileSnapshotter's dream-run registration."""

    def __init__(self) -> None:
        self.runs: list[Any] = []

    def record_dream_run(self, run: Any) -> str:
        self.runs.append(run)
        return str(getattr(run, "run_id", ""))


class _RecordingMerger(Merger):
    """The real Merger plus a call counter, so tests can distinguish 'blocked
    before merge' from 'merge ran and committed'."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.call_count = 0

    def merge(self, snapshot: Snapshot, result: ReflectionResult) -> MergeOutcome:
        self.call_count += 1
        return super().merge(snapshot, result)


class _NeverReflector:
    """Reflector double that records, and must never be handed a merge-boundary
    snapshot (reflect is never re-run once REFLECT_DONE is journaled)."""

    def __init__(self) -> None:
        self.calls: list[Snapshot] = []

    def reflect(self, snapshot: Snapshot) -> ReflectOutcome:
        self.calls.append(snapshot)
        return ReflectOutcome(ok=True, result=None)


def _event(profile: str = "alice", rng: TurnRange = _RANGE) -> PoolEvent:
    return PoolEvent(
        kind=PoolEventKind.DREAM_TRIGGER,
        profile_id=profile,
        turn_range=rng,
        balance=12.0,
        fired_at=1.0,
    )


def _chain(
    tmp_path: Path,
    store: _VectorFake,
    *,
    budget: int = DEFAULT_DELTA_BUDGET_TOKENS,
) -> tuple[FileSnapshotter, DreamTrigger, _RecordingLLM, _RecordingMerger]:
    """Production-shaped wiring: trigger intake -> FileSnapshotter capture ->
    real ReflectOrchestrator -> real Merger -> trigger safe-clear purger."""
    meta = _MetaFake()
    fs = FileSnapshotter(store=store, meta=meta, directory=tmp_path / "dreams")
    trigger = DreamTrigger(snapshotter=fs, auto_trigger=True, purger=fs.purge_snapshot)
    llm = _RecordingLLM()
    reflector = ReflectOrchestrator(
        llm=llm,
        directory=tmp_path / "dreams",
        packer=DeltaPacker(budget_tokens=budget),
        on_done=trigger.on_reflect_complete,
    )
    merger = _RecordingMerger(
        graph_main=store,
        graph_isolated=None,
        meta=meta,
        on_committed=trigger.on_merge_committed,
    )
    pipeline = DreamPipeline(trigger=trigger, snapshotter=fs, reflector=reflector, merger=merger)
    fs.on_ready = pipeline.on_snapshot_ready
    return fs, trigger, llm, merger


def test_d1_full_overflow_reflect_defers_and_never_calls_cloud(tmp_path: Path) -> None:
    """Defense line 1 (orchestrator): a snapshot whose every chunk is over the
    delta budget is deferred, not reflected. Nothing hits the LLM, no REFLECT_DONE
    is persisted, and the report still carries the overflow count."""
    snap = _snap(_stamp("huge", "I prefer dark mode. " * 7000))
    packer = DeltaPacker()  # dynamic budget: the ~35k-token chunk exceeds the 32k ceiling
    assert packer.pack(snap).overflow_chunk_ids == ("huge",)
    llm = _RecordingLLM()
    done: list[str] = []
    outcome = ReflectOrchestrator(
        llm=llm,
        directory=tmp_path / "dreams",
        packer=packer,
        on_done=lambda p: done.append(p),
    ).reflect(snap)
    assert outcome.ok is False
    assert outcome.result is None
    assert "delta budget" in (outcome.error or "")
    assert outcome.report is not None
    assert outcome.report.overflow_count == 1
    assert outcome.report.delta_tokens == 0
    assert llm.calls == []  # the cloud call is skipped entirely
    assert done == []  # on_done never fired: the snapshot stays at the reflect boundary
    assert list(tmp_path.glob("*.json")) == []  # nothing journaled for a deferred dream


def test_d1_verifier_repro_over_budget_chunk_survives_then_later_dream_completes(
    tmp_path: Path,
) -> None:
    """Verifier repro (the data-loss chain is broken): a snapshot whose only
    chunk does not fit the default budget survives the full reflect -> merge ->
    commit -> safe-clear chain untouched, stays journaled at the reflect
    boundary, and a later dream with a bigger budget picks it up and completes
    the commit + clear-as-mark normally."""
    store = _VectorFake([_stamp("huge", "I prefer dark mode. " * 2200)])
    fs, trigger, llm, merger = _chain(tmp_path, store)
    trigger.handle_event(_event())

    # defense 1 engages at the reflect boundary: nothing to pack -> no cloud call
    assert llm.calls == []
    assert merger.call_count == 0  # the merger is never reached
    assert store.purged == []  # the safe-clear never fired
    assert store.marked == []  # no mark either
    assert [c.chunk_id for c in store.chunks] == ["huge"]  # the chunk survives

    snapshot = fs.active("alice")
    assert snapshot is not None
    pending = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams").recover()
    assert [s.snapshot_id for s in pending] == [snapshot.snapshot_id]
    assert resume_boundary(pending[0]) == "reflect"  # re-pickable by a later dream

    # A2.5: the deferred dream's failure report reset the trigger's in-flight
    # bookkeeping (no stale DREAMING state is left behind, exactly like a real
    # reflect failure). The later dream re-picks the journaled snapshot through
    # the NFR-2.3 recovery seam -- the same arm the daemon uses at boot.
    assert trigger.resume("alice", snapshot.turn_range) is True

    # a later dream (same profile, bigger budget) re-processes the retained chunk:
    # it packs, reflects one triple, commits, and the safe-clear completes
    llm2 = _RecordingLLM()
    pipeline2 = DreamPipeline(
        trigger=trigger,
        snapshotter=fs,
        reflector=ReflectOrchestrator(
            llm=llm2,
            directory=tmp_path / "dreams",
            packer=DeltaPacker(budget_tokens=12000),
            on_done=trigger.on_reflect_complete,
        ),
        merger=_RecordingMerger(
            graph_main=store,
            graph_isolated=None,
            meta=_MetaFake(),
            on_committed=trigger.on_merge_committed,
        ),
    )
    pipeline2.run(snapshot)

    assert len(llm2.calls) == 1
    assert "I prefer dark mode" in llm2.calls[0][1]  # the retained chunk reached the model
    # the safe-clear is id-scoped now: exactly the consumed row, never a range delete
    assert store.marked == ["huge"]  # marked consolidated, never deleted
    assert store.purged == []
    assert [c.chunk_id for c in store.chunks] == ["huge"]  # retained as evidence scene
    assert store.chunks[0].consolidated is True


def test_d1_partial_overflow_with_empty_triples_defers_merge(tmp_path: Path) -> None:
    """Defense line 2 (pipeline): the packed delta reflects fine, but with the
    overflow chunk flagged and ZERO triples extracted, the snapshot is NOT handed
    to the merger -- committing would clear source chunks the model never saw."""
    noise = _stamp("noise", "lorem ipsum dolor sit amet", turn_start=0, turn_end=1)
    huge = _stamp("huge", "z" * 44000, turn_start=0, turn_end=1)
    store = _VectorFake([noise, huge])
    fs, trigger, llm, merger = _chain(tmp_path, store)
    trigger.handle_event(_event())

    assert len(llm.calls) == 1  # the packed (noise-only) delta WAS reflected
    assert "huge" not in llm.calls[0][1]  # the overflow chunk never reached the model
    assert merger.call_count == 0  # engine-side insurance: merge blocked
    assert store.purged == []  # no commit, no clear
    assert store.marked == []  # and no mark either
    assert {c.chunk_id for c in store.chunks} == {"noise", "huge"}  # nothing dropped


def test_d1_merge_boundary_recovery_respects_persisted_overflow(tmp_path: Path) -> None:
    """The overflow flag survives the journal round-trip: a crashed dream that
    reflected a truncated delta and crashed before merge resumes at the MERGE
    boundary with the guard active -- reflect is never re-run and the deferred
    merge cannot mark the overflow chunk."""
    noise = _stamp("noise", "lorem ipsum dolor sit amet", turn_start=0, turn_end=1)
    huge = _stamp("huge", "z" * 140000, turn_start=0, turn_end=1)  # ~35k tokens: over the 32k ceiling
    store = _VectorFake([noise, huge])
    fs1 = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    snap = fs1.request("alice", _RANGE).snapshot
    assert snap is not None
    outcome = ReflectOrchestrator(
        llm=_RecordingLLM(),
        directory=tmp_path / "dreams",
        packer=DeltaPacker(),
    ).reflect(snap)
    assert outcome.ok
    assert outcome.result is not None
    assert outcome.result.triples == ()
    assert outcome.result.overflow_chunk_ids == ("huge",)

    # a fresh boot recovers at the merge boundary with the payload intact
    fs2 = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    pending = fs2.recover()
    assert len(pending) == 1
    assert resume_boundary(pending[0]) == "merge"
    fs2.adopt(pending[0])
    assert pending[0].turn_range == _RANGE

    reflector = _NeverReflector()
    merger = _RecordingMerger(
        graph_main=store,
        graph_isolated=None,
        meta=_MetaFake(),
        on_committed=lambda p: fs2.purge_snapshot(p, _RANGE),
    )
    pipeline = DreamPipeline(
        trigger=DreamTrigger(snapshotter=NullSnapshotter(), purger=fs2.purge_snapshot),
        snapshotter=fs2,
        reflector=reflector,  # type: ignore[arg-type]
        merger=merger,
    )
    pipeline.run(pending[0])

    assert reflector.calls == []  # reflect must never re-run at the merge boundary
    assert merger.call_count == 0  # the persisted overflow held the guard
    assert store.purged == []
    assert store.marked == []
    assert {c.chunk_id for c in store.chunks} == {"noise", "huge"}


def test_d1_control_all_noise_without_overflow_still_commits_and_marks(tmp_path: Path) -> None:
    """Control: a legitimately empty result with NO overflow (an all-noise
    session) must still merge, commit, and safe-clear exactly as it did before
    the delta layer -- the guard only defers overflow-truncated empties."""
    store = _VectorFake(
        [
            _stamp("noise-a", "lorem ipsum dolor sit amet", turn_start=0, turn_end=1),
            _stamp("noise-b", "consectetur adipiscing elit sed do eiusmod", turn_start=0, turn_end=1),
        ]
    )
    fs, trigger, llm, merger = _chain(tmp_path, store)
    trigger.handle_event(_event())

    assert len(llm.calls) == 1
    assert merger.call_count == 1  # merge ran and committed (empty result, no overflow)
    # no overflow: the allow-list equals every chunk, so the id-scoped clear is
    # behavior-equivalent to the old full-range clear -- both rows are marked
    assert sorted(store.marked) == ["noise-a", "noise-b"]
    assert store.purged == []
    assert {c.chunk_id for c in store.chunks} == {"noise-a", "noise-b"}  # retained
    assert all(c.consolidated for c in store.chunks)
    assert trigger.status("alice").state is DreamState.IDLE
    assert FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams").recover() == []


def test_purge_snapshot_explicit_consumed_allow_list_is_id_scoped(tmp_path: Path) -> None:
    """The clear seam accepts an explicit id allow-list (or equivalent mechanism):
    only those rows are marked consolidated, never-seen rows stay unmarked, and
    a merge-complete snapshot never re-clears."""
    store = _VectorFake(
        [
            _stamp("c1", "a" * 8, turn_start=0, turn_end=1),
            _stamp("c2", "b" * 8, turn_start=2, turn_end=3),
        ]
    )
    fs = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    assert fs.request("alice", _RANGE).ok
    assert fs.purge_snapshot("alice", _RANGE, consumed_chunk_ids=["c1"]) == 1
    assert sorted(c.chunk_id for c in store.chunks) == ["c1", "c2"]  # both retained
    assert store.purged == []  # id-scoped: no range purge fired
    assert store.marked == ["c1"]
    assert next(c for c in store.chunks if c.chunk_id == "c1").consolidated is True
    assert not any(c.consolidated for c in store.chunks if c.chunk_id == "c2")
    # marker guard: a re-drive is a no-op, never a double clear
    assert fs.purge_snapshot("alice", _RANGE, consumed_chunk_ids=["c1", "c2"]) == 0
    assert sorted(c.chunk_id for c in store.chunks) == ["c1", "c2"]
    assert not any(c.consolidated for c in store.chunks if c.chunk_id == "c2")


def test_d1_verifier_probe_partial_overflow_with_triples_keeps_overflow_chunks(
    tmp_path: Path,
) -> None:
    """Verifier probe (the HIGH data-loss residual, fixed end-to-end): 22 chunks
    over a 60-turn window at the default 10000-token budget pack 19 (c0-c18) and
    overflow 3 (c19-c21). One triple is extracted so the
    merge commits -- but the safe-clear now marks ONLY the consumed rows, so
    the 3 chunks the model never saw stay unmarked in the store for a later
    dream instead of being silently cleared."""
    store = _VectorFake(
        [
            _stamp(f"c{i}", "I prefer dark mode. " * 100, turn_start=i * 2, turn_end=i * 2 + 1)
            for i in range(22)
        ]
    )
    fs, trigger, llm, merger = _chain(tmp_path, store)
    trigger.handle_event(_event(rng=TurnRange(0, 60)))

    assert len(llm.calls) == 1
    assert llm.calls[0][1].count("<chunk>") == 19  # exactly the packed window reached the model
    assert "c0" in llm.calls[0][1] and "c18" in llm.calls[0][1]
    assert "c19" not in llm.calls[0][1] and "c21" not in llm.calls[0][1]
    assert merger.call_count == 1  # one triple extracted -> the commit goes through
    assert set(store.marked) == {f"c{i}" for i in range(19)}  # consumed rows only
    assert store.purged == []  # id-scoped, never range-scoped
    remaining = {c.chunk_id: c.consolidated for c in store.chunks}
    assert set(remaining) == {f"c{i}" for i in range(22)}  # every chunk retained
    assert all(remaining[c] for c in remaining if c in {f"c{i}" for i in range(19)})  # packed marked
    overflow_ids = {f"c{i}" for i in range(19, 22)}
    assert not any(remaining[c] for c in remaining if c in overflow_ids)  # overflow unmarked
    assert trigger.status("alice").state is DreamState.IDLE


def test_d1_recovery_partial_overflow_with_triples_marks_only_consumed(tmp_path: Path) -> None:
    """Merge-boundary recovery WITH triples: the journaled consumed allow-list
    survives the boot round-trip, so the resumed committed merge marks ONLY the
    packed rows -- the never-reflected overflow chunk stays unmarked for a later
    dream (verifier ask: packed ids read back from the journal at resume)."""
    chunks = [
        _stamp(f"c{i}", "I prefer dark mode", turn_start=i * 2, turn_end=i * 2 + 1) for i in range(5)
    ] + [_stamp("huge", "z" * 140000, turn_start=50, turn_end=51)]  # ~35k tokens: over the 32k ceiling
    store = _VectorFake(chunks)
    fs1 = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    snap = fs1.request("alice", TurnRange(0, 60)).snapshot
    assert snap is not None
    outcome = ReflectOrchestrator(
        llm=_RecordingLLM(),
        directory=tmp_path / "dreams",
        packer=DeltaPacker(),
    ).reflect(snap)
    assert outcome.ok
    assert outcome.result is not None
    assert len(outcome.result.triples) == 1  # a real triple is waiting to commit
    assert outcome.result.overflow_chunk_ids == ("huge",)

    # crash after reflect, before merge: a fresh boot recovers at the merge boundary
    fs2 = FileSnapshotter(store=store, meta=_MetaFake(), directory=tmp_path / "dreams")
    pending = fs2.recover()
    assert len(pending) == 1
    assert resume_boundary(pending[0]) == "merge"
    fs2.adopt(pending[0])
    assert pending[0].reflect_result is not None  # the journal payload came back

    reflector = _NeverReflector()
    merger = _RecordingMerger(
        graph_main=store,
        graph_isolated=None,
        meta=_MetaFake(),
        on_committed=lambda p: fs2.purge_snapshot(p, pending[0].turn_range),
    )
    pipeline = DreamPipeline(
        trigger=DreamTrigger(snapshotter=NullSnapshotter(), purger=fs2.purge_snapshot),
        snapshotter=fs2,
        reflector=reflector,  # type: ignore[arg-type]
        merger=merger,
    )
    pipeline.run(pending[0])

    assert reflector.calls == []  # reflect never re-runs at the merge boundary
    assert merger.call_count == 1  # triples present -> the committed merge proceeds
    assert sorted(store.marked) == ["c0", "c1", "c2", "c3", "c4"]  # consumed rows only
    assert store.purged == []
    remaining = {c.chunk_id: c.consolidated for c in store.chunks}
    assert set(remaining) == {f"c{i}" for i in range(5)} | {"huge"}  # every chunk retained
    assert all(remaining[f"c{i}"] for i in range(5))
    assert remaining["huge"] is False  # the overflow chunk stays unmarked


# ---------------------------------------------------------------- FR-2.5b monthly token ledger
#
# The monthly token ledger records what a dream consumed (packed delta +
# provider-reported output tokens) into the current UTC month (T3b: pure token
# bookkeeping, the USD budget gate was removed). It is observability-only —
# it never defers a reflect. Auto-recovery falls out of UTC year-month keying:
# a fresh month reads a zero counter, so no rollover job exists.


class _LedgerMetaFake:
    """MetaStore-shaped double for the ledger's two port calls + the audit seam."""

    def __init__(self) -> None:
        self.counters: dict[tuple[str, str], int] = {}
        self.audit: list[AuditEntry] = []

    def add_token_usage(self, profile_id: str, year_month: str, tokens: int) -> None:
        key = (profile_id, year_month)
        self.counters[key] = self.counters.get(key, 0) + tokens

    def token_usage(self, profile_id: str, year_month: str) -> int:
        return self.counters.get((profile_id, year_month), 0)

    def audit_append(self, entry: AuditEntry) -> None:
        self.audit.append(entry)


class _UsageLLM:
    """ChatLLM double: returns an empty reflection with provider-reported usage."""

    def __init__(self, usage: Usage) -> None:
        self._usage = usage
        self.calls = 0

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system, user
        self.calls += 1
        return ChatResult(text="[]", usage=self._usage, model="test-model", driver="stub")


class _LedgerClock:
    def __init__(self, ts: float) -> None:
        self.ts = ts

    def __call__(self) -> float:
        return self.ts


_AUG = 1785542400.0  # 2026-08-01T00:00:00Z
_SEP = 1788220800.0  # 2026-09-01T00:00:00Z


def test_reflect_success_records_delta_and_output_to_ledger(tmp_path: Path) -> None:
    """A successful reflect meters the packed delta plus the provider-reported
    output tokens into the current UTC month (NFR-2.2 substrate)."""
    meta = _LedgerMetaFake()
    ledger = TokenLedger(meta, clock=_LedgerClock(_AUG))
    snap = _snap(_stamp("c1", "I prefer dark mode", turn_start=0, turn_end=1))
    llm = _UsageLLM(
        Usage(
            prompt_tokens=10,
            completion_tokens=50,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=0,
        )
    )
    outcome = ReflectOrchestrator(
        llm=llm,
        directory=tmp_path / "dreams",
        ledger=ledger,
    ).reflect(snap)
    assert outcome.ok
    assert outcome.report is not None
    assert outcome.report.provider_usage is not None
    assert outcome.report.provider_usage.completion_tokens == 50
    assert ledger.usage("alice") == outcome.report.delta_tokens + 50


def test_reflect_never_gated_by_prior_month_usage(tmp_path: Path) -> None:
    """T3b: the ledger has no budget gate — a month that already recorded
    millions of tokens does NOT defer the next dream; the ledger only counts."""
    meta = _LedgerMetaFake()
    ledger = TokenLedger(meta, clock=_LedgerClock(_AUG))
    ledger.record("alice", delta_tokens=4_000_000)  # a heavy prior month
    snap = _snap(_stamp("c1", "I prefer dark mode", turn_start=0, turn_end=1))
    llm = _UsageLLM(
        Usage(prompt_tokens=1, completion_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    )
    outcome = ReflectOrchestrator(
        llm=llm,
        directory=tmp_path / "dreams",
        ledger=ledger,
    ).reflect(snap)
    assert outcome.ok
    assert llm.calls == 1  # never capture-only
    assert ledger.usage("alice") == 4_000_000 + outcome.report.delta_tokens


def test_reflect_metering_rolls_over_with_the_utc_month(tmp_path: Path) -> None:
    """T3b: the year-month key keeps working — the next UTC month reads a fresh
    zero counter, and a new reflect records into the new month only."""
    meta = _LedgerMetaFake()
    clock = _LedgerClock(_AUG)
    ledger = TokenLedger(meta, clock=clock)
    snap = _snap(_stamp("c1", "I prefer dark mode", turn_start=0, turn_end=1))
    llm = _UsageLLM(
        Usage(prompt_tokens=1, completion_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    )
    first = ReflectOrchestrator(llm=llm, directory=tmp_path / "dreams", ledger=ledger).reflect(snap)
    assert first.ok
    august_usage = first.report.delta_tokens
    clock.ts = _SEP  # September 1 rolls the year-month key over
    assert ledger.usage("alice") == 0
    second = ReflectOrchestrator(llm=llm, directory=tmp_path / "dreams", ledger=ledger).reflect(snap)
    assert second.ok
    assert ledger.usage("alice") == second.report.delta_tokens
    assert second.report.delta_tokens == august_usage  # the same dream, same cost


# ---------------------------------------------------------------- config ceiling (T3a / AC4)


def _oversized_snapshot() -> Snapshot:
    """A backlog whose render comfortably exceeds any 5k..32k ceiling."""
    return _snap(
        *(_stamp(f"c{i}", "z" * 5000, turn_start=i, turn_end=i) for i in range(30)),
    )


def test_delta_pack_ceiling_follows_config_key() -> None:
    """The dynamic clamp's ceiling reads the dream.delta_budget_ceiling_tokens
    config key (a packer bound to a live Config), not the module constant."""
    from mnemoseed_local.config import Config, DreamConfig

    config = Config()
    config.dream = DreamConfig(delta_budget_ceiling_tokens=8000)
    request = DeltaPacker(config=config).pack(_oversized_snapshot())
    assert request.budget_tokens == 8000
    assert request.delta_tokens <= 8000
    assert request.overflow_chunk_ids  # the excess is reported, never dropped


def test_delta_pack_ceiling_without_config_uses_constant_default() -> None:
    """Unbound packers keep the module constant as the ceiling source (the
    constant remains the default value source for the config key)."""
    request = DeltaPacker().pack(_oversized_snapshot())
    assert request.budget_tokens == DELTA_BUDGET_CEILING_TOKENS
    assert request.delta_tokens <= DELTA_BUDGET_CEILING_TOKENS


def test_delta_pack_ceiling_hot_applies_to_next_pack() -> None:
    """The packer holds a live Config reference: a configwrite ceiling change
    affects the NEXT pack of the SAME packer instance (no daemon restart)."""
    from mnemoseed_local.config import Config, DreamConfig

    config = Config()
    packer = DeltaPacker(config=config)
    first = packer.pack(_oversized_snapshot())
    assert first.budget_tokens == DELTA_BUDGET_CEILING_TOKENS  # 32000 default

    # hot-apply: the configwrite seam replaces config.dream on the SAME object
    config.dream = DreamConfig(delta_budget_ceiling_tokens=8000)
    second = packer.pack(_oversized_snapshot())
    assert second.budget_tokens == 8000
    assert second.delta_tokens <= 8000
    assert second.overflow_chunk_ids
