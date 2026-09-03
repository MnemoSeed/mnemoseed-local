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
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.turn import FlushRequest, HostId, IngestEvent, SessionEndRequest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "opencode_hook"

INGEST_FIXTURES = ["user_prompt.json", "user_prompt_agent.json", "assistant_message.json", "tool_use.json"]

#: The hook -> wire-event -> endpoint invariant table pinned inside plugin.ts
#: (the static contract test parses the comment table and compares exactly).
EXPECTED_MAPPING = {
    "user_prompt": "/ingest",
    "assistant_message": "/ingest",
    "tool_use": "/ingest",
    "provider_error": "/ingest",
    "session_end": "/session/end",
    "flush": "/flush",
    "session_recall_read": "/session/recent",
    "session_recall_pending": "/session/recall-pending",
    "memory_reinforce": "/memory/reinforce",
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


def test_agent_fixture_carries_the_canonical_field_and_legacy_fixtures_stay_valid() -> None:
    """B2.9 wire compat: the canonical `agent` body field validates, and the
    pre-attribution payloads (no canonical field) still validate with an
    explicit null — old hooks and new daemons interoperate both ways."""
    attributed = IngestEvent.model_validate(_fixture("user_prompt_agent.json"))
    assert attributed.agent == "build"
    for name in ("user_prompt.json", "assistant_message.json", "tool_use.json"):
        assert IngestEvent.model_validate(_fixture(name)).agent is None


def test_blank_agent_normalizes_to_unknown() -> None:
    """A whitespace-only host label is the honest unknown, never a stored
    blank (ProfileRef blank-identity precedent); a real label passes through."""
    base = {
        "host": "opencode",
        "event": "user_prompt",
        "session_id": "oc-sess-01",
        "profile_id": "default",
        "ts": 1755500000.0,
        "content": {"text": "x"},
    }
    for blank in ("", "   ", "\t\n"):
        event = IngestEvent.model_validate({**base, "agent": blank})
        assert event.agent is None, repr(blank)
    assert IngestEvent.model_validate({**base, "agent": "build"}).agent == "build"


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
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
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


def test_opencode_idle_flush_lifecycle_keeps_the_session_ingestable(config_path: Path) -> None:
    """End-to-end regression for the 2026-08-19 dogfood finding: drive the
    daemon exactly as the FIXED hook does — each completed reply fires
    session.idle -> /flush, session.deleted -> /session/end. The old mapping
    (idle -> /session/end) sealed the session after the first reply and every
    later turn 409-dropped silently. Assert: ingest continues after flushes,
    both roles of both turns land verbatim, and only the terminal settle seals
    the session."""
    sid = "oc-lifecycle"

    def post_ingest(event: str, ts: float, text: str) -> None:
        response = client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": event,
                "session_id": sid,
                "profile_id": "default",
                "ts": ts,
                "content": {"text": text},
            },
        )
        assert response.status_code == 202, response.text

    with TestClient(create_app()) as client:
        # turn 0 completes -> host fires session.idle -> flush (NOT settle)
        post_ingest("user_prompt", 1.0, "第一条用户消息")
        post_ingest("assistant_message", 2.0, "第一条助手回复")
        flushed = client.post("/flush", json={"session_id": sid, "profile_id": "default"})
        assert flushed.status_code == 200, flushed.text
        assert flushed.json()["closed_turns"] == 1
        # the flush drained the turn: still mid-session, already recallable
        mid = client.post("/session/recent", json={"profile_id": "default", "sessions": 1})
        mid_texts = [c["text"] for c in mid.json()["sessions"][0]["chunks"]]
        assert any("第一条用户消息" in t and "第一条助手回复" in t for t in mid_texts)
        # the session must STILL accept ingest (old mapping answered 409 here)
        post_ingest("user_prompt", 3.0, "第二条用户消息")
        post_ingest("assistant_message", 4.0, "第二条助手回复")
        flushed2 = client.post("/flush", json={"session_id": sid, "profile_id": "default"})
        assert flushed2.json()["closed_turns"] == 1
        # session.deleted is the terminal settle
        settled = client.post("/session/end", json={"session_id": sid, "profile_id": "default", "ts": 5.0})
        assert settled.status_code == 200, settled.text
        assert settled.json()["turns"] == 2
        # the re-anchoring surface carries BOTH roles of BOTH turns
        body = client.post(
            "/session/recent", json={"profile_id": "default", "sessions": 1, "per_session": 10}
        )
        texts = [c["text"] for c in body.json()["sessions"][0]["chunks"]]
        assert any("第一条用户消息" in t and "第一条助手回复" in t for t in texts)
        assert any("第二条用户消息" in t and "第二条助手回复" in t for t in texts)
        # settled really is terminal: late arrivals are rejected, not lost silently
        late = client.post(
            "/ingest",
            json={
                "host": "opencode",
                "event": "user_prompt",
                "session_id": sid,
                "profile_id": "default",
                "ts": 6.0,
                "content": {"text": "迟到消息"},
            },
        )
        assert late.status_code == 409


# ---------------------------------------------------------------- packaging


def test_plugin_ts_is_shipped_as_package_data() -> None:
    plugin = resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts")
    assert plugin.is_file()


# ---------------------------------------------------------------- static contract on plugin.ts


def test_plugin_registers_the_required_hooks() -> None:
    source = _plugin_source()
    assert '"config": async' in source
    assert '"chat.message": async' in source
    assert '"chat.system.transform": async' in source
    assert '"tool.execute.after": async' in source
    assert '"experimental.session.compacting": async' in source
    assert re.search(r"(?m)^\s*event: async", source)


def test_plugin_injects_the_mnemoseed_mcp_registration_through_the_config_hook() -> None:
    """B2.6 host-plugin bundling: the config hook registers the daemon's MCP
    server into the loading host's config (per-host isolation — cfg belongs
    to the host whose plugin dir loaded this file). Create-if-absent at BOTH
    levels — cfg.mcp ??= {} (the map) and mcp["mnemoseed"] ??= {..} (the
    entry) — so a user's existing manual "mnemoseed" registration (README
    §MCP gateway) is never overwritten; the entry shape matches the A3 README
    sample plus an explicit enabled flag."""
    source = _plugin_source()
    assert "cfg.mcp ??=" in source, "the mcp map must be created when absent"
    assert 'mcp["mnemoseed"] ??=' in source, "an existing manual registration must win untouched"
    for token in ('"mnemoseed"', '"mnemoseed-local"', '"mcp"', '"type": "local"', '"enabled": true'):
        assert token in source, token


def test_plugin_short_circuits_the_whole_bundle_on_the_options_tuple_switch() -> None:
    """B2.6 single switch: the ["spec", {enabled:false}] plugin-array tuple
    short-circuits the WHOLE bundle — the entry returns {} so neither the
    config hook nor any hook is registered (research doc §7: the options
    tuple is the only official per-plugin channel; probe round 2 confirmed
    the options object reaches the plugin's second argument)."""
    source = _plugin_source()
    assert re.search(r"export default async function MnemoSeedLocalPlugin\([^)]*options", source, re.S), (
        "the plugin entry must accept the tuple's options argument"
    )
    assert "=== false" in source, "only an explicit enabled:false disables"
    assert "return {}" in source, "the disabled bundle registers nothing"


def test_plugin_mentions_all_daemon_endpoints_and_host_id() -> None:
    source = _plugin_source()
    for endpoint in (
        '"/ingest"',
        '"/session/end"',
        '"/flush"',
        '"/session/recent"',
        '"/session/recall-pending"',
        '"/memory/reinforce"',
    ):
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


def test_plugin_maps_idle_to_flush_and_only_deleted_settles() -> None:
    """Live dogfood finding (2026-08-19): opencode fires ``session.idle``
    after EVERY completed assistant reply (idle = agent went quiet, NOT
    conversation over). Mapping idle -> /session/end settled the session at
    the first reply; every later /ingest answered HTTP 409
    (SessionSettledError) and the fire-and-forget hook swallowed it into
    console.debug — sessions silently truncated to turn 0 and assistant-turn
    capture became unverifiable (verbatim red line breached). Corrected
    lifecycle: idle/error -> /flush (close + drain the in-flight turn, the
    session stays ingestable); only session.deleted -> /session/end. Pin the
    mapping as CODE so a regression fails the Python gate."""
    source = _plugin_source()
    idle_block = re.search(r'case "session\.idle":.*?break', source, re.S)
    assert idle_block is not None
    assert 'case "session.error":' in idle_block.group(0)
    assert "flushSession(" in idle_block.group(0), "idle/error must flush, not settle"
    assert "settle(" not in idle_block.group(0), "idle is NOT session end"
    deleted_block = re.search(r'case "session\.deleted":.*?break', source, re.S)
    assert deleted_block is not None
    assert "settle(" in deleted_block.group(0), "deleted is the terminal settle"


def test_plugin_pins_settle_once_dedup_and_assistant_pending_sweep() -> None:
    """Settle-once keeps a noisy host from spamming /session/end. Assistant
    capture reliability (senior QA review 2026-08-19, findings 1+2): the old
    rollback-on-failure retried ONLY if the host happened to re-fire
    message.updated for that message — the final reply of a session had no
    retry point at all, and a settle racing an in-flight fetch silently
    409-dropped the last assistant turn. The contract is now a PENDING SET
    with a deterministic sweep: a failed or textless fetch parks the
    messageID per session; `session.idle`/`session.error`/`session.deleted`
    ENQUEUE the sweep and the drain through the per-session FIFO chain (the
    same chain that keeps crash replay strictly before live posts — ordering
    is a CHAIN property, as deterministic as the old inline await, and it
    cannot interleave with content posts), so the last reply always gets a
    final retry and settle can never overtake an outstanding fetch. Pin the
    set, the enqueued sweep sites, and the settle-once dedup."""
    source = _plugin_source()
    assert "pendingAssistant" in source, "failed/textless fetches must park, not vanish"
    assert "sweepPendingAssistant(" in source, "the deterministic retry sweep must exist"
    assert "enqueueForSession(" in source, "turn content is FIFO-chained per session"
    idle_block = re.search(r'case "session\.idle":.*?break', source, re.S)
    assert idle_block is not None and "sweepPendingAssistant(" in idle_block.group(0)
    deleted_block = re.search(r'case "session\.deleted":.*?break', source, re.S)
    assert deleted_block is not None and "sweepPendingAssistant(" in deleted_block.group(0), (
        "settle must be ordered after the sweep — via the chain, not a sleep"
    )
    assert "seen(settledSessions," in source
    assert "DEDUP_CAP = 1000" in source


def test_plugin_fetch_has_a_timeout_so_a_hung_sdk_call_cannot_park_forever() -> None:
    """Senior QA review 2026-08-19, finding 1 variant 4: the SDK parts fetch
    had NO timeout (only daemon POSTs got AbortSignal.timeout) — a
    never-resolving client.session.messages() lost the assistant turn AND
    leaked the promise. The fetch now races a bounded timeout; the loser
    parks in the pending set (retried at the next sweep)."""
    source = _plugin_source()
    assert "FETCH_TIMEOUT_MS" in source
    assert re.search(r"Promise\.race\(", source), "SDK fetch must be timeout-raced"


def test_plugin_fetches_assistant_parts_via_session_messages_plural() -> None:
    """Live dogfood findings (2026-08-19, PRD-B2.1 baseline fix 3): the
    original failure AND its first 'fix' were the SAME JS method-unbinding
    TypeError (``Cannot read properties of undefined (reading '_client')``)
    — the hey-api gen client body is ``(options.client ?? this._client)``
    and ``const list = client?.session?.messages`` strips the receiver. The
    console.debug-only failure reporting misdiagnosed it as 'singular
    endpoint missing' (the singular ``session.message`` endpoint DOES exist
    in SDK 1.18.18 with dual path params; the hook just must not use it).
    Pin: the plural call on its RECEIVER, NO singular form, NO extraction
    alias of any shape, the request's path-param shape, and the info.id
    lookup — so SDK-contract AND binding-form drift fail the Python gate
    instead of the memory store."""
    source = _plugin_source()
    assert re.search(r"\.session\?\.messages\b", source), "must guard client.session.messages (plural)"
    assert not re.search(r"\.session\??\.[\"']?message\b(?!s)", source), (
        "no singular session.message access in any form (optional chain or bracket)"
    )
    assert re.search(r"client\.session\.messages\(\{", source), (
        "the call must run on its receiver — extracting the method loses `this`"
    )
    assert not re.search(r"(?:const|let|var)\s+[\w{}\s,]*=\s*client\?\.session\?\.messages\b", source), (
        "method extraction unbinds `this` (TypeError: reading '_client')"
    )
    assert "path: { id: sessionID }" in source, "pin the list call's path-param shape"
    assert re.search(r"info\??\.id === messageID", source), "must look the message up by info.id"


def test_plugin_pins_runtime_host_id_in_code() -> None:
    """`"opencode"` also appears in the header comment, which must not be able
    to satisfy the pin: the runtime constant line is what the daemon sees."""
    source = _plugin_source()
    assert re.search(r'(?m)^const HOST_ID = "opencode"$', source)


def test_plugin_endpoint_call_sites_have_the_pinned_arities() -> None:
    """Each daemon endpoint has exactly the call sites the mapping table
    promises: /session/end single-site (session.deleted), /flush two
    (idle/error + pre-compact), /ingest three (user prompt, assistant message
    — shared by the completion path and the pending-sweep retry via
    postAssistantIngest — and tool use), /memory/reinforce one (the T3
    consumption guard only). The recall READ path (/session/recent) is an
    awaited fetch inside the transform handler, NOT a post() call site.
    Multi-line formatting must not matter: count by regex, not literal."""
    source = _plugin_source()
    assert len(re.findall(r'post\(\s*"/session/end"', source)) == 1
    assert len(re.findall(r'post\(\s*"/flush"', source)) == 2
    assert len(re.findall(r'post\(\s*"/ingest"', source)) == 3
    assert len(re.findall(r'post\(\s*"/memory/reinforce"', source)) == 1


def test_plugin_lifts_host_agent_into_the_canonical_ingest_body() -> None:
    """B2.9 hook slice: hookInput.agent rides the canonical user-prompt ingest
    body as `agent` (the segmenter's turn-attribution anchor); raw.agent stays
    for one transition generation so an older daemon still sees the label. The
    crash-replay lane lifts the same attribution from the host history's
    per-message agent (UserMessage.agent in the SDK types)."""
    source = _plugin_source()
    assert "if (agent) body.agent = agent" in source
    assert "function agentOf(info: any)" in source, "one shared per-message agent extractor"
    assert len(re.findall(r"if \(agent\) raw\.agent = agent", source)) == 2, (
        "live AND crash-replay lanes both keep the raw.agent transition generation"
    )
    assert re.search(r"postUserIngest\(\s*sessionID,\s*text,\s*stamp,\s*raw,\s*agent\s*\)", source), (
        "the chat.message lane must pass the canonical agent through"
    )
    assert re.search(r"postUserIngest\(sessionID, text, createdS, raw, agent\)", source), (
        "the crash-replay lane must attribute replayed prompts from the host's per-message agent"
    )


def test_plugin_pins_the_t1_t3_recall_injection_and_consumption_guard() -> None:
    """PRD-B2.1 T1/T3 mechanics: the attempt-once session gate, the
    consumption-fingerprint registry (needle -> chunk ids) and the per-session
    cited set; the hard 4000-char injection budget; the needle builder; the
    consumption hook; and both fence literals. The single /memory/reinforce
    call site is a pure usage event: it must never touch the watermark (it is
    not content) nor re-arm recovery."""
    source = _plugin_source()
    for token in (
        "injectedSessions",
        "injectedRegistry",
        "citedChunks",
        "MAX_INJECT_CHARS = 4000",
    ):
        assert token in source, token
    assert "needlesOf(" in source
    assert "noteConsumption(" in source
    assert "<mnemoseed-memory-recall>" in source
    assert "</mnemoseed-memory-recall>" in source
    # QA re-review NIT-1: the daemon's ReinforceRequest caps chunk_ids at
    # max_length=64; the hook must split hits into <=64-id batches so an
    # overflow can never 422 the whole cited batch after marking it cited.
    assert "REINFORCE_BATCH_SIZE = 64" in source, "reinforce hits must batch to the daemon's 64-id cap"
    reinforce_site = source.index('post("/memory/reinforce"')
    window = source[reinforce_site : reinforce_site + 300]
    assert "noteWatermark" not in window, "reinforce is not content — no watermark ack"
    assert "scheduleRecovery" not in window, "reinforce failure must not re-arm reconciliation"
    # IMPORTANT-1 (QA): the transform is the only handler the host AWAITS on
    # the model-call path — it must fail open via try/catch like its siblings
    # (a fault must never reject the awaited call nor mutate the system array).
    transform_block = re.search(r"async function onChatSystemTransform.*?\n}", source, re.S)
    assert transform_block is not None, "onChatSystemTransform handler block not found"
    assert "try {" in transform_block.group(0), "the awaited transform handler must fail open"
    assert "catch" in transform_block.group(0), "the awaited transform handler must fail open"


def test_plugin_pins_the_t2_mid_session_recall_pull() -> None:
    """PRD-B2.1 T2 (D8) hook gates: the pending-recall pull is armed by an
    ACKED user ingest (per-session armed/acked flags), runs as an awaited
    fetch with a bounded timeout in the transform, injects only a non-empty
    selection, and never leaks into the post() lanes — the wire table gains
    the session_recall_pending row and the invariant comment documents the
    bounded awaited pull next to the once-per-session tails read. T1 chunk
    ids ride along as seen_chunk_ids so the daemon never re-serves them."""
    source = _plugin_source()
    for token in (
        "pendingPull",
        "t1InjectedChunkIds",
        "RECALL_PULL_TIMEOUT_MS = 300",
        "RECALL_PULL_MAX_CHARS = 1200",
        "pullPendingRecall(",
    ):
        assert token in source, token
    # the pull is an awaited FETCH, not a post() call site (arity invariant)
    assert len(re.findall(r'post\(\s*"/session/recall-pending"', source)) == 0
    # the transform runs the T2 branch AFTER the T1 injection site — the T1
    # attempt gate must never early-return past the pull (T1 and T2 are
    # independent injections)
    t1_site = source.index("buildRecallInjection(")
    t2_site = source.index("pullPendingRecall(")
    assert t2_site > t1_site, "the T2 pull must live after the T1 injection site in the transform"
    # the invariant comment documents the bounded pull as the second awaited
    # network call (after the once-per-session tails read)
    assert "bounded pending-recall pull" in source
    assert "session_recall_pending" in source


def test_plugin_pins_the_b24_time_awareness_hook_slice() -> None:
    """PRD-B2.4 T3 (hook slice): the T1 session-recent read carries
    self_session_id on the SAME single awaited POST; the injected block may
    render the self-anchor line and group-header started= attributes gated on
    window/window_truncated. The read stays an awaited fetch — no new post()
    call site, no third awaited network call (the transform invariant comment
    still names exactly the two bounded reads)."""
    source = _plugin_source()
    assert "self_session_id: sessionID" in source
    assert '<session-self id="' in source
    assert ' started="' in source
    assert "window_truncated" in source
    assert len(re.findall(r'post\(\s*"/session/recent"', source)) == 0
    assert "The ONLY awaited network calls in the transform handler are" in source


def test_plugin_escapes_attributes_in_injected_lines() -> None:
    """NIT-4: session ids and started values are interpolated into HTML-ish
    attributes inside the injection fence — defense-in-depth requires the
    attribute-dangerous characters be escaped. ONE shared helper must back BOTH
    the new session-self line and the existing group header, mapping the four
    attribute-safe escapes, and must be applied at every interpolated attribute
    (id, ended, started)."""
    source = _plugin_source()
    assert re.search(r"function escapeAttr\(value: string\): string", source), (
        "a single shared attribute-escape helper must exist"
    )
    for literal in ('"&amp;"', '"&lt;"', '"&gt;"', '"&quot;"'):
        assert literal in source, f"escape helper must map {literal}"
    self_line = re.search(r"function sessionSelfLine.*?\n}", source, re.S)
    assert self_line is not None and "escapeAttr(" in self_line.group(0), (
        "the session-self id/started attributes must be escaped"
    )
    started = re.search(r"function groupStarted.*?\n}", source, re.S)
    assert started is not None and "escapeAttr(" in started.group(0), (
        "the group started= attribute must be escaped"
    )
    header = re.search(r"const header = `<session-tail[^`]*`", source)
    assert header is not None and "escapeAttr(" in header.group(0), (
        "the group header id/ended interpolation must be escaped"
    )


def test_plugin_stays_fire_and_forget_and_debug_only() -> None:
    source = _plugin_source()
    # 2s timeout via AbortSignal; env knobs; failures go to console.debug only.
    assert "AbortSignal.timeout(" in source
    assert "MNEMOSEED_LOCAL_BASEURL" in source
    assert "MNEMOSEED_LOCAL_PROFILE_ID" in source
    assert "console.debug(" in source
    assert "console.log(" not in source


def test_plugin_ts_parses_clean_under_esbuild(tmp_path: Path) -> None:
    """Senior QA review 2026-08-19, finding 12a: every gate (pytest/ruff/mypy)
    is Python-only — the shipped hook is pinned by REGEX, so a syntax-broken
    plugin.ts (unbalanced brace, bad template quote) would sail green through
    CI and then kill capture SILENTLY at host load (fire-and-forget: the host
    session shows nothing). That is exactly the silent-failure class behind
    all three 2026-08-19 dogfoods. esbuild gives a real parse gate with zero
    repo dependencies (npx-cached); type-checking is deliberately out of
    scope (the file is dependency-free TS by design)."""
    if shutil.which("npx") is None:
        pytest.skip("npx unavailable on this machine")
    plugin = resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts")
    out = tmp_path / "plugin.js"
    result = subprocess.run(
        f'npx --yes esbuild "{plugin}" --outfile="{out}" --log-level=error',
        shell=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"plugin.ts must parse clean: {result.stderr}"
    assert out.is_file()


def test_plugin_has_an_optin_observability_seam() -> None:
    """Senior QA review 2026-08-19, finding 12b + finding 2: the
    console.debug-only failure sink made three real defects invisible, and
    fire-and-forget POSTs never even LOOKED at the daemon's status (a 409
    'rejected' is indistinguishable from 'accepted' on the wire). The hook
    now (a) inspects every POST's response status and reports non-2xx, and
    (b) exposes an opt-in debug lane (env ``MNEMOSEED_LOCAL_DEBUG``) that
    escalates failures to console.error + a JSONL debug sink, so the next
    class of runtime failure is visible WITHOUT ad-hoc probe surgery."""
    source = _plugin_source()
    assert "MNEMOSEED_LOCAL_DEBUG" in source, "opt-in env flag must exist"
    assert "debugLog(" in source, "the debug sink lane must exist"
    assert re.search(r"!?\bresponse\.ok\b", source) or re.search(r"!?\br\.ok\b", source), (
        "POST must inspect the daemon's status — a swallowed 409 is how the settle-sealing bug hid"
    )


def test_plugin_caps_tool_output_payloads() -> None:
    """Senior QA review 2026-08-19, finding 5 (hook half): tool_use payloads
    were POSTed untruncated into the verbatim provenance lane — one runaway
    build log would bloat RAM buffers (in-memory until drain) and the lance
    store. Cap with an explicit, greppable truncation marker."""
    source = _plugin_source()
    assert "MAX_TOOL_OUTPUT_CHARS" in source
    assert "[... truncated" in source


# ---------------------------------------------------------------- B2.2 crash durability


def test_plugin_pins_the_crash_replay_mechanism() -> None:
    """PRD-B2.2 T1/T2 (KISS single mechanism): on host startup, each session
    is reconciled LAZILY at its first event in this hook process — the host's
    OWN persisted history (client.session.messages) is replayed for the tail
    missing since the watermark, with a 30s overlap the daemon's near-dup
    absorb eats. Pin the watermark file, the once-per-session lazy trigger,
    the overlap constant, the receiver-bound list call, the per-idle persist
    cadence, and the escalate-on-failure degradation (watermark trouble must
    never touch the main lane)."""
    source = _plugin_source()
    assert "hook-watermarks.json" in source, "T1 watermark file"
    assert "REPLAY_OVERLAP_MS = 30000" in source, "30s overlap margin (T2)"
    assert "reconcileSession(" in source and "reconciledSessions" in source, (
        "lazy, once-per-session-per-process reconciliation (T2)"
    )
    assert re.search(r"case \"session\.idle\":.*?reconcile", source) or re.search(
        r"chat\.message.*?reconcile", source, re.S
    ), "reconciliation hooks into the lazy first-event path"
    assert re.search(r"client\.session\.messages\(\{", source), (
        "host history is read via the bound list call (no new API surface)"
    )


def test_plugin_replay_keeps_the_engineering_red_lines() -> None:
    """PRD-B2.2 T4: (a) TOKEN red-line — the replay path must never construct
    or call any LLM client (the whole plugin talks to exactly two parties:
    the daemon REST and the host's own SDK); (b) hot-path zero-cost — the
    existing chat.message/ingest lane gains no awaited I/O (reconcile is
    fire-and-forget like every other post); (c) reversibility — the only new
    filesystem artifact is the single watermark file."""
    source = _plugin_source()
    assert ".chat(" not in source and "LLM" not in source, (
        "no LLM client anywhere in the hook — replay is byte-reshuffling only"
    )
    chat_block = re.search(r"async function onChatMessage.*?\n  \}", source, re.S)
    assert chat_block is not None and "await reconcileSession" not in chat_block.group(0), (
        "reconcile must stay fire-and-forget — the hot path awaits nothing"
    )
    assert source.count("hook-watermarks.json") >= 1 and "journal" not in source.lower(), (
        "single listed artifact; NO WAL/journal machinery (PRD boundary)"
    )
