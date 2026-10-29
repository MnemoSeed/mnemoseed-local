"""A stand-in for the isolated Atlas verifier that ends three ways.

The verifier owns a daemon tree and must reclaim it whether the run completes,
fails, or is killed from outside with no unwinding. This script drives the
harness's own ownership and teardown surface, then ends the way its mode says,
so a parent test can inspect the tree afterwards.

Usage: python atlas_teardown_fixture.py <workdir> <launcher> <port> <mode>
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

LOOPBACK_HOST = "127.0.0.1"
KILLED_SLEEP_SECONDS = 300.0
TREE_READY_TIMEOUT_SECONDS = 60.0
TREE_READY_POLL_SECONDS = 0.1


def load_harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "atlas_verify.py"
    spec = importlib.util.spec_from_file_location("atlas_verify_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fail() -> None:
    raise RuntimeError("the verification failed after it owned the tree")


def _hang() -> None:
    time.sleep(KILLED_SLEEP_SECONDS)


BODIES: dict[str, Callable[[], None]] = {
    "completed": lambda: None,
    "raised": _fail,
    "killed": _hang,
}


def _await_tree_ready(workdir: Path) -> None:
    """Every exit path must tear down a live tree, so the body waits for it."""
    deadline = time.monotonic() + TREE_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if (workdir / "grandchild.pid").exists() and (workdir / "heartbeat").exists():
            return
        time.sleep(TREE_READY_POLL_SECONDS)
    raise RuntimeError(f"the run-owned tree never became ready at {workdir}")


def main() -> int:
    workdir, launcher, port, mode = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
    harness = load_harness()
    workdir.mkdir(parents=True, exist_ok=True)
    daemon = harness.spawn_owned_daemon(
        [sys.executable, launcher, str(workdir), LOOPBACK_HOST, str(port)],
        env=os.environ.copy(),
        cwd=str(workdir),
    )
    (workdir / "verifier.pid").write_text(str(os.getpid()), encoding="utf-8")
    try:
        _await_tree_ready(workdir)
        BODIES[mode]()
    finally:
        harness.stop_owned_daemon(daemon, f"http://{LOOPBACK_HOST}:{port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
