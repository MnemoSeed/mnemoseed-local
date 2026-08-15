"""DreamLLM port contract (PRD-02 T6; FR-2.14, FR-2.6).

The DreamLLM port is the dream engine's cloud-LLM seam: a narrow chat call, a
typed health probe for the console's live-check button (design/07 section 8), and a
provider-reported usage record the delta cost layer (T5, NFR-2.2) can reconcile
against its local estimator. Every driver in ``mnemoseed_local.llm.drivers``
implements this port; T3's ``ReflectOrchestrator`` consumes it through the
widened chat seam (``reflect.ChatLLM``).

Degradation is typed (FR-2.6): any transport or auth failure inside ``chat``
raises ``LLMUnavailable``, and ``check`` never raises — it returns a
``HealthReport``. ``OAuthNotImplemented`` is an ``LLMUnavailable`` subclass so
an OAuth provider with no implementation (e.g. an Anthropic subscription,
which is deliberately not supported) degrades through the same typed branch
callers already catch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol


@dataclass(frozen=True)
class Usage:
    """Provider-reported token counts. Fields are optional and per-provider:
    OpenAI reports prompt/completion; Anthropic reports input/output plus the
    two cache legs; Ollama's native API reports prompt_eval_count / eval_count.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


@dataclass(frozen=True)
class ChatResult:
    """One chat completion: the text plus optional provider-reported usage."""

    text: str
    usage: Usage | None = None
    model: str = ""
    driver: str = ""


@dataclass(frozen=True)
class HealthReport:
    """Typed connectivity probe result; never raised, always returned."""

    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMDriverInfo:
    """Driver identity for the LLM registry."""

    name: str
    description: str = ""


class DreamLLM(Protocol):
    """The full T6 chat port drivers implement.

    T3's ``ReflectOrchestrator`` accepts any object whose ``chat`` returns a
    ChatResult OR plain text (``reflect.ChatLLM``); this protocol is the driver
    surface itself, with the extra ``check()`` probe.
    """

    info: ClassVar[LLMDriverInfo]

    def chat(self, *, system: str, user: str) -> ChatResult: ...
    def check(self) -> HealthReport: ...


class LLMError(Exception):
    """Base LLM-layer error."""


class LLMUnavailable(LLMError):
    """A provider chat call failed on transport or auth (FR-2.6)."""


class LLMRouteError(LLMError):
    """Role routing: no configured route for the requested role."""


class UnknownLLMDriverError(LLMError):
    """A driver name no registered LLM driver provides."""

    def __init__(self, layer: str, driver: str, available: Sequence[str]) -> None:
        if available:
            # This type is llm-layer specific, so the driver namespace is "llm
            # driver" regardless of the registry instance name.
            message = f"unknown llm driver {driver!r} (available: {', '.join(available)})"
        else:
            message = f"unknown {layer} driver {driver!r} (no {layer} drivers registered)"
        super().__init__(message)


class OAuthNotImplemented(LLMUnavailable):
    """This OAuth provider has no implementation here (only codex/grok are built)."""
