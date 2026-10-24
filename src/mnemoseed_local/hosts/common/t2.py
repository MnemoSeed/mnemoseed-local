"""Host-agnostic T2 recall-block assembly: items + signal + budget -> text.

Byte-identical in semantics to the opencode plugin's ``buildT2Injection``: the
same fence + disclaimer envelope, no group headers (the daemon already shaped
the payload), and the daemon's ``budget_chars`` IS the item budget.
"""

from __future__ import annotations

from typing import Any

from mnemoseed_local.hosts.common.fence import (
    RECALL_DISCLAIMER,
    RECALL_FENCE_CLOSE,
    RECALL_FENCE_OPEN,
    sanitize_recall_text,
)
from mnemoseed_local.schema.stamp import EXPLICIT_PIN_SOURCE

#: Provenance affix (byte-identical to the opencode plugin): a per-line pin
#: marker on an explicitly-pinned recall item; captured items are NOT
#: annotated (absence is the captured signal). 9 chars including the leading
#: separator; decoration only, NEVER part of the verbatim text.
PIN_SUFFIX = " ⟵ pinned"


def build_t2_context(
    items: list[dict[str, Any]] | Any, item_budget: int, signal: str | None = None
) -> str | None:
    """Assemble the fenced mid-session recall block from a served selection.

    The envelope re-checks each FULL item fail-closed — an oversized line or
    assembled block is dropped WHOLE (defense in depth, unreachable by
    design). There is NO slicing floor in this path: the daemon's
    ``_MIN_SLICE_CHARS`` governs only its own boundary-item tail cut; full
    items under any positive budget are served and must append.

    ``signal`` is the prebuilt visibility text (compact line, or the compact
    line plus the verbose count line separated by one newline). It renders
    right after the fence-open line and its cost is the FIRST budget shed
    under pressure — the shed result is byte-identical to the no-signal
    build, so the signal is purely additive when it fits.
    """
    if not isinstance(items, list):
        return None
    base = _build_t2_lines(items, item_budget, signal=None)
    if base is None:
        return None
    if signal:
        with_signal = _build_t2_lines(items, item_budget, signal=signal)
        if with_signal is not None:
            return with_signal
    return base


def _build_t2_lines(items: list[dict[str, Any]] | Any, item_budget: int, signal: str | None) -> str | None:
    wrapper = len(RECALL_FENCE_OPEN) + 1 + len(RECALL_DISCLAIMER) + 1 + len(RECALL_FENCE_CLOSE)
    lines: list[str] = [RECALL_FENCE_OPEN]
    if signal:
        lines.append(signal)
    lines.append(RECALL_DISCLAIMER)
    remaining = item_budget
    committed = False
    # The per-line affix is the FIRST budget shed under pressure. `kept_affix`
    # sums the affix cost already committed in `lines`, and `affix_indices`
    # records the ABSOLUTE `lines` positions of the lines that carry it — so a
    # later overrun can refund their cost and rebuild exactly those lines bare
    # instead of dropping a daemon-legal selection. Indices (not flags) are
    # tracked so a rebuild can never be mis-indexed against `lines[2 + i]`
    # when the affix is shed a second time in one call.
    kept_affix = 0
    affix_indices: list[int] = []
    for item in items:
        raw = item.get("text") if isinstance(item, dict) else None
        text = sanitize_recall_text(raw if isinstance(raw, str) else "")
        if not text:
            continue
        # The affix rides ONLY an explicitly-pinned item; a captured (other
        # source) or source-less item is NOT annotated — absence is the
        # captured signal, the most token-lean rendering.
        pinned = isinstance(item, dict) and item.get("source") == EXPLICIT_PIN_SOURCE
        affix_len = len(PIN_SUFFIX) if pinned else 0
        line_cost = len(text) + 1 + affix_len
        if line_cost > remaining:
            # Drop order: a kept affix must never change item keep/drop
            # semantics — so on an overrun shed EVERY kept affix (refund their
            # cost and rebuild the committed lines bare) BEFORE dropping. Only
            # a line whose BARE cost still exceeds the recovered budget stays
            # fail-closed (whole selection dropped, unchanged).
            if len(text) + 1 <= remaining + kept_affix:
                if kept_affix:
                    remaining += kept_affix
                    kept_affix = 0
                    for index in affix_indices:
                        lines[index] = lines[index][: -len(PIN_SUFFIX)]
                    affix_indices = []
                lines.append(text)
                remaining -= len(text) + 1
                committed = True
                continue
            return None
        lines.append(text + PIN_SUFFIX if pinned else text)
        remaining -= line_cost
        kept_affix += affix_len
        if pinned:
            affix_indices.append(len(lines) - 1)
        committed = True
    if not committed:
        return None
    lines.append(RECALL_FENCE_CLOSE)
    block = "\n".join(lines)
    if len(block) > item_budget + wrapper:
        return None
    return block
