"""F1 seam wiring: StrippingPipeline keeps the submit path O(1) and runs the
stripper as a lazy drain on read, so the /ingest HTTP handler stays untouched.

Also pins the daemon default (create_app) to the stripping pipeline and the
end-to-end /ingest -> segmenter -> F1 -> buffer path with a hot-reload swap.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.capture import (
    ContentTarget,
    InMemoryCapturePipeline,
    Rule,
    RuleSet,
    StripAction,
    Stripper,
    StripperError,
    StrippingPipeline,
    TurnSegmenter,
)
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.schema.turn import HostId, Turn, TurnRole, TurnStep

SESSION = "sess-f1-1"
PROFILE = "prof-main"

NPM_LOG = (
    "npm warn deprecated stable@0.1.8: Modern JS already guarantees Array#sort stability.\n"
    "added 1206 packages in 1m\n"
    "this line is real\n"
)
EXPECTED_NPM = "this line is real\n"


def _tool_step(output: str) -> TurnStep:
    return TurnStep(role=TurnRole.TOOL, content=output, tool_name="Bash")


def _turn(*steps: TurnStep) -> Turn:
    return Turn(
        turn_index=0,
        session_id=SESSION,
        profile_id=PROFILE,
        host=HostId.GENERIC,
        started_at=0.0,
        steps=list(steps),
    )


def _rule(rid: str, pattern: str) -> Rule:
    return Rule(
        id=rid,
        target=ContentTarget.TOOL_OUTPUT,
        action=StripAction.STRIP_LINE,
        pattern=pattern,
    )


def test_submit_is_lazy_drain_and_stats_accumulate_on_read() -> None:
    raw = InMemoryCapturePipeline()
    pipeline = StrippingPipeline(delegate=raw)
    turn = _turn(_tool_step(NPM_LOG))
    pipeline.submit_turn(turn)

    # nothing stripped yet: the HTTP path only did an O(1) raw append
    assert raw.turns(SESSION)[0].steps[0].content == NPM_LOG

    results = pipeline.drain(SESSION)
    assert len(results) == 1
    assert results[0].turn.steps[0].content == EXPECTED_NPM
    assert results[0].stats.bytes_out < results[0].stats.bytes_in
    assert "npm-warn" in results[0].stats.rules_hit
    # delegated buffer keeps the raw provenance copy
    assert raw.turns(SESSION)[0].steps[0].content == NPM_LOG


def test_turns_read_strips_and_is_idempotent() -> None:
    pipeline = StrippingPipeline()
    pipeline.submit_turn(_turn(_tool_step(NPM_LOG)))

    first = pipeline.turns(SESSION)
    assert first[0].steps[0].content == EXPECTED_NPM
    second = pipeline.turns(SESSION)
    assert second[0] == first[0]
    assert pipeline.drain(SESSION) == []


def test_cumulative_stats_do_not_double_count_after_repeated_drain() -> None:
    pipeline = StrippingPipeline()
    pipeline.submit_turn(_turn(_tool_step(NPM_LOG)))
    pipeline.drain(SESSION)
    pipeline.drain(SESSION)
    assert pipeline.stats.bytes_in == len(NPM_LOG.encode("utf-8"))
    assert pipeline.stats.bytes_out == len(EXPECTED_NPM.encode("utf-8"))
    assert pipeline.stats.rules_hit["npm-warn"] == 1


def test_hot_reload_swap_applies_to_next_turn() -> None:
    old_rules = RuleSet(rules=(_rule("drop-verbose", r"^verbose "),))
    pipeline = StrippingPipeline(stripper=Stripper(old_rules))

    pipeline.submit_turn(_turn(_tool_step("verbose one\nkeep-a\n")))
    assert pipeline.turns(SESSION)[0].steps[0].content == "keep-a\n"

    new_rules = RuleSet(
        rules=(
            _rule("drop-verbose", r"^verbose "),
            _rule("drop-keep", r"^keep-\w+$"),
        )
    )
    pipeline.reload_rules(new_rules)
    pipeline.submit_turn(_turn(_tool_step("verbose two\nkeep-b\n")))
    results = pipeline.drain(SESSION)
    assert len(results) == 1
    assert results[0].turn.steps[0].content == ""
    # the first turn was already processed under the old ruleset, untouched
    assert pipeline.turns(SESSION)[0].steps[0].content == "keep-a\n"


def test_pipeline_reload_rejects_bad_ruleset() -> None:
    pipeline = StrippingPipeline()
    with pytest.raises(StripperError):
        pipeline.reload_rules(RuleSet(rules=(_rule("bad", r"([unclosed"),)))


def test_end_session_and_settled_pass_through() -> None:
    from mnemoseed_local.storage.ports import TurnRange

    pipeline = StrippingPipeline()
    pipeline.submit_turn(_turn(_tool_step(NPM_LOG)))
    pipeline.end_session(SESSION, TurnRange(start=0, end=2))
    assert pipeline.settled(SESSION) == TurnRange(start=0, end=2)
    pipeline.end_session(SESSION, TurnRange(start=0, end=2))  # idempotent passthrough
    assert pipeline.settled(SESSION) == TurnRange(start=0, end=2)


def test_sessions_exposed_after_drain() -> None:
    pipeline = StrippingPipeline()
    pipeline.submit_turn(_turn(_tool_step(NPM_LOG)))
    assert pipeline.sessions() == (SESSION,)


def test_http_end_to_end_ingest_to_f1_buffer() -> None:

    pipeline = StrippingPipeline()

    @asynccontextmanager
    async def fake_lifespan(application):
        application.state.capture = pipeline
        application.state.segmenter = TurnSegmenter(pipeline)
        yield

    app = create_app()
    app.router.lifespan_context = fake_lifespan
    with TestClient(app) as client:
        assert (
            client.post(
                "/ingest",
                json={
                    "host": "claude_code",
                    "event": "tool_use",
                    "session_id": SESSION,
                    "profile_id": PROFILE,
                    "ts": 1.0,
                    "content": {"tool_name": "Bash", "input": {"cmd": "npm i"}, "output": NPM_LOG},
                },
            ).status_code
            == 202
        )
        assert (
            client.post(
                "/session/end",
                json={"session_id": SESSION, "profile_id": PROFILE},
            ).status_code
            == 200
        )

    assert pipeline.turns(SESSION)[0].steps[0].content == EXPECTED_NPM
    assert pipeline.stats.rules_hit["npm-warn"] >= 1
    assert isinstance(pipeline, StrippingPipeline)


def test_app_default_capture_is_stripping_pipeline() -> None:
    app = create_app()
    assert isinstance(app.state.capture, StrippingPipeline)
    turn = _turn(_tool_step(NPM_LOG))
    app.state.capture.submit_turn(turn)
    stripped = app.state.capture.turns(SESSION)
    assert stripped[0].steps[0].content == EXPECTED_NPM
