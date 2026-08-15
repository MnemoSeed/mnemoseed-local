"""Deterministic offline DreamLLM driver (tests + M1 manual-first fallback).

The dream engine's offline seam (design/02 section 8): wraps the T3
``StubReflectLLM`` into the full T6 DreamLLM port (ChatResult + typed health
probe) so a config route can select it by name (``driver = "stub"``). The
reflection quality is exactly the deterministic rule-based stub's, so this
driver is only for tests and the manual review phase — never a production
default (FR-2.14 keeps the defaults on the real providers).

Construction performs no network I/O and the dream package is imported lazily
inside ``__init__``: importing the dream engine here at module import time
would be a cycle (``mnemoseed_local.dream`` imports ``mnemoseed_local.llm.types`` via
``dream.reflect``), and the daemon must be able to build this driver before
the reflect seam exists.
"""

from __future__ import annotations

from typing import Any

from mnemoseed_local.llm.registry import LLM_DRIVERS, register
from mnemoseed_local.llm.types import ChatResult, HealthReport, LLMDriverInfo


@register(LLM_DRIVERS)
class StubLLM:
    """DreamLLM delegating to the deterministic offline StubReflectLLM."""

    info = LLMDriverInfo(
        name="stub",
        description="deterministic offline DreamLLM (delegates to dream.reflect.StubReflectLLM)",
    )

    def __init__(self, model: str = "stub", **kwargs: Any) -> None:
        # Lazy import (see module docstring): resolves to whatever the reflect
        # seam currently names StubReflectLLM (tests swap it for a counting stub
        # through monkeypatch on mnemoseed_local.dream.reflect).
        from mnemoseed_local.dream.reflect import StubReflectLLM

        self._llm = StubReflectLLM()
        self.model = model
        self.params: dict[str, Any] = kwargs

    def chat(self, *, system: str, user: str) -> ChatResult:
        text = self._llm.chat(system=system, user=user)
        return ChatResult(text=text, model=self.model, driver="stub")

    def check(self) -> HealthReport:
        return HealthReport(ok=True, detail={"model": self.model})
