"""The memory protocol as text: the handshake block (L1) and the paste-ready
surface blocks (L3).

One template plus a tool-name mapping produce every block. Tool names are
placeholders resolved from the gateway's own ``TOOLS``, and :func:`render`
refuses a name the gateway does not serve, so "the text only names tools that
exist" is a structural property of the builder rather than a review of free
text. The stdio gateway and the loopback ``POST /mcp`` route both answer
``initialize`` through ``handle_message``, so they serve the same bytes.

Each rule is a situation -> action pair, and the recall rule asks for the
user's own words rather than synonyms. The block is best-effort delivery: the
MCP specification defines ``InitializeResult.instructions`` as optional, so no
text here and no surface that prints it may claim the block was ingested.
"""

from __future__ import annotations

from collections.abc import Iterable

#: The tool identifiers the template may interpolate. A placeholder outside this
#: set is unreachable; one inside it that the gateway does not serve makes
#: :func:`render` refuse instead of emitting a dead reference.
TOOL_PLACEHOLDERS = ("recall", "capture", "remember", "supersede")

#: Length ceiling: the block rides the system prompt of every session, so it has
#: to stay short. A block past it is refused rather than silently shipped.
MAX_CHARS = 1800

#: The surfaces ``instructions --surface`` answers, in print order.
SURFACES = ("claude_chat", "chatgpt", "claude_code", "generic")

#: The generic lead: the block exactly as the handshake serves it.
PREAMBLE = "MnemoSeed is this machine's long-term memory for you, shared across sessions and hosts."

#: The four execution intentions, the meaning of an empty result, and the
#: pin-versus-captured distinction. ``{recall}``-style placeholders resolve to
#: the identifiers in ``TOOL_PLACEHOLDERS``.
BODY = """\
The rules below are situations and actions, not suggestions:

1. If a memory block was already injected into this conversation, that block is
   your memory channel: use it. Do not call {recall} again for the same topic.
2. If no memory block was injected and this client has no capture hook, you are
   the memory channel yourself. When the user mentions past work, an earlier
   decision, or a stated preference, call {recall} first, querying with the
   user's own words (names, paths, issue numbers, the wording they used); never
   rewrite the query into your own synonyms. One call per topic is enough.
3. No memory block, this client has no capture hook, and the user states a
   durable decision, preference, or constraint: call {capture} with their words
   quoted verbatim. Never capture your own guesses, drafts, or reasoning.
4. If the user explicitly asks to remember or pin something, call {remember}.
   Call {supersede} only when the user asks to replace one specific belief.

An empty result means no relevant memory exists: say so plainly and continue.
Do not guess, and do not repeat the query.

A pin is an entry the user explicitly asked to keep: provenance_source is
"memory.remember", or explicit_pin is true. A captured entry is one written
from a conversation instead, with explicit_pin false.
"""

#: The per-surface leads: the entry point a user actually pastes into, plus this
#: surface's own caveat. Each block is one lead followed by the shared body, so
#: the four surfaces cannot drift apart and no lead may claim the text was
#: ingested.
SURFACE_PREAMBLES: dict[str, str] = {
    "claude_chat": (
        "Paste this into Claude's personal preferences (Settings > Profile). Those preferences "
        "are account-level and cloud-synced, so this text travels with the account; the memory "
        "tools come from the mnemoseed MCP server connected on this machine, and this chat "
        "surface has no capture hook - memory is written only when the assistant calls them."
    ),
    "chatgpt": (
        "Paste this into ChatGPT's custom instructions (Settings > Personalization). Custom "
        "instructions are account-level and cloud-synced, and this surface cannot register the "
        "local mnemoseed MCP server: follow the rules below wherever a mnemoseed MCP server is "
        "connected, and where no memory tool is available say plainly that this surface has no "
        "memory instead of guessing."
    ),
    "claude_code": (
        "Keep this in a CLAUDE.md or project instruction file. Claude Code has the mnemoseed "
        "capture hook, so an injected memory block is the normal case (rule 1); the tools below "
        "are the deliberate supplement for the sessions that start without one."
    ),
}


def render(tool_names: Iterable[str], *, preamble: str = PREAMBLE) -> str:
    """The block: ``preamble`` plus the template with every tool name resolved.

    Raises ``ValueError`` when a placeholder names a tool the caller's surface
    does not serve, so a renamed or removed tool can never leave a dead
    reference in the text a client is about to read.
    """
    available = set(tool_names)
    missing = [name for name in TOOL_PLACEHOLDERS if name not in available]
    if missing:
        raise ValueError(
            "the instruction text names tools this surface does not serve: " + ", ".join(missing)
        )
    body = BODY.format(**{name: name for name in TOOL_PLACEHOLDERS})
    block = f"{preamble}\n\n{body}".rstrip()
    if len(block) > MAX_CHARS:
        raise ValueError(f"the instruction block is {len(block)} chars, over the {MAX_CHARS} ceiling")
    return block


def surface_text(surface: str, tool_names: Iterable[str]) -> str:
    """The paste-ready block for one surface: that surface's lead plus the
    shared body. The generic surface IS the handshake block."""
    if surface == "generic":
        return render(tool_names)
    try:
        preamble = SURFACE_PREAMBLES[surface]
    except KeyError:
        raise ValueError(
            f"unknown instruction surface {surface!r}; expected one of {', '.join(SURFACES)}"
        ) from None
    return render(tool_names, preamble=preamble)


def tool_names() -> list[str]:
    """The gateway's tool names, read off its own surface (the single binding
    between the tool set and the text that names it)."""
    from mnemoseed_local.mcp_gateway.server import TOOLS

    return [str(tool["name"]) for tool in TOOLS]


def gateway_text(surface: str = "generic") -> str:
    """The handshake block, or one surface's paste-ready block, for this gateway."""
    return surface_text(surface, tool_names())
