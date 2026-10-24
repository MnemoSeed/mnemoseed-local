"""Shared strict-JSON config surgery for host installers: tolerant strict
load, canonical serialization, and an atomic temp-file rewrite."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


class ConfigParseError(Exception):
    """The target config is not strict JSON with the expected structure;
    the file stays byte-for-byte untouched."""


def load_object(target: Path) -> dict[str, object]:
    """Strict-JSON load; blank/absent files read as an empty object. utf-8-sig
    tolerates a BOM-prefixed file (stripped on the next atomic rewrite)."""
    if not target.is_file():
        return {}
    text = target.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigParseError(f"{target} is not strict JSON ({exc}); fix it manually, then retry") from exc
    if not isinstance(data, dict):
        raise ConfigParseError(f"{target} does not contain a JSON object")
    return data


def serialize(data: Mapping[str, object]) -> bytes:
    """Canonical bytes: UTF-8 regardless of the process locale, non-ASCII kept
    literal, two-space indent, trailing newline."""
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_atomic(target: Path, data: Mapping[str, object], dry_run: bool = False) -> bool:
    """Atomically rewrite ``target`` with ``data``; returns whether the file
    changed (byte-identical content is a no-op). A dry run reports the change
    without writing. Temp file + fsync + ``os.replace`` in the target dir, so
    a crash mid-write can never destroy the user's config."""
    payload = serialize(data)
    if target.is_file() and target.read_bytes() == payload:
        return False
    if dry_run:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return True
