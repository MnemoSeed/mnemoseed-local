"""Local Stripper rule engine behaviour through its public surface (FR-1.2).

One rule engine consumes Turn objects; an ordered, data-driven ruleset strips
mechanical noise (build logs, package-manager output, dead-loop tracebacks,
ANSI codes, progress bars) and must never touch prose. All assertions go
through ``Stripper.strip_turn`` / ``strip_text`` so a ruleset refactor does not
break the tests.
"""

from __future__ import annotations

import time

import pytest

from mnemoseed_local.capture import (
    RULESET_V1,
    ContentTarget,
    Rule,
    RuleSet,
    StripAction,
    Stripper,
    StripperError,
)
from mnemoseed_local.schema.turn import HostId, Turn, TurnRole, TurnStep

# ---------------------------------------------------------------- fixtures


def _tool_step(output: str, name: str = "Bash") -> TurnStep:
    return TurnStep(role=TurnRole.TOOL, content=output, tool_name=name)


def _msg_step(role: TurnRole, text: str) -> TurnStep:
    return TurnStep(role=role, content=text)


def _turn(*steps: TurnStep) -> Turn:
    return Turn(
        turn_index=0,
        session_id="sess-strip-1",
        profile_id="prof-main",
        host=HostId.GENERIC,
        started_at=0.0,
        steps=list(steps),
    )


def _rule(
    rid: str,
    action: StripAction,
    pattern: str = "",
    target: ContentTarget = ContentTarget.TOOL_OUTPUT,
    min_run: int = 2,
) -> Rule:
    return Rule(id=rid, target=target, action=action, pattern=pattern, min_run=min_run)


def _rule_set(*rules: Rule) -> RuleSet:
    return RuleSet(rules=tuple(rules))


def _rule_by_id(ruleset: RuleSet, rid: str) -> Rule:
    for rule in ruleset.rules:
        if rule.id == rid:
            return rule
    raise AssertionError(f"rule {rid!r} not in ruleset")


NPM_LOG = (
    "npm notice created a lockfile as package-lock.json. You should commit this file.\n"
    "npm warn deprecated stable@0.1.8: Modern JS already guarantees Array#sort stability.\n"
    "npm warn deprecated core-js@2.6.12: core-js@<3.23.3 is no longer maintained.\n"
    "added 1206 packages in 1m\n"
    "found 0 vulnerabilities\n"
)
EXPECTED_NPM = "npm notice created a lockfile as package-lock.json. You should commit this file.\n"

PIP_LOG = (
    "Collecting requests\n"
    "  Downloading requests-2.31.0-py3-none-any.whl (62 kB)\n"
    "     \u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
    "\u2501\u2501\u2501 62/62 kB 4.5 MB/s eta 0:00:01\n"
    "Installing collected packages: urllib3, idna, charset-normalizer, certifi, requests\n"
    "Successfully installed certifi-2023.7.22 idna-3.4 requests-2.31.0 urllib3-1.26.16\n"
)
EXPECTED_PIP = ""

CARGO_LOG = (
    "   Compiling libc v0.2.147\n"
    "   Compiling memchr v2.6.3\n"
    "   Finished `dev` profile [unoptimized + debuginfo] target(s) in 2.36s\n"
)
EXPECTED_CARGO = ""

UV_LOG = "   Built mnemoseed v0.0.1\n   Installed 256 packages in 1.02s\n   Audited 256 packages in 0.04ms\n"
EXPECTED_UV = ""

BUILD_LOG = (
    "[ 25%] Building CXX object CMakeFiles/foo.dir/main.cpp.o\n"
    "[ 50%] Building CXX object CMakeFiles/foo.dir/util.cpp.o\n"
    "[100%] Linking CXX executable foo\n"
    "gyp ERR! stack Error: ENOENT\n"
    "gyp ERR! cwd G:\\Development\\foo\n"
)
EXPECTED_BUILD = ""

RUSTC_WARN = (
    "warning: unused variable: `name`\n"
    "  --> src/main.rs:14:9\n"
    "   |\n"
    '14 |     let name = "x";\n'
    "   |         ^^^^\n"
    "   |\n"
    "   = note: `#[warn(unused_variables)]` on by default\n"
)
EXPECTED_RUSTC = '   |\n14 |     let name = "x";\n   |         ^^^^\n   |\n'

# Cargo renders the same diagnostic with unicode box-drawing arrow (U+2500).
RUSTC_WARN_UNICODE = (
    "warning: unused variable: `name`\n"
    "  ──> src/main.rs:14:9\n"
    "   |\n"
    '14 |     let name = "x";\n'
    "   |         ^^^^\n"
    "   |\n"
    "   = note: `#[warn(unused_variables)]` on by default\n"
)

# Summary forms rustc emits before/after the diagnostic body
# ("N warnings emitted" and backtick-quoted generated-N-warnings lines).
RUSTC_WARN_SUMMARIES = (
    "warning: 1 warning emitted\n",
    "warning: 2 warnings emitted\n",
    "warning: `demo` (lib) generated 3 warnings\n",
)

PROGRESS_LOG = (
    "Downloading Electron Framework\n"
    "\r[#........] 10%\r[##.......] 20%\r[#######..] 70%\n"
    "[====================================>  ] 90%\n"
    "100%|\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588| 2/2 [00:00<00:00, 20.81it/s]\n"
    "\u2839 Installing dependencies...\n"
    "\u2713 12 files migrated\n"
)
EXPECTED_PROGRESS = "Downloading Electron Framework\n\u2713 12 files migrated\n"

TRACEBACK_BLOCK = (
    "Traceback (most recent call last):\n"
    '  File "/app/loop.py", line 3, in <module>\n'
    "    loop()\n"
    '  File "/app/loop.py", line 2, in loop\n'
    "    self.ping()\n"
    "TimeoutError: connection timed out\n"
)
DEAD_LOOP = TRACEBACK_BLOCK * 40

# Lines that look like prose but carry the same tokens the rules strip; neither
# message text nor tool output containing them may be touched by ruleset v1.
SAFE_LINES = [
    "added 5 packages to the project in 5 minutes",
    "removed 3 stale branches from the repo",
    "found 0 vulnerabilities in our last audit",
    "I am 100% sure this is the right answer",
    "Compiling the report took all morning",
    "Finished the review in 5 seconds, then shipped it",
    "Downloading the report was the slow part",
    "using cached credentials for the api",
    "warning: the tests take a while",
    "src/main.rs:14:9 is where the borrow lives",
    "you should commit the lockfile, not delete it",
    "the npm registry was slow this morning",
    "we built the wheel of the car, not of python",
    "= note that this is prose, not a compiler",
    "Collecting shells by the seashore",
]


# ---------------------------------------------------------------- rule families


def test_npm_log_family() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(NPM_LOG)))
    assert result.turn.steps[0].content == EXPECTED_NPM
    assert "npm-warn" in result.stats.rules_hit
    assert "npm-summary" in result.stats.rules_hit
    assert "npm-vulns" in result.stats.rules_hit
    assert result.stats.bytes_out < result.stats.bytes_in


def test_npm_audit_singular_vuln_stripped() -> None:
    stripper = Stripper(RULESET_V1)
    for audit in (
        "found 1 vulnerability\n",
        "found 1 vulnerability in 2 scanned packages\n",
    ):
        result = stripper.strip_turn(_turn(_tool_step(audit)))
        assert result.turn.steps[0].content == ""
        assert "npm-vulns" in result.stats.rules_hit


def test_npm_audit_severity_qualified_vulns_stripped() -> None:
    stripper = Stripper(RULESET_V1)
    for audit in (
        "found 1 high severity vulnerability in 2 scanned packages\n",
        "found 3 moderate severity vulnerabilities\n",
        "found 5 critical severity vulnerabilities in 20 scanned packages\n",
    ):
        result = stripper.strip_turn(_turn(_tool_step(audit)))
        assert result.turn.steps[0].content == ""
        assert "npm-vulns" in result.stats.rules_hit


def test_pip_log_family() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(PIP_LOG)))
    assert result.turn.steps[0].content == EXPECTED_PIP
    assert "pip-acquire" in result.stats.rules_hit
    assert "pip-download" in result.stats.rules_hit
    assert "pip-install-summary" in result.stats.rules_hit
    assert result.stats.compression_ratio == 1.0


def test_cargo_log_family() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(CARGO_LOG)))
    assert result.turn.steps[0].content == EXPECTED_CARGO
    assert "cargo-compile" in result.stats.rules_hit
    assert "cargo-finished" in result.stats.rules_hit


def test_uv_log_family() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(UV_LOG)))
    assert result.turn.steps[0].content == EXPECTED_UV
    assert "uv-built" in result.stats.rules_hit
    assert "uv-counts" in result.stats.rules_hit


def test_build_log_family() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(BUILD_LOG)))
    assert result.turn.steps[0].content == EXPECTED_BUILD
    assert "cmake-stage" in result.stats.rules_hit
    assert "gyp-noise" in result.stats.rules_hit


def test_rustc_diagnostic_chrome_stripped_but_code_kept() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(RUSTC_WARN)))
    assert result.turn.steps[0].content == EXPECTED_RUSTC
    assert "rustc-warning" in result.stats.rules_hit
    assert "rustc-location" in result.stats.rules_hit
    assert "rustc-note" in result.stats.rules_hit


def test_rustc_unicode_arrow_location_stripped_code_kept() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(RUSTC_WARN_UNICODE)))
    assert result.turn.steps[0].content == EXPECTED_RUSTC
    assert "rustc-warning" in result.stats.rules_hit
    assert "rustc-location" in result.stats.rules_hit


def test_rustc_warning_summary_lines_stripped() -> None:
    stripper = Stripper(RULESET_V1)
    for summary in RUSTC_WARN_SUMMARIES:
        result = stripper.strip_turn(_turn(_tool_step(summary)))
        assert result.turn.steps[0].content == ""
        assert "rustc-warning" in result.stats.rules_hit


def test_rustc_warning_summary_lines_never_touch_diagnostic_code() -> None:
    stripper = Stripper(RULESET_V1)
    code_lines = '   |\n14 |     let name = "x";\n   |         ^^^^\n   |\n'
    result = stripper.strip_turn(_turn(_tool_step(code_lines)))
    assert result.turn.steps[0].content == code_lines
    assert result.stats.rules_hit == {}


def test_progress_bars_spinners_cr_fragments_stripped() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(PROGRESS_LOG)))
    assert result.turn.steps[0].content == EXPECTED_PROGRESS
    assert "cr-progress-fragment" in result.stats.rules_hit
    assert "percent-bar" in result.stats.rules_hit
    assert "tqdm-bar" in result.stats.rules_hit
    assert "spinner-braille" in result.stats.rules_hit


def test_ansi_redaction_is_span_precise() -> None:
    stripper = Stripper(RULESET_V1)
    ansi_text = "before \x1b[1;31mred\x1b[0m after"
    text, stats = stripper.strip_text(ansi_text, ContentTarget.BOTH)
    assert text == "before red after"
    assert "ansi-codes" in stats

    chinese = "\u90e8\u7f72 \x1b[32m\u6210\u529f\x1b[0m \u4e86"
    text, _ = stripper.strip_text(chinese, ContentTarget.MESSAGE_TEXT)
    assert text == "\u90e8\u7f72 \u6210\u529f \u4e86"

    osc = "A\x1b]0;my title\x07B"
    text, _ = stripper.strip_text(osc, ContentTarget.TOOL_OUTPUT)
    assert text == "AB"


# ---------------------------------------------------------------- dead-loop dedupe


def test_dead_loop_traceback_blocks_collapsed_to_one() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(DEAD_LOOP)))
    assert result.turn.steps[0].content == TRACEBACK_BLOCK
    assert "collapse-repeated-blocks" in result.stats.rules_hit
    saved = 1.0 - (result.stats.bytes_out / result.stats.bytes_in)
    assert saved > 0.95


def test_single_line_repeat_collapsed() -> None:
    stripper = Stripper(RULESET_V1)
    looped = "Error: connection refused\n" * 500
    result = stripper.strip_turn(_turn(_tool_step(looped)))
    assert result.turn.steps[0].content == "Error: connection refused\n"


def test_two_identical_adjacent_lines_preserved() -> None:
    stripper = Stripper(RULESET_V1)
    text = "Build finished in 2.1s\nBuild finished in 2.1s\n"
    result = stripper.strip_turn(_turn(_tool_step(text)))
    assert result.turn.steps[0].content == text
    assert "collapse-repeated-blocks" not in result.stats.rules_hit


def test_three_identical_adjacent_lines_collapsed() -> None:
    stripper = Stripper(RULESET_V1)
    text = "Error: connection refused\n" * 3
    result = stripper.strip_turn(_turn(_tool_step(text)))
    assert result.turn.steps[0].content == "Error: connection refused\n"
    assert "collapse-repeated-blocks" in result.stats.rules_hit


# ---------------------------------------------------------------- host-injected artifacts
#
# The host injects two system-artifact shapes into conversation turns that are
# NOT user/assistant speech: the session-compaction summary wrapper and the
# task-notification XML block. Both are structural-matched as scaffold noise;
# words like "session", "summary", "task", "开始" alone never match.

COMPACTION_WRAPPER = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier portion of the conversation.\n\n"
    "Summary:\n"
    "1. Primary Request and Intent:\n"
    "   The user is building a memory layer; they prefer concise reviews.\n"
    "Continue the conversation from where it left off without asking the user any further questions. "
    "Resume directly \u2014 do not acknowledge the summary, do not recap what was happening, do not "
    'preface with "I\'ll continue" or similar. Pick up the last task as if the break never happened.'
)

TASK_NOTIFICATION = (
    "<task-notification>\n"
    "<task-id>af715e0e7684ee2a4</task-id>\n"
    "<tool-use-id>Agent_133</tool-use-id>\n"
    "<output-file>C:\\Users\\temp\\task.output</output-file>\n"
    "<status>completed</status>\n"
    '<summary>Agent "research" finished</summary>\n'
    "<note>a task-notification fires each time this agent stops</note>\n"
    "<result>the structured report\nwith more lines\n</result>\n"
    "<usage><subagent_tokens>682</subagent_tokens></usage>\n"
    "</task-notification>\n"
)

# Human prose that merely mentions sessions / summaries / tasks / compaction or
# quotes a fragment of the wrapper: structural anchoring must leave them intact.
COMPACTION_NEAR_MISSES = [
    "This session is being continued from a previous conversation that ran out of context.",
    "The summary below covers the earlier portion, and we should pick up from there.",
    "\u8bfb\u5b8c\u90a3\u4e2a "
    "compaction summary \u4e4b\u540e\uff0c\u6211\u5bf9\u8bbe\u8ba1\u65b9\u6848\u66f4\u6e05\u695a\u4e86\u3002",
    "\u4e0a\u6b21 session \u7684 summary "
    "\u91cc\u5199\u7684\u662f\u6211\u4eec\u8ba8\u8bba\u5230\u5b89\u88c5\u4f53\u9a8c\u3002",
    "\u8fd9\u4e2a task \u5b8c\u6210\u4e86\uff0c\u8fd8\u6709\u4e00\u4e2a task \u5f85\u5904\u7406\u3002",
    "\u6211\u4eec\u5148\u628a\u4e4b\u524d\u7684\u8ba8\u8bba\u603b\u7ed3\u4e00\u4e0b\uff0c\u518d\u7ee7\u7eed\u3002",
    "I read the session summary and the task list after the break.",
    "Continue the conversation from where it left off",
]


def test_session_compaction_wrapper_stripped() -> None:
    text, hits = Stripper(RULESET_V1).strip_text(COMPACTION_WRAPPER, ContentTarget.MESSAGE_TEXT)
    assert text == ""
    assert "compaction-summary-wrapper" in hits


def test_task_notification_block_stripped() -> None:
    text, hits = Stripper(RULESET_V1).strip_text(TASK_NOTIFICATION, ContentTarget.MESSAGE_TEXT)
    assert text == ""
    assert "task-notification-block" in hits


def test_task_notification_inline_keeps_surrounding_speech() -> None:
    blended = (
        "\u5148\u770b\u770b\u7ed3\u679c\u3002\n"
        + TASK_NOTIFICATION
        + "\u63a5\u7740\u6211\u4eec\u7ee7\u7eed\u3002\n"
    )
    text, hits = Stripper(RULESET_V1).strip_text(blended, ContentTarget.MESSAGE_TEXT)
    assert text == "\u5148\u770b\u770b\u7ed3\u679c\u3002\n\u63a5\u7740\u6211\u4eec\u7ee7\u7eed\u3002\n"
    assert "task-notification-block" in hits


def test_compaction_wrapper_with_trailing_user_speech_survives() -> None:
    blended = COMPACTION_WRAPPER + (
        "\n\u53e6\u5916\uff0c\u6211\u89c9\u5f97\u504f\u597d\u8bb0\u5f55\u5e94\u8be5\u66f4\u7b80\u6d01\u3002"
    )
    text, hits = Stripper(RULESET_V1).strip_text(blended, ContentTarget.MESSAGE_TEXT)
    assert (
        "\u53e6\u5916\uff0c\u6211\u89c9\u5f97\u504f\u597d\u8bb0\u5f55\u5e94\u8be5\u66f4\u7b80\u6d01\u3002"
        in text
    )
    assert "compaction-summary-wrapper" in hits


def test_artifact_rules_leave_compaction_discussion_prose_untouched() -> None:
    stripper = Stripper(RULESET_V1)
    for line in COMPACTION_NEAR_MISSES:
        for target in (ContentTarget.MESSAGE_TEXT, ContentTarget.TOOL_OUTPUT):
            text, stats = stripper.strip_text(line, target)
            assert text == line, f"stripped near-miss: {line!r}"
            assert stats == {}


def test_artifact_matcher_stats_account_for_full_block_bytes() -> None:
    stripper = Stripper(RULESET_V1)
    blended = TASK_NOTIFICATION + COMPACTION_WRAPPER
    result = stripper.strip_turn(_turn(_msg_step(TurnRole.USER, blended)))
    assert result.turn.steps[0].content == ""
    assert result.stats.rules_hit["task-notification-block"] == 1
    assert result.stats.rules_hit["compaction-summary-wrapper"] == 1
    assert result.stats.noise_class_rate == 1.0
    assert result.stats.noise_matched_bytes == result.stats.noise_removed_bytes


# ---------------------------------------------------------------- ruleset safety


def test_prose_turn_comes_out_byte_identical() -> None:
    user_prose = (
        "\u6211\u89c9\u5f97\u8fd9\u4e2a API \u8bbe\u8ba1\u8fd8\u561f\u597d\u7684\uff0c"
        "added 3 packages in 5 minutes\u3002"
    )
    assistant_prose = "The build output should stay visible for the review."
    git_output = "git status --short\n M src/mnemoseed/capture/stripper.py\n?? tests/test_stripper.py\n"
    stripper = Stripper(RULESET_V1)
    turn = _turn(
        _msg_step(TurnRole.USER, user_prose),
        _msg_step(TurnRole.ASSISTANT, assistant_prose),
        _tool_step(git_output, name="Bash"),
    )
    result = stripper.strip_turn(turn)
    assert result.stats.rules_hit == {}
    assert result.stats.bytes_in == result.stats.bytes_out
    assert result.stats.compression_ratio == 0.0
    for original, stripped in zip(turn.steps, result.turn.steps, strict=True):
        assert stripped.content == original.content
    assert result.turn == turn


def test_prose_like_lines_never_matched_by_any_rule() -> None:
    stripper = Stripper(RULESET_V1)
    for line in SAFE_LINES:
        for target in (ContentTarget.MESSAGE_TEXT, ContentTarget.TOOL_OUTPUT):
            text, stats = stripper.strip_text(line, target)
            assert text == line, f"line stripped: {line!r} (hits {stats!r})"
            assert stats == {}

    for rule in RULESET_V1.rules:
        for line in SAFE_LINES:
            if rule.target is ContentTarget.BOTH:
                targets = (ContentTarget.MESSAGE_TEXT, ContentTarget.TOOL_OUTPUT)
            else:
                targets = (rule.target,)
            for target in targets:
                # strip_text per-rule is a proxy: run each rule family alone.
                single = _rule_set(rule)
                text, _ = Stripper(single).strip_text(line, target)
                assert text == line, f"rule {rule.id!r} stripped prose line {line!r}"


def test_rule_ordering_is_observable() -> None:
    a_general = _rule("a-general", StripAction.STRIP_LINE, pattern=r"^npm \S")
    b_specific = _rule("b-specific", StripAction.STRIP_LINE, pattern=r"^npm warn ")
    text = "npm warn deprecated x\nnpm info y\n"

    ordered = Stripper(_rule_set(a_general, b_specific)).strip_text(text, ContentTarget.TOOL_OUTPUT)
    assert ordered[0] == ""
    assert ordered[1]["a-general"] == 2
    assert "b-specific" not in ordered[1]

    reversed_ = Stripper(_rule_set(b_specific, a_general)).strip_text(text, ContentTarget.TOOL_OUTPUT)
    assert reversed_[0] == ""
    assert reversed_[1]["b-specific"] == 1
    assert reversed_[1]["a-general"] == 1


def test_target_filtering_per_content_kind() -> None:
    message_rule = _rule(
        "msg-only",
        StripAction.STRIP_LINE,
        pattern=r"^strip me$",
        target=ContentTarget.MESSAGE_TEXT,
    )
    tool_rule = _rule(
        "tool-only",
        StripAction.STRIP_LINE,
        pattern=r"^strip me$",
        target=ContentTarget.TOOL_OUTPUT,
    )
    stripper = Stripper(_rule_set(message_rule, tool_rule))
    turn = _turn(
        _msg_step(TurnRole.USER, "strip me"),
        _msg_step(TurnRole.ASSISTANT, "keep me"),
        _tool_step("strip me", name="Read"),
    )
    result = stripper.strip_turn(turn)
    contents = [step.content for step in result.turn.steps]
    assert contents == ["", "keep me", ""]


# ---------------------------------------------------------------- hot update and idempotence


def test_hot_reload_swap_takes_effect() -> None:
    turns_first = _rule_set(_rule("drop-x", StripAction.STRIP_LINE, pattern=r"^remove-x$"))
    turns_second = _rule_set(
        _rule("drop-x", StripAction.STRIP_LINE, pattern=r"^remove-x$"),
        _rule("drop-y", StripAction.STRIP_LINE, pattern=r"^remove-y$"),
    )
    stripper = Stripper(turns_first)
    sample = "remove-x\nremove-y\nkeep\n"

    result = stripper.strip_turn(_turn(_tool_step(sample)))
    assert result.turn.steps[0].content == "remove-y\nkeep\n"
    assert "drop-y" not in result.stats.rules_hit

    stripper.reload_rules(turns_second)
    result = stripper.strip_turn(_turn(_tool_step(sample)))
    assert result.turn.steps[0].content == "keep\n"
    assert "drop-y" in result.stats.rules_hit


def test_reload_does_not_retroactively_reprocess_pending_content() -> None:
    drafter = _rule_set(_rule("keep-only", StripAction.STRIP_LINE, pattern=r"^keep$"))
    clipper = _rule_set(_rule("clip", StripAction.STRIP_LINE, pattern=r"^clip$"))
    stripper = Stripper(drafter)
    first = stripper.strip_turn(_turn(_tool_step("first\nkeep\n")))
    assert first.turn.steps[0].content == "first\n"
    stripper.reload_rules(clipper)
    # reload must not reopen already-processed output; it only governs the next turn
    assert first.turn.steps[0].content == "first\n"


def test_reload_rejects_bad_ruleset_and_keeps_old_rules() -> None:
    stripper = Stripper(RULESET_V1)
    bad = _rule_set(_rule("broken", StripAction.STRIP_LINE, pattern=r"([unclosed"))
    with pytest.raises(StripperError):
        stripper.reload_rules(bad)
    result = stripper.strip_turn(_turn(_tool_step(NPM_LOG)))
    assert result.turn.steps[0].content == EXPECTED_NPM  # old ruleset still active


def test_reload_rejects_duplicate_rule_ids() -> None:
    stripper = Stripper(RULESET_V1)
    dup = _rule_set(
        _rule("same", StripAction.STRIP_LINE, pattern=r"^a$"),
        _rule("same", StripAction.STRIP_LINE, pattern=r"^b$"),
    )
    with pytest.raises(StripperError):
        stripper.reload_rules(dup)


def test_reload_rejects_missing_pattern_for_strip_line() -> None:
    stripper = Stripper(RULESET_V1)
    incomplete = _rule_set(_rule("nopattern", StripAction.STRIP_LINE, pattern=""))
    with pytest.raises(StripperError):
        stripper.reload_rules(incomplete)


def test_strip_turn_is_idempotent() -> None:
    stripper = Stripper(RULESET_V1)
    first = stripper.strip_turn(_turn(_tool_step(NPM_LOG), _tool_step(DEAD_LOOP)))
    second = stripper.strip_turn(first.turn)
    assert second.turn == first.turn
    assert second.stats.rules_hit == {}
    assert second.stats.bytes_in == first.stats.bytes_out


def test_strip_turn_never_mutates_input() -> None:
    stripper = Stripper(RULESET_V1)
    raw = _turn(_tool_step(NPM_LOG))
    before = raw.steps[0].content
    stripper.strip_turn(raw)
    assert raw.steps[0].content == before


# ---------------------------------------------------------------- edge inputs


# ---------------------------------------------------------------- noise-class rate (NFR-1.2)


def test_noise_class_rate_full_line_strip_is_one() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn(_tool_step(PIP_LOG)))
    assert result.turn.steps[0].content == ""
    assert result.stats.noise_matched_bytes == result.stats.bytes_in
    assert result.stats.noise_removed_bytes == result.stats.bytes_in
    assert result.stats.noise_class_rate == 1.0


def test_noise_class_rate_redact_span_counts_matched_bytes() -> None:
    stripper = Stripper(RULESET_V1)
    ansi_text = "before \x1b[1;31mred\x1b[0m after\nkeep\n"
    result = stripper.strip_turn(_turn(_tool_step(ansi_text)))
    # the rule matches the two escape sequences, not the styled word "red"
    escapes = "\x1b[1;31m" + "\x1b[0m"
    assert result.turn.steps[0].content == "before red after\nkeep\n"
    assert result.stats.matched_by_rule["ansi-codes"] == len(escapes.encode("utf-8"))
    assert result.stats.noise_matched_bytes == result.stats.noise_removed_bytes
    assert result.stats.noise_class_rate == 1.0


def test_noise_class_rate_collapse_run_counts_whole_block() -> None:
    stripper = Stripper(RULESET_V1)
    looped = "Error: connection refused\n" * 5
    result = stripper.strip_turn(_turn(_tool_step(looped)))
    unit = len(b"Error: connection refused\n")
    # a run of 5 identical units is flagged as a whole block: matched bytes
    # count all 5 units, removed bytes count the 4 dropped after the canonical
    assert result.turn.steps[0].content == "Error: connection refused\n"
    assert result.stats.noise_matched_bytes == 5 * unit
    assert result.stats.noise_removed_bytes == 4 * unit
    assert result.stats.noise_class_rate == pytest.approx(4 / 5)


def test_noise_class_rate_zero_when_nothing_matched() -> None:
    stripper = Stripper(RULESET_V1)
    prose = "the report should stay visible\n"
    result = stripper.strip_turn(_turn(_tool_step(prose)))
    assert result.stats.rules_hit == {}
    assert result.stats.noise_matched_bytes == 0
    assert result.stats.noise_class_rate == 0.0


def test_noise_class_rate_never_counts_kept_bytes() -> None:
    stripper = Stripper(RULESET_V1)
    text = "npm warn deprecated x\nkeep this line\n"
    result = stripper.strip_turn(_turn(_tool_step(text)))
    # only the matched line feeds the denominator; the kept line never appears
    expected_matched = len(b"npm warn deprecated x\n")
    assert result.stats.noise_matched_bytes == expected_matched
    assert result.stats.noise_class_rate == 1.0


def test_empty_turn_and_empty_text() -> None:
    stripper = Stripper(RULESET_V1)
    result = stripper.strip_turn(_turn())
    assert result.turn.steps == []
    assert (result.stats.bytes_in, result.stats.bytes_out) == (0, 0)
    text, stats = stripper.strip_text("", ContentTarget.TOOL_OUTPUT)
    assert text == ""
    assert stats == {}


def test_unicode_bytes_accounting() -> None:
    stripper = Stripper(RULESET_V1)
    mixed = "\u4e2d\u6587\u6d4b\u8bd5 \x1b[31mabc\x1b[0m \U0001f600\t\u884c"
    result = stripper.strip_turn(_turn(_tool_step(mixed)))
    text = result.turn.steps[0].content
    # ansi removed only; every era of unicode (BMP, astral, CJK) intact
    assert "\U0001f600" in text
    assert "\u4e2d\u6587\u6d4b\u8bd5" in text
    assert result.stats.bytes_in == len(mixed.encode("utf-8"))
    assert result.stats.bytes_out == len(text.encode("utf-8"))


def test_huge_dead_loop_input_finishes_and_collapses() -> None:
    huge = TRACEBACK_BLOCK * 4000  # ~1 MB of repeated blocks
    stripper = Stripper(RULESET_V1)
    started = time.perf_counter()
    result = stripper.strip_turn(_turn(_tool_step(huge)))
    elapsed = time.perf_counter() - started
    assert result.turn.steps[0].content == TRACEBACK_BLOCK
    assert elapsed < 10.0
