"""InvalidRun in the run path must land an UNKNOWN record in the append-only report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mnemoseed_local.eval.realcorpus_recall import cli
from mnemoseed_local.eval.realcorpus_recall.manifest import (
    CorpusManifest,
    InvalidRun,
    write_manifest,
)


def _args(tmp_path: Path, pin: Path, queries: Path) -> argparse.Namespace:
    return argparse.Namespace(
        pin=str(pin),
        queries=str(queries),
        thresholds=None,
        temp_root=str(tmp_path / "tmp"),
        home=str(tmp_path / "home"),
        out=str(tmp_path / "report.jsonl"),
        profile_id="default",
    )


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    pin = tmp_path / "pin.json"
    write_manifest(
        CorpusManifest(
            corpus_manifest_sha256="a",
            config_sha256="b",
            embedder_model_version="c",
            code_revision="d",
        ),
        pin,
    )
    document = {
        "status": "DRAFT — owner signature pending; not frozen",
        "signed": False,
        "queries": [
            {
                "qid": "Q-001",
                "text": "q",
                "lang": "zh",
                "kind": "miss_seed",
                "target_chunk_ids": ["chunk-a"],
                "target_content_sha256": "",
                "ingestion_verified": True,
                "rationale": "probe",
                "author": "draft",
                "date": "2026-11-22",
                "holdout": True,
                "seen": False,
                "frozen_sha256": "",
            }
        ],
    }
    queries = tmp_path / "queries.json"
    queries.write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / "home").mkdir()
    return pin, queries


def test_invalid_run_writes_unknown_report(tmp_path: Path, monkeypatch: Any) -> None:
    pin, queries = _inputs(tmp_path)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise InvalidRun("synthetic drift")

    monkeypatch.setattr(cli, "_verify_home", _boom)
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "append_report", lambda path, record: captured.append(dict(record)))

    code = cli.run_command(_args(tmp_path, pin, queries))

    assert code != 0
    assert len(captured) == 1
    assert captured[0]["verdict"] == "UNKNOWN"
    assert captured[0]["contract"]["invalid_runs"] == 1
    assert captured[0]["contract"]["bars"]["B1"]["verdict"] == "UNKNOWN"
