"""The MCP handshake instruction block and the paste-ready surface blocks.

The handshake block is one template whose tool names are interpolated from the
gateway's own surface, so "the text names only tools the gateway serves" holds by
construction: the renderer refuses a name the gateway does not have. The blocks
``instructions --surface`` prints reuse the same template, so the four surfaces
cannot drift apart.
"""

from __future__ import annotations

import argparse

import pytest

from mnemoseed_local import cli
from mnemoseed_local.mcp_gateway import instructions
from mnemoseed_local.mcp_gateway.server import TOOLS

GATEWAY_TOOL_NAMES = [str(tool["name"]) for tool in TOOLS]

#: The surfaces ``instructions --surface`` answers.
SURFACES = ("claude_chat", "chatgpt", "claude_code", "generic")

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


def test_the_block_carries_every_required_phrase() -> None:
    text = _flatten(_surfaced("generic"))
    for phrase in REQUIRED_PHRASES:
        assert phrase in text, phrase


def test_the_block_states_the_four_intentions_as_situations_and_actions() -> None:
    """Every rule is a situation -> action pair (the implementation-intention
    shape); a capability statement ("you can use X") is not one."""
    text = _flatten(_surfaced("generic"))
    for name in ("recall", "capture", "remember", "supersede"):
        assert name in text, name
    # intention 1: with an injected block, use it and do not re-query
    assert "already injected" in text
    # intention 2: no block -> recall first, in the user's own words, once
    assert "One call per topic" in text
    # intention 3: no block + a durable statement -> capture, never speculation
    assert "Never capture" in text and "guesses" in text
    # intention 4: an explicit ask -> remember; replacement -> supersede
    assert "explicitly asks to remember" in text


def test_the_block_claims_no_activation() -> None:
    """No surface may report that the text reached a model, or that memory is
    active: the MCP specification only says a client MAY use ``instructions``."""
    lowered = _flatten(_surfaced("generic")).lower()
    for forbidden in ("memory active", "is active", "activated", "will call"):
        assert forbidden not in lowered, forbidden


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
