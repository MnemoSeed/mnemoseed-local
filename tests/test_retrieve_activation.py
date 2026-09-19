from __future__ import annotations

from dataclasses import replace

import pytest

from mnemoseed_local.retrieve.activation import ActivationSnapshot, ShortTermActivation


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

    assert activation.score("graph:one", profile_id="p", session_id="s", now=105.0) == 2.0
    assert activation.score("graph:one", profile_id="p", session_id="s", now=115.0) < 2.0
    assert activation.score("graph:one", profile_id="p", session_id="s", now=float("inf")) == 0.0


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
    assert disabled.score("chunk:a", profile_id="p", session_id="s", now=100.0) == 0.0

    activation = _activation()
    activation.refresh("chunk:a", profile_id="p", session_id="s", now=100.0)
    corrupt = replace(activation.snapshot(), entries={("p", "s", "chunk:a"): "bad"})
    assert corrupt.score("chunk:a", profile_id="p", session_id="s", now=100.0) == 0.0
