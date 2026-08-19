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
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

DRIVER = Path(__file__).parent / "ts_hook" / "hook_driver.mjs"


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
    result = subprocess.run(
        ["node", str(DRIVER), str(bundle), scenario],
        shell=False,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
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
        assert req["sessions"] == 2
        assert req["per_session"] == 8
    # injection alone never reinforces (TA-6: being injected is not being used)
    assert transcript["reinforcePosts"] == []


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
