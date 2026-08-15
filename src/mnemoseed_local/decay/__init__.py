"""Decay engine (PRD-04 FR-4.1 / FR-4.2 / FR-4.4, design/01 stage ⑤).

The time-based weight sweep that makes unused memories fade: an
Ebbinghaus-style exponential curve layered per node type, a daemon-owned
periodic sweep over every profile's unreinforced nodes and chunks, batch
weight writes through the existing storage ports, a crash-safe resume cursor,
one audit entry per sweep pass, and the reinforcement event side that turns
retrieval usage into a ``last_reinforced`` refresh + bounded rebound
(FR-4.2) — the counterpart that makes fresh hits decay-neutral for the next
sweep interval.
"""

from __future__ import annotations

from mnemoseed_local.decay.model import (
    CONSOLIDATED_LAMBDA_MULTIPLIER,
    DEFAULT_LAMBDA_PER_TYPE,
    LAMBDA_TARGETS,
    SECONDS_PER_DAY,
    decay_weight,
    half_life_days,
    lambda_for,
)
from mnemoseed_local.decay.reinforce import ReinforceConfig, Reinforcer
from mnemoseed_local.decay.sweeper import DecaySweeper, SweepStats

__all__ = [
    "CONSOLIDATED_LAMBDA_MULTIPLIER",
    "DEFAULT_LAMBDA_PER_TYPE",
    "LAMBDA_TARGETS",
    "SECONDS_PER_DAY",
    "DecaySweeper",
    "ReinforceConfig",
    "Reinforcer",
    "SweepStats",
    "decay_weight",
    "half_life_days",
    "lambda_for",
]
