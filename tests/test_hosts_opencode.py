"""A3 T2: OpenCode host hook wire contract (design/01 §4.5).

Golden fixtures under ``tests/fixtures/opencode_hook/`` pin the exact JSON
bodies the shipped ``plugin.ts`` intends to POST: each must validate against
the daemon's pydantic wire models AND round-trip through the real daemon
surface (wire compatibility, not just schema). A static contract test pins
the hook registrations and the hook -> endpoint mapping table inside
``plugin.ts``, so TS-side drift fails the Python gate.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.turn import FlushRequest, HostId, IngestEvent, SessionEndRequest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "opencode_hook"

INGEST_FIXTURES = ["user_prompt.json", "assistant_message.json", "tool_use.json"]

#: The hook -> wire-event -> endpoint invariant table pinned inside plugin.ts
#: (the static contract test parses the comment table and compares exactly).
EXPECTED_MAPPING = {
    "user_prompt": "/ingest",
    "assistant_message": "/ingest",
    "tool_use": "/ingest",
    "session_end": "/session/end",
    "flush": "/flush",
}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _plugin_source() -> str:
    return resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts").read_text(encoding="utf-8")


# ---------------------------------------------------------------- golden fixtures


@pytest.mark.parametrize("name", INGEST_FIXTURES + ["session_end.json", "flush.json"])
def test_fixtures_are_single_line_json(name: str) -> None:
    """The plugin serializes with JSON.stringify — one line, no pretty print."""
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    assert len(text.splitlines()) == 1


@pytest.mark.parametrize("name", INGEST_FIXTURES)
def test_ingest_fixtures_validate(name: str) -> None:
    event = IngestEvent.model_validate(_fixture(name))
    assert event.host is HostId.OPENCODE


def test_session_end_fixture_validates() -> None:
    SessionEndRequest.model_validate(_fixture("session_end.json"))


def test_flush_fixture_validates() -> None:
    FlushRequest.model_validate(_fixture("flush.json"))


# ---------------------------------------------------------------- golden <-> daemon e2e


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Embedded single-process daemon config (same harness as test_daemon)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


@pytest.mark.parametrize("name", INGEST_FIXTURES)
def test_ingest_fixtures_accepted_by_daemon(config_path: Path, name: str) -> None:
    with TestClient(create_app()) as client:
        response = client.post("/ingest", json=_fixture(name))
        assert response.status_code == 202, response.text


def test_session_end_fixture_accepted_by_daemon(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        opened = client.post("/ingest", json=_fixture("user_prompt.json"))
        assert opened.status_code == 202, opened.text
        settled = client.post("/session/end", json=_fixture("session_end.json"))
        assert settled.status_code == 200, settled.text
        assert settled.json()["status"] == "settled"


def test_flush_fixture_accepted_by_daemon(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        opened = client.post("/ingest", json=_fixture("user_prompt.json"))
        assert opened.status_code == 202, opened.text
        flushed = client.post("/flush", json=_fixture("flush.json"))
        assert flushed.status_code == 200, flushed.text
        assert flushed.json()["status"] == "flushed"


# ---------------------------------------------------------------- packaging


def test_plugin_ts_is_shipped_as_package_data() -> None:
    plugin = resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts")
    assert plugin.is_file()


# ---------------------------------------------------------------- static contract on plugin.ts


def test_plugin_registers_the_required_hooks() -> None:
    source = _plugin_source()
    assert '"chat.message": async' in source
    assert '"tool.execute.after": async' in source
    assert '"experimental.session.compacting": async' in source
    assert re.search(r"(?m)^\s*event: async", source)


def test_plugin_mentions_all_daemon_endpoints_and_host_id() -> None:
    source = _plugin_source()
    for endpoint in ('"/ingest"', '"/session/end"', '"/flush"'):
        assert endpoint in source, endpoint
    # HostId pinned to the daemon-side enum value.
    assert '"opencode"' in source
    assert HostId.OPENCODE.value == "opencode"


def test_plugin_wires_the_pinned_hook_endpoint_mapping() -> None:
    """The mapping table in the plugin header is the single doc truth; parse
    it and compare the full invariant list (event -> endpoint) exactly."""
    source = _plugin_source()
    found = dict(re.findall(r"->\s*([a-z_]+)\s*->\s*POST\s+(/\S+)", source))
    assert found == EXPECTED_MAPPING


def test_plugin_pins_the_settle_trigger_cases_in_code() -> None:
    """The three session-lifecycle triggers are the primary session-drain path
    (design §4.5: only pushing /ingest never drains). Pin them as CODE — the
    header comment's `session.idle|error|deleted` shorthand must not count."""
    source = _plugin_source()
    for trigger in ('case "session.idle":', 'case "session.error":', 'case "session.deleted":'):
        assert trigger in source, trigger


def test_plugin_pins_settle_once_dedup_and_assistant_retry_rollback() -> None:
    """Settle-once keeps a noisy host from spamming /session/end; the
    rollback keeps a transient parts-fetch failure from dropping an assistant
    turn forever (宁可重复不丢 is the cross-channel backstop, not the hook's
    own retry). Pin both guards as code."""
    source = _plugin_source()
    assert "seen(settledSessions," in source
    assert "seen(sentAssistant," in source
    assert "unseen(sentAssistant," in source
    assert "DEDUP_CAP = 1000" in source


def test_plugin_fetches_assistant_parts_via_session_messages_plural() -> None:
    """Live dogfood finding (2026-08-19): the hook called a SINGULAR
    ``session.message`` — a method this opencode SDK never exposed — so every
    assistant-turn parts fetch short-circuited (``typeof query !==
    "function"``) and assistant capture died SILENTLY into console.debug
    (verbatim red line breached: user turns only in the store). The shipped
    SDK (``@opencode-ai/sdk`` gen client) exposes only the LIST endpoint
    ``session.messages({ path: { id } }) -> [{ info, parts }]``. Pin the
    plural call, the absent singular name, and the info.id lookup, so
    SDK-contract drift fails the Python gate instead of the memory store."""
    source = _plugin_source()
    assert re.search(r"\.session\?\.messages\b", source), "must call client.session.messages (plural)"
    assert not re.search(r"\.session\?\.message\b(?!s)", source), (
        "singular session.message does not exist in the SDK"
    )
    assert re.search(r"info\??\.id === messageID", source), "must look the message up by info.id"


def test_plugin_pins_runtime_host_id_in_code() -> None:
    """`"opencode"` also appears in the header comment, which must not be able
    to satisfy the pin: the runtime constant line is what the daemon sees."""
    source = _plugin_source()
    assert re.search(r'(?m)^const HOST_ID = "opencode"$', source)


def test_plugin_endpoint_call_sites_have_the_pinned_arities() -> None:
    """Each daemon endpoint has exactly the call sites the mapping table
    promises: /session/end and /flush single-site, /ingest three (user prompt,
    assistant message, tool use)."""
    source = _plugin_source()
    assert source.count('post("/session/end"') == 1
    assert source.count('post("/flush"') == 1
    assert source.count('post("/ingest"') == 3


def test_plugin_stays_fire_and_forget_and_debug_only() -> None:
    source = _plugin_source()
    # 2s timeout via AbortSignal; env knobs; failures go to console.debug only.
    assert "AbortSignal.timeout(" in source
    assert "MNEMOSEED_LOCAL_BASEURL" in source
    assert "MNEMOSEED_LOCAL_PROFILE_ID" in source
    assert "console.debug(" in source
    assert "console.log(" not in source
