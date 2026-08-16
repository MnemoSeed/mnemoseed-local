"""Decay curve model (PRD-04 FR-4.1, design/01 stage ⑤).

The Ebbinghaus-style exponential curve:

    w = base_confidence × exp(-λ × days_since(last_reinforced))

λ is layered by memory type (design/01 §5): fact 0.01 (half-life ≈ 69 days),
preference 0.005 (≈ 139 days), episode 0.03 (≈ 23 days). The defaults and the
writable key set live in :mod:`mnemoseed_local.config` (the established home for
tunable defaults, like the dream trigger keys and LLM routes); this module
carries the curve math and the λ resolution rule.

The base_confidence factor is a ceiling: the weight never exceeds the
memory's trust at any horizon, and reinforcement events (which rebound toward
1.0) temporarily exceed it until the next sweep re-asserts the ceiling.

A consolidated chunk (post-dream merge marker, design/03 §4) resolves its λ
at 3× the base rate: once the gist lives in the graph, the verbatim evidence
scene's value diminishes and the chunk fades fast.

The FR-4.1 interference term (λ_eff = λ_base × (1 + κ × interference_load))
is explicitly DEFERRED: it needs a similar-neighbor read port that the storage
layer does not expose yet, so λ_eff equals λ_base today, with the consolidated
multiplier as the only modifier. The κ term lands together with that read port.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from mnemoseed_local.config import DEFAULT_LAMBDA_PER_TYPE, LAMBDA_TARGETS

__all__ = [
    "CONSOLIDATED_LAMBDA_MULTIPLIER",
    "DEFAULT_LAMBDA_PER_TYPE",
    "LAMBDA_TARGETS",
    "SECONDS_PER_DAY",
    "decay_weight",
    "half_life_days",
    "lambda_for",
]

SECONDS_PER_DAY = 86400.0

#: design/03 §4: a consolidated chunk decays at 3× its type rate (the evidence
#: scene fades once the dream folded the gist into the graph).
CONSOLIDATED_LAMBDA_MULTIPLIER = 3.0

_FALLBACK_LAMBDA = 0.01


def decay_weight(confidence: float, lam: float, days: float) -> float:
    """FR-4.1: ``w = base_confidence × exp(-λ × days)``, clamped to [0, 1].

    At t=0 the curve returns the confidence ceiling; at t→∞ it vanishes to the
    0.0 floor; the output never leaves the unit range.
    """
    if days <= 0.0:
        return min(1.0, max(0.0, confidence))
    value = confidence * math.exp(-lam * days)
    return min(1.0, max(0.0, value))


def lambda_for(
    node_type: str,
    lambda_per_type: Mapping[str, float],
    *,
    consolidated: bool = False,
) -> float:
    """Resolve one type's λ: an explicit map entry wins, then the per-type
    design default, then the conservative fact rate. A consolidated marker
    (design/03 §4, chunk side only) scales the resolved rate by 3×."""
    if node_type in lambda_per_type:
        lam = float(lambda_per_type[node_type])
    else:
        lam = float(DEFAULT_LAMBDA_PER_TYPE.get(node_type, _FALLBACK_LAMBDA))
    if consolidated:
        lam *= CONSOLIDATED_LAMBDA_MULTIPLIER
    return lam


def half_life_days(lam: float) -> float:
    """The retention horizon at which a full-confidence weight halves."""
    return math.log(2.0) / lam
