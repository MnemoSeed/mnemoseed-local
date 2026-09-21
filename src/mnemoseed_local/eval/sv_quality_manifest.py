"""Immutable manifest for single-vs-dual quality runs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MANIFEST_VERSION = "v1"
STAGED_EXCLUDED_PATH = "tests/test_eval_206_true_conflicts.py"
STAGED_EXCLUDED_HASH = "FDDEA62462EEC219EFDC291E8F6C924484361194D1E95A7C81CAA3FC4BC0CED2"


def preregistration_hash(
    *,
    prompt_version: str,
    canary_seed: int,
    routes: Sequence[str],
    material_ids: Sequence[str],
    oracle_version: str,
    denominator: str,
) -> str:
    """Hash every preregistered input that would invalidate a comparison."""
    payload = json.dumps(
        {
            "prompt_version": prompt_version,
            "canary_seed": canary_seed,
            "routes": list(routes),
            "material_ids": list(material_ids),
            "oracle_version": oracle_version,
            "denominator": denominator,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_manifest(run_root: Path, manifest: Mapping[str, Any]) -> Path:
    """Atomically write one manifest without overwriting prior run state."""
    run_root.mkdir(parents=True, exist_ok=True)
    target = run_root / "manifest.json"
    if target.exists():
        raise RuntimeError(f"refusing to overwrite prior run root {run_root}")
    payload = dict(manifest)
    payload.setdefault("manifest_version", MANIFEST_VERSION)
    payload.setdefault("staged_excluded_path", STAGED_EXCLUDED_PATH)
    payload.setdefault("staged_excluded_hash", STAGED_EXCLUDED_HASH)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    descriptor, tmp_name = tempfile.mkstemp(dir=str(run_root), prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        Path(tmp_name).replace(target)
    finally:
        leftover = Path(tmp_name)
        if leftover.exists():
            leftover.unlink()
    return target


@dataclass(frozen=True)
class CellManifest:
    """Per-cell auditable surface for one comparison arm."""

    cell_id: str
    model: str
    route: str
    vote_b_model: str | None
    vote_b_distinct: bool
    material_ids: tuple[str, ...]
    tokens: int
    duration_s: float
    failures: tuple[str, ...]
    degraded: int
    retries: int
    denominator: str


@dataclass(frozen=True)
class SvQualityManifest:
    """Full immutable run record covering every preregistered field."""

    repository_sha: str
    provider: str
    cell_a: CellManifest
    cell_b: CellManifest
    canary_seed: int
    canary_hash: str
    positive_hash: str
    negative_hash: str
    preregistration_hash: str
    raw_paths: tuple[str, ...]
    raw_hashes: tuple[str, ...]
    verdict: str
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize with the staged-exclusion sentinel always present."""
        payload = asdict(self)
        payload["manifest_version"] = MANIFEST_VERSION
        payload["staged_excluded_path"] = STAGED_EXCLUDED_PATH
        payload["staged_excluded_hash"] = STAGED_EXCLUDED_HASH
        return payload
