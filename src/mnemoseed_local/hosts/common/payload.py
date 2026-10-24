"""The additionalContext payload envelope shared by injecting hosts."""

from __future__ import annotations

from typing import Any


def additional_context_payload(block: str, hook_event_name: str) -> dict[str, Any]:
    """The JSON-stdout shape a host reads for mid-session injection."""
    return {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": block,
        }
    }
