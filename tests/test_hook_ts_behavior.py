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
