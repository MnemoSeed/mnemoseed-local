"""Writer lock ownership and the out-of-ledger invocation receipt."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mnemoseed_local.instrument.ledger import (
    InstrumentPaths,
    InvocationReceiptUnavailable,
    WriterLock,
    WriterLockHeld,
    read_events,
    write_invocation_receipt,
)

CHILD_LOCK_SCRIPT = """
import pathlib
import sys
import time

from mnemoseed_local.instrument.ledger import WriterLock

lock = WriterLock(pathlib.Path(sys.argv[1]))
lock.acquire()
pathlib.Path(sys.argv[2]).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 60
while not pathlib.Path(sys.argv[3]).exists() and time.monotonic() < deadline:
    time.sleep(0.05)
lock.release()
"""


def test_a_second_holder_is_refused(paths: InstrumentPaths) -> None:
    paths.writer_lock.parent.mkdir(parents=True, exist_ok=True)
    holder = WriterLock(paths.writer_lock)
    holder.acquire()
    try:
        with pytest.raises(WriterLockHeld) as failure:
            WriterLock(paths.writer_lock).acquire()
        assert failure.value.diag_code == "writer_lock_held"
    finally:
        holder.release()


def test_the_release_is_idempotent(paths: InstrumentPaths) -> None:
    lock = WriterLock(paths.writer_lock)
    lock.acquire()
    lock.release()
    lock.release()


def test_the_lock_file_is_never_removed(paths: InstrumentPaths) -> None:
    lock = WriterLock(paths.writer_lock)
    lock.acquire()
    lock.release()
    assert paths.writer_lock.exists()


def test_the_lock_is_not_taken_over_from_a_stale_pid(paths: InstrumentPaths) -> None:
    paths.writer_lock.parent.mkdir(parents=True, exist_ok=True)
    paths.writer_lock.write_text("999999", encoding="utf-8")
    lock = WriterLock(paths.writer_lock)
    lock.acquire()
    lock.release()
    assert paths.writer_lock.exists()


def test_the_lock_is_contended_across_processes(
    paths: InstrumentPaths,
    tmp_path: Path,
) -> None:
    paths.writer_lock.parent.mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "child-ready"
    release = tmp_path / "child-release"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            CHILD_LOCK_SCRIPT,
            str(paths.writer_lock),
            str(ready),
            str(release),
        ],
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "the child process never acquired the writer lock"
        with pytest.raises(WriterLockHeld):
            WriterLock(paths.writer_lock).acquire()
    finally:
        release.write_text("go", encoding="utf-8")
        child.wait(timeout=60)


def test_the_receipt_is_written_outside_the_ledger(
    paths: InstrumentPaths,
    initialize_home,
) -> None:
    home = initialize_home()
    receipt = write_invocation_receipt(
        paths,
        run_id="run-1",
        attempt_id="attempt-1",
        diag_code="writer_lock_held",
        detail={"reason": "the writer lock is held"},
    )
    assert receipt.parent == paths.invocations_dir
    assert receipt.name == "run-1.attempt-1.receipt.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["receipt_version"] == 1
    assert payload["run_id"] == "run-1"
    assert payload["attempt_id"] == "attempt-1"
    assert "writer_lock_held" in json.dumps(payload)
    assert read_events(home.ledger, cohort_id=home.primary.cohort_id) == []


def test_the_receipt_is_created_exclusively(paths: InstrumentPaths) -> None:
    paths.invocations_dir.mkdir(parents=True, exist_ok=True)
    target = paths.invocations_dir / "run-1.attempt-1.receipt.json"
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(InvocationReceiptUnavailable) as failure:
        write_invocation_receipt(
            paths,
            run_id="run-1",
            attempt_id="attempt-1",
            diag_code="writer_lock_held",
            detail={"reason": "the writer lock is held"},
        )
    assert failure.value.diag_code == "invocation_receipt_unavailable"
    assert target.read_text(encoding="utf-8") == "existing"


def test_a_retry_never_overwrites_the_receipt(paths: InstrumentPaths) -> None:
    first = write_invocation_receipt(
        paths,
        run_id="run-1",
        attempt_id="attempt-1",
        diag_code="writer_lock_held",
        detail={"reason": "the writer lock is held"},
    )
    original = first.read_bytes()
    try:
        again = write_invocation_receipt(
            paths,
            run_id="run-1",
            attempt_id="attempt-1",
            diag_code="writer_lock_held",
            detail={"reason": "the writer lock is held"},
        )
    except InvocationReceiptUnavailable:
        again = None
    assert first.read_bytes() == original
    if again is not None:
        assert again == first


def test_a_different_receipt_payload_is_refused(paths: InstrumentPaths) -> None:
    first = write_invocation_receipt(
        paths,
        run_id="run-1",
        attempt_id="attempt-1",
        diag_code="writer_lock_held",
        detail={"reason": "the writer lock is held"},
    )
    original = first.read_bytes()
    with pytest.raises(InvocationReceiptUnavailable) as failure:
        write_invocation_receipt(
            paths,
            run_id="run-1",
            attempt_id="attempt-1",
            diag_code="ledger_missing",
            detail={"reason": "another reason"},
        )
    assert failure.value.diag_code == "invocation_receipt_unavailable"
    assert first.read_bytes() == original


def test_an_unwritable_receipt_location_is_reported(paths: InstrumentPaths) -> None:
    paths.invocations_dir.parent.mkdir(parents=True, exist_ok=True)
    paths.invocations_dir.write_text("not a directory", encoding="utf-8")
    with pytest.raises(InvocationReceiptUnavailable) as failure:
        write_invocation_receipt(
            paths,
            run_id="run-1",
            attempt_id="attempt-1",
            diag_code="writer_lock_held",
            detail={"reason": "the writer lock is held"},
        )
    assert failure.value.diag_code == "invocation_receipt_unavailable"


def test_a_refused_run_leaves_the_ledger_untouched(
    paths: InstrumentPaths,
    initialize_home,
) -> None:
    home = initialize_home()
    holder = WriterLock(paths.writer_lock)
    holder.acquire()
    try:
        receipt = write_invocation_receipt(
            paths,
            run_id="run-1",
            attempt_id="attempt-1",
            diag_code="writer_lock_held",
            detail={"reason": "the writer lock is held"},
        )
    finally:
        holder.release()
    assert receipt.exists()
    assert read_events(home.ledger, cohort_id=home.primary.cohort_id) == []
