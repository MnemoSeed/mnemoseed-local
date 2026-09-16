"""Dream-side nomination materialization (S-A; design/12 section 3.2/6).

The scanner converts live read-conflict pointers into durable, exactly-once
nominations: reciprocal in-effect pairs only, MetaStore appends only, zero
graph mutation, zero model calls. One-sided overwrites, missing/closed peers,
cross-profile pairs and self pairs produce NO nomination (repair is S-C's
job, never a mint). Runs best-effort on the dream worker after a merge
commit; a failure never fails the merge and never blocks ingest.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mnemoseed_local.schema.graph import GraphNode
from mnemoseed_local.storage.ports import (
    CANONICAL_NOMINATION_KINDS,
    EvidenceKind,
    EvidencePointer,
    GraphStore,
    MetaStore,
    NodeFilter,
    NominationOutcome,
    NominationRejectedError,
    NominationRequest,
    Page,
    derive_nomination_id,
)

logger = logging.getLogger(__name__)

#: The scanner's canonical kind for read-conflict pairs (F6: vote_disagreement
#: is schema-reserved with no producer in this slice).
READ_CONFLICT_KIND = "read_conflict"

if READ_CONFLICT_KIND not in CANONICAL_NOMINATION_KINDS:  # pragma: no cover - import-time guard
    raise ImportError("read_conflict kind is outside the closed canonical set")

READ_CONFLICT_SOURCE_CHANNELS: tuple[str, ...] = ("read_conflict_flag",)


@dataclass(frozen=True)
class MaterializeReport:
    """One scanner pass's outcome (typed, never silent)."""

    appended: tuple[str, ...] = ()
    duplicated: tuple[str, ...] = ()
    skipped: int = 0


def mint_generation(terminal_count: int) -> int:
    """Generation minting (freeze F5): 1 + terminal-marker count.

    Pre-materialization the generation is DERIVED from this rule; after the
    carrier row lands it is PERSISTED and never recomputed (a re-scan
    re-derives the same value and hits the UNIQUE dedup instead of minting).
    ``terminal_count`` comes from the injected terminal-reader port; S-A
    ships the production reader returning 0 (S-C receipts do not exist yet).
    """
    return 1 + int(terminal_count)


def _live_node(graph: GraphStore, node_id: str) -> GraphNode | None:
    """The fresh in-effect node for one id, or None when it carries no live pointer."""
    node = graph.get_node(node_id)
    if node is None or not node.is_current:
        return None
    if not node.read_conflict_id:
        return None
    return node


def _scannable_nodes(graph: GraphStore, profile_id: str) -> list[GraphNode]:
    """Current nodes of one profile carrying a live read-conflict pointer."""
    found: list[GraphNode] = []
    offset = 0
    while True:
        page = graph.list_nodes(
            NodeFilter(profile_id=profile_id, has_read_conflict=True),
            Page(offset=offset, limit=1000),
        )
        found.extend(page.items)
        if len(page.items) < 1000:
            return found
        offset += len(page.items)


def materialize_nominations(
    graph: GraphStore,
    meta: MetaStore,
    profile_id: str,
    *,
    clock: Callable[[], float] | None = None,
    terminal_reader: Callable[[str, str, str], int] | None = None,
) -> MaterializeReport:
    """Scan one profile's live pointers and mint nominations (F5/F6).

    Emits IFF the pair is reciprocal, same-profile and both endpoints are
    current; versions are captured as expected. Every other shape is skipped
    with zero writes. A malformed pair (port validation) is skipped with a
    warning so one bad pair can never starve later valid pairs; infra faults
    still abort the pass loudly. Idempotent: a second scan re-derives the
    same generation and hits the carrier's UNIQUE constraint (typed dedup).
    """
    now = (clock or time.time)()
    read_terminal = terminal_reader or (lambda _profile, _lo, _hi: 0)
    appended: list[str] = []
    duplicated: list[str] = []
    skipped = 0
    seen: set[tuple[str, str]] = set()
    for stub in _scannable_nodes(graph, profile_id):
        node = _live_node(graph, stub.node_id)
        if node is None:
            skipped += 1
            continue
        peer_id = node.read_conflict_id or ""
        if node.node_id == peer_id:
            skipped += 1
            continue
        lo, hi = sorted((node.node_id, peer_id))
        pair_key: tuple[str, str] = (lo, hi)
        if pair_key in seen:
            continue
        peer = graph.get_node(peer_id)
        if peer is None or not peer.is_current or peer.profile_id != node.profile_id:
            skipped += 1
            seen.add(pair_key)
            continue
        if peer.read_conflict_id != node.node_id:
            skipped += 1
            seen.add(pair_key)
            continue
        seen.add(pair_key)
        generation = mint_generation(read_terminal(profile_id, pair_key[0], pair_key[1]))
        request = NominationRequest(
            profile_id=node.profile_id,
            canonical_kind=READ_CONFLICT_KIND,
            node_a=node.node_id,
            version_a=int(node.version),
            expected_peer_a=peer_id,
            node_b=peer_id,
            version_b=int(peer.version),
            expected_peer_b=node.node_id,
            source_generation=generation,
            observed_at=now,
            evidence=(
                EvidencePointer(kind=EvidenceKind.NODE, id=node.node_id),
                EvidencePointer(kind=EvidenceKind.NODE, id=peer_id),
            ),
            source_channels=READ_CONFLICT_SOURCE_CHANNELS,
        )
        expected_id = derive_nomination_id(
            request.canonical_kind,
            request.profile_id,
            request.node_a,
            request.version_a,
            request.node_b,
            request.version_b,
            request.source_generation,
        )
        try:
            result = meta.append_reconcile_nomination(request)
        except NominationRejectedError as exc:
            logger.warning("skipping malformed pair %s: %s", pair_key, exc)
            skipped += 1
            continue
        if result.outcome is NominationOutcome.APPENDED:
            appended.append(result.nomination_id)
        else:
            duplicated.append(result.nomination_id)
        if result.nomination_id != expected_id:  # pragma: no cover - derivation drift guard
            logger.warning(
                "nomination id drift for %s: carrier %s != derived %s",
                pair_key,
                result.nomination_id,
                expected_id,
            )
    return MaterializeReport(appended=tuple(appended), duplicated=tuple(duplicated), skipped=skipped)


def run_committed_nominations(graph: Any, meta: Any, profile_id: str) -> None:
    """Best-effort scanner entry for the post-merge seam (F4).

    Swallows every exception (precedent: _record_run_completion, the trigger
    purger guard): a scanner failure never fails the merge, never blocks
    ingest, and never mutates trigger state.
    """
    try:
        materialize_nominations(graph, meta, profile_id)
    except Exception as exc:  # noqa: BLE001 - best-effort seam, never a merge failure
        logger.warning("nomination scan failed for %s: %s", profile_id, exc)


def after_commit(record: Callable[[Any], None], scan: Callable[[], None], completion: Any) -> None:
    """Chain the ordinary run-completion record then the scanner (F4).

    The record callback runs first and unguarded (its own best-effort
    contract); the scan runs after and swallows failures. Ordering is frozen:
    record → scan, both on the dream worker thread.
    """
    record(completion)
    try:
        scan()
    except Exception as exc:  # noqa: BLE001 - scanner failure never breaks completion
        logger.warning("post-commit nomination scan failed: %s", exc)


__all__ = [
    "MaterializeReport",
    "READ_CONFLICT_KIND",
    "READ_CONFLICT_SOURCE_CHANNELS",
    "after_commit",
    "materialize_nominations",
    "mint_generation",
    "run_committed_nominations",
]
