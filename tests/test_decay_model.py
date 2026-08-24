"""Decay curve math (PRD-04 FR-4.1): the Ebbinghaus-style exponential model.

FR-4.1 is the engine's core: ``w = base_confidence × exp(-λ × days)`` with a
per-type λ. These unit tests pin the pure curve (t=0 ceiling, t=∞ floor, λ
monotonicity, half-life definition, confidence scaling) and the λ resolution
rule (explicit map value, else the per-type default).
"""

from __future__ import annotations

import pytest

from mnemoseed_local.decay.model import (
    CONSOLIDATED_LAMBDA_MULTIPLIER,
    DEFAULT_LAMBDA_PER_TYPE,
    LAMBDA_TARGETS,
    decay_weight,
    half_life_days,
    lambda_for,
)
from mnemoseed_local.schema.graph import NodeType

# ---------------------------------------------------------------- FR-4.1 curve


def test_curve_t0_returns_confidence_ceiling() -> None:
    """At t=0 a memory holds its confidence ceiling: w = confidence × exp(0)."""
    assert decay_weight(0.8, 0.01, 0.0) == pytest.approx(0.8)
    assert decay_weight(1.0, 0.01, 0.0) == pytest.approx(1.0)
    assert decay_weight(0.9, 0.03, 0.0) == pytest.approx(0.9)


def test_curve_never_exceeds_one() -> None:
    """Ceiling 1.0: even a freshly reinforced full-confidence memory stays ≤ 1."""
    assert decay_weight(1.0, 0.001, 0.0) <= 1.0
    assert decay_weight(0.95, 0.001, 0.0) <= 1.0
    assert decay_weight(1.0, 1e-9, 1.0) <= 1.0


def test_curve_floor_at_zero_for_infinite_elapsed() -> None:
    """t=∞ floor 0.0: the exponential vanishes, never going negative."""
    assert decay_weight(1.0, 0.01, 1e9) == 0.0
    assert decay_weight(0.7, 0.005, 1e12) == 0.0


def test_curve_stays_within_unit_range() -> None:
    """Guardrail: every output stays in [0.0, 1.0] across the curve's domain."""
    for confidence, lam, days in (
        (1.0, 0.01, 0.0),
        (0.5, 0.03, 10.0),
        (1.0, 0.005, 400.0),
        (0.9, 0.01, 1e6),
    ):
        weight = decay_weight(confidence, lam, days)
        assert 0.0 <= weight <= 1.0


def test_curve_monotonic_decreasing_in_time() -> None:
    """More elapsed days never yields a higher weight."""
    earlier = decay_weight(1.0, 0.01, 10.0)
    later = decay_weight(1.0, 0.01, 60.0)
    assert earlier > later


def test_curve_higher_lambda_decays_faster() -> None:
    """λ monotonicity: episode-class memory (0.03) sinks faster than a
    preference-class one (0.005) over the same horizon."""
    slow = decay_weight(1.0, 0.005, 60.0)
    fast = decay_weight(1.0, 0.03, 60.0)
    assert slow > fast


def test_curve_matches_half_life_definition() -> None:
    """The design/01 half-lives (fact ≈ 69d, preference ≈ 139d, episode ≈ 23d)
    fall out of the curve: w(λ, half_life) == 0.5 for a full-confidence base."""
    for lam, expected_half_life in ((0.01, 69.3), (0.005, 138.6), (0.03, 23.1)):
        half_life = half_life_days(lam)
        assert half_life == pytest.approx(expected_half_life, abs=0.5)
        assert decay_weight(1.0, lam, half_life) == pytest.approx(0.5, abs=0.01)


def test_curve_is_confidence_scaled() -> None:
    """FR-4.1's base_confidence factor: a 0.7-confidence memory decays to
    exactly 0.7× the full-confidence value at the same horizon."""
    full = decay_weight(1.0, 0.01, 60.0)
    partial = decay_weight(0.7, 0.01, 60.0)
    assert partial == pytest.approx(0.7 * full)


# ---------------------------------------------------------------- λ resolution


def test_default_lambda_per_type_matches_design_layers() -> None:
    """The PRD-04 λ layers: fact 0.01 / preference 0.005 / episode 0.03."""
    assert DEFAULT_LAMBDA_PER_TYPE["PREFERENCE"] == pytest.approx(0.005)
    assert DEFAULT_LAMBDA_PER_TYPE["ANIMA"] == pytest.approx(0.005)
    assert DEFAULT_LAMBDA_PER_TYPE["EPISODE"] == pytest.approx(0.03)
    assert DEFAULT_LAMBDA_PER_TYPE["INTENTION"] == pytest.approx(0.03)
    assert DEFAULT_LAMBDA_PER_TYPE["chunk"] == pytest.approx(0.03)
    for fact_type in ("USER", "HABIT", "DECISION", "PROJECT", "TOOL", "SKILL_SEQUENCE"):
        assert DEFAULT_LAMBDA_PER_TYPE[fact_type] == pytest.approx(0.01)


def test_lambda_targets_cover_node_types_plus_chunk() -> None:
    """The writable λ map accepts every frozen node type and the chunk
    pseudo-type (the vector store carries no node_type)."""
    assert NodeType.frozen_set() <= LAMBDA_TARGETS
    assert "chunk" in LAMBDA_TARGETS
    assert "pin" in LAMBDA_TARGETS


def test_pin_tier_resolves_the_flashbulb_default() -> None:
    """design/09 §3.1: the explicit-pin chunk class decays at preference pace —
    λ_pin defaults to 0.005 (~139-day half-life), overridable via the map."""
    assert DEFAULT_LAMBDA_PER_TYPE["pin"] == pytest.approx(0.005)
    assert lambda_for("pin", {}) == pytest.approx(0.005)
    assert lambda_for("pin", {"pin": 0.02}) == pytest.approx(0.02)
    assert half_life_days(lambda_for("pin", {})) == pytest.approx(138.6, abs=0.5)


def test_lambda_for_resolves_override_then_default_fallback() -> None:
    """Explicit map entries win; missing types fall back to their design
    default; unknown types fall back to the conservative 0.01 fact rate."""
    assert lambda_for("PREFERENCE", {"PREFERENCE": 0.1}) == pytest.approx(0.1)
    assert lambda_for("EPISODE", {}) == pytest.approx(0.03)
    assert lambda_for("chunk", {}) == pytest.approx(0.03)
    assert lambda_for("USER", {"chunk": 0.5}) == pytest.approx(0.01)
    assert lambda_for("ANIMA", {}) == pytest.approx(0.005)


def test_lambda_for_consolidated_chunk_multiplies_by_three() -> None:
    """design/03 §4: a consolidated chunk (post-dream merge marker) decays at
    3x its type rate — the gist lives in the graph, the evidence scene fades
    fast. The multiplier applies on top of the resolved base λ."""
    assert CONSOLIDATED_LAMBDA_MULTIPLIER == pytest.approx(3.0)
    assert lambda_for("chunk", {}, consolidated=True) == pytest.approx(0.03 * 3.0)
    assert lambda_for("chunk", {"chunk": 0.05}, consolidated=True) == pytest.approx(0.05 * 3.0)


def test_lambda_for_consolidated_scales_any_resolved_rate() -> None:
    """The consolidated marker scales whatever base rate resolved (default or
    override); the sweep only ever passes it for the ``chunk`` pseudo-type
    (design/03 §4), but the rule itself is type-agnostic."""
    assert lambda_for("chunk", {}, consolidated=False) == pytest.approx(0.03)
    assert lambda_for("PREFERENCE", {}, consolidated=True) == pytest.approx(0.005 * 3.0)
    assert lambda_for("EPISODE", {"EPISODE": 0.02}, consolidated=True) == pytest.approx(0.06)
