"""Rollout filename helpers shared by host adapters and the import runner.

A rollout file name carries the run id as a uuid; hosts with no uuid use the
stem. Conversation lineage (``session_meta.session_id``) is a separate fact.
"""

from __future__ import annotations

import re
from pathlib import Path

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def rollout_run_id(path: Path) -> str:
    """The run id from a rollout filename: its uuid, or the stem when absent."""
    matches = _UUID_RE.findall(path.stem)
    return matches[-1] if matches else path.stem
