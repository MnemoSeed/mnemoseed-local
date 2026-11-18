"""The MCP handshake instruction block and the paste-ready surface blocks.

The handshake block is one template whose tool names are interpolated from the
gateway's own surface, so "the text names only tools the gateway serves" holds by
construction: the renderer refuses a name the gateway does not have. The blocks
``instructions --surface`` prints reuse the same template, so the four surfaces
cannot drift apart.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from string import Formatter

import pytest

from mnemoseed_local import cli
from mnemoseed_local.mcp_gateway import instructions
from mnemoseed_local.mcp_gateway.server import TOOLS

GATEWAY_TOOL_NAMES = [str(tool["name"]) for tool in TOOLS]

#: The surfaces ``instructions --surface`` answers.
SURFACES = ("claude_chat", "chatgpt", "claude_code", "generic")

#: AC2's oracle: the handshake block as the contract holds it. The served text
#: must be exactly this template with its placeholders resolved, so an
#: identifier that did not come from the placeholder set can never reach it.
#: The module's constants stay authoritative; this copy is the contract pin and
#: changes only when the frozen text is deliberately amended.
CONTRACT_PREAMBLE = "MnemoSeed is this machine's long-term memory for you, shared across sessions and hosts."

CONTRACT_BODY = """\
The rules below are situations and actions, not suggestions:

1. If a memory block was already injected, use it as your memory channel; do not
   call {recall} again for the same topic.
2. If no memory block was injected and this client has no capture hook, you are
   the memory channel yourself. For past work, a decision, or a stated
   preference, call {recall} first with the user's own words; never use synonyms.
   One call per topic suffices.
3. No memory block, this client has no capture hook, and the user states a
   durable decision, preference, or constraint: call {capture} with their words
   verbatim. Never capture your own guesses, drafts, or reasoning.
4. If the user explicitly asks to remember or pin something, call {remember};
   call {supersede} only to replace one specific belief.
5. Entry failure not absence: retry once via 'mnemoseed-local mcp' (stdio, own
   credential); negatives: observed_at, reverify.

6. Recall missed: log it with {recall_feedback}(not_useful, ISO observed_at);
   the signal never reranks.

An empty result means no relevant memory exists: say so and continue; do not
guess or repeat the query.
{recent_sessions} unresolved: resolution failed, not "no relevant history";
follow next_step (bind, add resume_query id).

A pin is one the user asked to keep: provenance_source "memory.remember" or
explicit_pin true. A captured entry comes from a conversation: explicit_pin
false.
"""

#: Phrases the delivery contract requires: the injected-block intention, the
#: recall wording, the no-speculation rule, the empty-result meaning and the
#: pin-versus-captured distinction (the vocabulary SKILL.md already uses, so no
#: second one is invented).
REQUIRED_PHRASES = (
    "memory channel",
    "the user's own words",
    "verbatim",
    "empty result",
    "explicit_pin",
    "memory.remember",
    "resolution failed",
    "next_step",
)


def _surfaced(surface: str) -> str:
    return instructions.surface_text(surface, GATEWAY_TOOL_NAMES)


def _flatten(text: str) -> str:
    """The block is hard-wrapped; phrase assertions read it unwrapped."""
    return " ".join(text.split())


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and name in action.choices:
            return action.choices[name]
    raise AssertionError(f"no {name!r} subparser")


# ---------------------------------------------------------------- the block itself


def test_the_block_stays_inside_the_length_ceiling() -> None:
    assert len(_surfaced("generic")) <= instructions.MAX_CHARS
    assert instructions.MAX_CHARS == 1800


def test_every_mentioned_tool_exists_in_the_gateway_surface() -> None:
    """Structural, not a scan of free text: the placeholders are a declared
    constant, the renderer refuses to produce text for a name the gateway does
    not serve, and the produced text carries every one of them resolved."""
    assert set(instructions.TOOL_PLACEHOLDERS) <= set(GATEWAY_TOOL_NAMES)

    text = _surfaced("generic")
    for name in instructions.TOOL_PLACEHOLDERS:
        assert name in text
    assert "{" not in text and "}" not in text  # nothing left unresolved

    for missing in instructions.TOOL_PLACEHOLDERS:
        absent = [name for name in GATEWAY_TOOL_NAMES if name != missing]
        with pytest.raises(ValueError):
            instructions.render(absent)


def test_every_tool_identifier_in_the_rendered_block_comes_from_the_placeholders() -> None:
    """AC2: the template's references are exactly its placeholders, and the
    served block is that template round-tripped — the comparison is against the
    contract template, never a scan of the free prose, so appending a dead tool
    name anywhere in the text fails here."""
    resolved = CONTRACT_BODY.format(**{name: name for name in instructions.TOOL_PLACEHOLDERS})

    assert _surfaced("generic") == f"{CONTRACT_PREAMBLE}\n\n{resolved}".rstrip()
    for surface in ("claude_chat", "chatgpt", "claude_code"):
        assert _surfaced(surface).endswith(resolved.rstrip()), surface

    fields = {field for _, field, _, _ in Formatter().parse(instructions.BODY) if field is not None}
    assert fields == set(instructions.TOOL_PLACEHOLDERS)


def test_the_block_carries_every_required_phrase() -> None:
    text = _flatten(_surfaced("generic"))
    for phrase in REQUIRED_PHRASES:
        assert phrase in text, phrase


def test_the_block_states_the_six_intentions_as_situations_and_actions() -> None:
    """Every rule is a situation -> action pair (the implementation-intention
    shape); a capability statement ("you can use X") is not one."""
    text = _flatten(_surfaced("generic"))
    for name in ("recall", "capture", "remember", "supersede", "recent_sessions"):
        assert name in text, name
    # intention 1: with an injected block, use it and do not re-query
    assert "already injected" in text
    # intention 2: no block -> recall first, in the user's own words, once
    assert "One call per topic" in text
    # intention 3: no block + a durable statement -> capture, never speculation
    assert "Never capture" in text and "guesses" in text
    # intention 4: an explicit ask -> remember; replacement -> supersede
    assert "explicitly asks to remember" in text
    # intention 5: an entry failure is not absence; retry via the stdio gateway
    assert "Entry failure" in text and "not absence" in text
    assert "mnemoseed-local mcp" in text and "observed_at" in text
    # intention 6: an unresolved recovery is a resolution failure, not an empty recall
    assert "unresolved: resolution failed" in text and "next_step" in text


def test_entry_failure_rule_fits_every_surface_below_the_ceiling() -> None:
    """The one added rule stays <=122 chars, and all five surfaces render."""
    import re

    resolved = _surfaced("generic")
    match = re.search(r"5\. (.*?)\n\n", resolved, re.S)
    assert match is not None
    rule = " ".join(match.group(1).split())
    assert len(rule) <= 122, len(rule)
    for phrase in (
        "Entry failure",
        "not absence",
        "mnemoseed-local mcp",
        "retry once",
        "observed_at",
        "reverify",
    ):
        assert phrase in rule, phrase
    for surface in SURFACES:
        text = instructions.surface_text(surface, GATEWAY_TOOL_NAMES)
        assert len(text) <= instructions.MAX_CHARS, surface


def test_namespace_absent_still_recalls_via_stdio_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-A3: the host registers no MCP tools, yet the stdio gateway recalls."""
    import io
    import json

    from mnemoseed_local import cli as cli_module
    from mnemoseed_local.mcp_gateway import server

    config = tmp_path / "opencode.json"
    config.write_text('{"mcp": {}}', encoding="utf-8")
    monkeypatch.setattr("mnemoseed_local.cli.OPENCODE_CONFIG_PATH", config)
    assert cli_module._opencode_mcp_registered(config) is False

    payload = {"memory": {"entries": [{"kind": "fact", "text": "durable recall", "score": 0.9}]}}

    class _Client:
        profile_id = "default"

        def post(self, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
            del path, body
            return payload

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "recall", "arguments": {"query": "durable recall"}},
    }
    stdin = io.StringIO(json.dumps(request) + "\n")
    stdout = io.StringIO()
    code = server.serve(stdin=stdin, stdout=stdout, client=_Client())  # type: ignore[arg-type]
    assert code == 0
    result = json.loads(stdout.getvalue())["result"]
    assert result["isError"] is False
    assert "durable recall" in result["content"][0]["text"]


def test_the_block_distinguishes_unresolved_recovery_from_no_history() -> None:
    """An empty recall result means no relevant memory; an unresolved recovery is a
    resolution failure that must be answered with its next_step, not 'no history'."""
    text = _flatten(_surfaced("generic"))
    assert "unresolved: resolution failed" in text
    assert 'not "no relevant history"' in text
    assert "next_step" in text
    assert "resume_query" in text


def test_the_block_claims_no_activation() -> None:
    """No surface may report that the text reached a model, or that memory is
    active: the MCP specification only says a client MAY use ``instructions``."""
    lowered = _flatten(_surfaced("generic")).lower()
    for forbidden in ("memory active", "is active", "activated", "will call"):
        assert forbidden not in lowered, forbidden


def test_the_handshake_block_states_the_hookless_premise_only_as_a_condition() -> None:
    """``initialize.instructions`` reaches hook-capable clients too, so the
    handshake block may never assert that the surface has no capture hook: the
    premise appears only inside the conditional rules."""
    text = _flatten(_surfaced("generic"))
    assert "this surface has no capture hook" not in text
    assert "no memory block was injected and this client has no capture hook" in text
    assert "this client has no capture hook, and the user states" in text


def test_the_block_carries_no_markup_or_machine_specific_path() -> None:
    """It is pasted into free-text preference fields: plain prose, no fence, no
    absolute path that would only resolve on the machine that printed it."""
    text = _surfaced("generic")
    assert "```" not in text
    assert not text.lstrip().startswith("{")
    assert "\\\\" not in text


def test_an_unknown_surface_is_refused() -> None:
    with pytest.raises(ValueError):
        instructions.surface_text("no_such_surface", GATEWAY_TOOL_NAMES)


# ---------------------------------------------------------------- the CLI surface


@pytest.mark.parametrize("surface", SURFACES)
def test_instructions_verb_prints_a_paste_ready_block(
    surface: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["instructions", "--surface", surface]) == 0
    out = capsys.readouterr().out
    assert out.strip()
    for name in instructions.TOOL_PLACEHOLDERS:
        assert name in out


def test_the_four_surface_blocks_are_distinct(capsys: pytest.CaptureFixture[str]) -> None:
    printed: dict[str, str] = {}
    for surface in SURFACES:
        assert cli.main(["instructions", "--surface", surface]) == 0
        printed[surface] = capsys.readouterr().out.strip()
    assert len(set(printed.values())) == len(SURFACES)


def test_each_block_adapts_the_framing_to_its_surface(capsys: pytest.CaptureFixture[str]) -> None:
    frames = {
        "claude_chat": "personal preferences",
        "chatgpt": "custom instructions",
        "claude_code": "capture hook",
    }
    for surface, frame in frames.items():
        assert cli.main(["instructions", "--surface", surface]) == 0
        out = capsys.readouterr().out.lower()
        assert frame in out
        assert "memory active" not in out


def test_the_generic_block_is_the_handshake_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["instructions", "--surface", "generic"]) == 0
    assert capsys.readouterr().out.strip() == instructions.gateway_text().strip()


def test_the_cli_surface_choices_derive_from_the_module_table() -> None:
    choices = next(
        action.choices
        for action in _subparser(cli.build_parser(), "instructions")._actions
        if action.dest == "surface"
    )
    assert tuple(choices) == instructions.SURFACES == SURFACES
