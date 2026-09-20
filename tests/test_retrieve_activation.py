from __future__ import annotations

from dataclasses import replace

import pytest

from mnemoseed_local.retrieve.activation import ActivationSnapshot, ShortTermActivation
from mnemoseed_local.retrieve.assemble import (
    AssembleConfig,
    AssembledContext,
    AssembledEntry,
    Assembler,
    CoverageReport,
)
from mnemoseed_local.retrieve.hybrid import (
    Candidate,
    HybridConfig,
    HybridRecall,
    ScoreBreakdown,
    _breakdown,
    _merge,
)
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance


def _activation(**overrides: object) -> ShortTermActivation:
    values: dict[str, object] = {
        "enabled": True,
        "half_life": 10.0,
        "boost_value": 2.0,
        "capacity": 2,
        "clock": lambda: 100.0,
    }
    values.update(overrides)
    return ShortTermActivation(**values)  # type: ignore[arg-type]


def test_snapshot_is_immutable_and_scoped() -> None:
    activation = _activation()
    activation.refresh("chunk:one", profile_id="p1", session_id="s1", now=100.0)
    snapshot = activation.snapshot()

    assert isinstance(snapshot, ActivationSnapshot)
    assert snapshot.boost("chunk:one", profile_id="p1", session_id="s1", now=100.0) == 2.0
    assert snapshot.boost("chunk:one", profile_id="p1", session_id="s2", now=100.0) == 0.0
    with pytest.raises((AttributeError, TypeError)):
        snapshot.entries["x"] = 1  # type: ignore[index]


def test_refresh_not_stack_and_decay_is_monotonic() -> None:
    activation = _activation()
    activation.refresh("graph:one", profile_id="p", session_id="s", now=100.0)
    activation.refresh("graph:one", profile_id="p", session_id="s", now=105.0)

    assert activation.boost("graph:one", profile_id="p", session_id="s", now=105.0) == 2.0
    assert activation.boost("graph:one", profile_id="p", session_id="s", now=115.0) < 2.0
    assert activation.boost("graph:one", profile_id="p", session_id="s", now=float("inf")) == 0.0


def test_capacity_eviction_is_bounded_and_deterministic() -> None:
    activation = _activation(capacity=2)
    for memory_id in ("chunk:a", "chunk:b", "chunk:c"):
        activation.refresh(memory_id, profile_id="p", session_id="s", now=100.0)

    snapshot = activation.snapshot()
    assert len(snapshot.entries) == 2
    assert snapshot.boost("chunk:a", profile_id="p", session_id="s", now=100.0) == 0.0
    assert snapshot.boost("chunk:c", profile_id="p", session_id="s", now=100.0) == 2.0


def test_disabled_and_corrupt_state_fail_open() -> None:
    disabled = _activation(enabled=False)
    disabled.refresh("chunk:a", profile_id="p", session_id="s", now=100.0)
    assert disabled.boost("chunk:a", profile_id="p", session_id="s", now=100.0) == 0.0

    activation = _activation()
    activation.refresh("chunk:a", profile_id="p", session_id="s", now=100.0)
    corrupt = replace(activation.snapshot(), entries={("p", "s", "chunk:a"): "bad"})
    assert corrupt.boost("chunk:a", profile_id="p", session_id="s", now=100.0) == 0.0


def test_preregistered_defaults_are_off_and_bounded() -> None:
    activation = ShortTermActivation.default(clock=lambda: 100.0)

    assert activation.snapshot().enabled is False
    assert activation.snapshot().half_life == 600.0
    assert activation.snapshot().boost_value == 0.15
    assert activation.snapshot().capacity == 200


def test_refreshes_only_returned_explicit_group_members() -> None:
    from mnemoseed_local.daemon.memory import MemoryService

    service = object.__new__(MemoryService)
    service._activation = _activation(capacity=8)
    entries = tuple(
        AssembledEntry("graph", item, "graph", item, 1.0, 1, (), conflict_group=group)
        for item, group in (("a", "g1"), ("b", "g1"), ("c", "g2"), ("d", None))
    )
    context = AssembledContext(entries, 0, 10, 4, CoverageReport(0, 0, 4, 0))

    service._refresh_activation(context, profile_id="p", session_id="s")

    snapshot = service._activation.snapshot()
    assert snapshot.boost("graph:a", profile_id="p", session_id="s", now=100.0) == 2.0
    assert snapshot.boost("graph:b", profile_id="p", session_id="s", now=100.0) == 2.0
    assert snapshot.boost("graph:c", profile_id="p", session_id="s", now=100.0) == 2.0
    assert snapshot.boost("graph:d", profile_id="p", session_id="s", now=100.0) == 2.0


def test_refresh_does_not_infer_or_write_conflicts() -> None:
    from mnemoseed_local.daemon.memory import MemoryService

    service = object.__new__(MemoryService)
    service._activation = _activation()
    entry = AssembledEntry("graph", "a", "graph", "a", 1.0, 1, (), conflict_group=None)
    context = AssembledContext((entry,), 0, 10, 1, CoverageReport(0, 0, 1, 0))

    service._refresh_activation(context, profile_id="p", session_id="s")

    assert service._activation.snapshot().boost("graph:a", profile_id="p", session_id="s", now=100.0) == 2.0


def test_non_returned_group_member_is_not_refreshed() -> None:
    from mnemoseed_local.daemon.memory import MemoryService

    service = object.__new__(MemoryService)
    service._activation = _activation(capacity=8)
    service._activation.refresh("graph:outside", profile_id="p", session_id="s", now=90.0)
    entry = AssembledEntry("graph", "inside", "graph", "inside", 1.0, 1, (), conflict_group="g1")
    context = AssembledContext((entry,), 0, 10, 1, CoverageReport(0, 0, 1, 0))

    service._refresh_activation(context, profile_id="p", session_id="s")

    snapshot = service._activation.snapshot()
    assert snapshot.entries[("p", "s", "graph:outside")] == (90.0, 2.0)


def test_refresh_failure_is_warning_and_fail_open(caplog: pytest.LogCaptureFixture) -> None:
    from mnemoseed_local.daemon.memory import MemoryService

    service = object.__new__(MemoryService)

    class RaisingActivation:
        def refresh(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("injected refresh failure")

    service._activation = RaisingActivation()
    entry = AssembledEntry("graph", "a", "graph", "a", 1.0, 1, ())
    context = AssembledContext((entry,), 0, 10, 1, CoverageReport(0, 0, 1, 0))
    with caplog.at_level("WARNING", logger="mnemoseed_local.daemon.memory"):
        service._refresh_activation(context, profile_id="p", session_id="s")
    assert "activation refresh failed; recall proceeds" in caplog.text


def test_refresh_does_not_need_store_assignment() -> None:
    from mnemoseed_local.daemon.memory import MemoryService

    service = object.__new__(MemoryService)
    service._activation = _activation()
    entry = AssembledEntry("graph", "a", "graph", "a", 1.0, 1, (), conflict_group=None)
    context = AssembledContext((entry,), 0, 10, 1, CoverageReport(0, 0, 1, 0))
    service._refresh_activation(context, profile_id="p", session_id="s")
    assert service._activation.snapshot().boost("graph:a", profile_id="p", session_id="s", now=100.0) == 2.0


def _candidate(memory_id: str, score: float) -> Candidate:
    breakdown = ScoreBreakdown(score, 0.0, 0.0, 0.0, 0.0, score)
    item = ChunkStamp(
        chunk_id=memory_id,
        profile_id="p",
        text=memory_id,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test",
        cues=Cues(),
        provenance=Provenance(asserted_by="test", source="test"),
    )
    return Candidate("chunk", memory_id, "vector", item, score, breakdown)


def test_activation_cannot_displace_top_k_membership() -> None:
    snapshot = _activation(capacity=8)
    snapshot.refresh("chunk:boosted", profile_id="p", session_id="s", now=100.0)
    on = _merge(
        [_candidate("boosted", 0.1), _candidate("kept", 0.2), _candidate("tail", 0.15)],
        [],
        activation_snapshot=snapshot.snapshot(),
        profile_id="p",
        session_id="s",
        now=100.0,
    )
    off = _merge([_candidate("boosted", 0.1), _candidate("kept", 0.2), _candidate("tail", 0.15)], [])

    class Stores:
        def pool_state(self, profile_id: str):
            return type("State", (), {"watermark": None})()

        def list_chunks(self, filter, page):
            return type("Page", (), {"total": 0})()

    def assemble(recall: HybridRecall) -> set[str]:
        return {
            entry.id
            for entry in Assembler(AssembleConfig(top_k=2))
            .assemble(
                recall,
                profile_id="p",
                meta_store=Stores(),
                vector_store=Stores(),
                graph_store=Stores(),
            )
            .entries
        }

    assert assemble(on) == assemble(off)
    assert [candidate.id for candidate in on.candidates[:2]] == ["boosted", "kept"]


def test_activation_preserves_non_activation_components_with_custom_weights() -> None:
    config = HybridConfig(
        weight_semantic=2.25, weight_cue_overlap=0.4, weight_decay=1.7, weight_centrality=3.1
    )
    candidate = _candidate("one", 0.3)
    snapshot = _activation(capacity=8)
    snapshot.refresh("chunk:one", profile_id="p", session_id="s", now=100.0)
    result = _merge(
        [candidate],
        [],
        activation_snapshot=snapshot.snapshot(),
        profile_id="p",
        session_id="s",
        now=100.0,
        config=config,
    )
    assert result.candidates[0].breakdown.semantic == candidate.breakdown.semantic
    assert result.candidates[0].breakdown.cue_overlap == candidate.breakdown.cue_overlap
    assert result.candidates[0].breakdown.decay_weight == candidate.breakdown.decay_weight
    assert result.candidates[0].breakdown.graph_centrality == candidate.breakdown.graph_centrality


def test_merge_total_uses_every_supplied_non_default_weight() -> None:
    config = HybridConfig(
        weight_semantic=2.0,
        weight_cue_overlap=3.0,
        weight_decay=4.0,
        weight_centrality=5.0,
    )
    breakdown = _breakdown(
        semantic=0.2,
        cue_overlap=0.3,
        decay_weight=0.4,
        graph_centrality=0.5,
        config=config,
    )
    candidate = replace(_candidate("configured", breakdown.total), breakdown=breakdown)
    result = _merge([candidate], [], config=config)
    assert result.candidates[0].breakdown.total == pytest.approx(
        2.0 * 0.2 + 3.0 * 0.3 + 4.0 * 0.4 + 5.0 * 0.5
    )


def test_non_returned_group_member_refresh_makes_no_partner_calls() -> None:
    from mnemoseed_local.daemon.memory import MemoryService

    service = object.__new__(MemoryService)

    class SpyActivation:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def refresh(self, memory_id: str, **kwargs: object) -> None:
            self.calls.append(memory_id)

    spy = SpyActivation()
    service._activation = spy
    entry = AssembledEntry("graph", "inside", "graph", "inside", 1.0, 1, (), conflict_group="g1")
    context = AssembledContext((entry,), 0, 10, 1, CoverageReport(0, 0, 1, 0))
    service._refresh_activation(context, profile_id="p", session_id="s")
    assert spy.calls == ["graph:inside"]


def test_sessionless_recall_is_off_golden() -> None:
    candidates = [_candidate("one", 0.3), _candidate("two", 0.2)]
    baseline = _merge(candidates, [])
    assert (
        _merge(
            candidates,
            [],
            activation_snapshot=ActivationSnapshot({}, False, 10.0, 2.0, 8),
            profile_id="p",
            session_id=None,
            now=100.0,
        )
        == baseline
    )


def test_fixed_clock_serialization_is_stable() -> None:
    candidate = _candidate("one", 0.3)
    snapshot = _activation(capacity=8)
    snapshot.refresh("chunk:one", profile_id="p", session_id="s", now=100.0)

    def serialized() -> bytes:
        result = _merge(
            [candidate],
            [],
            activation_snapshot=snapshot.snapshot(),
            profile_id="p",
            session_id="s",
            now=100.0,
        )
        return repr(
            tuple(
                (item.id, item.score, item.selection_score, item.breakdown.short_term_activation, item.kind)
                for item in result.candidates
            )
        ).encode("ascii")

    expected = b"(('one', 0.8999999999999999, 0.3, 0.6, 'chunk'),)"
    assert serialized() == expected
    assert serialized() == expected
