"""The status verb's refinement line: one computed state, three honest values.

The computation lives in the daemon's observability module and the CLI's
status verb consumes it unchanged. Availability comes from the daemon's own
DreamRouteReadiness gate (the same bounded probe the dream scheduler uses),
injected or monkeypatched here — a test never reaches the real loopback.

States: disabled (the capture-recall posture), enabled with an unavailable
route, and ready. The capture/recall and reachability facts stay separate
from this line in the status output.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import mnemoseed_local.daemon.observability as observability
from mnemoseed_local.cli import main
from mnemoseed_local.config import RoleLLMConfig

#: The capture-recall setup posture (both keys explicit).
CAPTURE_RECALL_CONFIG = (
    'preset = "embedded"\n\n[dream]\nauto_trigger = false\n\n[capture]\nauto_recall = true\n'
)

#: The legacy auto-dream posture: refinement enabled, route must answer.
ENABLED_CONFIG = 'preset = "embedded"\n\n[dream]\nauto_trigger = true\n'


def _dream_routes(base_url: str = "http://127.0.0.1:11434") -> dict[str, RoleLLMConfig]:
    return {
        "dream": RoleLLMConfig(
            role="dream",
            driver="ollama",
            model="qwen3.5:9b",
            params={"base_url": base_url},
        )
    }


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".mnemoseed-local"
    home.mkdir(parents=True)
    # cli.py binds its own CONFIG_DIR/CONFIG_PATH at import time while
    # load_config() reads the config-module bindings, so both namespaces
    # must point at the same hermetic home.
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", home / "config.toml")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    return home


class _FakeDaemonClient:
    """Answers the two reads cmd_status makes; no daemon, no loopback."""

    profile_id = "default"

    def __init__(self, health: dict[str, Any], config: dict[str, Any]) -> None:
        self._health = health
        self._config = config

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del params
        if path == "/healthz":
            return self._health
        return {"config": self._config}


def _fake_client(auto_trigger: bool) -> _FakeDaemonClient:
    return _FakeDaemonClient(
        health={"status": "ok", "preset": "embedded", "gate": {"ok": True}},
        config={"dream": {"auto_trigger": auto_trigger, "llm": {"dream": {"driver": "ollama"}}}},
    )


# (a) disabled posture: a distinct state, derived without any route probe.


def test_disabled_posture_reports_capture_recall_mode_without_probing() -> None:
    def _forbidden_probe() -> tuple[bool, str]:
        raise AssertionError("a disabled refinement must not probe the route")

    status = observability.refinement_status(
        auto_trigger=False,
        auto_recall=True,
        routes=_dream_routes(),
        probe=_forbidden_probe,
    )
    assert status.state == "disabled"
    assert status.live_check is False
    assert status.line() == "refinement: disabled (capture-recall mode)"


def test_disabled_without_auto_recall_does_not_claim_capture_recall_mode() -> None:
    status = observability.refinement_status(
        auto_trigger=False,
        auto_recall=False,
        routes=_dream_routes(),
        probe=lambda: (True, "unused"),
    )
    assert status.state == "disabled"
    assert status.line() == "refinement: disabled (auto recall off)"


# (b) enabled + unreachable route: availability injection follows the dream
# readiness test pattern (httpx.get fails), exercising the default probe.


def test_enabled_with_unreachable_route_reports_provider_unavailable(monkeypatch) -> None:
    def _fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("httpx.get", _fail)
    status = observability.refinement_status(auto_trigger=True, auto_recall=True, routes=_dream_routes())
    assert status.state == "enabled_unavailable"
    assert status.live_check is True
    assert status.line() == (
        "refinement: enabled, provider unavailable "
        "(live check: Ollama API unavailable at http://127.0.0.1:11434: connection refused)"
    )


def test_enabled_unavailable_via_injected_probe() -> None:
    status = observability.refinement_status(
        auto_trigger=True,
        auto_recall=True,
        routes={},
        probe=lambda: (False, "route down"),
    )
    assert status.state == "enabled_unavailable"
    assert status.line() == "refinement: enabled, provider unavailable (live check: route down)"


# (c) enabled + reachable route: ready, with the live-check detail carried.


def test_enabled_with_reachable_route_reports_ready(monkeypatch) -> None:
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"models": [{"name": "qwen3.5:9b"}]},
    )
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: response)
    status = observability.refinement_status(auto_trigger=True, auto_recall=True, routes=_dream_routes())
    assert status.state == "ready"
    assert status.live_check is True
    assert status.line() == (
        "refinement: ready (live check: Ollama API ready; model 'qwen3.5:9b' is present)"
    )


def test_non_ollama_route_is_ready_without_a_network_call(monkeypatch) -> None:
    def _fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a non-Ollama route is not gated by a network probe")

    monkeypatch.setattr("httpx.get", _fail)
    routes = {
        "dream": RoleLLMConfig(
            role="dream",
            driver="openai_compatible",
            model="gpt-4o",
            params={"base_url": "https://api.example.invalid"},
        )
    }
    status = observability.refinement_status(auto_trigger=True, auto_recall=True, routes=routes)
    assert status.state == "ready"
    assert status.line() == "refinement: ready (live check: not an Ollama route)"


def test_timeout_reaches_the_route_probe(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _FakeReadiness:
        def __init__(self, routes: Any, *, timeout_s: float) -> None:
            seen["routes"] = routes
            seen["timeout_s"] = timeout_s

        def refresh(self) -> bool:
            return True

        def status(self) -> dict[str, Any]:
            return {"detail": "fake gate"}

    monkeypatch.setattr(observability, "DreamRouteReadiness", _FakeReadiness)
    routes = _dream_routes()
    status = observability.refinement_status(
        auto_trigger=True, auto_recall=True, routes=routes, timeout_s=0.5
    )
    assert seen["routes"] == routes
    assert seen["timeout_s"] == 0.5
    assert status.state == "ready"
    assert status.line() == "refinement: ready (live check: fake gate)"


def test_three_states_are_distinct() -> None:
    states = {
        observability.REFINEMENT_DISABLED,
        observability.REFINEMENT_UNAVAILABLE,
        observability.REFINEMENT_READY,
    }
    assert len(states) == 3


def test_line_refuses_an_unknown_state() -> None:
    with pytest.raises(ValueError, match="unknown refinement state"):
        observability.RefinementStatus(state="invented", detail="", live_check=False).line()


# (d) the CLI status verb: existing lines stay, the refinement line is added.


def test_cli_status_keeps_existing_lines_and_adds_refinement(cli_home: Path, monkeypatch, capsys) -> None:
    (cli_home / "config.toml").write_text(CAPTURE_RECALL_CONFIG, encoding="utf-8")
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _fake_client(False))
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "daemon: mnemoseed-local" in out
    assert "dream auto_trigger: False" in out
    assert "dream llm driver:  ollama" in out
    assert "profile: default" in out
    assert "refinement: disabled (capture-recall mode)" in out
    # The refinement line is additive and comes after the existing summary.
    assert out.index("profile: default") < out.index("refinement: disabled")


def test_cli_status_json_includes_the_refinement_state(cli_home: Path, monkeypatch, capsys) -> None:
    (cli_home / "config.toml").write_text(CAPTURE_RECALL_CONFIG, encoding="utf-8")
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _fake_client(False))
    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refinement"] == {
        "state": "disabled",
        "detail": "capture-recall mode",
        "live_check": False,
    }
    assert "health" in payload and "config" in payload


def test_cli_enabled_with_unreachable_route_reports_provider_unavailable(
    cli_home: Path, monkeypatch, capsys
) -> None:
    (cli_home / "config.toml").write_text(ENABLED_CONFIG, encoding="utf-8")

    def _fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("httpx.get", _fail)
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _fake_client(True))
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "refinement: enabled, provider unavailable (live check: Ollama API unavailable" in out
    assert "dream auto_trigger: True" in out


def test_cli_status_with_a_corrupt_config_reports_unknown_not_a_traceback(
    cli_home: Path, monkeypatch, capsys
) -> None:
    (cli_home / "config.toml").write_text("not [valid toml", encoding="utf-8")
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _fake_client(False))
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "refinement: unknown (the config could not be loaded)" in out
    assert "daemon: mnemoseed-local" in out


def test_cli_status_json_with_a_corrupt_config_reports_null_refinement(
    cli_home: Path, monkeypatch, capsys
) -> None:
    (cli_home / "config.toml").write_text("not [valid toml", encoding="utf-8")
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _fake_client(False))
    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refinement"] is None
    assert "health" in payload and "config" in payload


# (e) one computed state: the CLI routes through the daemon observability
# module's function, never a private copy.


def test_cli_status_consumes_the_daemon_observability_computation(
    cli_home: Path, monkeypatch, capsys
) -> None:
    (cli_home / "config.toml").write_text(CAPTURE_RECALL_CONFIG, encoding="utf-8")
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _fake_client(False))
    seen: dict[str, Any] = {}

    def _pinned(**kwargs: Any) -> observability.RefinementStatus:
        seen.update(kwargs)
        return observability.RefinementStatus(
            state=observability.REFINEMENT_DISABLED,
            detail="capture-recall mode",
            live_check=False,
        )

    monkeypatch.setattr(observability, "refinement_status", _pinned)
    assert main(["status"]) == 0
    assert seen["auto_trigger"] is False
    assert seen["auto_recall"] is True
    assert "dream" in seen["routes"]
    assert "refinement: disabled (capture-recall mode)" in capsys.readouterr().out
