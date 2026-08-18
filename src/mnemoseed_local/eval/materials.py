"""Eval materials (B3 T4): the canary batch + read-only snapshot replay.

Materials come in two shapes:

- ``canary``: a factory-generated labeled session (``Material.session``), the
  ground-truth-bearing kind;
- ``replay``: a frozen dream snapshot journal — REAL captured sessions with
  REAL stamps (the B1-harness shape, made durable). Chunk stamps ride
  verbatim into the eval run: tier/origin are never re-derived, only the run
  journal phases are reset (``fresh_replay``) so each matrix cell reflects
  the material with its own seats.

Replay files only ever accumulate (the materials library is append-only);
catalog ordering is deterministic (canary batch first, replays sorted by
filename). Catalog construction is total: a broken replay file is NOT loaded
at catalog time — it surfaces as a typed ``MaterialError`` row when the
matrix actually runs it, never a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mnemoseed_local.dream.snapshot import Snapshot, SnapshotPhase, load_snapshot_file
from mnemoseed_local.eval.canary import CanarySession, canary_sessions

#: The pinned factory seed for the built-in canary batch (corpus identity is
#: part of the bar: bumping it deliberately is a PRD-level change).
DEFAULT_CANARY_SEED = 20260818
DEFAULT_CANARY_COUNT = 1


class MaterialError(Exception):
    """A replay material that could not be loaded (typed, reportable)."""


@dataclass(frozen=True)
class Material:
    """One eval material. Exactly one of ``session`` / (``snapshot``|``path``) set."""

    kind: str  # "canary" | "replay"
    name: str
    session: CanarySession | None = None
    snapshot: Snapshot | None = None  # eager replay (already journal-loaded)
    path: Path | None = None  # lazy replay (resolved when the matrix runs it)


def load_replay(path: Path) -> Material:
    """Load one frozen snapshot journal into an eager replay material."""
    snapshot = load_snapshot_file(path)
    if snapshot is None:
        raise MaterialError(f"replay material {path.name!r} is not a loadable snapshot journal")
    return Material(kind="replay", name=path.stem, snapshot=snapshot, path=path)


def fresh_replay(snapshot: Snapshot) -> Snapshot:
    """Strip a journaled snapshot's RUN history for a fresh eval pass.

    Chunk stamps, turn range, and the snapshot id ride verbatim; phases reset
    to SNAPSHOT_DONE and the carried reflect payload drops — every cell judges
    the material with its own models instead of replaying someone else's
    journal entry.
    """
    return Snapshot(
        snapshot_id=snapshot.snapshot_id,
        profile_id=snapshot.profile_id,
        turn_range=snapshot.turn_range,
        chunks=snapshot.chunks,
        created_at=snapshot.created_at,
        phases=frozenset({SnapshotPhase.SNAPSHOT_DONE.value}),
        reflect_result=None,
    )


def material_catalog(
    materials_dir: Path | None,
    *,
    canary_seed: int = DEFAULT_CANARY_SEED,
    canary_count: int = DEFAULT_CANARY_COUNT,
) -> tuple[Material, ...]:
    """The deterministic material list: built-in canary batch, then every
    ``*.json`` replay under ``materials_dir`` sorted by filename (lazy — the
    matrix resolves them at run time so a bad file is a report row)."""
    materials: list[Material] = [
        Material(kind="canary", name=session.session_id, session=session)
        for session in canary_sessions(canary_seed, sessions=canary_count)
    ]
    if materials_dir is not None and materials_dir.is_dir():
        for path in sorted(materials_dir.glob("*.json")):
            materials.append(Material(kind="replay", name=path.stem, path=path))
    return tuple(materials)
