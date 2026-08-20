"""PRD-B2.5 CLI surface: the daemon on/off user switch.

- off: writes the disabled marker FIRST (a watcher / up must not boot a fresh
  daemon during the poll, and the marker must never land on a revived one),
  then POST /daemon/shutdown (best-effort), then polls for listener
  disappearance; reports are liveness-aware; rc 0; idempotent when the marker
  is already present (no repeated shutdown).
- on: remove the marker first; a running daemon is reported, never restarted;
  otherwise the existing up start path runs.
- up: refused (rc 1) while the marker is present — run_server never reached.
- daemon_state: the marker is CONFIG_DIR/daemon.off; presence = disabled,
  absence = enabled (the install default, zero config).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.cli import build_parser, main
from mnemoseed_local.daemon_state import (
    disabled_marker,
    is_disabled,
    set_disabled,
    set_enabled,
)
from mnemoseed_local.rest_client import DaemonClient, DaemonRestError, DaemonUnavailableError


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic home: the cli and config module namespaces both point at
    tmp_path; the marker helpers read config.CONFIG_DIR at call time."""
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    return tmp_path


# ---------------------------------------------------------------- daemon_state


def test_marker_default_absent_means_enabled(home: Path) -> None:
    assert disabled_marker() == home / "daemon.off"
    assert not is_disabled()


def test_set_disabled_creates_marker(home: Path) -> None:
    set_disabled()
    assert (home / "daemon.off").exists()
    assert is_disabled()


def test_set_enabled_removes_marker_and_is_idempotent(home: Path) -> None:
    set_disabled()
    set_enabled()
    assert not is_disabled()
    set_enabled()  # absent marker: no-op
    assert not is_disabled()


def test_set_disabled_creates_a_missing_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONFIG_DIR may not exist yet: set_disabled creates it before writing
    the marker (the marker must survive a fresh install layout)."""
    missing = home / "fresh" / "home"
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", missing)
    assert not missing.exists()
    set_disabled()
    assert missing.exists()
    assert (missing / "daemon.off").exists()
    assert is_disabled()


# ---------------------------------------------------------------- CLI fakes


class _FakeDaemonClient:
    """Answers /healthz until the shutdown POST lands, then reports gone."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self._running = True

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        del body
        self.posted.append(path)
        self._running = False
        return {"ok": True, "status": "shutting_down"}

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del params
        if not self._running:
            raise DaemonUnavailableError("cannot reach the daemon: listener gone")
        return {"status": "ok"}


class _UnreachableDaemonClient:
    """The daemon never answers (already stopped / not running)."""

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        del path, body
        raise DaemonUnavailableError("cannot reach the daemon: connection refused")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del path, params
        raise DaemonUnavailableError("cannot reach the daemon: connection refused")


class _StaysUpDaemonClient:
    """Answers the shutdown POST but never goes down (hung teardown shape)."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        del body
        self.posted.append(path)
        return {"ok": True, "status": "shutting_down"}

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del path, params
        return {"status": "ok"}


class _RefusingDaemonClient:
    """A daemon that answers but does not serve the shutdown endpoint (an
    older build): the POST comes back non-2xx instead of timing out. `alive`
    controls the /healthz probe after the refusal."""

    def __init__(self, status: int = 404, *, alive: bool = True) -> None:
        self._status = status
        self._alive = alive

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        del path, body
        raise DaemonRestError(self._status, "refused")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del path, params
        if not self._alive:
            raise DaemonUnavailableError("cannot reach the daemon: connection refused")
        return {"status": "ok"}


class _DyingAfterPollClient:
    """Reachable through the poll window, gone by the final probe: a drain
    that closed the listener just after the poll deadline."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self._probes = 0

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        del body
        self.posted.append(path)
        return {"ok": True, "status": "shutting_down"}

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del path, params
        self._probes += 1
        if self._probes >= 2:
            raise DaemonUnavailableError("cannot reach the daemon: listener gone")
        return {"status": "ok"}


class _SlowDaemonClient(DaemonClient):
    """A bound-but-slow listener shape (the B6 stall): /healthz answers only
    after a delay proportional to the client's configured timeout (10%), so a
    leaked 30s probe client parks the poll ~30x longer than the 1s probe cap."""

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        del body
        return {"ok": True, "status": "shutting_down"}

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del path, params
        time.sleep(self.timeout / 10.0)
        return {"status": "ok"}


def _fake_up_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace the storage stack and the daemon runner with call counters, so
    no real daemon boots during the on -> up path tests."""
    calls = {"build_stores": 0, "run_server": 0}

    class _FakeUpStores:
        async def close(self) -> None:
            pass

    def _fake_build_stores(config: object) -> _FakeUpStores:
        del config
        calls["build_stores"] += 1
        return _FakeUpStores()

    def _fake_run_server(host: str, port: int) -> int:
        del host, port
        calls["run_server"] += 1
        return 0

    monkeypatch.setattr("mnemoseed_local.storage.factory.build_stores", _fake_build_stores)
    monkeypatch.setattr("mnemoseed_local.daemon.runner.run_server", _fake_run_server)
    return calls


# ---------------------------------------------------------------- off


def test_off_writes_marker_before_shutdown_post(home: Path, monkeypatch, capsys) -> None:
    """The marker lands BEFORE the shutdown POST: during the poll a watcher /
    up must not boot a fresh daemon, and the marker must never land on a
    revived one."""
    marker_seen_at_post: list[bool] = []

    class _OrderRecordingClient(_FakeDaemonClient):
        def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            marker_seen_at_post.append((home / "daemon.off").exists())
            return super().post(path, body)

    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _OrderRecordingClient())
    assert main(["off"]) == 0
    assert marker_seen_at_post == [True], "the marker must exist before the shutdown POST"
    assert (home / "daemon.off").exists(), "off must write the disabled marker"
    assert "memory service disabled" in capsys.readouterr().out


def test_off_unreachable_daemon_still_converges(home: Path, monkeypatch, capsys) -> None:
    """Unreachable daemon: no crash, the marker still lands, honest report,
    rc 0 — a refused shutdown POST is the already-stopped signal."""
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _UnreachableDaemonClient())
    assert main(["off"]) == 0
    assert (home / "daemon.off").exists()
    out = capsys.readouterr().out
    assert "memory service disabled" in out
    assert "error:" not in out


def test_off_idempotent_when_marker_present(home: Path, monkeypatch, capsys) -> None:
    """Already disabled and no daemon running: rc 0, 'already off', no
    repeated shutdown POST, no extra note."""
    set_disabled()

    class _NoPostAllowedClient(_UnreachableDaemonClient):
        def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            del path, body
            raise AssertionError("an already-disabled off must not POST shutdown again")

    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _NoPostAllowedClient())
    assert main(["off"]) == 0
    out = capsys.readouterr().out
    assert "already off" in out
    assert "currently running" not in out
    assert is_disabled(), "the marker must survive an idempotent off"


def test_off_already_off_reports_running_daemon(home: Path, monkeypatch, capsys) -> None:
    """Already disabled but a daemon IS running: rc 0, 'already off', plus the
    honest note that the daemon is running and will not be restarted by up."""
    set_disabled()
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _FakeDaemonClient())
    assert main(["off"]) == 0
    out = capsys.readouterr().out
    assert "already off" in out
    assert "currently running" in out
    assert "will not be restarted" in out
    assert is_disabled()


def test_off_reports_still_running_when_listener_survives_the_poll(home: Path, monkeypatch, capsys) -> None:
    """The daemon accepted the shutdown POST but keeps answering /healthz past
    the poll: the final probe stays alive, so the report says the daemon is
    still running (a revived or never-stopping daemon), never 'shutting down'."""
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _StaysUpDaemonClient())
    monkeypatch.setattr("mnemoseed_local.cli._OFF_POLL_TIMEOUT_S", 0.2)
    assert main(["off"]) == 0
    out = capsys.readouterr().out
    assert "daemon is still running" in out
    assert "may still be shutting down" not in out
    assert (home / "daemon.off").exists()


def test_off_reports_shutting_down_when_listener_closes_after_poll(home: Path, monkeypatch, capsys) -> None:
    """The listener closes just after the poll window (the drain is still in
    progress): the final probe is dead, so the report says 'may still be
    shutting down' — never 'still running'."""
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _DyingAfterPollClient())
    monkeypatch.setattr("mnemoseed_local.cli._OFF_POLL_TIMEOUT_S", 0.2)
    assert main(["off"]) == 0
    out = capsys.readouterr().out
    assert "may still be shutting down" in out
    assert "daemon is still running" not in out
    assert (home / "daemon.off").exists()


def test_off_refused_live_daemon_reports_still_running_with_guidance(home: Path, monkeypatch, capsys) -> None:
    """A live daemon that does not serve the shutdown endpoint (older build):
    rc 0, marker lands, and the report says the daemon is still running with
    manual-stop guidance — the service is disabled and stays off."""
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _RefusingDaemonClient())
    assert main(["off"]) == 0
    out = capsys.readouterr().out
    assert "still running" in out
    assert "did not accept" in out
    assert "stop it manually" in out
    assert "mnemoseed-local on" in out
    assert "error:" not in capsys.readouterr().err
    assert (home / "daemon.off").exists()


def test_off_refused_daemon_gone_reports_plain_convergence(home: Path, monkeypatch, capsys) -> None:
    """The daemon answered the POST but is no longer alive at the probe: plain
    convergence, no manual-stop guidance."""
    monkeypatch.setattr(
        "mnemoseed_local.rest_client.resolve_client",
        lambda args: _RefusingDaemonClient(alive=False),
    )
    assert main(["off"]) == 0
    out = capsys.readouterr().out
    assert "memory service disabled" in out
    assert "stop it manually" not in out
    assert (home / "daemon.off").exists()


def test_off_refused_internal_error_still_converges(home: Path, monkeypatch, capsys) -> None:
    """Any non-2xx refusal (not just 404) is swallowed: an internal-error
    response converges with the honest report, rc 0 — the swallow is not
    status-specific."""
    monkeypatch.setattr(
        "mnemoseed_local.rest_client.resolve_client",
        lambda args: _RefusingDaemonClient(status=500),
    )
    assert main(["off"]) == 0
    out = capsys.readouterr().out
    assert "still running" in out
    assert "did not accept" in out
    assert (home / "daemon.off").exists()


def test_off_marker_write_failure_is_honest_error(home: Path, monkeypatch, capsys) -> None:
    """The marker write is the first step: a write failure stops the verb with
    an honest error (rc 1) and no shutdown POST follows."""

    def _explode_set_disabled() -> Path:
        raise OSError("disk full")

    monkeypatch.setattr("mnemoseed_local.daemon_state.set_disabled", _explode_set_disabled)
    posted: list[str] = []

    class _NoPostClient(_FakeDaemonClient):
        def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            posted.append(path)
            return {"ok": True, "status": "shutting_down"}

    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _NoPostClient())
    assert main(["off"]) == 1
    assert posted == [], "a failed marker write must stop off before the POST"
    assert "error:" in capsys.readouterr().err
    assert not (home / "daemon.off").exists()


def test_off_unexpected_post_failure_guards_with_guidance(home: Path, monkeypatch, capsys) -> None:
    """An unexpected (non-typed) shutdown-POST failure: the marker is already
    written (state converged), and the verb reports honestly with manual-stop
    guidance, rc 1 — never a raw traceback."""

    class _ExplodingPostClient(_FakeDaemonClient):
        def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            del path, body
            raise RuntimeError("boom")

    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _ExplodingPostClient())
    assert main(["off"]) == 1
    err = capsys.readouterr().err
    assert "shutdown request failed" in err
    assert "stop it manually" in err
    assert (home / "daemon.off").exists(), "the marker stays: the state already converged"


def test_off_already_off_client_resolution_failure_is_honest_error(home: Path, monkeypatch, capsys) -> None:
    """The already-off branch's client resolution failure: an honest error
    (rc 1, no traceback) — the base 'already off' line still reports the
    converged state."""
    set_disabled()

    def _explode_resolve(args: object) -> DaemonClient:
        del args
        raise RuntimeError("cannot resolve base url")

    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", _explode_resolve)
    assert main(["off"]) == 1
    out = capsys.readouterr()
    assert "already off" in out.out
    assert "error:" in out.err
    assert is_disabled()


# ---------------------------------------------------------------- probe timeouts


def test_probe_client_caps_timeout_at_one_second() -> None:
    """Liveness probes must not ride the 30s client timeout: a half-dead
    daemon (accepts but never answers) would otherwise park an off poll for
    minutes instead of the 15s wall-clock budget."""
    from mnemoseed_local.cli import _probe_client

    client = DaemonClient(base_url="http://127.0.0.1:7788")
    assert client.timeout == 30.0  # the default the probes must not inherit
    assert _probe_client(client).timeout == 1.0


def test_off_slow_probe_completes_in_bounded_time(home: Path, monkeypatch, capsys) -> None:
    """A bound-but-slow listener (the B6 stall shape) must not park the off
    poll: probes carry a short per-call timeout, so the whole verb finishes in
    bounded wall time and the poll budget stays a wall-clock total."""
    monkeypatch.setattr(
        "mnemoseed_local.rest_client.resolve_client",
        lambda args: _SlowDaemonClient(base_url="http://127.0.0.1:7788"),
    )
    monkeypatch.setattr("mnemoseed_local.cli._OFF_POLL_TIMEOUT_S", 0.2)
    start = time.monotonic()
    assert main(["off"]) == 0
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"off took {elapsed:.1f}s with a slow /healthz"
    assert "daemon is still running" in capsys.readouterr().out
    assert (home / "daemon.off").exists()


# ---------------------------------------------------------------- on


def test_on_not_running_removes_marker_and_starts(home: Path, monkeypatch, capsys) -> None:
    set_disabled()
    (home / "config.toml").write_text('preset = "embedded"\n', encoding="utf-8")
    calls = _fake_up_runtime(monkeypatch)
    monkeypatch.setattr(
        "mnemoseed_local.cli._dream_model_check",
        lambda config: (True, "model 'qwen3.5:9b' present"),
    )
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _UnreachableDaemonClient())
    assert main(["on"]) == 0
    assert not is_disabled(), "on must remove the disabled marker"
    assert calls == {"build_stores": 1, "run_server": 1}, "on must run the up start path"


def test_on_already_running_does_not_restart(home: Path, monkeypatch, capsys) -> None:
    set_disabled()
    calls: dict[str, int] = {"run_server": 0}

    def _explode_run_server(host: str, port: int) -> int:
        del host, port
        calls["run_server"] += 1
        raise AssertionError("an already-running daemon must not be restarted")

    monkeypatch.setattr("mnemoseed_local.daemon.runner.run_server", _explode_run_server)
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _FakeDaemonClient())
    assert main(["on"]) == 0
    assert not is_disabled(), "on must remove the disabled marker even when already running"
    assert calls["run_server"] == 0
    assert "already on" in capsys.readouterr().out


# ---------------------------------------------------------------- up refusal


def test_up_refused_when_disabled(home: Path, monkeypatch, capsys) -> None:
    """The pinned refusal: rc 1, the exact stderr sentence, and run_server is
    never reached — the gate runs before any preflight (no config needed)."""
    set_disabled()
    calls: dict[str, int] = {"run_server": 0}

    def _explode_run_server(host: str, port: int) -> int:
        del host, port
        calls["run_server"] += 1
        raise AssertionError("run_server must not be reached while disabled")

    monkeypatch.setattr("mnemoseed_local.daemon.runner.run_server", _explode_run_server)
    assert main(["up"]) == 1
    assert calls["run_server"] == 0
    assert (
        capsys.readouterr().err.strip()
        == "error: memory service is disabled (run 'mnemoseed-local on' to re-enable)"
    )


def test_up_proceeds_when_enabled(home: Path, monkeypatch, capsys) -> None:
    """No marker: the existing up path is untouched."""
    (home / "config.toml").write_text('preset = "embedded"\n', encoding="utf-8")
    calls = _fake_up_runtime(monkeypatch)
    monkeypatch.setattr(
        "mnemoseed_local.cli._dream_model_check",
        lambda config: (True, "model 'qwen3.5:9b' present"),
    )
    assert main(["up"]) == 0
    assert calls == {"build_stores": 1, "run_server": 1}


# ---------------------------------------------------------------- parser


def test_on_off_are_flat_verbs() -> None:
    parser = build_parser()
    assert parser.parse_args(["on"]).command == "on"
    assert parser.parse_args(["off"]).command == "off"
