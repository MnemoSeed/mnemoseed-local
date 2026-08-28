"""Cortical graph schema — node types, version chains, edges.

A node is a fact/preference/habit/episode/skill-sequence/decision. The version
chain is the engineering form of reconsolidation: a rewrite pins valid_to on
the old version and links the new one in — nothing is ever overwritten.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from mnemoseed_local.schema.stamp import Provenance


class NodeType(StrEnum):
    """Frozen v1 node-type enum (prd-08 appendix A.2)."""

    USER = "USER"
    HABIT = "HABIT"
    PREFERENCE = "PREFERENCE"
    ANIMA = "ANIMA"
    INTENTION = "INTENTION"
    CONSTRAINT = "CONSTRAINT"
    EPISODE = "EPISODE"
    SKILL_SEQUENCE = "SKILL_SEQUENCE"
    DECISION = "DECISION"
    PROJECT = "PROJECT"
    TOOL = "TOOL"

    @classmethod
    def frozen_set(cls) -> frozenset[str]:
        """The exact v1 frozen enum values (schema-freeze review checklist)."""
        return frozenset(member.value for member in cls)


class RelType(StrEnum):
    HAS = "has"
    HOLDS = "holds"
    BOUND_BY = "bound_by"
    EVIDENCED_BY = "evidenced_by"
    CONTAINS = "contains"
    SUPERSEDES = "supersedes"
    USED_IN = "used_in"
    MASTERED = "mastered"
    CO_OCCURRED = "co_occurred"  # co-activation edge for spreading activation


class PromotionStatus(StrEnum):
    """Promotion-gate lifecycle on a cortical node (design/02 §11, prd-08 A.2).

    ``promoted`` is the default so every pre-v5 node and every un-gated write is
    back-compatible. The gate itself (quarantine on failed checks, TTL scrap,
    revocation-by-versioning) is a later task; this field only carries the value.
    """

    PENDING = "pending"
    PROMOTED = "promoted"
    QUARANTINED = "quarantined"
    SCRAPPED = "scrapped"


class GraphNode(BaseModel):
    """Cortical node. props carries per-type fields (statement/tool_chain/...)."""

    node_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    profile_id: str
    node_type: NodeType
    entities: list[str] = Field(default_factory=list)  # traversal entry points
    props: dict[str, Any] = Field(default_factory=dict)

    # weights and state
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    decay_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    never_decay: bool = False  # e.g. hard constraints
    last_reinforced: float = Field(default_factory=time.time)
    reinforce_count: int = 0

    # freshness / reconcile flags (prd-08 appendix A.2)
    needs_reconcile: bool = False
    pending_consolidation: bool = False
    peripheral_gaps: bool = False
    conflict_flag: bool = False
    conflict_group: str | None = None  # shared group id pairs both parties

    # Read-side reversible annotation: the peer node this in-effect statement
    # was observed conflicting with on a read. Evidence pointer, not a
    # correctness verdict — resolution happens offline. None = not flagged.
    read_conflict_id: str | None = None

    # promotion gate (design/02 §11, prd-08 A.2 v5): carrier only — the gate
    # logic and retrieval filtering land in a later task.
    promotion_status: PromotionStatus = PromotionStatus.PROMOTED

    # usage counters (console Detail; never derived from audit_log)
    hit_count: int = 0
    last_hit_at: float | None = None

    # version chain (bi-temporal)
    version: int = 1
    prev_version_id: str | None = None
    valid_from: float = Field(default_factory=time.time)
    valid_to: float | None = None  # None = currently in effect

    cognitive_tier: int = 1
    provenance: Provenance
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @property
    def is_current(self) -> bool:
        return self.valid_to is None


class Edge(BaseModel):
    src: str
    dst: str
    rel: RelType
    weight: float = 1.0  # co-occurrence edges: +1 per shared-session activation
    profile_id: str
    created_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------- payload schema

# Required `props` fields per node type, as specified in design/03 §3 erDiagram
# plus the ANIMA / PREFERENCE extension. Types without a row (PROJECT, TOOL)
# carry a free-form payload. Extra keys are allowed: props is an extensible
# per-type store, only the documented fields are validated.
NODE_PAYLOAD_SCHEMA: dict[NodeType, dict[str, type]] = {
    NodeType.USER: {"name": str},
    NodeType.HABIT: {"statement": str},
    NodeType.PREFERENCE: {
        "domain": str,
        "statement": str,
        "valence": float,
        "prior_width": float,
        "trait_anchor": str,
        "evidence_chain": list,
    },
    NodeType.ANIMA: {
        "name": str,
        "core_traits": list,
        "dye_layer": dict,
        "idiographic_notes": str,
        "drift_history": list,
    },
    NodeType.INTENTION: {"trigger_condition": str, "action": str, "status": str},
    NodeType.CONSTRAINT: {"rule": str, "severity": str},
    NodeType.EPISODE: {"summary": str, "session_ref": str},
    NodeType.SKILL_SEQUENCE: {"task_type": str, "tool_chain": list, "success_rate": float},
    NodeType.DECISION: {"statement": str},
    NodeType.PROJECT: {},
    NodeType.TOOL: {},
}

INTENTION_STATUSES: frozenset[str] = frozenset({"pending", "fired", "cancelled"})


def validate_node_payload(node_type: NodeType, props: dict[str, Any]) -> None:
    """Validate a node payload against the frozen per-type field requirements.

    Raises ValueError naming the missing/ill-typed field. Called by drivers at
    every graph write boundary; the schema model itself stays transport-only so
    intermediate objects are not over-constrained.
    """
    required = NODE_PAYLOAD_SCHEMA[node_type]
    for field_name, expected in required.items():
        if field_name not in props:
            raise ValueError(f"{node_type.value} node payload missing required field {field_name!r}")
        value = props[field_name]
        if not _type_matches(value, expected):
            expected_name = _type_name(expected)
            raise ValueError(
                f"{node_type.value} node payload field {field_name!r} must be {expected_name}, "
                f"got {type(value).__name__}"
            )
    if node_type is NodeType.INTENTION:
        status = props.get("status")
        if isinstance(status, str) and status not in INTENTION_STATUSES:
            raise ValueError(f"INTENTION status must be one of {sorted(INTENTION_STATUSES)}, got {status!r}")


def _type_matches(value: Any, expected: type) -> bool:
    if expected is float:
        # JSON/TOML authors commonly hand an int where a float is specified
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected is list:
        return isinstance(value, list)
    if expected is dict:
        return isinstance(value, dict)
    return isinstance(value, expected)


def _type_name(expected: type) -> str:
    return "float" if expected is float else expected.__name__
