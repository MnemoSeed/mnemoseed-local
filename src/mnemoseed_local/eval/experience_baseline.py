"""Deterministic Batch-1 parser/contract/scorer baseline runner.

Runs the frozen golden corpus through the real parser and the real scorer
without any model, provider, daemon, config, or network use, and reports the
frozen numbers with explicit NOT_OBSERVED topics. Transport-only helpers over
plain JSON dicts: stdlib-only with no storage, config, daemon, provider, or
network use.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import importlib
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mnemoseed_local.eval.experience_contract import parse_experience_raw
from mnemoseed_local.eval.experience_metrics import score_experience

BASELINE_VERSION = 1
SEED_POLICY = (
    "deterministic: frozen fixtures only, no sampling and no model; "
    "cases run in sorted case_id order and every comparison is exact"
)
GUARDED_ENTRY_POINTS = (
    "socket.socket.connect",
    "socket.create_connection",
    "urllib.request.urlopen",
    "http.client.HTTPConnection.request",
)
BANNED_CLAIM_PHRASES = (
    "real model quality is",
    "generaliz",
    "production-ready",
    "promotion gate passed",
    "efficacy",
    "simulat",
    "recall improv",
    "extraction precision",
)
BASELINE_CLAIMS = (
    "Synthetic-only baseline: frozen fixtures through the deterministic parser and scorer.",
    "No model calls, no network calls, no live reads or writes.",
    "Precision and recall below describe contract matching on synthetic fixtures only.",
    "Natural-language extraction quality, model behavior, coverage, eligibility, "
    "and live impact are NOT_OBSERVED.",
)
FORBIDDEN_MODULE_PREFIXES = ("mnemoseed_local.daemon", "mnemoseed_local.config")
CANONICAL_EXCLUDED = ("started_at", "duration_s", "out_path")
CANONICAL_HOME_TOKEN = "<isolated-home>"


def check_banned_claims(text: str) -> dict[str, Any]:
    """Scan baseline prose for banned claim language; never silently pass."""
    lowered = text.lower()
    hits = [phrase for phrase in BANNED_CLAIM_PHRASES if phrase in lowered]
    return {"passed": not hits, "hits": hits, "checked": list(BANNED_CLAIM_PHRASES)}


def canonical_baseline(report: dict[str, Any]) -> dict[str, Any]:
    """Render the environment-portable form used for repeatability comparison.

    Run timing, the output path, and the isolated home root vary between
    runs by design; fixture file paths vary by invocation directory. None
    of them is scored output, so the canonical form excludes or normalizes
    them while keeping every scored field exact.
    """
    snap = copy.deepcopy(report)
    for key in CANONICAL_EXCLUDED:
        snap.pop(key, None)
    isolation = snap.get("isolation")
    if isinstance(isolation, dict):
        isolation["home"] = CANONICAL_HOME_TOKEN
    snap["files_read"] = [Path(entry).name for entry in snap.get("files_read", [])]
    return snap


def canonical_sha256(report: dict[str, Any]) -> str:
    """Hash the canonical form for portable repeatability evidence."""
    return hashlib.sha256(json.dumps(canonical_baseline(report), sort_keys=True).encode("utf-8")).hexdigest()


def _resolve_target(dotted: str) -> tuple[Any, str]:
    """Split a guarded entry point into its parent object and attribute name."""
    parts = dotted.split(".")
    for width in range(len(parts) - 1, 0, -1):
        try:
            owner: Any = importlib.import_module(".".join(parts[:width]))
        except ImportError:
            continue
        for part in parts[width:-1]:
            owner = getattr(owner, part)
        return owner, parts[-1]
    raise ImportError(f"cannot resolve guarded entry point {dotted!r}")


@contextmanager
def _guarded_counters() -> Iterator[dict[str, int]]:
    """Count calls to forbidden network entry points while the baseline runs."""
    counts = {name: 0 for name in GUARDED_ENTRY_POINTS}
    originals: dict[str, Any] = {}
    owners: dict[str, Any] = {}
    try:
        for name in GUARDED_ENTRY_POINTS:
            owner, attr = _resolve_target(name)
            original = getattr(owner, attr)
            originals[name] = original
            owners[name] = (owner, attr)

            def _counting(*args: Any, _original: Any = original, _name: str = name, **kwargs: Any) -> Any:
                counts[_name] += 1
                return _original(*args, **kwargs)

            setattr(owner, attr, _counting)
        yield counts
    finally:
        for name, original in originals.items():
            owner, attr = owners[name]
            setattr(owner, attr, original)


def _list_home(home: str | None) -> set[str] | None:
    """List the isolated home directory, or null when it is absent."""
    if home is None:
        return None
    try:
        return set(os.listdir(home))
    except (FileNotFoundError, NotADirectoryError):
        return None


def _home_writes(before: set[str] | None, after: set[str] | None) -> list[str]:
    """Diff two home snapshots into an explicit write list."""
    if before is None and after is None:
        return []
    if before is None:
        return ["home-directory-created"] + sorted(after or set())
    return sorted((after or set()) - before)


def _new_forbidden_modules(before: set[str]) -> list[str]:
    """Report forbidden modules imported during the run, never ambient ones."""
    found = [
        name
        for name in set(sys.modules) - before
        if name.startswith(FORBIDDEN_MODULE_PREFIXES)
        or (name.startswith("mnemoseed_local") and "provider" in name)
    ]
    return sorted(found)


def run_baseline(
    inputs_path: str | Path, truth_path: str | Path, *, out_path: str | Path | None = None
) -> dict[str, Any]:
    """Run the frozen corpus end to end and return the baseline report."""
    started = datetime.datetime.now(datetime.UTC).isoformat()
    begun = time.perf_counter()
    home = os.environ.get("MNEMOSEED_LOCAL_HOME")
    home_before = _list_home(home)
    modules_before = set(sys.modules)
    with open(inputs_path, encoding="utf-8") as handle:
        inputs = json.load(handle)
    with open(truth_path, encoding="utf-8") as handle:
        truth = json.load(handle)
    with _guarded_counters() as counts:
        predictions = [
            {
                "case_id": case["case_id"],
                "result": parse_experience_raw(
                    copy.deepcopy(case["raw"]), sources=copy.deepcopy(case["sources"])
                ),
            }
            for case in sorted(inputs["cases"], key=lambda item: str(item["case_id"]))
        ]
        score = score_experience(predictions, truth["cases"])
    duration = time.perf_counter() - begun
    home_after = _list_home(home)
    variants = [str(case.get("variant")) for case in inputs["cases"]]
    claims = list(BASELINE_CLAIMS)
    report: dict[str, Any] = {
        "baseline_version": BASELINE_VERSION,
        "seed_policy": SEED_POLICY,
        "corpus": {
            "inputs_version": inputs["version"],
            "truth_version": truth["version"],
            "cases": len(inputs["cases"]),
            "base": variants.count("base"),
            "robustness": variants.count("robustness"),
        },
        "started_at": started,
        "duration_s": duration,
        "predictions": predictions,
        "score": score,
        "NOT_OBSERVED": list(score["NOT_OBSERVED"]),
        "claims": claims,
        "banned_claims": check_banned_claims(" ".join(claims)),
        "ops": {"guarded": list(GUARDED_ENTRY_POINTS), "counts": dict(counts)},
        "isolation": {
            "forbidden_modules": _new_forbidden_modules(modules_before),
            "home": home,
            "home_writes": _home_writes(home_before, home_after),
        },
        "files_read": [str(inputs_path), str(truth_path)],
        "out_path": None if out_path is None else str(out_path),
    }
    if out_path is not None:
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, indent=2)
    return report


def main(argv: list[str] | None = None) -> int:
    """Write the baseline report for two fixture paths: inputs, truth, out."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print("usage: experience-baseline INPUTS_JSON TRUTH_JSON OUT_JSON")
        return 2
    inputs_path, truth_path, out_path = args
    report = run_baseline(inputs_path, truth_path, out_path=out_path)
    fact = report["score"]["fact"]
    experience = report["score"]["experience"]
    print(
        f"baseline v{report['baseline_version']}: {report['corpus']['cases']} cases, "
        f"fact P={fact['P']} R={fact['R']}, experience P={experience['P']} R={experience['R']}, "
        f"errors={sum(len(items) for items in report['score']['errors'].values())}"
    )
    print(f"report: {out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
