"""Deterministic offline verifier DreamLLM driver (B1 T3; tests only).

The mirror of ``stub.py`` for the ensemble verify seat: wraps the T2
``StubVerifyLLM`` judge into the full DreamLLM port (ChatResult + typed health
probe) so a config route can select it by name (``driver = "stub_verifier"``).
The judging quality is exactly the deterministic evidence-presence rule's, so
this driver is only for tests — never a production default (the factory
verifier route stays on the local ollama track, config.py).

Construction performs no network I/O and the dream package is imported lazily
inside ``__init__`` (same cycle-avoidance as stub.py: ``mnemoseed_local.dream``
imports ``mnemoseed_local.llm.types`` via ``dream.verify``).
"""

from __future__ import annotations

from typing import Any

from mnemoseed_local.llm.registry import LLM_DRIVERS, register
from mnemoseed_local.llm.types import ChatResult, HealthReport, LLMDriverInfo


@register(LLM_DRIVERS)
class StubVerifierLLM:
    """DreamLLM delegating to the deterministic offline StubVerifyLLM judge."""

    info = LLMDriverInfo(
        name="stub_verifier",
        description="deterministic offline verifier DreamLLM (delegates to dream.verify.StubVerifyLLM)",
    )

    def __init__(self, model: str = "stub_verifier", **kwargs: Any) -> None:
        # Lazy import (see module docstring): resolves to whatever the verify
        # seam currently names StubVerifyLLM (tests may swap it through
        # monkeypatch on mnemoseed_local.dream.verify).
        from mnemoseed_local.dream.verify import StubVerifyLLM

        self._llm = StubVerifyLLM()
        self.model = model
        self.params: dict[str, Any] = kwargs

    def chat(self, *, system: str, user: str) -> ChatResult:
        text = self._llm.chat(system=system, user=user)
        return ChatResult(text=text, model=self.model, driver="stub_verifier")

    def check(self) -> HealthReport:
        return HealthReport(ok=True, detail={"model": self.model})
