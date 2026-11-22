"""Standalone real-corpus recall evaluation runner."""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mnemoseed_local.eval.realcorpus_recall.contract import (
    ContractResult,
    Thresholds,
    Verdict,
    check_no_drift,
    judge,
)
from mnemoseed_local.eval.realcorpus_recall.corpus import (
    copy_corpus,
    default_temp_root,
    directory_digest,
    restore_writable_state,
    snapshot_pristine,
)
from mnemoseed_local.eval.realcorpus_recall.isolation import daemon_context, pin_home_in_config
from mnemoseed_local.eval.realcorpus_recall.manifest import (
    CorpusManifest,
    InvalidRun,
    compute_corpus_manifest,
    read_manifest,
    verify_manifest,
    write_manifest,
)
from mnemoseed_local.eval.realcorpus_recall.metrics import QuerySpec, RunMetrics, score_run
from mnemoseed_local.eval.realcorpus_recall.replay import HttpRecallService, ProductRecallReplay
from mnemoseed_local.eval.realcorpus_recall.report import append_report

DEFAULT_PIN_PATH = Path("docs/zh/eval/realcorpus-recall/corpus_manifest.sha256")
DEFAULT_QUERIES_PATH = Path("docs/zh/eval/realcorpus-recall/queries-v1.json")


def _load_queries(path: Path) -> list[QuerySpec]:
    document = json.loads(path.read_text(encoding="utf-8"))
    raw = document["queries"] if isinstance(document, dict) else document
    return [QuerySpec(**item) for item in raw]


def _load_thresholds(path: Path | None) -> Thresholds:
    if path is None:
        return Thresholds()
    document = json.loads(path.read_text(encoding="utf-8"))
    return Thresholds(**document)


def copy_command(args: argparse.Namespace) -> int:
    pin_path = Path(args.pin)
    copy = copy_corpus(
        Path(args.source),
        temp_root=Path(args.temp_root) if args.temp_root else default_temp_root(),
        code_revision=args.code_revision,
    )
    write_manifest(copy.manifest, pin_path)
    print(
        json.dumps(
            {
                "corpus_home": str(copy.home),
                "file_count": copy.file_count,
                "total_bytes": copy.total_bytes,
                "elapsed_seconds": copy.elapsed_seconds,
                "pin": str(pin_path),
            },
            indent=2,
        )
    )
    return 0


def verify_command(args: argparse.Namespace) -> int:
    expected = read_manifest(Path(args.pin))
    actual = compute_corpus_manifest(
        Path(args.home),
        code_revision=expected.code_revision,
        embedder_version=expected.embedder_model_version,
    )
    verify_manifest(actual, expected)
    print("manifest verified")
    return 0


def _verify_home(home: Path, expected: CorpusManifest) -> None:
    verify_manifest(
        compute_corpus_manifest(
            home,
            code_revision=expected.code_revision,
            embedder_version=expected.embedder_model_version,
        ),
        expected,
    )


def _emit(
    out: str,
    expected: CorpusManifest,
    thresholds: Thresholds,
    result: ContractResult,
    runs: Sequence[RunMetrics],
    **extra: Any,
) -> int:
    record: dict[str, Any] = {
        "utc": datetime.now(UTC).isoformat(),
        "run_id": uuid.uuid4().hex,
        "verdict": result.verdict.value,
        "pins": expected.to_dict(),
        "thresholds": dict(thresholds.__dict__),
        "ratified_ids": sorted(thresholds.ratified_ids),
        "unratified_ids": sorted(thresholds.unratified_ids),
        "contract": result.to_dict(),
        "runs": [
            {
                "run_index": run.run_index,
                "baseline_sha256": run.baseline_sha256,
                "queries": [
                    {
                        "qid": metric.qid,
                        "kind": metric.kind,
                        "holdout": metric.holdout,
                        "seen": metric.seen,
                        "ingestion_verified": metric.ingestion_verified,
                        "hit": metric.hit,
                        "rank": metric.rank,
                        "tokens_used": metric.tokens_used,
                    }
                    for metric in run.per_query
                ],
            }
            for run in runs
        ],
    }
    record.update(extra)
    append_report(Path(out), record)
    summary: dict[str, Any] = {"verdict": result.verdict.value, "report": out}
    summary.update(extra)
    print(json.dumps(summary, indent=2))
    return 0 if result.verdict is Verdict.PASS else 1


def run_command(args: argparse.Namespace) -> int:
    pin_path = Path(args.pin)
    expected = read_manifest(pin_path)
    queries = _load_queries(Path(args.queries))
    thresholds = _load_thresholds(Path(args.thresholds) if args.thresholds else None)
    temp_root = Path(args.temp_root) if args.temp_root else default_temp_root()
    runs: list[RunMetrics] = []

    try:
        if args.home:
            home = Path(args.home)
        else:
            home = copy_corpus(
                Path(args.source),
                temp_root=temp_root,
                code_revision=expected.code_revision,
                embedder_version=expected.embedder_model_version,
            ).home
        _verify_home(home, expected)
        pristine = snapshot_pristine(home, temp_root / f"s0-pristine-{uuid.uuid4().hex[:8]}")
        baseline = directory_digest(pristine)
        work = temp_root / f"s0-work-{uuid.uuid4().hex[:8]}"
        for index in range(thresholds.n_runs):
            restore_writable_state(pristine, work)
            if directory_digest(work) != baseline:
                raise InvalidRun("pristine restore mismatch")
            pin_home_in_config(work)
            with daemon_context(work) as handle:
                replay = ProductRecallReplay(HttpRecallService(handle.base_url, home=work))
                observations = {
                    query.qid: replay.replay(query.text, qid=query.qid, profile_id=args.profile_id)
                    for query in queries
                }
                runs.append(score_run(observations, queries, run_index=index, baseline_sha256=baseline))
        check_no_drift(runs)
        result = judge(runs, queries, thresholds)
    except InvalidRun as exc:
        result = judge(runs, queries, thresholds, invalid_runs=1)
        return _emit(args.out, expected, thresholds, result, runs, error=str(exc))
    return _emit(args.out, expected, thresholds, result, runs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mnemoseed_local.eval.realcorpus_recall")
    sub = parser.add_subparsers(dest="command", required=True)

    copy = sub.add_parser("copy", help="measure a corpus copy and write the provenance pin")
    copy.add_argument("--source", default=str(Path.home() / ".mnemoseed-local"))
    copy.add_argument("--temp-root", default=None)
    copy.add_argument("--pin", default=str(DEFAULT_PIN_PATH))
    copy.add_argument("--code-revision", default=None)

    verify = sub.add_parser("verify", help="verify a corpus home against its pin")
    verify.add_argument("--home", required=True)
    verify.add_argument("--pin", default=str(DEFAULT_PIN_PATH))

    run = sub.add_parser("run", help="run the full contract over independent cold starts")
    run.add_argument("--source", default=str(Path.home() / ".mnemoseed-local"))
    run.add_argument("--home", default=None, help="existing pinned corpus copy; skips re-copy")
    run.add_argument("--temp-root", default=None)
    run.add_argument("--pin", default=str(DEFAULT_PIN_PATH))
    run.add_argument("--queries", default=str(DEFAULT_QUERIES_PATH))
    run.add_argument("--thresholds", default=None)
    run.add_argument("--out", default="realcorpus-recall.jsonl")
    run.add_argument("--profile-id", default="default")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "copy":
        return copy_command(args)
    if args.command == "verify":
        return verify_command(args)
    return run_command(args)
