"""One-shot recall-feedback signal on the MCP surface (issue #274).

The first cut stores the signal and nothing else: no reranking, no retention
change, no calibration. Both the gateway contract and the daemon write are
tested through their public surfaces, with an isolated data home.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import auth_support
import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.mcp_gateway import instructions, server
from mnemoseed_local.rest_client import DaemonUnavailableError
from mnemoseed_local.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

OBSERVED_AT = "2026-11-18T12:34:56+00:00"


class _CaptureClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.profile_id = "default"
        self.error = error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, body))
        if self.error is not None:
            raise self.error
        return {"stored": True, "kind": "not_useful", "observed_at": OBSERVED_AT}


@pytest.fixture(autouse=True)
def _drivers() -> None:
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)


def _tool() -> dict[str, Any]:
    return next(tool for tool in server.TOOLS if tool["name"] == "recall_feedback")


def test_tool_schema_has_a_closed_kind_and_a_required_observed_at() -> None:
    tool = _tool()
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["kind", "observed_at"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["kind"]["enum"] == ["not_useful"]
    assert schema["properties"]["observed_at"]["type"] == "string"
    assert tool["description"]


def test_call_posts_the_signal_verbatim_and_returns_the_payload() -> None:
    client = _CaptureClient()
    result = server.call_tool(client, "recall_feedback", {"kind": "not_useful", "observed_at": OBSERVED_AT})
    assert result["isError"] is False
    assert client.calls == [
        (
            "/memory/recall_feedback",
            {"profile_id": "default", "kind": "not_useful", "observed_at": OBSERVED_AT},
        )
    ]
    assert json.loads(result["content"][0]["text"]) == {
        "stored": True,
        "kind": "not_useful",
        "observed_at": OBSERVED_AT,
    }


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"kind": "bogus", "observed_at": OBSERVED_AT}, "kind"),
        ({"kind": "not_useful", "observed_at": "not-a-date"}, "observed_at"),
        ({"kind": "not_useful"}, "observed_at"),
    ],
)
def test_invalid_arguments_fail_loudly_without_posting(arguments: dict[str, Any], reason: str) -> None:
    client = _CaptureClient()
    result = server.call_tool(client, "recall_feedback", arguments)
    assert result["isError"] is True
    assert reason in result["content"][0]["text"]
    assert client.calls == []


def test_daemon_failure_is_a_typed_entry_unavailable() -> None:
    client = _CaptureClient(error=DaemonUnavailableError("down"))
    result = server.call_tool(client, "recall_feedback", {"kind": "not_useful", "observed_at": OBSERVED_AT})
    assert result["isError"] is True
    assert result["entry_failure"]["kind"] == "entry_unavailable"


def test_instruction_block_names_the_verb_and_stays_inside_the_ceiling() -> None:
    text = instructions.gateway_text()
    assert "recall_feedback" in text
    assert "not_useful" in text
    assert len(text) <= instructions.MAX_CHARS
    assert "unresolved: resolution failed" in text
    assert "Entry failure" in text


def _bootable_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        '[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        '[dream.llm.dream]\ndriver = "stub"\nmodel = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", home)


def _feedback(client: TestClient, *, kind: str = "not_useful", observed_at: str = OBSERVED_AT) -> Any:
    return client.post(
        "/memory/recall_feedback",
        json={"profile_id": "default", "kind": kind, "observed_at": observed_at},
    )


def _rows(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/v1/audit", params={"action": "recall_feedback"})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_daemon_appends_the_signal_to_the_meta_audit_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _bootable_home(home, monkeypatch)
    with auth_support.boot(home) as client:
        first = _feedback(client)
        assert first.status_code == 200, first.text
        assert first.json() == {"ok": True, "kind": "not_useful", "observed_at": OBSERVED_AT}
        rows = _rows(client)
        assert len(rows) == 1
        assert rows[0]["detail"] == {
            "profile_id": "default",
            "kind": "not_useful",
            "observed_at": OBSERVED_AT,
        }
        second = _feedback(client, observed_at="2026-11-18T12:35:00+00:00")
        assert second.status_code == 200
        assert len(_rows(client)) == 2


def test_daemon_refuses_unknown_kind_and_bad_time_without_appending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _bootable_home(home, monkeypatch)
    with auth_support.boot(home) as client:
        bad_kind = _feedback(client, kind="bogus")
        bad_time = _feedback(client, observed_at="not-a-date")
        assert bad_kind.status_code == 422
        assert bad_time.status_code == 422
        assert _rows(client) == []
