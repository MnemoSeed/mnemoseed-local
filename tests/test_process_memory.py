"""Best-effort process private-commit probe behind the observability surfaces.

Readings are advisory: a platform without a probe, or a failed read, is None
(unknown), never a guess. On Windows the probe must actually read — a silently
None probe on the primary platform would hide the very ratchet the field
exists to expose.
"""

from __future__ import annotations

import sys

import pytest

from mnemoseed_local.util.process_memory import private_commit_bytes, process_commit_mb


def test_private_commit_bytes_is_positive_when_readable() -> None:
    value = private_commit_bytes()
    assert value is None or value > 0


def test_private_commit_bytes_probe_is_honest_per_platform() -> None:
    """win32 must read (the commit ratchet lives there); linux reads or
    honestly degrades to None on an unusual /proc; platforms without a probe
    report None."""
    value = private_commit_bytes()
    if sys.platform == "win32":
        assert value is not None
        assert value > 0
    elif sys.platform == "linux":
        assert value is None or value > 0
    else:
        assert value is None


def test_process_commit_mb_is_bytes_scaled_to_mb() -> None:
    value = process_commit_mb()
    assert value is None or value > 0.0


def test_process_commit_mb_failure_path_is_none_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed probe read (None at the byte level) propagates as None through
    the MB scaling — the advisory contract, never a raise."""
    monkeypatch.setattr("mnemoseed_local.util.process_memory.private_commit_bytes", lambda: None)
    assert process_commit_mb() is None
