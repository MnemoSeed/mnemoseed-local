"""Shared conversation-visibility field parsing and count-line rendering."""

from __future__ import annotations

from typing import Any

#: The daemon wire keys of the visibility field.
VISIBILITY_KEY = "visibility"
VISIBILITY_MODES = ("compact", "verbose")


def visibility_mode(served: dict[str, Any] | None) -> str | None:
    """The rendering mode of a pull payload, None when off or unusable."""
    if not isinstance(served, dict):
        return None
    visibility = served.get(VISIBILITY_KEY)
    if not isinstance(visibility, dict):
        return None
    mode = visibility.get("mode")
    return mode if mode in VISIBILITY_MODES else None


def recall_count_line(memories: int, chars: int) -> str:
    """The verbose T2 count line (appended after the compact signal line)."""
    return f"[mnemoseed] {memories} memories, {chars} chars"


def capture_count_line(count: int) -> str:
    """The verbose capture count line (replaces the compact capture line)."""
    return f"[mnemoseed] captured {count} turns since the last report"
