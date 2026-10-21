"""S3 fixtures: the deterministic real product stores of one isolated home.

The builders seed the product databases through the real driver API only: the
nodes are written with ``upsert_node`` and the terminal receipts with
``apply_reconciliation``. No ledger row, no manifest, and no repair run is
produced here; the repair evidence belongs to the repair slice.

Every caller-supplied number is checked before the first durable product
effect: an invalid count, limit, or epoch raises a typed instrument error and
leaves the home untouched.

The returned cohort identity is complete, while its source provenance, its
configuration evidence, and the daily control link stay explicitly unbound:
the command line finalizes them before the first ledger write.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mnemoseed_local.instrument.ledger import (
    ALLOWED_REPAIR_LIMITS,
    BindingIncompleteError,
    ClockValueError,
    CohortRecord,
    InstrumentPaths,
    canonical_json,
    compute_cohort_id,
    compute_config_hash,
    compute_instrument_hash,
    schema_hash,
    sha256_hex,
)
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import Provenance
from mnemoseed_local.storage.drivers._time import iso8601_utc
from mnemoseed_local.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import (
    ReconciliationApplication,
    ReconciliationReceipt,
    derive_composite_group_id,
)

DAILY_KIND = "daily_cohort"
CONTROL_KIND = "repair_control"

STALE_REVISION = "stale_revision"
PROTECTED_ENDPOINT = "protected_endpoint"
RECIPE_KINDS: tuple[str, ...] = (STALE_REVISION, PROTECTED_ENDPOINT)

DOWNWEIGHT_FACTOR = 0.9

_PROFILE_KEYS: tuple[str, ...] = ("a", "b")
_PROFILE_IDS: tuple[str, ...] = ("instrument-profile-a", "instrument-profile-b")

_APPLICATION_REASON_CODE = "conflict_confirmed"
_CANONICAL_KIND = "read_conflict"

_PROVENANCE_SOURCE = "instrument-fixture"

_ROLE_SUFFIXES: dict[str, str] = {
    "left": "-left",
    "right": "-right",
    "protected": "-protected",
}

_UNBOUND_BINDING = "unbound_pre_registration"
_UNBOUND_SOURCE_COMMIT = "unbound:source_commit"
_UNBOUND_SOURCE_HASH = "unbound:source_hash"


@dataclass(frozen=True)
class SeedReceiptSpec:
    """The caller-supplied seed input of one terminal receipt."""

    nomination_id: str
    profile_id: str
    applied_at_epoch: float
    kind: str


@dataclass(frozen=True)
class FixtureHandle:
    """One seeded product pair with the pre-registration identity of its cohort."""

    control_id: str | None
    graph: SqliteGraphDriver
    meta: SqliteMetaDriver
    profile_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    cohort: CohortRecord
    seed_manifest_hash: str


@dataclass(frozen=True)
class _ProfileLayout:
    """The seeded node roles of one profile."""

    profile_id: str
    left: GraphNode
    right: GraphNode
    protected: GraphNode


@dataclass(frozen=True)
class _SeedRequest:
    """One terminal receipt the seed writes into a product pair."""

    nomination_id: str
    profile_index: int
    applied_at_epoch: float
    kind: str


def build_daily_cohort_fixture(paths: InstrumentPaths, *, seed_clock_epoch: float) -> FixtureHandle:
    """Seed the daily cohort pair of one home with both deterministic recipes."""
    epoch = _require_epoch(seed_clock_epoch, field="seed_clock_epoch")
    requests = [
        _SeedRequest(
            nomination_id=f"nom-fixture-{kind}",
            profile_index=0,
            applied_at_epoch=epoch + 0.5 + index,
            kind=kind,
        )
        for index, kind in enumerate(RECIPE_KINDS)
    ]
    return _seed_pair(
        paths.cohort_graph_db,
        paths.cohort_meta_db,
        kind=DAILY_KIND,
        prefix="instrument-daily",
        control_id=None,
        repair_limit=None,
        seed_clock_epoch=epoch,
        fixture_seed_clock_raw=None,
        requests=requests,
    )


def build_repair_control_fixture(
    paths: InstrumentPaths,
    *,
    control_id: str,
    pending_count: int,
    repair_limit: int,
    seed_clock_epoch: float,
) -> FixtureHandle:
    """Seed the control pair of one home with the requested pending backlog."""
    epoch = _require_epoch(seed_clock_epoch, field="seed_clock_epoch")
    count = _require_pending_count(pending_count)
    limit = _require_repair_limit(repair_limit)
    requests = [
        _SeedRequest(
            nomination_id=f"nom-control-{index:04d}",
            profile_index=index % len(_PROFILE_KEYS),
            applied_at_epoch=epoch + float(index),
            kind=STALE_REVISION,
        )
        for index in range(count)
    ]
    return _seed_pair(
        paths.control_graph_db,
        paths.control_meta_db,
        kind=CONTROL_KIND,
        prefix="instrument-control",
        control_id=control_id,
        repair_limit=limit,
        seed_clock_epoch=epoch,
        fixture_seed_clock_raw=float.hex(epoch),
        requests=requests,
    )


def _seed_pair(
    graph_db: Path,
    meta_db: Path,
    *,
    kind: str,
    prefix: str,
    control_id: str | None,
    repair_limit: int | None,
    seed_clock_epoch: float,
    fixture_seed_clock_raw: str | None,
    requests: list[_SeedRequest],
) -> FixtureHandle:
    """Seed one product pair, its terminal receipts, and its pre-registration cohort."""
    graph = SqliteGraphDriver(path=graph_db)
    meta = SqliteMetaDriver(path=meta_db)
    layouts = _seed_profiles(graph, prefix=prefix, epoch=seed_clock_epoch)
    receipts: list[ReconciliationReceipt] = []
    for request in requests:
        layout = layouts[request.profile_index]
        spec = SeedReceiptSpec(
            nomination_id=request.nomination_id,
            profile_id=layout.profile_id,
            applied_at_epoch=request.applied_at_epoch,
            kind=request.kind,
        )
        receipts.append(_apply_receipt(graph, layout, spec))
    manifest = _seed_manifest(
        kind=kind,
        control_id=control_id,
        repair_limit=repair_limit,
        seed_clock_epoch=seed_clock_epoch,
        layouts=layouts,
        receipts=receipts,
    )
    seed_hash = _seed_manifest_hash(manifest)
    return FixtureHandle(
        control_id=control_id,
        graph=graph,
        meta=meta,
        profile_ids=tuple(layout.profile_id for layout in layouts),
        node_ids=_node_ids(layouts),
        cohort=_build_cohort(
            kind=kind,
            seed_manifest_hash=seed_hash,
            control_id=control_id,
            fixture_seed_clock_raw=fixture_seed_clock_raw,
        ),
        seed_manifest_hash=seed_hash,
    )


def seed_receipt(fixture: FixtureHandle, spec: SeedReceiptSpec) -> ReconciliationReceipt:
    """Write one terminal receipt through the real product applier."""
    layout = _profile_layout(fixture, spec.profile_id)
    return _apply_receipt(fixture.graph, layout, spec)


def build_application(fixture: FixtureHandle, spec: SeedReceiptSpec) -> ReconciliationApplication:
    """The terminal application of one seed receipt, rebuilt from the same spec."""
    return _application(_profile_layout(fixture, spec.profile_id), spec)


def _apply_receipt(
    graph: SqliteGraphDriver, layout: _ProfileLayout, spec: SeedReceiptSpec
) -> ReconciliationReceipt:
    return graph.apply_reconciliation(_application(layout, spec), downweight_factor=DOWNWEIGHT_FACTOR)


def _application(layout: _ProfileLayout, spec: SeedReceiptSpec) -> ReconciliationApplication:
    """The application of one recipe: a superseded left revision, or a protected loser."""
    applied_at = _require_epoch(spec.applied_at_epoch, field="applied_at_epoch")
    if spec.kind == STALE_REVISION:
        left_id = layout.left.node_id
        left_version = layout.left.version - 1
        left_peer = layout.right.node_id
        right_id = layout.right.node_id
        right_version = layout.right.version
        right_peer = layout.left.node_id
        winner_id = layout.right.node_id
        loser_id = layout.left.node_id
        verdict = "left_loses"
    elif spec.kind == PROTECTED_ENDPOINT:
        left_id = layout.protected.node_id
        left_version = layout.protected.version
        left_peer = layout.left.node_id
        right_id = layout.left.node_id
        right_version = layout.left.version
        right_peer = layout.protected.node_id
        winner_id = layout.left.node_id
        loser_id = layout.protected.node_id
        verdict = "right_loses"
    else:
        raise BindingIncompleteError({"field": "kind"})
    return ReconciliationApplication(
        nomination_id=spec.nomination_id,
        profile_id=layout.profile_id,
        disposition="accepted",
        reason_code=_APPLICATION_REASON_CODE,
        canonical_kind=_CANONICAL_KIND,
        composite_group_id=derive_composite_group_id(spec.nomination_id),
        evidence_event_ids=(_evidence_id(spec.nomination_id),),
        verdict=verdict,
        quality="fixture",
        left_node_id=left_id,
        left_version=left_version,
        left_expected_peer_id=left_peer,
        right_node_id=right_id,
        right_version=right_version,
        right_expected_peer_id=right_peer,
        winner_node_id=winner_id,
        loser_node_id=loser_id,
        loser_prior_version=None,
        loser_new_version=None,
        applied_at=applied_at,
    )


def _require_pending_count(value: object) -> int:
    """The requested backlog size: zero is legal, no maximum is imposed."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BindingIncompleteError({"field": "pending_count"})
    return value


def _require_repair_limit(value: object) -> int:
    """The repair cap of the control fixture; the frozen allowlist is the only one."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in ALLOWED_REPAIR_LIMITS:
        raise BindingIncompleteError({"field": "repair_limit"})
    return value


def _require_epoch(value: object, *, field: str) -> float:
    """A caller-supplied clock epoch: a finite number the product can timestamp."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ClockValueError({"field": field}, diag_code="unparsable")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ClockValueError({"field": field}, diag_code="non_finite") from exc
    if not math.isfinite(normalized):
        raise ClockValueError({"field": field}, diag_code="non_finite")
    try:
        iso8601_utc(normalized)
    except (ValueError, OverflowError, OSError) as exc:
        raise ClockValueError({"field": field}, diag_code="unparsable") from exc
    return normalized


def _seed_profiles(graph: SqliteGraphDriver, *, prefix: str, epoch: float) -> tuple[_ProfileLayout, ...]:
    layouts: list[_ProfileLayout] = []
    for key, profile_id in zip(_PROFILE_KEYS, _PROFILE_IDS, strict=True):
        layout = _seed_profile(graph, prefix=prefix, key=key, profile_id=profile_id, epoch=epoch)
        layouts.append(layout)
    return tuple(layouts)


def _seed_profile(
    graph: SqliteGraphDriver,
    *,
    prefix: str,
    key: str,
    profile_id: str,
    epoch: float,
) -> _ProfileLayout:
    role_ids = {role: f"{prefix}-{key}{suffix}" for role, suffix in _ROLE_SUFFIXES.items()}
    left_id = role_ids["left"]
    right_id = role_ids["right"]
    protected_id = role_ids["protected"]
    left = _append_revision(
        graph,
        _node(
            node_id=left_id,
            profile_id=profile_id,
            node_type=NodeType.HABIT,
            peer_id=right_id,
            epoch=epoch,
        ),
        epoch=epoch,
    )
    right = _append_revision(
        graph,
        _node(
            node_id=right_id,
            profile_id=profile_id,
            node_type=NodeType.HABIT,
            peer_id=left_id,
            epoch=epoch,
        ),
        epoch=epoch,
    )
    protected = _append_revision(
        graph,
        _node(
            node_id=protected_id,
            profile_id=profile_id,
            node_type=NodeType.CONSTRAINT,
            peer_id=left_id,
            epoch=epoch,
            never_decay=True,
        ),
        epoch=epoch,
    )
    return _ProfileLayout(profile_id=profile_id, left=left, right=right, protected=protected)


def _append_revision(graph: SqliteGraphDriver, node: GraphNode, *, epoch: float) -> GraphNode:
    """Write the seeded node and the revision that supersedes it."""
    graph.upsert_node(node)
    revision = node.model_copy(
        update={
            "version": node.version + 1,
            "prev_version_id": f"{node.node_id}:{node.version}",
            "valid_from": epoch,
            "updated_at": epoch,
        }
    )
    graph.append_version(revision, invalidate_at=epoch)
    return revision


def _node(
    *,
    node_id: str,
    profile_id: str,
    node_type: NodeType,
    peer_id: str,
    epoch: float,
    never_decay: bool = False,
) -> GraphNode:
    """One seeded node of a conflict pair."""
    props = _node_props(node_type, node_id)
    return GraphNode.model_validate(
        {
            "node_id": node_id,
            "profile_id": profile_id,
            "node_type": node_type,
            "props": props,
            "entities": [],
            "confidence": 0.5,
            "decay_weight": 1.0,
            "never_decay": never_decay,
            "last_reinforced": epoch,
            "read_conflict_id": peer_id,
            "valid_from": epoch,
            "created_at": epoch,
            "updated_at": epoch,
            "version": 1,
            "prev_version_id": None,
            "provenance": _seed_provenance(epoch),
        }
    )


def _node_props(node_type: NodeType, node_id: str) -> dict[str, Any]:
    """The payload of one seeded node, valid for the frozen node type schema."""
    if node_type is NodeType.HABIT:
        return {"statement": f"instrument fixture node {node_id}"}
    return {"rule": f"instrument fixture node {node_id}", "severity": "high"}


def _seed_provenance(epoch: float) -> Provenance:
    """The synthetic provenance stamp of one seeded node."""
    return Provenance(
        asserted_by=_PROVENANCE_SOURCE,
        source=_PROVENANCE_SOURCE,
        confidence=0.5,
        asserted_at=epoch,
        history=[],
    )


def _profile_layout(fixture: FixtureHandle, profile_id: str) -> _ProfileLayout:
    """The seeded node roles of one profile of a fixture.

    Each role comes from the seeded node id suffix, so a missing or ambiguous
    role fails loudly instead of letting a sort order pick a side.
    """
    roles: dict[str, list[GraphNode]] = {role: [] for role in _ROLE_SUFFIXES}
    for node_id in fixture.node_ids:
        node = fixture.graph.get_node(node_id)
        if node is None or node.profile_id != profile_id:
            continue
        for role, suffix in _ROLE_SUFFIXES.items():
            if node_id.endswith(suffix):
                roles[role].append(node)
                break
    if any(len(nodes) != 1 for nodes in roles.values()):
        raise BindingIncompleteError({"field": "profile_id"})
    return _ProfileLayout(
        profile_id=profile_id,
        left=roles["left"][0],
        right=roles["right"][0],
        protected=roles["protected"][0],
    )


def _node_ids(layouts: tuple[_ProfileLayout, ...]) -> tuple[str, ...]:
    nodes = [node for layout in layouts for node in (layout.left, layout.right, layout.protected)]
    return tuple(node.node_id for node in nodes)


def _seed_manifest(
    *,
    kind: str,
    control_id: str | None,
    repair_limit: int | None,
    seed_clock_epoch: float,
    layouts: tuple[_ProfileLayout, ...],
    receipts: list[ReconciliationReceipt],
) -> dict[str, Any]:
    """The canonical description of what the seed wrote into the product pair."""
    return {
        "control_id": control_id,
        "kind": kind,
        "nodes": _node_entries(layouts),
        "profiles": [layout.profile_id for layout in layouts],
        "receipts": _receipt_entries(receipts),
        "repair_limit": repair_limit,
        "seed_clock_raw": float.hex(seed_clock_epoch),
    }


def _node_entries(layouts: tuple[_ProfileLayout, ...]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for layout in layouts:
        for node in (layout.left, layout.right, layout.protected):
            entries.append(
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type.value,
                    "never_decay": node.never_decay,
                    "profile_id": node.profile_id,
                    "version": node.version,
                }
            )
    return entries


def _receipt_entries(receipts: list[ReconciliationReceipt]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for receipt in receipts:
        entries.append(
            {
                "applied_at": iso8601_utc(receipt.applied_at),
                "disposition": str(receipt.disposition),
                "nomination_id": receipt.nomination_id,
                "profile_id": receipt.profile_id,
                "reason_code": str(receipt.reason_code),
            }
        )
    return entries


def _seed_manifest_hash(manifest: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(manifest).encode("utf-8"))


def _evidence_id(nomination_id: str) -> int:
    """A deterministic synthetic evidence id of one nomination."""
    return int(sha256_hex(nomination_id.encode("utf-8"))[:12], 16) % 1_000_000 + 1


def _build_cohort(
    *,
    kind: str,
    seed_manifest_hash: str,
    control_id: str | None,
    fixture_seed_clock_raw: str | None,
) -> CohortRecord:
    """The pre-registration cohort identity of one seeded product pair.

    The creation instant is the host clock; the fixture clock never backdates
    it. The source provenance, the configuration evidence, and the daily
    control link are left explicitly unbound for the command line to finalize.
    """
    created_raw = float.hex(time.time())
    created_iso = iso8601_utc(float.fromhex(created_raw))
    instrument_hash = compute_instrument_hash()[0]
    ledger_schema_hash = schema_hash()
    cohort_id = compute_cohort_id(
        kind=kind,
        cohort_created_at_iso=created_iso,
        seed_manifest_hash=seed_manifest_hash,
        schema_hash=ledger_schema_hash,
        instrument_hash=instrument_hash,
    )
    config_json = _unbound_config_json(kind=kind, control_id=control_id)
    return CohortRecord(
        cohort_id=cohort_id,
        kind=kind,
        cohort_created_at_raw=created_raw,
        seed_manifest_hash=seed_manifest_hash,
        schema_hash=ledger_schema_hash,
        instrument_hash=instrument_hash,
        config_hash=compute_config_hash(config_json),
        config_json=config_json,
        source_commit=_UNBOUND_SOURCE_COMMIT,
        source_hash=_UNBOUND_SOURCE_HASH,
        control_id=control_id,
        fixture_seed_clock_raw=fixture_seed_clock_raw,
    )


def _unbound_config_json(*, kind: str, control_id: str | None) -> str:
    """The unbound configuration evidence of a pre-registration cohort.

    It is deliberately not a configuration evidence document: its key set does
    not match the frozen one, so the evidence check fails loudly until the
    command line replaces it with the finalized document.
    """
    unbound = ["config_hash", "config_json", "source_commit", "source_hash"]
    if control_id is None:
        unbound.append("control_id")
    return canonical_json(
        {
            "binding": _UNBOUND_BINDING,
            "finalized_by": "instrument_cli",
            "kind": kind,
            "unbound_fields": sorted(unbound),
        }
    )
