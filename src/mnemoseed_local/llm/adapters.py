"""ReflectLLM adapter: a full DreamLLM over the narrow T3 str-returning seam.

T6's ``ReflectOrchestrator`` accepts ChatResults directly through the widened
``reflect.ChatLLM`` seam, but any call site that typed strictly against a
str-returning chat keeps working through this tiny adapter.
"""

from __future__ import annotations

from mnemoseed_local.llm.types import DreamLLM


class ReflectLLMAdapter:
    """Wrap a DreamLLM so its ``chat`` returns plain text, for T3 seams."""

    def __init__(self, llm: DreamLLM) -> None:
        self._llm = llm

    def chat(self, *, system: str, user: str) -> str:
        """Delegate to the driver and return only its text payload."""
        return self._llm.chat(system=system, user=user).text
