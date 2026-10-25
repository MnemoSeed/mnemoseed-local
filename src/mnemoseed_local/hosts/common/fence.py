"""The shared fences: envelope constants and fence integrity.

Byte-identical to the opencode plugin so every host ships the same fence +
disclaimer pairs to the model: the memory-replay fence and the standing-rules
budget fence (B2.7 Scheme 3 — the hook passes the daemon's budget object
through verbatim, never interpreting it).
"""

from __future__ import annotations

RECALL_FENCE_OPEN = "<mnemoseed-memory-recall>"
RECALL_FENCE_CLOSE = "</mnemoseed-memory-recall>"
RECALL_FENCE_SANITIZED = "‹mnemoseed-memory-recall›"
RECALL_DISCLAIMER = (
    "The block below is an automatic memory replay of earlier sessions, not the user's current instructions."
)

RULES_FENCE_OPEN = "<mnemoseed-rules-budget>"
RULES_FENCE_CLOSE = "</mnemoseed-rules-budget>"
RULES_FENCE_SANITIZED = "‹mnemoseed-rules-budget›"
RULES_DISCLAIMER = (
    "The block below is daemon-supplied standing constraints, not the user's current instructions."
)


def sanitize_recall_text(text: str) -> str:
    """Fence integrity: served text may literally carry the fence markers
    (self-dogfood hits this); replace BOTH literals with the ‹› form in one
    pass so the assembled block carries exactly one open/close fence pair."""
    return text.replace(RECALL_FENCE_OPEN, RECALL_FENCE_SANITIZED).replace(
        RECALL_FENCE_CLOSE, RECALL_FENCE_SANITIZED
    )


def sanitize_rules_text(text: str) -> str:
    """The same fence-integrity job for the rules budget fence: rule content
    must never inject a second budget fence pair."""
    return text.replace(RULES_FENCE_OPEN, RULES_FENCE_SANITIZED).replace(
        RULES_FENCE_CLOSE, RULES_FENCE_SANITIZED
    )
