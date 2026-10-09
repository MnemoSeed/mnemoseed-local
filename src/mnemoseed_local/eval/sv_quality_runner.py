"""Dry-run preflight for the single-vs-dual quality comparison.

Explicit dry-run/preflight only: this module never executes a real
provider-backed comparison. A future separate execute module owns any real
run; ``--real`` and ``allow_real`` here refuse with zero seam calls.
Provider contact stays behind the injected PONG seam so tests use stubs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mnemoseed_local.config import live_home_dir
from mnemoseed_local.eval.sv_quality_fixtures import (
    SV_QUALITY_CANARY_SEED,
    SV_QUALITY_PROMPT_VERSION,
    negative_corpus_hash,
    positive_corpus_hash,
    sv_canary_corpus_hash,
    sv_canary_material_ids,
)
from mnemoseed_local.eval.sv_quality_manifest import (
    MANIFEST_VERSION,
    STAGED_EXCLUDED_HASH,
    STAGED_EXCLUDED_PATH,
    preregistration_hash,
    write_manifest,
)
from mnemoseed_local.eval.sv_quality_metrics import DENOMINATOR_POLICY, ORACLE_VERSION
from mnemoseed_local.storage.ports import Disposition

LIVE_PORT = 7788
DEFAULT_PORT = 17891


class QuotaRejected(RuntimeError):
    """The provider refused quota; the preflight records it without retrying."""


@dataclass(frozen=True)
class AdjudicationRow:
    """Minimal terminal outcome for one fixture under one cell."""

    fixture_id: str
    is_positive: bool
    disposition: Disposition


@dataclass(frozen=True)
class SvQualityConfig:
    """Frozen preflight configuration validated before any seam contact."""

    run_root: Path
    canonical_run_root: str
    repository_sha: str
    port: int = DEFAULT_PORT
    provider: str = "luna"
    cell_a_model: str = "luna-a"
    cell_b_model: str = "luna-a"
    verifier_model: str = "luna-v"
    vote_b_model: str = "luna-b"
    allow_fallback: bool = False


def _reject_muse_model(value: str) -> None:
    lowered = value.casefold()
    if "muse" in lowered or "claude" in lowered:
        raise ValueError(f"execution model {value!r} must never enter eval output")


_EXTENDED_LENGTH_PREFIX = "\\\\?\\"
_EXTENDED_LENGTH_UNC_PREFIX = "\\\\?\\UNC\\"


def _strip_extended_length(text: str) -> str:
    """Drop an extended-length prefix so one directory keeps one spelling."""
    slashed = text.replace("/", "\\")
    upper = slashed.upper()
    if upper.startswith(_EXTENDED_LENGTH_UNC_PREFIX):
        return "\\\\" + slashed[len(_EXTENDED_LENGTH_UNC_PREFIX) :]
    if upper.startswith(_EXTENDED_LENGTH_PREFIX):
        return slashed[len(_EXTENDED_LENGTH_PREFIX) :]
    return text


def _resolved_text(text: str | Path) -> str:
    """Absolute, fully resolved path text without an extended-length prefix."""
    resolved = Path(os.path.abspath(os.path.expanduser(_strip_extended_length(str(text)))))
    return str(resolved.resolve())


def _isolation_parts(text: str) -> tuple[str, ...]:
    """Case-folded directory components: one directory has one comparable form."""
    return tuple(part for part in text.casefold().replace("\\", "/").split("/") if part)


def _is_within(text: str, directory: str) -> bool:
    """True when text is the directory itself or lies inside it."""
    parts = _isolation_parts(text)
    prefix = _isolation_parts(directory)
    return parts[: len(prefix)] == prefix


def canonical_run_root(run_root: Path) -> str:
    """Canonicalize a run root, rejecting the live home and the installed runtime."""
    text = str(run_root).replace("\\", "/")
    lowered = text.casefold()
    if ".." in Path(text).parts or ".." in lowered.split("/"):
        raise ValueError("run root traversal is rejected")
    try:
        canonical = _resolved_text(run_root)
    except OSError as exc:
        raise ValueError(f"run root cannot be canonicalized: {exc}") from exc
    # The live-home check runs before the installed-runtime substring check so a
    # run root inside the live home is named as a live-home breach first.
    live_resolved = _resolved_text(live_home_dir())
    if _is_within(canonical, live_resolved):
        raise ValueError("run root must stay isolated from the live home")
    if ".mnemoseed-local" in canonical.replace("\\", "/").casefold():
        raise ValueError("configured run root must stay isolated from the installed runtime")
    return canonical


def build_config(
    *,
    run_root: Path,
    repository_sha: str,
    port: int = DEFAULT_PORT,
    provider: str = "luna",
    cell_a_model: str = "luna-a",
    cell_b_model: str = "luna-a",
    verifier_model: str = "luna-v",
    vote_b_model: str = "luna-b",
    allow_fallback: bool = False,
) -> SvQualityConfig:
    """Validate the Luna-only allowlist, distinct routing, and isolation."""
    if allow_fallback:
        raise ValueError("fallback mixing is rejected for the quality comparison")
    if port == LIVE_PORT:
        raise ValueError("live port 7788 is never used for eval runs")
    if provider.casefold() != "luna":
        raise ValueError("only the Luna provider is allowed for this comparison")
    if not repository_sha.strip():
        raise ValueError("repository SHA is required")
    for value in (cell_a_model, cell_b_model, verifier_model, vote_b_model, provider):
        _reject_muse_model(value)
    if vote_b_model in (cell_a_model, cell_b_model, verifier_model):
        raise ValueError("vote-B must differ from reflect and verifier routes")
    canonical = canonical_run_root(run_root)
    return SvQualityConfig(
        run_root=run_root,
        canonical_run_root=canonical,
        repository_sha=repository_sha,
        port=port,
        provider=provider,
        cell_a_model=cell_a_model,
        cell_b_model=cell_b_model,
        verifier_model=verifier_model,
        vote_b_model=vote_b_model,
        allow_fallback=allow_fallback,
    )


PongSeam = Callable[[str], None]


def build_dry_run_manifest(config: SvQualityConfig) -> dict[str, Any]:
    """Assemble the complete preflight manifest without contacting seats."""
    material_ids = sv_canary_material_ids()
    routes = (config.cell_a_model, config.cell_b_model, config.vote_b_model)
    prereg = preregistration_hash(
        prompt_version=SV_QUALITY_PROMPT_VERSION,
        canary_seed=SV_QUALITY_CANARY_SEED,
        routes=routes,
        material_ids=material_ids,
        oracle_version=ORACLE_VERSION,
        denominator=DENOMINATOR_POLICY,
    )
    return {
        "manifest_version": MANIFEST_VERSION,
        "run_id": uuid.uuid4().hex[:8],
        "repository_sha": config.repository_sha,
        "hardware": platform.machine(),
        "provider": config.provider,
        "cell_a": {
            "cell_id": "single",
            "model": config.cell_a_model,
            "route": f"luna|{config.cell_a_model}",
        },
        "cell_b": {
            "cell_id": "dual",
            "model": config.cell_b_model,
            "route": f"luna|{config.cell_b_model}",
            "vote_b_model": config.vote_b_model,
        },
        "vote_b_distinct": config.vote_b_model
        not in (config.cell_a_model, config.cell_b_model, config.verifier_model),
        "prompt_version": SV_QUALITY_PROMPT_VERSION,
        "canary_seed": SV_QUALITY_CANARY_SEED,
        "material_ids": list(material_ids),
        "canary_hash": sv_canary_corpus_hash(),
        "positive_hash": positive_corpus_hash(),
        "negative_hash": negative_corpus_hash(),
        "preregistration_hash": prereg,
        "oracle_version": ORACLE_VERSION,
        "denominator": DENOMINATOR_POLICY,
        "tokens": {"single": 0, "dual": 0},
        "durations": {"single": 0.0, "dual": 0.0},
        "failures": [],
        "degraded": {"single": 0, "dual": 0},
        "retries": {"single": 0, "dual": 0},
        "raw_paths": [],
        "raw_hashes": [],
        "verdict": "indeterminate",
        "recommendation": "single_off",
        "staged_excluded_path": STAGED_EXCLUDED_PATH,
        "staged_excluded_hash": STAGED_EXCLUDED_HASH,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dry_run": True,
    }


@dataclass
class SvQualityRunner:
    """Owns one preflight; configuration freezes once the run starts."""

    config: SvQualityConfig
    pong: PongSeam
    _started: bool = field(default=False, init=False)

    def reconfigure(self) -> None:
        """Reject any reconfiguration once the lifecycle began."""
        raise RuntimeError("reconfiguration after a run starts requires new preregistration")

    def run(self, *, allow_real: bool = False) -> dict[str, Any]:
        """Dry-run PONG preflight in A-then-B order; real execution refuses."""
        if allow_real:
            raise RuntimeError("real execution lives in a future execute module, not this preflight")
        self._started = True
        attempts: dict[str, int] = {"single": 0, "dual": 0}
        try:
            attempts["single"] += 1
            self.pong("single")
            attempts["dual"] += 1
            self.pong("dual")
        except QuotaRejected as exc:
            return {
                "dry_run": True,
                "attempts": attempts,
                "quota_rejected": True,
                "error": str(exc),
            }
        return {"dry_run": True, "attempts": attempts, "quota_rejected": False}

    def write_dry_run_manifest(self) -> Path:
        """Persist the complete dry-run manifest for operator inspection."""
        return write_manifest(self.config.run_root, build_dry_run_manifest(self.config))


def main(argv: list[str] | None = None) -> int:
    """Preflight entry; --real refuses without contacting any seam."""
    parser = argparse.ArgumentParser(prog="python -m mnemoseed_local.eval.sv_quality_runner")
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--repository-sha", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args(argv)
    if args.real:
        print(json.dumps({"dry_run": False, "refused": True}))
        return 2
    config = build_config(
        run_root=Path(args.run_root),
        repository_sha=args.repository_sha,
        port=args.port,
    )

    def _unused(cell_id: str) -> None:
        raise RuntimeError("no seam supplied for preflight inspection")

    runner = SvQualityRunner(config, _unused)
    path = runner.write_dry_run_manifest()
    print(json.dumps({"dry_run": True, "manifest": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
