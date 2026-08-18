"""Built-in DreamLLM drivers (PRD-02 T6; FR-2.14 / FR-2.7), local MVP set.

Importing this package registers every driver into ``mnemoseed_local.llm.registry``
(import side effect, as the storage package does): openai_compatible
(cloud-compatible class), ollama (local offline track), stub (deterministic
offline seam for tests and the manual-first phase), and stub_verifier (the
B1 ensemble-verify judging-seat equivalent, tests only).
"""

from __future__ import annotations

from mnemoseed_local.llm.drivers import (  # noqa: F401 - import side effect registers drivers
    ollama,
    openai_compatible,
    stub,
    stub_verifier,
)

__all__ = ["ollama", "openai_compatible", "stub", "stub_verifier"]
