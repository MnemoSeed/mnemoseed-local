"""B2.2 hook BEHAVIOR tests (node driver over the esbuild-bundled plugin.ts).

Regex pins cannot pin behavior — and the two senior-QA BLOCKERs (watermark
advancing on a send-clock; live/replay mis-bind by arrival order) are
behavioral. This harness bundles the SHIPPED plugin.ts with esbuild (the
same npx-cached toolchain as the syntax gate) and drives it under node with
a canned SDK client + recording fetch. Skips cleanly when node/npx are
unavailable (CI has both).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

DRIVER = Path(__file__).parent / "ts_hook" / "hook_driver.mjs"


@pytest.fixture(autouse=True)
def _hermetic_plugin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundled plugin reads its opt-in seams from the AMBIENT environment
    at module load; scenarios must decide their own env, never inherit one a
    developer shell happens to carry."""
    for var in ("MNEMOSEED_LOCAL_DEBUG", "MNEMOSEED_LOCAL_PROFILE_ID", "MNEMOSEED_LOCAL_BASEURL"):
        monkeypatch.delenv(var, raising=False)


def _bundle(tmp_path: Path) -> Path:
    if shutil.which("npx") is None or shutil.which("node") is None:
        pytest.skip("node toolchain unavailable on this machine")
    plugin = resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts")
    out = tmp_path / "plugin.bundle.mjs"
    result = subprocess.run(
        f'npx --yes esbuild "{plugin}" --bundle --format=esm --platform=node '
        f'--outfile="{out}" --log-level=error',
        shell=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"esbuild bundle failed: {result.stderr}"
    return out


def _run(bundle: Path, scenario: str) -> dict:
    # Scenarios that pin the debug sink need the opt-in lane ARMED — it is
    # read from the env at plugin load, so pass it explicitly instead of
    # inheriting whatever the invoking shell carries.
    result = subprocess.run(
        ["node", str(DRIVER), str(bundle), scenario],
        shell=False,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "MNEMOSEED_LOCAL_DEBUG": "1"},
    )
    assert result.returncode == 0, f"driver failed: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_watermark_advances_only_on_daemon_ack(tmp_path: Path) -> None:
    """BLOCKER-1 (senior QA, B2.2): the mark is an ACK-clock, never a
    send-clock. A POST the daemon rejects must not advance the watermark —
    otherwise a >30s outage dies silently behind the overlap window and the
    crash-replay promise is void."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "ack-watermark")
    rejected = transcript["marksRejected"] or {}
    assert rejected.get("sess-behavior", 0) <= 500, (
        "rejected POST advanced the watermark: outage loss becomes silent"
    )
    accepted = transcript["marksAccepted"] or {}
    assert accepted.get("sess-behavior", 0) > 500, f"acked turn failed to advance the watermark: {accepted}"


def test_replayed_tail_arrives_before_the_live_turn(tmp_path: Path) -> None:
    """BLOCKER-2 (senior QA, B2.2): the segmenter cuts turns on ARRIVAL
    order, so the replayed host-history tail must reach the daemon strictly
    before the first live post of the session — otherwise the live turn is
    mis-bound under an old anchor with an inverted interval."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "replay-before-live")
    order = [(post["event"], post["text"]) for post in transcript["order"]]
    assert ("assistant_message", "历史助手回复") in order
    assert ("user_prompt", "重启后的新消息") in order
    live_index = order.index(("user_prompt", "重启后的新消息"))
    replay_user_index = order.index(("user_prompt", "历史用户消息"))
    replay_assistant_index = order.index(("assistant_message", "历史助手回复"))
    assert replay_user_index < replay_assistant_index < live_index, (
        f"replay must fully precede the live turn: {order}"
    )


def test_completed_assistant_posts_exactly_once_across_live_and_replay(tmp_path: Path) -> None:
    """The live message.updated path and a replay of the same message share
    the sentAssistant fingerprint — a completed reply is ingested once."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "assistant-dedup")
    assert transcript["assistantCount"] == 1, transcript


def test_aborted_assistant_shape_lands_in_the_debug_sink(tmp_path: Path) -> None:
    """An aborted assistant message (time.error, no time.completed) must be
    logged into hook-debug.jsonl BEFORE the completion gate drops it — the
    shape line is how a live abort becomes distinguishable from a completed
    reply in the debug lane. Both the metadata.error trace and the error-less
    abort trace are recorded."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "completion-shape-debug")
    shapes = transcript["shapes"]
    assert len(shapes) == 2, f"both abort shapes must land in the debug sink: {shapes}"
    by_id = {shape["messageID"]: shape for shape in shapes}
    meta = by_id["m_aborted_meta"]
    assert meta["sessionID"] == "sess-behavior"
    assert meta["role"] == "assistant"
    assert meta["hasCompleted"] is False, f"an abort must not look completed: {meta}"
    assert meta.get("completed") is None
    assert meta["hasError"] is True
    assert meta["error"] == "The operation was aborted due to timeout"
    plain = by_id["m_aborted_plain"]
    assert plain["hasCompleted"] is False
    assert plain["hasError"] is False
    assert plain["error"] is None


def test_outage_hole_is_replayed_in_process_before_later_acks_leapfrog_it(tmp_path: Path) -> None:
    """Re-review IMPORTANT-NEW-1: daemon bounces mid-conversation — turn A
    rejected, turn B accepted after recovery. The rejected ingest must have
    RE-ARMED reconciliation in-process, so the missed tail replays (from the
    host's own history) BEFORE B's ack can leap the watermark over the hole.
    Without the nack recovery this is permanent silent loss."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "outage-hole")
    assert transcript["outageAccepted"] >= 1, f"outage turn never replayed: {transcript}"
    assert 0 <= transcript["replayedOutageIndex"] < transcript["recoveryIndex"], (
        f"replay must precede the recovery turn: {transcript}"
    )


# ---------------------------------------------------------------- B2.1 T1/T3 (auto recall)


def test_inject_once_injects_a_fenced_recall_block_and_is_attempt_once(tmp_path: Path) -> None:
    """PRD-B2.1 T1 (TA-3/TA-5): the session-start transform injects a fenced
    memory-replay block exactly once per session. An invalid system shape
    burns NOTHING (the gate is only armed by a real call); repeats append
    nothing; concurrent first calls can never double-inject; each session gets
    its own injection; the read excludes the calling session and pins the tail
    shape; injection alone must never reinforce (TA-6)."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "inject-once")
    o1, o2, c1, c2, o3, e1, e2 = transcript["systems"]
    # the invalid-shape call left no trace; the later valid call injected once
    assert o1[0] == "BASE SYSTEM PROMPT"
    assert len(o1) == 2, f"exactly one appended element expected: {o1}"
    block = o1[1]
    assert block.count("<mnemoseed-memory-recall>") == 1
    assert block.count("</mnemoseed-memory-recall>") == 1, "the ONLY raw closing literal is the final fence"
    assert "发布窗口" in block and "评测臂" in block
    assert "‹mnemoseed-memory-recall›" in block, "inner fence literals must be sanitized"
    # B2.4 T3: the fixture payload carries self_window + group windows — the
    # block renders the self-anchor line exactly once, inside the fence right
    # after the disclaimer, and the group headers gain started=.
    lines = block.split("\n")
    assert lines[0] == "<mnemoseed-memory-recall>"
    assert lines[1].startswith("The block below is an automatic memory replay")
    assert lines[2] == '<session-self id="behavior" started="2026-08-19T09:00:00.000Z"/>'
    assert lines[-1] == "</mnemoseed-memory-recall>"
    assert block.count("<session-self ") == 1, "exactly one self-anchor line"
    assert (
        '<session-tail id="new" ended="1970-01-01T00:00:40.000Z" started="2026-08-19T08:00:00.000Z">' in block
    )
    assert (
        '<session-tail id="old" ended="1970-01-01T00:00:20.000Z" started="2026-08-18T07:00:00.000Z">' in block
    )
    # repeat call on the same session appended nothing
    assert o2 == ["BASE"]
    # concurrent pair injected exactly once across the two outputs
    assert (len(c1) == 2 and len(c2) == 1) or (len(c1) == 1 and len(c2) == 2), (c1, c2)
    # a fresh session got its own injection
    assert o3[0] == "BASE3" and len(o3) == 2
    # IMPORTANT-3 (QA): an empty sessionID burns NOTHING — the attempt gate is
    # not consumed, so the real session id right after still injects once.
    assert e1 == ["B"], f"empty sessionID must leave the system untouched: {e1}"
    assert len(e2) == 2, f"the real session id must still inject after the empty-id call: {e2}"
    # the read pinned shape: exclude the calling session, 2 sessions x 8 chunks
    requests = transcript["recentRequests"]
    assert len(requests) == 4, f"one read per injected session: {requests}"
    for req in requests:
        assert req["profile_id"] == "default"
        assert req["exclude_session_id"] in ("sess-behavior", "sess-conc", "sess-other", "sess-emptyid")
        assert req["self_session_id"] == req["exclude_session_id"], (
            "the read must carry the caller's session id"
        )
        assert req["sessions"] == 2
        assert req["per_session"] == 8
    # injection alone never reinforces (TA-6: being injected is not being used)
    assert transcript["reinforcePosts"] == []


def test_inject_renders_self_anchor_and_group_started_attributes(tmp_path: Path) -> None:
    """PRD-B2.4 T3 (M4b + M5-lite): a payload with a truthy self_window renders
    EXACTLY one <session-self .../> line, inside the fence right after the
    disclaimer (anti-mutant: outside-fence placement). Group headers gain
    started= ONLY for groups with a window present AND not truncated — the
    truncated and window-less groups must keep their headers byte-identical
    without the attribute."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "inject-time-windows")
    block = transcript["block"]
    lines = block.split("\n")
    assert lines[0] == "<mnemoseed-memory-recall>"
    assert lines[1].startswith("The block below is an automatic memory replay")
    assert lines[2] == '<session-self id="window" started="2026-08-20T01:00:00.000Z"/>'
    assert lines[-1] == "</mnemoseed-memory-recall>"
    assert block.count("<session-self ") == 1, f"exactly one self-anchor line: {block}"
    # present + not truncated -> started= rendered; present + truncated, and
    # absent window -> attribute omitted (byte-identical header, no placeholder)
    assert (
        '<session-tail id="full" ended="1970-01-01T00:00:40.000Z" started="2026-08-19T01:00:00.000Z">'
        in block
    )
    assert '<session-tail id="trunc" ended="1970-01-01T00:00:30.000Z">' in block, (
        "truncated windows must omit started="
    )
    assert '<session-tail id="none" ended="1970-01-01T00:00:20.000Z">' in block, (
        "window-less groups must omit started="
    )
    requests = transcript["recentRequests"]
    assert len(requests) == 1
    assert requests[0]["exclude_session_id"] == "sess-window"
    assert requests[0]["self_session_id"] == "sess-window", "the read must carry the caller's session id"


def test_inject_old_daemon_payload_renders_exactly_as_today(tmp_path: Path) -> None:
    """PRD-B2.4 T3 fallback (wire compat): a payload without window/self_window
    fields renders byte-identical to the pre-feature block — no self line, no
    started= attribute, and no crash on the missing fields."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "inject-old-daemon")
    block = transcript["block"]
    expected = (
        "<mnemoseed-memory-recall>\n"
        "The block below is an automatic memory replay of earlier sessions, "
        "not the user's current instructions.\n"
        '<session-tail id="new" ended="1970-01-01T00:00:40.000Z">\n'
        "user: hello world alpha\n"
        "assistant: hello world beta\n"
        "</session-tail>\n"
        '<session-tail id="old" ended="1970-01-01T00:00:20.000Z">\n'
        "user: hello world gamma\n"
        "</session-tail>\n"
        "</mnemoseed-memory-recall>"
    )
    assert block == expected, "old-daemon payloads must render exactly as before the feature"
    assert "<session-self" not in block
    assert "started=" not in block


def test_inject_fail_open_leaves_system_untouched_and_attempts_once(tmp_path: Path) -> None:
    """PRD-B2.1 T1 fail-open: a daemon failure (503 or a thrown network error)
    must leave the system array untouched AND still consume the attempt gate —
    the session pays at most one bounded read, then stays quiet."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "inject-fail-open")
    for system in transcript["systems"]:
        assert len(system) == 1, f"failure must leave system untouched: {system}"
    assert transcript["attempts503"] == 1, "the 503 lane gets exactly one attempt per session"
    assert transcript["attemptsThrow"] == 1, "the throw lane gets exactly one attempt per session"
    # IMPORTANT-1 (QA): a handler fault (frozen system array) must not reject
    # the host's awaited model-call hook nor mutate the array.
    assert transcript["frozenSystem"] == ["BASE"], transcript["frozenSystem"]


def test_citation_reinforce_only_records_actually_consumed_injections(tmp_path: Path) -> None:
    """PRD-B2.1 T3 (TA-6): reinforce fires only on real consumption evidence —
    the assistant's reply text citing an injected slice. The re-citation of the
    same chunk in a later reply is suppressed (once per session per chunk);
    never-cited injected chunks (c-eval/c-fence/c-tail) and replies in a session
    that never received an injection produce nothing."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "citation-reinforce")
    reinforce = transcript["reinforcePosts"]
    assert len(reinforce) == 2, f"exactly two reinforce posts expected: {reinforce}"
    cited = [set(post["body"]["chunk_ids"]) for post in reinforce]
    assert {"c-oldstop"} in cited and {"c-eng"} in cited, f"only the actually-cited chunks: {reinforce}"
    for post in reinforce:
        assert post["body"]["profile_id"] == "default"
    assert "<mnemoseed-memory-recall>" in transcript["injectedBlock"]


def test_inject_budget_drops_oldest_and_needles_only_included_slices(tmp_path: Path) -> None:
    """PRD-B2.1 T1 budget red line: the assembled block stays within the 4000
    char cap INCLUDING fence, disclaimer and headers; the oldest content is
    dropped first (the 3000-char 甲 chunk AND the older session's chunk); a
    boundary chunk keeps only its tail slice (marked with …); and a DROPPED
    chunk is never registered as a needle — quoting it verbatim later produces
    zero reinforce posts (needle integrity: only injected slices can be cited)."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "inject-budget")
    block = transcript["block"]
    assert "乙" in block
    assert "甲" not in block, "the oldest big chunk must be dropped from the budget"
    assert "老session" not in block, "the older session must be dropped from the budget"
    # QA re-review NIT-2: the budget accounting reserves the fence, the
    # disclaimer and the group header UP FRONT, and the driver feeds content
    # that fills the remainder exactly — a correct build lands at EXACTLY
    # 4000 chars. A regression that stops accounting the wrapper costs (up to
    # ~199 chars) would shorten the block and still pass a `<=` check; the
    # equality pin makes it fail.
    assert block.startswith("<mnemoseed-memory-recall>"), "block must open with the fence"
    assert block.endswith("</mnemoseed-memory-recall>"), "block must close with the fence"
    assert len(block) == 4000, f"budget accounting must fill the cap exactly: {len(block)}"
    block2 = transcript["block2"]
    assert block2.startswith("<mnemoseed-memory-recall>"), "block2 must open with the fence"
    assert block2.endswith("</mnemoseed-memory-recall>"), "block2 must close with the fence"
    assert len(block2) == 4000, f"single-chunk block must fill the cap exactly: {len(block2)}"
    lines = block2.split("\n")
    header_index = next(i for i, line in enumerate(lines) if line.startswith("<session-tail "))
    assert lines[header_index + 1].startswith("…"), (
        "the boundary slice must open its content with the ellipsis marker"
    )
    assert transcript["reinforcePosts"] == [], "a citation of a DROPPED chunk must never reinforce"


def test_slice_needle_integrity_needles_derive_from_the_included_slice(tmp_path: Path) -> None:
    """IMPORTANT-2 (QA review): heterogeneous boundary content. The boundary
    chunk is an A-run followed by a B-run; the budget tail-slices it wholly
    inside the B run, so the A run is NEVER injected. Needles must derive from
    the EXACT included slice: quoting B reinforces c-het-bound, while quoting
    A (never injected) or invented content produces nothing."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "slice-needle-integrity")
    block = transcript["block"]
    assert "A" not in block, "the A run was never injected"
    assert "B" in block and "乙" in block, "the B slice and the newest chunk must be injected"
    assert len(block) <= 4000, f"assembled block must honor the cap: {len(block)}"
    reinforce = transcript["reinforcePosts"]
    assert len(reinforce) == 1, f"exactly one reinforce post expected: {reinforce}"
    body = reinforce[0]["body"]
    assert body["chunk_ids"] == ["c-het-bound"], f"only the injected slice is citable: {body}"
    assert body["profile_id"] == "default"


# ---------------------------------------------------------------- B2.1 T2 (mid-session auto recall)


def test_recall_pull_fires_only_after_an_acked_user_ingest(tmp_path: Path) -> None:
    """PRD-B2.1 T2 (D8): the pending-recall pull is gated on an ACKED user
    ingest — a transform before any user prompt, or one racing the post
    before its ack microtask, must not pull; the armed+acked transform pulls
    exactly once, injects the fenced selection, and clears the flags (the
    next transform pulls nothing more)."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "recall-pull-gating")
    g1, g2, g3, g4 = transcript["systems"]
    assert g1 == ["BASE"], "no user prompt yet: T2 must not pull"
    assert g2 == ["BASE2"], "the pre-ack transform must not pull"
    assert len(g3) == 2, f"the armed+acked transform must inject: {g3}"
    assert g3[1].count("<mnemoseed-memory-recall>") == 1
    assert "上次中段提到 LanceDb" in g3[1]
    assert g4 == ["BASE4"], "the served pull clears the flags"
    assert transcript["pullCount"] == 1, transcript["pullCount"]
    assert transcript["pullBodies"][0]["seen_chunk_ids"] == [], (
        "no T1 injection for this session: the seen list is empty"
    )


def test_recall_pull_empty_selection_keeps_the_arm_and_never_appends(tmp_path: Path) -> None:
    """PRD-B2.1 T2: an empty selection serves nothing but keeps the arm — the
    next acked user turn re-pulls (the daemon slot may have rotated); the
    system array is never appended to."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "recall-pull-empty-rearm")
    e1, e2 = transcript["systems"]
    assert e1 == ["BASE"]
    assert e2 == ["BASE2"]
    assert transcript["pullCount"] == 2, f"the arm must survive an empty serve: {transcript}"


def test_recall_pull_fail_open_keeps_output_untouched_and_rearms(tmp_path: Path) -> None:
    """PRD-B2.1 T2 fail-open: a 503 or a thrown network error leaves the
    system untouched AND keeps the arm — the next acked user turn retries the
    pull; an enabled:false lane serves nothing either."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "recall-pull-fail-open")
    f1, f2, f3 = transcript["systems"]
    assert f1 == ["BASE"], "a 503 pull must leave the system untouched"
    assert len(f2) == 2 and "上次中段提到 LanceDb" in f2[1], "the healthy retry must serve the selection"
    assert f3 == ["BASE3"], "an enabled:false lane must never append"
    assert transcript["pullCount"] == 3, transcript["pullCount"]


def test_recall_pull_is_independent_of_t1_and_reinforces_only_consumed(tmp_path: Path) -> None:
    """PRD-B2.1 T2 (D8 restructure): the T2 branch runs independently of the
    T1 attempt gate — the session-tail block (T1, first transform) and the
    mid-session pull block (T2, after an acked user ingest) COEXIST; the T1
    chunk ids ride the pull as seen_chunk_ids so the daemon never re-serves
    them; citing the T2-injected chunk reinforces it exactly once per session
    (TA-6: a re-citation is suppressed)."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "recall-pull-t1-independence")
    i1, i2 = transcript["systems"]
    assert len(i1) == 2, f"T1 must inject on the first transform: {i1}"
    assert "发布窗口" in i1[1], "the T1 session-tail block must be present"
    assert len(i2) == 3, f"T1 and T2 blocks must coexist: {i2}"
    assert i2[1] == i1[1], "the T1 block must not be re-injected"
    assert "上次中段提到 LanceDb" in i2[2], "the T2 pull block must be injected"
    assert i2[2].count("<mnemoseed-memory-recall>") == 1
    pull_body = transcript["pullBodies"][0]
    assert pull_body["profile_id"] == "default"
    assert pull_body["session_id"] == "sess-behavior"
    assert set(pull_body["seen_chunk_ids"]) == {"c-oldstop", "c-fence", "c-tail", "c-eval", "c-eng"}, (
        "the T1-injected chunk ids must ride the pull as seen"
    )
    reinforce = transcript["reinforcePosts"]
    assert len(reinforce) == 1, f"exactly one reinforce post expected: {reinforce}"
    assert reinforce[0]["body"]["chunk_ids"] == ["c-mid"], "only the T2-injected consumed chunk is citable"


def test_recall_pull_budget_equality_appends_the_full_selection(tmp_path: Path) -> None:
    """QA BLOCKER-1 + NIT-7: the hook's item budget is the daemon's WIRE
    budget_chars, never a hardcoded cap. A selection whose item cost lands
    inside (1200 - wrapper, 2000] must STILL be appended; the assembled block
    length is pinned EXACTLY — 156 wrapper chars + 1902 item cost = 2058 —
    mirroring the T1 inject-budget equality pin. A mutant that hardcodes the
    old 1200 cap drops this selection and fails."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "recall-pull-budget-equality")
    [b1] = transcript["systems"]
    assert len(b1) == 2, f"the full selection must be appended: {b1}"
    block = b1[1]
    assert block.count("<mnemoseed-memory-recall>") == 1
    assert block.endswith("</mnemoseed-memory-recall>")
    assert len(block) == 2058, f"block length must be wrapper(156) + items(1902): {len(block)}"
    assert transcript["blockLength"] == 2058, transcript["blockLength"]


def test_recall_pull_budget_below_slice_floor_still_appends(tmp_path: Path) -> None:
    """QA IMPORTANT-3: the daemon is the sole budget authority across the WHOLE
    positive-int range — a budget_chars=150 selection (below the T1 slice
    floor) is daemon-legal: a full item whose cost fits IS served, and the
    hook must append it instead of re-imposing a slicing floor it does not
    own. Block length pinned exactly: wrapper(156) + item(101) = 257."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "recall-pull-low-budget")
    [b1] = transcript["systems"]
    assert len(b1) == 2, f"the daemon-legal low-budget selection must be appended: {b1}"
    block = b1[1]
    assert len(block) == 257, f"block length must be wrapper(156) + item(101): {len(block)}"
    assert transcript["blockLength"] == 257, transcript["blockLength"]


def test_recall_pull_clears_the_arm_once_the_slot_was_consumed(tmp_path: Path) -> None:
    """QA IMPORTANT-2: a serve whose response was lost in transit must not
    produce endless empty pulls — the daemon answers slot_consumed:true with
    items:[], the transform clears the arm, and a subsequent transform issues
    ZERO additional pulls (fail-open preserved: the system stays untouched).
    QA BLOCKER-2: this response is now PRODUCIBLE by the real daemon (the
    consumed tombstone answers the retry), so the scenario is the live
    contract, not a hand-fed fiction."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "recall-pull-slot-consumed")
    s1, s2 = transcript["systems"]
    assert s1 == ["BASE"], "a consumed slot serves nothing"
    assert s2 == ["BASE2"], "the cleared arm must not pull again"
    assert transcript["pullCount"] == 1, transcript["pullCount"]


# ---------------------------------------------------------------- B2.6 host-plugin bundling


def test_config_hook_injects_the_mcp_registration_create_if_absent(tmp_path: Path) -> None:
    """B2.6 bundling: the config hook registers cfg.mcp["mnemoseed"] with the
    A3 MCP-gateway command, creating the mcp map when absent; a user's
    existing manual registration is never overwritten; a null cfg is a no-op."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "config-inject")
    expected = {"type": "local", "command": ["mnemoseed-local", "mcp"], "enabled": True}
    assert transcript["empty"]["mcp"]["mnemoseed"] == expected
    assert transcript["bare"]["mcp"]["mnemoseed"] == expected
    assert transcript["manual"]["mcp"]["mnemoseed"] == {"type": "remote", "url": "http://mcp.example"}, (
        "a user's manual registration must win untouched"
    )
    assert transcript["noThrow"] is True


def test_options_tuple_switch_short_circuits_the_whole_bundle(tmp_path: Path) -> None:
    """B2.6 single switch: {enabled:false} via the ["spec", options] tuple
    makes the factory return {} — NO config hook, NO hooks; enabled:true,
    empty options and absent options all load the full bundle."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "switch-short-circuit")
    assert transcript["offKeys"] == [], f"enabled:false must short-circuit everything: {transcript}"
    for keys in ("onKeys", "bareKeys", "noneKeys"):
        assert "config" in transcript[keys], keys
        assert "chat.message" in transcript[keys], keys
        assert "chat.system.transform" in transcript[keys], keys


def test_config_hook_is_fail_open_on_frozen_objects(tmp_path: Path) -> None:
    """B2.6 I3: the config hook try/catch is fail-open — Object.freeze(cfg) and
    freeze(cfg.mcp) must not throw, must not overwrite other keys, and a
    subsequent normal cfg still injects."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "config-inject-frozen")
    assert transcript["noThrow"] is True, "frozen cfg must not throw"
    assert transcript["frozenCfgOther"] == "keep", "other keys on frozen cfg must survive"
    assert transcript["frozenMcpOther"] == "keep2", "other keys on cfg with frozen mcp must survive"
    # frozen paths are fail-open: no throw and other keys survive; a frozen cfg
    # without mcp cannot be injected (??= throws), a frozen mcp cannot gain the
    # entry — both must not overwrite other keys
    assert transcript["frozenCfgHasMnemoseed"] is None
    assert transcript["frozenMcpHasMnemoseed"] is None
    expected = {"type": "local", "command": ["mnemoseed-local", "mcp"], "enabled": True}
    assert transcript["afterMnemoseed"] == expected
