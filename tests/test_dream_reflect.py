"""Dream reflection orchestrator (PRD-02 T3; FR-2.2 / FR-2.12 / design/02 §4-§5, §7).

The orchestrator consumes an adopted Snapshot (from the trigger's DREAMING
state), runs the reflection pipeline through the narrow ReflectLLM seam
(a deterministic StubReflectLLM in M1 and in tests), folds duplicate mentions
(AC-3), persists the REFLECT_DONE marker before advancing progress, degrades
with exponential-backoff retry x3 on failure (never raising, never blocking
ingestion), and reports completion through trigger.on_reflect_complete.

Tests assert behavior through the public surface: ReflectOrchestrator.reflect,
StubReflectLLM, ReflectionResult / ReflectedTriple / ReflectOutcome / Route.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.capture.pool import PoolEvent, PoolEventKind
from mnemoseed_local.dream import (
    DreamState,
    DreamTrigger,
    FileSnapshotter,
    NullSnapshotter,
    ReflectedTriple,
    ReflectionResult,
    ReflectLLM,
    ReflectOrchestrator,
    ReflectOutcome,
    Route,
    SnapshotPhase,
    StubReflectLLM,
    load_snapshot_file,
    write_snapshot_file,
)
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import Capability, ChunkFilter, DreamRun, TurnRange

_RANGE = TurnRange(0, 4)

_COMPLETE = frozenset({"snapshot_done", "reflect_done"})


# ---------------------------------------------------------------- fakes


class _Recorder:
    """Records on_done completions."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, profile_id: str) -> None:
        self.calls.append(profile_id)


class _Sleeper:
    """Records injected sleep durations instead of sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, secs: float) -> None:
        self.delays.append(secs)


class _CountingLLM:
    """Wraps another ReflectLLM and counts chat calls."""

    def __init__(self, inner: ReflectLLM) -> None:
        self.inner = inner
        self.calls = 0

    def chat(self, *, system: str, user: str) -> str:
        self.calls += 1
        return self.inner.chat(system=system, user=user)


class _FailingLLM:
    """Fails the first ``failures`` calls, then delegates to the real stub."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def chat(self, *, system: str, user: str) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("reflect model unreachable")
        return StubReflectLLM().chat(system=system, user=user)


class _GarbageLLM:
    """Always returns text that is not parseable JSON."""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *, system: str, user: str) -> str:
        del system, user
        self.calls += 1
        return "certainly not a json array"


class _MarkerProbe:
    """On_done seam that verifies the REFLECT_DONE marker was already persisted
    at call time (marker-before-progress ordering lock)."""

    def __init__(self, directory: Path, snapshot_id: str) -> None:
        self.directory = directory
        self.snapshot_id = snapshot_id
        self.reflect_done_at_call = False

    def __call__(self, profile_id: str) -> None:
        del profile_id
        on_disk = load_snapshot_file(self.directory / f"{self.snapshot_id}.json")
        self.reflect_done_at_call = on_disk is not None and SnapshotPhase.REFLECT_DONE.value in on_disk.phases


class _FakeStore:
    """VectorStore-shaped double: snapshot_read only (recovery test)."""

    def __init__(self, chunks: list[ChunkStamp]) -> None:
        self.chunks = chunks

    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    def snapshot_read(self, filter: ChunkFilter) -> list[ChunkStamp]:
        return [c for c in self.chunks if c.profile_id == filter.profile_id]


class _FakeMeta:
    """MetaStore-shaped double recording dream_runs registration."""

    def __init__(self) -> None:
        self.runs: list[DreamRun] = []

    def record_dream_run(self, run: DreamRun) -> str:
        self.runs.append(run)
        return run.run_id


# ---------------------------------------------------------------- helpers


def _stamp(
    chunk_id: str,
    text: str,
    *,
    tier: CognitiveTier = CognitiveTier.TIER_1,
    origin: str = "user",
    persona_id: str | None = None,
    turn_start: int | None = 0,
    turn_end: int | None = 1,
) -> ChunkStamp:
    asserted_by = "user" if origin == "user" else "anima-model"
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id="alice",
        text=text,
        cognitive_tier=tier,
        model_id="anima-model" if origin == "agent" else "test-model",
        persona_id=persona_id,
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by=asserted_by, session_id="s1", source="manual"),
        turn_start=turn_start,
        turn_end=turn_end,
    )


def _snap(
    *chunks: ChunkStamp,
    phases: frozenset[str] = frozenset({"snapshot_done"}),
) -> Snapshot:
    return Snapshot(
        snapshot_id="snap-p1",
        profile_id="alice",
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in chunks),
        created_at=1000.0,
        phases=phases,
    )


def _run_outcome(
    snap: Snapshot,
    directory: Path,
    *,
    on_done=None,
    llm: ReflectLLM | None = None,
    sleeper: _Sleeper | None = None,
) -> ReflectOutcome:
    return ReflectOrchestrator(
        llm=llm or StubReflectLLM(),
        directory=directory,
        on_done=on_done,
        sleep=sleeper or (lambda _: None),
    ).reflect(snap)


def _run(snap: Snapshot, directory: Path, *, on_done=None) -> ReflectionResult:
    outcome = _run_outcome(snap, directory, on_done=on_done)
    assert outcome.ok
    assert outcome.result is not None
    return outcome.result


def _find(result: ReflectionResult, predicate: str, obj: str) -> ReflectedTriple | None:
    for triple in result.triples:
        if triple.predicate == predicate and triple.object == obj:
            return triple
    return None


def _event(profile: str = "alice") -> PoolEvent:
    return PoolEvent(
        kind=PoolEventKind.DREAM_TRIGGER,
        profile_id=profile,
        turn_range=_RANGE,
        balance=12.0,
        fired_at=1.0,
    )


# ---------------------------------------------------------------- extraction + de-biasing


def test_extracts_core_preference_triple_from_user_chunk(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode and vim"))
    result = _run(snap, tmp_path)
    triple = _find(result, "prefers", "dark mode and vim")
    assert triple is not None
    assert triple.route is Route.CORE
    assert triple.tiers == (CognitiveTier.TIER_1,)
    assert triple.chunk_ids == ("c1",)
    assert triple.turn_range == _RANGE
    assert triple.preference is True
    assert 0.0 < triple.confidence <= 0.95


def test_strips_flavor_mannerism_and_emoji(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I just totally love coffee啦嘛喵～陛下!! 😍"))
    result = _run(snap, tmp_path)
    triple = _find(result, "prefers", "coffee")
    assert triple is not None
    for component in (triple.subject, triple.predicate, triple.object):
        assert "just" not in component
        assert "totally" not in component
        assert "啦" not in component and "喵" not in component
        assert "陛下" not in component
        assert "😍" not in component and "!" not in component and "～" not in component


def test_never_stores_speaking_style(tmp_path: Path) -> None:
    """Pure anima mannerism (honorifics + particles, no durable content) yields
    no speaking-style triple and none of the mannerism tokens survive."""
    snap = _snap(_stamp("c1", "陛下～人家好想你喵～嘻嘻嘻！", origin="agent", persona_id="anima-1"))
    result = _run(snap, tmp_path)
    for triple in result.triples:
        assert "says" not in triple.predicate and "speaks" not in triple.predicate
        assert triple.subject != "user" or triple.predicate != "says"
        blob = f"{triple.subject} {triple.predicate} {triple.object}"
        assert not any(token in blob for token in ("陛下", "喵", "嘻嘻", "人家", "想你"))
    # nothing durable to extract here at all
    assert result.triples == ()


def test_extracts_chinese_preference_and_strips_intensifier(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "我超级特别喜欢深色模式"))
    result = _run(snap, tmp_path)
    triple = _find(result, "prefers", "深色模式")
    assert triple is not None
    assert "超级" not in triple.object and "特别" not in triple.object


# ---------------------------------------------------------------- AC-3 dedup fold


def test_dedup_fold_ac3_repeated_preference_is_one_triple(tmp_path: Path) -> None:
    chunks = tuple(
        _stamp(f"c{i}", "I really really prefer dark mode !! 😍", turn_start=i, turn_end=i) for i in range(10)
    )
    result = _run(_snap(*chunks), tmp_path)
    preference_triples = [t for t in result.triples if t.preference]
    assert len(preference_triples) == 1
    folded = preference_triples[0]
    assert folded.chunk_ids == tuple(f"c{i}" for i in range(10))
    assert folded.confidence > 0.85  # reinforced by 10 mentions
    assert folded.confidence <= 0.95  # capped
    assert folded.route is Route.CORE


def test_distinct_preferences_do_not_fold(tmp_path: Path) -> None:
    snap = _snap(
        _stamp("c1", "I prefer dark mode"),
        _stamp("c2", "I prefer coffee"),
    )
    result = _run(snap, tmp_path)
    assert len([t for t in result.triples if t.preference]) == 2


# ---------------------------------------------------------------- dual-track routing


def test_tier3_durable_preference_routes_salvage_never_core(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode", tier=CognitiveTier.TIER_3))
    result = _run(snap, tmp_path)
    triple = _find(result, "prefers", "dark mode")
    assert triple is not None
    assert triple.route is Route.SALVAGE
    assert triple.route is not Route.CORE
    assert triple.tiers == (CognitiveTier.TIER_3,)


def test_tier3_noise_asserts_routes_isolated(tmp_path: Path) -> None:
    snap = _snap(
        _stamp(
            "c1",
            "The answer is definitely option B, trust me.",
            tier=CognitiveTier.TIER_3,
            origin="agent",
            persona_id="meh",
        )
    )
    result = _run(snap, tmp_path)
    assert len(result.triples) == 1
    noise = result.triples[0]
    assert noise.predicate == "asserts"
    assert noise.route is Route.ISOLATED
    assert noise.subject == "assistant"


def test_core_triple_folded_with_tier3_salvage_stays_salvage(tmp_path: Path) -> None:
    """Anti-backflow survives dedup folding: tier-3 evidence in a folded triple
    never lets it land in the main graph."""
    snap = _snap(
        _stamp("c1", "I prefer coffee", tier=CognitiveTier.TIER_1),
        _stamp("c2", "I prefer coffee", tier=CognitiveTier.TIER_3),
    )
    result = _run(snap, tmp_path)
    folded = _find(result, "prefers", "coffee")
    assert folded is not None
    assert folded.tiers == (CognitiveTier.TIER_1, CognitiveTier.TIER_3)
    assert folded.route is Route.SALVAGE  # never core


# ---------------------------------------------------------------- FR-2.12 evidence boundary


def test_fr212_preference_from_agent_rendered_text_excluded(tmp_path: Path) -> None:
    snap = _snap(
        _stamp("c1", "I love coffee", origin="user"),
        _stamp("c2", "I love coffee", origin="agent", persona_id="anima-1"),
    )
    result = _run(snap, tmp_path)
    preference_triples = [t for t in result.triples if t.preference]
    assert len(preference_triples) == 1
    assert preference_triples[0].chunk_ids == ("c1",)  # the agent mention is not evidence
    assert preference_triples[0].confidence == pytest.approx(0.7)  # single mention, unreinforced


def test_fr212_all_agent_preference_produces_no_preference(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I love coffee", origin="agent", persona_id="anima-1"))
    result = _run(snap, tmp_path)
    assert all(not t.preference for t in result.triples)
    assert result.triples == ()


# ---------------------------------------------------------------- marker + completion


def test_reflect_persists_marker_before_on_done(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    probe = _MarkerProbe(tmp_path, snap.snapshot_id)
    outcome = _run_outcome(snap, tmp_path, on_done=probe)
    assert outcome.ok
    assert probe.reflect_done_at_call


def test_reflect_persists_marker_and_keeps_source_immutable(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    outcome = _run_outcome(snap, tmp_path)
    assert outcome.ok
    on_disk = load_snapshot_file(tmp_path / f"{snap.snapshot_id}.json")
    assert on_disk is not None
    assert SnapshotPhase.REFLECT_DONE.value in on_disk.phases
    assert SnapshotPhase.REFLECT_DONE.value not in snap.phases


def test_reflect_output_parser_tolerates_fenced_and_prefaced_json() -> None:
    """D4 resilience (service-root stability): small/quantized models very
    reliably wrap their extraction in markdown fences (```json) or preface it
    with chatter — strict json.loads was degrading answers the model actually
    produced (0.6b/8b live evidence on the reflect contract). The parser must
    recover the widest [..] span without ever accepting garbage."""
    from mnemoseed_local.dream.reflect import _loads_json_array

    # strict path untouched
    assert _loads_json_array('[{"subject":"u"}]') == [{"subject": "u"}]
    # markdown fence (the 0.6b live shape)
    fenced = '```json\n[{"subject": "u", "object": "教练"}]\n```'
    assert _loads_json_array(fenced)[0]["object"] == "教练"
    # chatter preceding the array
    prefaced = 'here goes the answer\n[{"subject": "u"}]\n(~extra tail~)'
    assert _loads_json_array(prefaced) == [{"subject": "u"}]
    # bracket inside string content must not break the span choice
    inner = '[{"subject": "a[1]", "object": "[x]"}]'
    assert _loads_json_array(inner)[0]["object"] == "[x]"
    # garbage stays an error (the retry lane must never be fed trash)
    with pytest.raises(ValueError):
        _loads_json_array("totally not json at all")
    with pytest.raises(ValueError, match="not a JSON array"):
        _loads_json_array('{"subject": "u"}')


def test_reflect_marker_gate_makes_rerun_idempotent(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"), phases=_COMPLETE)
    llm = _CountingLLM(StubReflectLLM())
    done = _Recorder()
    outcome = _run_outcome(snap, tmp_path, on_done=done, llm=llm)
    assert outcome.ok
    assert outcome.skipped is True
    assert outcome.result is None
    assert llm.calls == 0
    assert done.calls == []


def test_orchestrator_completion_advances_trigger_to_merging(tmp_path: Path) -> None:
    trigger = DreamTrigger(snapshotter=NullSnapshotter(), auto_trigger=True)
    trigger.handle_event(_event())
    trigger.on_snapshot_ready("alice")
    assert trigger.status("alice").state is DreamState.DREAMING

    snap = _snap(_stamp("c1", "I prefer dark mode"))
    outcome = _run_outcome(snap, tmp_path, on_done=trigger.on_reflect_complete)
    assert outcome.ok
    assert trigger.status("alice").state is DreamState.MERGING


def test_empty_snapshot_reflects_ok_with_no_triples(tmp_path: Path) -> None:
    snap = _snap()
    outcome = _run_outcome(snap, tmp_path)
    assert outcome.ok
    assert outcome.result is not None
    assert outcome.result.triples == ()
    on_disk = load_snapshot_file(tmp_path / f"{snap.snapshot_id}.json")
    assert on_disk is not None
    assert SnapshotPhase.REFLECT_DONE.value in on_disk.phases


# ---------------------------------------------------------------- failure degradation (§7)


def test_llm_failure_retries_three_times_then_typed_failure(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    write_snapshot_file(tmp_path, snap)  # the capture side journaled it; reflect must not advance it
    llm = _FailingLLM(failures=99)
    sleeper = _Sleeper()
    done = _Recorder()
    outcome = _run_outcome(snap, tmp_path, on_done=done, llm=llm, sleeper=sleeper)
    assert not outcome.ok
    assert outcome.result is None
    assert outcome.error
    # initial call + 3 exponential-backoff retries, delays 1, 2, 4 seconds
    assert llm.calls == 4
    assert sleeper.delays == [1.0, 2.0, 4.0]
    assert done.calls == []  # progress never reported
    on_disk = load_snapshot_file(tmp_path / f"{snap.snapshot_id}.json")
    assert on_disk is not None
    assert SnapshotPhase.REFLECT_DONE.value not in on_disk.phases  # still journaled at reflect


def test_success_after_one_retry(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    llm = _FailingLLM(failures=1)
    sleeper = _Sleeper()
    outcome = _run_outcome(snap, tmp_path, llm=llm, sleeper=sleeper)
    assert outcome.ok
    assert outcome.result is not None
    assert len(outcome.result.triples) == 1
    assert sleeper.delays == [1.0]


def test_unparseable_llm_output_is_typed_failure_after_retries(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    llm = _GarbageLLM()
    sleeper = _Sleeper()
    outcome = _run_outcome(snap, tmp_path, llm=llm, sleeper=sleeper)
    assert not outcome.ok
    assert llm.calls == 4
    assert outcome.error


# ---------------------------------------------------------------- recovery compatibility


def test_recovered_snapshot_reflects_like_fresh(tmp_path: Path) -> None:
    store = _FakeStore([_stamp("c1", "I prefer dark mode", turn_start=0, turn_end=3)])
    meta = _FakeMeta()
    fresh = (
        FileSnapshotter(store=store, meta=meta, directory=tmp_path, clock=lambda: 1000.0)
        .request("alice", _RANGE)
        .snapshot
    )
    assert fresh is not None

    # daemon reboot: a new snapshotter reloads the merge-incomplete snapshot
    fs2 = FileSnapshotter(store=store, meta=meta, directory=tmp_path, clock=lambda: 1000.0)
    recovered = fs2.recover()
    assert len(recovered) == 1
    fs2.adopt(recovered[0])

    assert _run(recovered[0], tmp_path) == _run(fresh, tmp_path)


# ---------------------------------------------------------------- journey to disk


def test_result_prompt_version_round_trip_and_stub_json_schema(tmp_path: Path) -> None:
    from mnemoseed_local.dream import PROMPT_VERSION

    snap = _snap(_stamp("c1", "I prefer dark mode"))
    result = _run(snap, tmp_path)
    assert result.snapshot_id == "snap-p1"
    assert result.profile_id == "alice"
    assert result.prompt_version == PROMPT_VERSION
    assert result.turn_range == _RANGE
    # deterministic ordering of the emitted contract
    assert result.triples[0].route.value in ("core", "isolated", "salvage")


# ---------------------------------------------------------------- negation guard (g2)


def test_negation_guard_contradictory_polarities_never_fold(tmp_path: Path) -> None:
    """ "I always use vim" + "I never use vim" must NOT collapse into one
    false-confident reinforced triple: both mentions are dropped and the
    conflict is reported on the result."""
    snap = _snap(
        _stamp("c1", "I always use vim"),
        _stamp("c2", "I never use vim"),
    )
    result = _run(snap, tmp_path)
    assert _find(result, "has_habit", "use vim") is None
    assert result.triples == ()
    assert result.conflicts == (("user", "has_habit", "use vim"),)


def test_same_polarity_mentions_still_fold(tmp_path: Path) -> None:
    """AC-3 still applies within one polarity: two positive habit mentions
    collapse into one reinforced triple, no conflict reported."""
    snap = _snap(
        _stamp("c1", "I always use vim"),
        _stamp("c2", "I usually use vim"),
    )
    result = _run(snap, tmp_path)
    triple = _find(result, "has_habit", "use vim")
    assert triple is not None
    assert triple.polarity == "positive"
    assert triple.tiers == (CognitiveTier.TIER_1,)
    assert result.conflicts == ()


def test_reflected_triple_defaults_to_positive_polarity(tmp_path: Path) -> None:
    snap = _snap(_stamp("c1", "I prefer dark mode"))
    result = _run(snap, tmp_path)
    triple = _find(result, "prefers", "dark mode")
    assert triple is not None
    assert triple.polarity == "positive"


def test_reflect_journal_round_trips_result_and_conflicts(tmp_path: Path) -> None:
    from mnemoseed_local.dream import result_from_payload

    snap = _snap(
        _stamp("c1", "I always use vim"),
        _stamp("c2", "I never use vim"),
    )
    outcome = _run_outcome(snap, tmp_path)
    assert outcome.ok
    assert outcome.result is not None
    on_disk = load_snapshot_file(tmp_path / f"{snap.snapshot_id}.json")
    assert on_disk is not None
    assert SnapshotPhase.REFLECT_DONE.value in on_disk.phases
    restored = result_from_payload(on_disk.reflect_result)
    assert restored is not None
    assert restored == outcome.result
    assert restored.conflicts == (("user", "has_habit", "use vim"),)


# ---------------------------------------------------------------- E1-2 (F2) per-run resolve


class _ResolvableLLM:
    """A ChatLLM naming itself so a test can prove WHICH instance a run used.

    ``model`` mirrors the driver instances' attribute the run-start pinning
    seam reports (F2 dream_runs.model recording)."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.model = label
        self.used = 0

    def chat(self, *, system: str, user: str) -> str:
        del system, user
        self.used += 1
        return "[]"


def test_reflect_resolves_llm_at_run_start_when_resolver_wired(tmp_path: Path) -> None:
    """E1-2 (F2): with a ``resolve_llm`` seam, each reflect pass materializes
    the route at run start (pinned for that run), so a routing change lands on
    the NEXT run without rebuilding the orchestrator."""
    first = _ResolvableLLM("route-a")
    second = _ResolvableLLM("route-b")
    resolver = iter([first, second])

    reflector = ReflectOrchestrator(
        llm=first,  # boot-time fallback; superseded by the resolver
        resolve_llm=lambda: next(resolver),
        directory=tmp_path,
        sleep=lambda _: None,
    )
    outcome_a = reflector.reflect(_snap(_stamp("c1", "I prefer dark mode")))
    assert outcome_a.ok
    assert first.used == 1
    assert second.used == 0
    # the NEXT run resolves the changed route fresh, pinned for its own run
    outcome_b = reflector.reflect(_snap(_stamp("c2", "I prefer light mode")))
    assert outcome_b.ok
    assert second.used == 1


def test_reflect_on_run_started_reports_pinned_model_for_each_run(tmp_path: Path) -> None:
    """F2: the ``on_run_started`` seam fires per run with the RESOLVED
    instance's model (the run-start pin), so dream_runs.model can record it."""
    first = _ResolvableLLM("kimi-k3")
    second = _ResolvableLLM("deepseek-v4-flash")
    resolver = iter([first, second])
    pinned: list[tuple[str, str]] = []

    reflector = ReflectOrchestrator(
        llm=first,
        resolve_llm=lambda: next(resolver),
        on_run_started=lambda run_id, model: pinned.append((run_id, model)),
        directory=tmp_path,
        sleep=lambda _: None,
    )
    snap_a = _snap(_stamp("c1", "I prefer dark mode"))
    snap_b = _snap(_stamp("c2", "I prefer light mode"))
    assert reflector.reflect(snap_a).ok
    assert reflector.reflect(snap_b).ok
    assert pinned == [(snap_a.snapshot_id, "kimi-k3"), (snap_b.snapshot_id, "deepseek-v4-flash")]
