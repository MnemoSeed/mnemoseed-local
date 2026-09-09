"""Shared text normalization and payload keys for experience evaluation.

Transport-only helpers over plain JSON dicts. No storage carriers, no
daemon/config/provider reads; stdlib-only so evaluation tests stay hermetic.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Any

# Frozen intention statuses, mirrored from the node-payload validator so this
# module stays stdlib-only.
INTENTION_STATUSES = frozenset({"pending", "fired", "cancelled"})

# Trailing sentence-punctuation chars removed at most once by normalize_exp:
# ASCII . ! ? ; : , plus Chinese sentence punctuation.
_TRAILING_PUNCT = frozenset(".!?;:,\u3002\u3001\uff0c\uff1b\uff1a\uff01\uff1f")

_KINDS = ("fact", "lesson", "intention", "skill_sequence")


def normalize_exp(text: str) -> str:
    """Fold evaluation text to a canonical form for comparison."""
    if not isinstance(text, str):
        raise ValueError(f"normalize_exp expects str, got {type(text).__name__}")
    collapsed = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    if collapsed and collapsed[-1] in _TRAILING_PUNCT:
        collapsed = collapsed[:-1]
    return collapsed.rstrip()


def payload_key(kind: str, payload: dict[str, Any]) -> tuple[Any, ...]:
    """Return a deterministic structural key for an experience payload.

    Only the required semantic fields contribute; extra payload entries are
    ignored but never removed. Unknown kinds and bad shapes raise ValueError.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"payload_key expects a dict payload, got {type(payload).__name__}")
    if kind == "fact":
        subject, predicate, obj, polarity = _require_str_fields(
            payload, ("subject", "predicate", "object", "polarity"), kind
        )
        return ("fact", subject, predicate, obj, polarity)
    if kind == "lesson":
        (text,) = _require_str_fields(payload, ("text",), kind)
        return ("lesson", text)
    if kind == "intention":
        trigger, action = _require_str_fields(payload, ("trigger_condition", "action"), kind)[:2]
        status = payload.get("status")
        if not isinstance(status, str) or status not in INTENTION_STATUSES:
            raise ValueError(f"intention payload has invalid status {status!r}")
        return ("intention", trigger, action, normalize_exp(status))
    if kind == "skill_sequence":
        (task_type,) = _require_str_fields(payload, ("task_type",), kind)
        chain = payload.get("tool_chain")
        if not isinstance(chain, list):
            raise ValueError(
                f"skill_sequence payload field 'tool_chain' must be list, got {type(chain).__name__}"
            )
        rate = payload.get("success_rate")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise ValueError(f"skill_sequence payload has invalid success_rate {rate!r}")
        if isinstance(rate, float) and not math.isfinite(rate):
            raise ValueError(f"skill_sequence payload has invalid success_rate {rate!r}")
        return ("skill_sequence", task_type, _freeze(chain), rate)
    raise ValueError(f"unknown experience kind {kind!r}")


def _require_str_fields(payload: dict[str, Any], names: tuple[str, ...], kind: str) -> tuple[str, ...]:
    """Fetch required text fields normalized, rejecting missing/non-string ones."""
    values: list[str] = []
    for name in names:
        value = payload.get(name)
        if not isinstance(value, str):
            raise ValueError(f"{kind} payload field {name!r} must be str, got {type(value).__name__}")
        values.append(normalize_exp(value))
    return tuple(values)


def _freeze(value: Any) -> Any:
    """Freeze a JSON value into a hashable, order-stable structural form."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("num", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number {value!r}")
        return ("num", value)
    if isinstance(value, str):
        return normalize_exp(value)
    if value is None:
        return ("none", None)
    if isinstance(value, list):
        return ("list", tuple(_freeze(item) for item in value))
    if isinstance(value, dict):
        frozen = [(_freeze_key(key), _freeze(item)) for key, item in value.items()]
        return ("dict", tuple(sorted(frozen, key=repr)))
    raise ValueError(f"non-JSON value of type {type(value).__name__}")


def _freeze_key(key: Any) -> Any:
    """Freeze a mapping key, keeping bool and number keys distinct."""
    if isinstance(key, bool):
        return ("bool", key)
    if isinstance(key, int):
        return ("num", key)
    if isinstance(key, float):
        if not math.isfinite(key):
            raise ValueError(f"non-finite JSON number {key!r}")
        return ("num", key)
    if isinstance(key, str):
        return ("str", key)
    if key is None:
        return ("none", None)
    raise ValueError(f"non-JSON mapping key of type {type(key).__name__}")
