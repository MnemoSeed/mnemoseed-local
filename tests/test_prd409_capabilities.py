"""Additive /healthz.capabilities and the CLI capability summary."""

from __future__ import annotations

import argparse
from pathlib import Path

import auth_support
import pytest

from mnemoseed_local.dream.readiness import NO_ROUTE_DETAIL
from mnemoseed_local.schema.turn import HostId


def _write_config(home: Path, *, with_dream_route: bool) -> None:
    route = '[dream.llm.dream]\ndriver = "stub"\nmodel = "stub"\n' if with_dream_route else ""
    (home / "config.toml").write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream]\n"
        "auto_trigger = false\n" + route,
        encoding="utf-8",
    )


def test_healthz_capabilities_report_dream_reason_without_ollama(tmp_path: Path) -> None:
    _write_config(tmp_path, with_dream_route=False)
    with auth_support.boot(tmp_path) as client:
        body = client.get("/healthz").json()

    caps = body["capabilities"]
    assert caps["capture"]["ready"] is True
    assert caps["recall"]["ready"] is True
    assert caps["storage"]["ok"] is True
    assert caps["dream"]["ready"] is False
    assert NO_ROUTE_DETAIL in caps["dream"]["detail"]
    assert body["dream_route"]["ready"] is False
    assert body["status"] == "ok"


def test_healthz_capabilities_are_additive(tmp_path: Path) -> None:
    _write_config(tmp_path, with_dream_route=True)
    with auth_support.boot(tmp_path) as client:
        body = client.get("/healthz").json()

    assert {"status", "uptime_ms", "preset", "stores", "migrations", "gate", "dream_route"} <= set(body)
    assert "capabilities" in body


def test_capture_and_recall_work_without_any_llm_route(tmp_path: Path) -> None:
    _write_config(tmp_path, with_dream_route=False)
    with auth_support.boot(tmp_path) as client:
        response = client.post(
            "/ingest",
            json={
                "host": HostId.OPENCODE.value,
                "event": "user_prompt",
                "session_id": "sess-no-ollama",
                "profile_id": "default",
                "ts": 1_700_000_000.0,
                "content": {"text": "a durable note that needs no model"},
            },
        )
        assert response.status_code == 202, response.text
        client.post("/session/end", json={"profile_id": "default", "session_id": "sess-no-ollama"})
        recall = client.post(
            "/memory/recall",
            json={"profile_id": "default", "query": "durable note", "top_k": 5},
        )
        assert recall.status_code == 200, recall.text
        body = client.get("/healthz").json()
        assert body["capabilities"]["dream"]["ready"] is False


def test_cli_status_shows_capability_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemoseed_local import cli

    class _Client:
        profile_id = "default"

        def get(self, path: str) -> dict:
            if path == "/healthz":
                return {
                    "preset": "embedded",
                    "gate": {"ok": True},
                    "capabilities": {
                        "capture": {"ready": True},
                        "recall": {"ready": True},
                        "dream": {"ready": False, "detail": NO_ROUTE_DETAIL},
                    },
                }
            return {"config": {"dream": {"auto_trigger": False, "llm": {}}}}

    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _Client())
    monkeypatch.setattr(cli, "_refinement_state", lambda: None)
    out = _capture_stdout(cli.cmd_status, argparse.Namespace(json=False, baseurl=None))
    assert "dream=unavailable" in out
    assert NO_ROUTE_DETAIL in out


def _capture_stdout(func, *args) -> str:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        func(*args)
    return buffer.getvalue()
