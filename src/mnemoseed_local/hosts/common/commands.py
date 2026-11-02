"""Portable handler-command rendering shared by the config-surgery installers."""

from __future__ import annotations

import shlex
from collections.abc import Sequence


def render_command(head: str, tail: Sequence[str], *, windows: bool) -> str:
    """One handler command line: ``head`` stays a single literal argument — a
    PowerShell single-quoted string after the call operator on Windows, a
    POSIX-quoted shell word elsewhere."""
    if windows:
        literal = "'" + head.replace("'", "''") + "'"
        return " ".join(["&", literal, *tail])
    return " ".join([shlex.quote(head), *tail])
