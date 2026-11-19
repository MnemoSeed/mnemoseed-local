"""Shared scalar coercion for provider-reported token counts.

Drivers decode untrusted 2xx bodies: a count may arrive as an int, a numeric
string, or garbage such as a nested object. Every count is reduced to an int or
None here so a non-scalar never reaches the dream usage path.
"""

from __future__ import annotations

from typing import Any

from mnemoseed_local.llm.types import Usage


def coerce_count(value: Any) -> int | None:
    """Coerce a reported count to int; malformed values degrade to None."""
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def build_usage(
    prompt: Any,
    completion: Any,
    cache_read: Any = None,
    cache_creation: Any = None,
) -> Usage | None:
    """Build Usage from raw counts, coercing every leg to a scalar.

    A shape with no usable count yields None, so garbage never crosses the
    driver boundary.
    """
    counts = (
        coerce_count(prompt),
        coerce_count(completion),
        coerce_count(cache_read),
        coerce_count(cache_creation),
    )
    if all(count is None for count in counts):
        return None
    return Usage(
        prompt_tokens=counts[0],
        completion_tokens=counts[1],
        cache_read_input_tokens=counts[2],
        cache_creation_input_tokens=counts[3],
    )
