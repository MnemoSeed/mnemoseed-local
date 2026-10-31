"""Read-side temporal applicability evaluator (PRD-281, grammar v1).

Pure function of ``(text, now)``: no clock, no model calls, deterministic. The
chunk's verbatim text is the sole authority — only an explicit expiry
expression in the text can create the demotion signal; age, decay, provenance
and graph metadata never can (D04). An expiry expression is a frozen validity
marker governing an ISO date token in the same clause, outside quoted spans
and URL tokens. Everything that does not parse to exactly one explicit
expiry instant — ambiguity, missing timezone, two different instants, no
expression at all — is ``unknown`` and never demotes (fail-open: the cost of
a missed demotion is the status quo, the cost of a wrong one is burying a
still-valid memory).

The verdict feeds a structural rank tier (``expired_window``), not an
exclusion and not a score rewrite: historical reads stay reachable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

# EN markers match casefolded on word boundaries; the list is the frozen v1
# whole set — extensions require a grammar revision, never an ad-hoc word.
_EN_MARKERS = (
    "expires",
    "expiry",
    "expiration",
    "valid until",
    "valid through",
    "valid before",
    "good until",
    "available until",
    "effective until",
    "until",
    "before",
)

# ZH prefix markers match verbatim (the validity marker precedes the date).
_ZH_PREFIX_MARKERS = (
    "有效期至",
    "有效期到",
    "有效期：",
    "有效期:",
    "截止",
    "截止至",
    "截止日期",
    "到期日",
    "失效时间",
    "过期时间",
)

# ZH suffix markers follow the date ("…前有效"); the date must sit directly
# before the marker, and the adverbial 地 reading ("之前有效地…") is excluded.
_ZH_SUFFIX_MARKER = re.compile(r"\d{4}-\d{2}-\d{2}\s*(?:之前|前)有效(?!地)")

_EN_MARKER = re.compile(
    r"\b(?:" + "|".join(re.escape(marker) for marker in _EN_MARKERS) + r")\b",
    re.IGNORECASE,
)

# Clause delimiters for marker/date association: pause punctuation and line
# breaks. The ASCII dot stays inside a clause (it also ends abbreviations and
# domains), so a marker keeps governing across it.
_CLAUSE_BREAK = re.compile(r"[,，、;；。！？!?\n\r]")

# Positional cleanliness (PRD-281): dates inside quoted spans ("…", '…',
# backticks, 「」『』) or URL tokens (containing "://") never create an expiry.
# The {2,60} bound follows the capture-side _QUOTED precedent.
_QUOTED_SPAN = re.compile(r"\"[^\"]{2,60}\"|'[^']{2,60}'|`[^`]{2,60}`|「[^」]{2,60}」|『[^』]{2,60}』")
_URL_TOKEN = re.compile(r"\S*://\S*")

# Accepted date token forms: an ISO 8601 instant with an explicit timezone
# (Z or ±hh[:mm]) and the bare ISO calendar date. A timezone-less instant
# matches neither form and stays unknown.
_INSTANT = re.compile(
    r"(?<!\d)(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"T(?P<h>\d{2}):(?P<mi>\d{2})(?::(?P<s>\d{2})(?:\.(?P<frac>\d+))?)?"
    r"(?P<tz>Z|[+-]\d{2}(?::\d{2})?)(?!\d)"
)
_DATE_ONLY = re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})(?![T\d])(?!\s\d{2}:\d{2})")


@dataclass(frozen=True)
class TemporalVerdict:
    """Outcome of the temporal read: ``expired_window`` moves the candidate
    behind every candidate without an expired window (rank tier); ``unknown``
    means nothing was parsed, the token was ambiguous, or two different
    instants conflict — never demote."""

    expired_window: bool
    unknown: bool


def evaluate_temporal(text: str, now: float | None) -> TemporalVerdict:
    """Evaluate one chunk text against the reading instant.

    ``now`` is the caller-supplied reading instant (epoch seconds UTC); a
    missing clock is unknown and fails open. Equality counts as expired:
    ``now >= bound``.
    """
    if now is None:
        return TemporalVerdict(False, True)
    cleaned = _URL_TOKEN.sub(" ", _QUOTED_SPAN.sub(" ", text))
    bounds: set[float] = set()
    for clause in _CLAUSE_BREAK.split(cleaned):
        if not _governs_dates(clause):
            continue
        for match in _INSTANT.finditer(clause):
            epoch = _instant_epoch(match)
            if epoch is not None:
                bounds.add(epoch)
        for match in _DATE_ONLY.finditer(clause):
            epoch = _date_only_epoch(match)
            if epoch is not None:
                bounds.add(epoch)
    if len(bounds) != 1:
        # Nothing parsed, or two different instants conflict (identical
        # repeats deduplicate): unknown, fail-open.
        return TemporalVerdict(False, True)
    return TemporalVerdict(now >= next(iter(bounds)), False)


def _governs_dates(clause: str) -> bool:
    if _EN_MARKER.search(clause):
        return True
    if any(marker in clause for marker in _ZH_PREFIX_MARKERS):
        return True
    return _ZH_SUFFIX_MARKER.search(clause) is not None


def _instant_epoch(match: re.Match[str]) -> float | None:
    frac = (match["frac"] or "")[:6].ljust(6, "0")
    try:
        moment = datetime(
            int(match["y"]),
            int(match["mo"]),
            int(match["d"]),
            int(match["h"]),
            int(match["mi"]),
            int(match["s"] or 0),
            int(frac),
            tzinfo=timezone(_tz_offset(match["tz"])),
        )
    except ValueError:
        return None
    return moment.timestamp()


def _date_only_epoch(match: re.Match[str]) -> float | None:
    """A bare calendar date normalizes to its end-of-day 23:59:59Z: the whole
    named day is covered, and equality with that boundary is expired."""
    try:
        end_of_day = datetime(int(match["y"]), int(match["mo"]), int(match["d"]), 23, 59, 59, tzinfo=UTC)
    except ValueError:
        return None
    return end_of_day.timestamp()


def _tz_offset(spec: str) -> timedelta:
    if spec == "Z":
        return timedelta(0)
    sign = 1 if spec[0] == "+" else -1
    hours, _, minutes = spec[1:].partition(":")
    return sign * timedelta(hours=int(hours), minutes=int(minutes or 0))
