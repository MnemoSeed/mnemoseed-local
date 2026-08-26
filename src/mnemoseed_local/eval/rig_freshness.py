"""Shared eval-rig materialization contract: fail-loud root freshness.

Every eval rig (matrix, recall, rescue) materializes under a caller-given
root under ONE rule: fresh means absent or an empty directory; prior state
is contamination evidence and is never wiped.
"""

from __future__ import annotations

from pathlib import Path


class RigRootNotFresh(RuntimeError):
    """A rig root carried prior state when a point tried to materialize."""


def require_fresh_root(root: Path) -> None:
    """Refuse ``root`` unless fresh, then create it (parents included)."""
    if root.exists() and any(root.iterdir()):
        raise RigRootNotFresh(f"rig root {root} is not fresh: prior state present, refusing to materialize")
    root.mkdir(parents=True, exist_ok=True)
