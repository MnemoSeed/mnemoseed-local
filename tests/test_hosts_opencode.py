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

import auth_support
import pytest

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
    with auth_support.boot(config_path.parent) as client:
        response = client.post("/ingest", json=_fixture(name))
        assert response.status_code == 202, response.text
        # reverse assertion: the same ingest without the credential is refused
        refused = client.post(
            "/ingest",
            json=_fixture(name),
            headers={"Authorization": "Bearer not-the-credential"},
        )
        assert refused.status_code == 401


def test_session_end_fixture_accepted_by_daemon(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        opened = client.post("/ingest", json=_fixture("user_prompt.json"))
        assert opened.status_code == 202, opened.text
        settled = client.post("/session/end", json=_fixture("session_end.json"))
        assert settled.status_code == 200, settled.text
        assert settled.json()["status"] == "settled"


def test_flush_fixture_accepted_by_daemon(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
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

    with auth_support.boot(config_path.parent) as client:
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
    """V2 registration surface: every lane rides the plugin context — session
    hooks (prompt / context / compaction), the tool execute.after hook, the
    mcp transform, and one event-subscription loop for the bus events."""
    source = _plugin_source()
    assert 'ctx.session.hook("prompt"' in source
    assert 'ctx.session.hook("context"' in source
    assert 'ctx.session.hook("compaction"' in source
    assert 'ctx.tool.hook("execute.after"' in source
    assert "ctx.mcp.transform(" in source
    assert "ctx.event.subscribe(" in source
    # the V2 definition shape: default export carries id + setup
    assert 'id: "mnemoseed-local"' in source
    assert re.search(r"export default \{", source)
    assert re.search(r"(?m)^  setup\(ctx: any\)", source)


def test_plugin_injects_the_mnemoseed_mcp_registration_through_the_mcp_transform() -> None:
    """B2.6 host-plugin bundling plus the HTTP MCP transport: the mcp
    transform registers the daemon's MCP server into the loading host's config
    (per-host isolation — the editor belongs to the host whose plugin dir
    loaded this file). Create-if-absent: a user's existing manual "mnemoseed"
    registration (README §MCP gateway) is never overwritten; the one rewrite
    is the factory stdio entry, which migrates to the remote HTTP shape with
    the disabled flag carried over (a legacy enabled:false maps onto it). The
    injected entry is remote, without OAuth auto-detection, and carries an
    explicit timeout."""
    source = _plugin_source()
    assert "function installMcpEntry(editor: any)" in source, (
        "the entry install must run through the V2 mcp editor"
    )
    assert "editor.set(MCP_ENTRY_NAME" in source, "the entry install must use the editor"
    for token in ('"mnemoseed"', '"mcp"', '"type": "remote"', '"oauth": false', '"disabled": false'):
        assert token in source, token


def test_plugin_short_circuits_the_whole_bundle_on_the_options_switch() -> None:
    """B2.6 single switch: {enabled:false} via the V2 { package, options }
    entry short-circuits the WHOLE bundle — setup registers nothing (no mcp
    transform, no hooks, no event subscription) and returns a no-op cleanup."""
    source = _plugin_source()
    assert '(ctx.options as JsonRecord)["enabled"] === false' in source, (
        "the switch must read the V2 options object"
    )
    assert "=== false" in source, "only an explicit enabled:false disables"
    assert "return () => {}" in source, "the disabled bundle registers nothing"


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
    assert 'case "session.execution.failed":' in idle_block.group(0), (
        "the V2 terminal-failure event must share the flush arm"
    )
    assert "flushSession(" in idle_block.group(0), "idle/execution-failed must flush, not settle"
    assert "settle(" not in idle_block.group(0), "idle is NOT session end"
    deleted_block = re.search(r'case "session\.deleted":.*?break', source, re.S)
    assert deleted_block is not None
    assert "settle(" in deleted_block.group(0), "deleted is the terminal settle"


def test_plugin_pins_settle_once_dedup_and_event_driven_assistant_capture() -> None:
    """Settle-once keeps a noisy host from spamming /session/end. Assistant
    capture under the V2 stream is EVENT-DRIVEN: the reply text arrives as
    session.text.ended parts and posts at the step boundary
    (session.step.ended / session.step.failed) — no host fetch on the live
    lane, so the parked-fetch sweep machinery is gone. Recovery for a failed
    POST is the fingerprint rollback + re-armed reconciliation: the
    un-advanced watermark makes the session's next reconcile replay the turn
    from ctx.session.context through the same FIFO chain (ordering is a
    CHAIN property and cannot interleave with content posts). Pin the
    accumulator, the step-boundary post sites, the fingerprint dedup, and
    the settle-once dedup."""
    source = _plugin_source()
    assert "assistantTextParts" in source, "reply text accumulates from session.text.ended"
    assert "accumulateAssistantText(" in source
    assert "settleAssistantStep(" in source, "the step boundary posts the assembled reply"
    assert 'case "session.text.ended":' in source
    assert 'case "session.step.ended":' in source
    assert 'case "session.step.failed":' in source
    assert "pendingAssistant" not in source, "the v1 parked-fetch sweep is gone"
    assert "sweepPendingAssistant" not in source
    assert "enqueueForSession(" in source, "turn content is FIFO-chained per session"
    idle_block = re.search(r'case "session\.idle":.*?break', source, re.S)
    assert idle_block is not None and "flushSession(" in idle_block.group(0)
    deleted_block = re.search(r'case "session\.deleted":.*?break', source, re.S)
    assert deleted_block is not None and "settle(" in deleted_block.group(0)
    assert "seen(settledSessions," in source
    assert "DEDUP_CAP = 1000" in source


def test_plugin_history_read_has_a_timeout_so_a_hung_sdk_call_cannot_leak() -> None:
    """The one awaited SDK read (ctx.session.context for crash replay) races a
    bounded timeout — a never-resolving SDK promise must neither leak nor
    stall the reconcile chain; the loser returns null and the session stays
    unflagged for the next event's retry."""
    source = _plugin_source()
    assert "FETCH_TIMEOUT_MS" in source
    assert re.search(r"Promise\.race\(", source), "the history read must be timeout-raced"


def test_plugin_reads_history_via_the_session_context_domain() -> None:
    """The V2 SDK has no client.session.messages on the plugin context — the
    crash-replay lane reads the host's active context messages through
    ctx.session.context({ sessionID }) (SessionMessageInfo[]: user text on
    the message, assistant text/tool parts in the content array). Pin the
    guard, the call shape, and the response unwrapping — SDK-contract drift
    fails the Python gate instead of the memory store."""
    source = _plugin_source()
    assert re.search(r'ctx\?\.session\?\.context !== "function"', source), (
        "the history read must guard the V2 domain method"
    )
    assert re.search(r"ctx\.session\.context\(\{ sessionID \}\)", source), (
        "the call must run on its receiver with the V2 input shape"
    )
    assert ".session?.messages" not in source, "the v1 client.session.messages surface is gone"
    assert "message.time.created" in source, "user messages carry their created time"
    assert 'message?.type === "user"' in source and 'message?.type === "assistant"' in source, (
        "the replay loop discriminates on the V2 message type field"
    )


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


def test_plugin_rides_the_session_level_agent_on_user_prompts() -> None:
    """B2.9 wire slice on the V2 surface: the canonical `agent` body field
    anchors the daemon's turn routing (agent-bound profile routing), but the
    V2 host provides NO per-message agent on user prompts (SessionMessageUser)
    and the prompt hook event carries none either. The plugin therefore tracks
    the SESSION-level agent — session.created data.agent, updated by
    session.agent.selected — and rides it on live user ingests; an untracked
    session and replayed prompts stay honestly unattributed. Pin the tracker,
    both arming sites, the canonical-field lift, and the absence of any
    fabricated per-message agent."""
    source = _plugin_source()
    assert "const sessionAgents = new Map<string, string>()" in source, (
        "the session-level agent tracker must exist"
    )
    assert 'case "session.agent.selected":' in source, "the explicit agent switch must arm the tracker"
    created_block = re.search(r'case "session\.created":.*?break', source, re.S)
    assert created_block is not None and "data?.agent" in created_block.group(0), (
        "session.created with an agent must arm the tracker"
    )
    assert "if (agent) body.agent = agent" in source, "the canonical field carries the tracked session agent"
    assert "agentOf(" not in source, "no per-message agent extractor may survive without a source field"
    assert "raw.agent" not in source, "no agent may be fabricated into raw"
    assert re.search(r"postUserIngest\(\s*sessionID,\s*text,\s*stamp,\s*raw,\s*agent\s*\)", source), (
        "the prompt lane must pass the tracked session agent through"
    )
    assert "sessionAgents.delete(sessionID)" in source, "the tracker is dropped with the session"


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
    # IMPORTANT-1: the SessionContext hook is the only handler the host AWAITS
    # on the model-call path — it must fail open via try/catch like its
    # siblings (a fault must never reject the awaited call nor mutate system).
    transform_block = re.search(r"async function onSessionContext.*?\n}", source, re.S)
    assert transform_block is not None, "onSessionContext handler block not found"
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
    assert "The ONLY awaited network calls in the SessionContext handler are" in source


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
    assert re.search(r"reconcileSession\(pluginCtx, sessionID\)", source), (
        "reconciliation hooks into the lazy first-event path"
    )
    assert re.search(r"ctx\.session\.context\(\{ sessionID \}\)", source), (
        "host history is read via the V2 context domain (no new API surface)"
    )


def test_plugin_replay_keeps_the_engineering_red_lines() -> None:
    """PRD-B2.2 T4: (a) TOKEN red-line — the replay path must never construct
    or call any LLM client (the whole plugin talks to exactly two parties:
    the daemon REST and the host's own SDK); (b) hot-path zero-cost — the
    existing prompt/ingest lane gains no awaited I/O (reconcile is
    fire-and-forget like every other post); (c) reversibility — the only new
    filesystem artifact is the single watermark file."""
    source = _plugin_source()
    assert ".chat(" not in source and "LLM" not in source, (
        "no LLM client anywhere in the hook — replay is byte-reshuffling only"
    )
    chat_block = re.search(r"async function onPromptAdmitted.*?\n  \}", source, re.S)
    assert chat_block is not None and "await reconcileSession" not in chat_block.group(0), (
        "reconcile must stay fire-and-forget — the hot path awaits nothing"
    )
    assert source.count("hook-watermarks.json") >= 1 and "journal" not in source.lower(), (
        "single listed artifact; NO WAL/journal machinery (PRD boundary)"
    )


# ---------------------------------------------------------------- bounded disk growth


def test_plugin_bounds_the_debug_sink_with_a_named_cap_and_rotation() -> None:
    """The opt-in JSONL sink is otherwise append-forever. Pin the
    module-level cap constant, the single-generation archive path, and that the
    rotation runs inside the debug lane's fail-open chain."""
    source = _plugin_source()
    assert "const _HOOK_DEBUG_LOG_MAX_BYTES = 10 * 1024 * 1024" in source, (
        "the debug-sink cap must be a named module-level constant"
    )
    assert "hook-debug.jsonl.1" in source or "DEBUG_LOG_ARCHIVE_PATH" in source
    assert "rotateDebugLogIfOversized" in source, "the rotation helper must exist"
    # the sink chain mkdir -> rotate -> append, so rotation is fail-open
    assert re.search(
        r"debugSinkChain[\s\S]*?rotateDebugLogIfOversized\(\)[\s\S]*?appendFile\(DEBUG_LOG_PATH",
        source,
    ), "rotation must sit inside the debug lane's serialized fail-open chain"


def test_plugin_bounds_stale_watermark_artifacts_by_age_with_a_guard() -> None:
    """Stale watermark tmp artifacts are swept by age, bounded, with
    an interval guard so the whole directory is not stat-ed on every write."""
    source = _plugin_source()
    assert "const WATERMARK_STALE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000" in source, (
        "the stale-artifact TTL must be a named module-level constant"
    )
    assert "const WATERMARK_STALE_SWEEP_INTERVAL_MS" in source, "the interval guard must be named"
    assert "sweepStaleWatermarkArtifacts(" in source, "the sweep entry must exist"
    assert "watermarkStaleSweepLastAt" in source, "the guard clock must exist"


# ---------------------------------------------------------------- conversation visibility


def test_plugin_pins_the_visibility_signal_vocabulary_and_mode_gate() -> None:
    """the fixed neutral signal lines live as named constants, the wire
    mode gate accepts only compact/verbose/off (anything else reads off), and
    the mode's only source is the daemon's wire `visibility` field."""
    source = _plugin_source()
    assert 'const T1_SIGNAL = "[mnemoseed] replayed context from a previous session"' in source
    assert 'const T2_SIGNAL = "[mnemoseed] recalled related memories"' in source
    assert 'const CAPTURE_SIGNAL = "[mnemoseed] captured this session"' in source
    gate = re.search(r"function visibilityModeOf[^}]+}", source, re.S)
    assert gate is not None, "the shared mode gate must exist"
    body = gate.group(0)
    for mode in ('"compact"', '"verbose"', '"off"'):
        assert mode in body, mode
    assert "visibility?.mode" in body, "the mode reads only the wire field"


def test_plugin_pins_the_independent_first_capture_latch() -> None:
    """the first-capture latch is its OWN map (never reconciledSessions),
    armed on every ingest ack, rendered once under the observed compact mode,
    and cleaned up with the session."""
    source = _plugin_source()
    assert "const firstCaptureSeen" in source
    assert "armCaptureSignal(" in source
    assert "takeCaptureSignal(" in source
    # armed at all three ingest ack callbacks
    assert len(re.findall(r"armCaptureSignal\(sessionID\)", source)) == 3
    # rendered only under the observed compact mode
    render = re.search(r"takeCaptureSignal\(sessionID\)", source)
    assert render is not None
    # cleaned up with the session lifecycle
    deleted = re.search(r'case "session\.deleted":.*?break', source, re.S)
    assert deleted is not None and "firstCaptureSeen.delete(" in deleted.group(0)
    assert "lastVisibilityMode.delete(" in deleted.group(0)


def test_plugin_visibility_signal_is_budget_shed_not_content_shed() -> None:
    """the signal rides inside the block via withSignalAfterFence and the
    shed path restores the base block byte-identically (never shrinks existing
    content to make room for the signal)."""
    source = _plugin_source()
    assert "function withSignalAfterFence(" in source
    t1_wrap = re.search(r"function buildRecallInjection\(.*?\n\}", source, re.S)
    assert t1_wrap is not None and "withSignalAfterFence(" in t1_wrap.group(0)
    assert "MAX_INJECT_CHARS" in t1_wrap.group(0)
    t2_wrap = re.search(r"function buildT2Injection\(.*?\n\}", source, re.S)
    assert t2_wrap is not None and "withSignalAfterFence(" in t2_wrap.group(0)
