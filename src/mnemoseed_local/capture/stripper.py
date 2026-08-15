"""F1 Local Stripper — ordered rule engine for mechanical noise (FR-1.2).

The engine walks an ordered, data-driven ruleset over Turn content. Every rule
names a target content kind (tool output / message text / both), an action
(strip a whole line, redact a span, collapse a repeated block) and either a
regex pattern or a per-unit predicate. The ruleset itself is plain data
(capture/rulesets_v1.py), so the daemon can hot-swap it via reload_rules
without a restart; a swap governs the next stripped turn.

Red line (design/01 stage 1): F1 never touches prose. Rules match only
mechanical shapes plus host-injected scaffolding (session-compaction summary
wrappers, ``<task-notification>`` blocks — both anchored on structural
markers, so prose that mentions or paraphrases them never matches); a
pure-prose turn exits byte-identical, and strip_turn returns copies — the
input Turn is never mutated, so the raw provenance copy stays available.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from mnemoseed_local.schema.turn import Turn, TurnRole, TurnStep


class ContentTarget(StrEnum):
    """Which content kind a rule may touch."""

    TOOL_OUTPUT = "tool_output"  # TOOL step content
    MESSAGE_TEXT = "message_text"  # USER and ASSISTANT step content
    BOTH = "both"


class StripAction(StrEnum):
    """What a rule does to matching content."""

    STRIP_LINE = "strip_line"  # drop whole matching lines
    REDACT_SPAN = "redact_span"  # remove matching spans, keep surroundings
    COLLAPSE_RUNS = "collapse_runs"  # dedupe repeated blocks


class StripperError(Exception):
    """A ruleset that cannot be applied (bad regex, missing fields)."""


@dataclass(frozen=True)
class Rule:
    """One data-driven stripping rule.

    ``pattern`` is matched against each line unit (content plus its original
    terminator) for STRIP_LINE and against the whole text for REDACT_SPAN.
    ``predicate`` is an alternative to ``pattern`` for STRIP_LINE.
    ``min_run`` only applies to COLLAPSE_RUNS.
    """

    id: str
    target: ContentTarget
    action: StripAction
    pattern: str = ""
    predicate: Callable[[str], bool] | None = None
    min_run: int = 2


@dataclass(frozen=True)
class RuleSet:
    """An ordered collection of rules; the unit a hot reload swaps in."""

    rules: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class StripStats:
    """Per-turn or cumulative stripping telemetry for the benchmark harness.

    ``matched_by_rule`` records, per rule, the UTF-8 byte size of the content
    that rule matched as strippable noise. It is the NFR-1.2 denominator;
    ``bytes_in - bytes_out`` is the numerator. ``compression_ratio`` measures
    the whole-corpus full-byte ratio and stays a reported observation only.
    """

    bytes_in: int = 0
    bytes_out: int = 0
    rules_hit: dict[str, int] = field(default_factory=dict)
    matched_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def compression_ratio(self) -> float:
        """Fraction of input bytes saved (0.0 .. 1.0); 0 for empty input."""
        if self.bytes_in <= 0:
            return 0.0
        return (self.bytes_in - self.bytes_out) / self.bytes_in

    @property
    def noise_matched_bytes(self) -> int:
        """Bytes the rules matched as strippable noise (NFR-1.2 denominator)."""
        return sum(self.matched_by_rule.values())

    @property
    def noise_removed_bytes(self) -> int:
        """Bytes actually removed by the strip rules (NFR-1.2 numerator)."""
        return max(0, self.bytes_in - self.bytes_out)

    @property
    def noise_class_rate(self) -> float:
        """NFR-1.2 noise-class stripping rate over this content.

        numerator: bytes actually removed (``noise_removed_bytes``);
        denominator: bytes the rules matched as strippable noise
        (``noise_matched_bytes``) — both sides confined to rule-hit content,
        never the whole corpus. REDACT_SPAN and STRIP_LINE remove every byte
        they match (contribution 1.0). COLLAPSE_RUNS matches a whole repeated
        block but keeps one canonical copy, so an N-unit run contributes
        (N-1)/N — the documented approximation is that the retained canonical
        byte still counts as matched because the block was classified as noise.
        0 when nothing was matched.
        """
        matched = self.noise_matched_bytes
        if matched <= 0:
            return 0.0
        return self.noise_removed_bytes / matched


@dataclass(frozen=True)
class StrippedTurn:
    """A stripped Turn plus the statistics that describe the strip."""

    turn: Turn
    stats: StripStats


@dataclass(frozen=True)
class _CompiledRule:
    rule: Rule
    pattern: re.Pattern[str] | None


class Stripper:
    """Walks an ordered ruleset over Turn / text content."""

    def __init__(self, ruleset: RuleSet) -> None:
        self._compiled = self._compile(ruleset)
        self._ruleset = ruleset

    @property
    def ruleset(self) -> RuleSet:
        return self._ruleset

    def reload_rules(self, ruleset: RuleSet) -> None:
        """Swap the active ruleset; a bad ruleset is rejected and the old one
        stays in force (the compiled set is only replaced on success)."""
        compiled = self._compile(ruleset)
        self._ruleset = ruleset
        self._compiled = compiled

    def strip_turn(self, turn: Turn) -> StrippedTurn:
        """Strip every text-bearing step of a Turn; the input is never mutated."""
        new_steps: list[TurnStep] = []
        bytes_in = 0
        bytes_out = 0
        hits: dict[str, int] = {}
        matched: dict[str, int] = {}
        for step in turn.steps:
            target = ContentTarget.TOOL_OUTPUT if step.role is TurnRole.TOOL else ContentTarget.MESSAGE_TEXT
            text, step_hits, step_matched = self._strip_measured(step.content, target)
            bytes_in += len(step.content.encode("utf-8"))
            bytes_out += len(text.encode("utf-8"))
            for rule_id, count in step_hits.items():
                hits[rule_id] = hits.get(rule_id, 0) + count
            for rule_id, size in step_matched.items():
                matched[rule_id] = matched.get(rule_id, 0) + size
            new_steps.append(step.model_copy(update={"content": text}))
        stripped = turn.model_copy(update={"steps": new_steps})
        return StrippedTurn(
            turn=stripped,
            stats=StripStats(bytes_in=bytes_in, bytes_out=bytes_out, rules_hit=hits, matched_by_rule=matched),
        )

    def strip_text(self, text: str, target: ContentTarget) -> tuple[str, dict[str, int]]:
        """Apply the ruleset to one content blob; returns (text, rule hits)."""
        text, hits, _ = self._strip_measured(text, target)
        return text, hits

    def _strip_measured(self, text: str, target: ContentTarget) -> tuple[str, dict[str, int], dict[str, int]]:
        """Apply the ruleset; returns (text, rule hits, matched bytes per rule).

        ``matched bytes`` is the UTF-8 size of the content the rule classified
        as strippable noise — exact for REDACT_SPAN and STRIP_LINE (every
        matched byte is removed), and the documented approximation for
        COLLAPSE_RUNS (the whole repeated block counts as matched, including
        the one canonical copy that is kept).
        """
        hits: dict[str, int] = {}
        matched: dict[str, int] = {}
        for compiled in self._compiled:
            rule = compiled.rule
            if rule.target is not ContentTarget.BOTH and rule.target is not target:
                continue
            before = text
            if rule.action is StripAction.REDACT_SPAN:
                pattern = compiled.pattern
                assert pattern is not None
                text, count = pattern.subn("", text)
                matched_bytes = _utf8_len(before) - _utf8_len(text)
            elif rule.action is StripAction.STRIP_LINE:
                text, count, matched_bytes = _strip_lines(text, rule, compiled.pattern)
            else:  # COLLAPSE_RUNS
                text, count, matched_bytes = _collapse_runs(text, rule.min_run)
            if count:
                hits[rule.id] = hits.get(rule.id, 0) + count
                matched[rule.id] = matched.get(rule.id, 0) + matched_bytes
        return text, hits, matched

    @staticmethod
    def _compile(ruleset: RuleSet) -> tuple[_CompiledRule, ...]:
        seen: set[str] = set()
        compiled: list[_CompiledRule] = []
        for rule in ruleset.rules:
            if rule.id in seen:
                raise StripperError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
            if rule.action is StripAction.REDACT_SPAN:
                if not rule.pattern:
                    raise StripperError(f"rule {rule.id!r}: redact-span requires a pattern")
            elif rule.action is StripAction.STRIP_LINE:
                if not rule.pattern and rule.predicate is None:
                    raise StripperError(f"rule {rule.id!r}: strip-line requires a pattern or predicate")
            elif rule.action is StripAction.COLLAPSE_RUNS:
                if rule.pattern or rule.predicate is not None:
                    raise StripperError(f"rule {rule.id!r}: collapse-runs takes no pattern or predicate")
                if rule.min_run < 2:
                    raise StripperError(f"rule {rule.id!r}: collapse-runs min_run must be >= 2")
            else:  # pragma: no cover - exhaustive StrEnum
                raise StripperError(f"rule {rule.id!r}: unknown action {rule.action!r}")
            pattern = None
            if rule.pattern:
                try:
                    pattern = re.compile(rule.pattern)
                except re.error as exc:
                    raise StripperError(f"rule {rule.id!r}: invalid pattern: {exc}") from exc
            compiled.append(_CompiledRule(rule=rule, pattern=pattern))
        return tuple(compiled)


# ---------------------------------------------------------------- line machinery


def _split_terminated(text: str) -> list[tuple[str, str]]:
    """Split into (content, terminator) units, preserving every terminator byte.
    Terminators are one of \r\n / \r / \n; the final unit carries ''."""
    units: list[tuple[str, str]] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in ("\r", "\n"):
            term = "\r\n" if char == "\r" and index + 1 < length and text[index + 1] == "\n" else char
            units.append((text[start:index], term))
            index += len(term)
            start = index
        else:
            index += 1
    if start == length and not units and text == "":
        units.append(("", ""))
    elif start < length:
        units.append((text[start:length], ""))
    return units


def _join_terminated(units: list[tuple[str, str]]) -> str:
    return "".join(content + term for content, term in units)


def _strip_lines(text: str, rule: Rule, pattern: re.Pattern[str] | None) -> tuple[str, int, int]:
    """Drop every unit the rule matches.

    Returns (text, removed_count, matched_bytes) where matched_bytes is the
    UTF-8 size of the dropped units — exact: every matched byte is removed.
    """
    units = _split_terminated(text)
    kept: list[tuple[str, str]] = []
    removed = 0
    matched_bytes = 0
    for content, term in units:
        unit = content + term
        if rule.predicate is not None:
            matched = rule.predicate(unit)
        elif pattern is not None:
            matched = pattern.match(unit) is not None
        else:  # pragma: no cover - _compile rejects this shape
            matched = False
        if matched:
            removed += 1
            matched_bytes += _utf8_len(unit)
        else:
            kept.append((content, term))
    if removed == 0:
        return text, 0, 0
    return _join_terminated(kept), removed, matched_bytes


def _collapse_runs(text: str, min_run: int) -> tuple[str, int, int]:
    """Collapse repeated blocks: adjacent duplicate units, then a full-coverage
    periodic repetition of the whole unit sequence, to a single occurrence.

    Returns (text, removed_count, matched_bytes). matched_bytes counts the
    whole repeated block as matched-as-strippable — including the one canonical
    copy that is kept — so an N-unit run contributes (N-1)/N to the rate.
    """
    units = _split_terminated(text)
    if len(units) < min_run:
        return text, 0, 0
    collapsed, removed, matched = _collapse_adjacent(units, min_run)
    collapsed, removed_blocks, matched_blocks = _collapse_periodic(collapsed, min_run)
    total = removed + removed_blocks
    if total == 0:
        return text, 0, 0
    return _join_terminated(collapsed), total, matched + matched_blocks


def _collapse_adjacent(units: list[tuple[str, str]], min_run: int) -> tuple[list[tuple[str, str]], int, int]:
    kept: list[tuple[str, str]] = []
    removed = 0
    matched_bytes = 0
    index = 0
    length = len(units)
    while index < length:
        end = index + 1
        while end < length and units[end] == units[index]:
            end += 1
        run = end - index
        if run >= min_run:
            kept.append(units[index])
            removed += run - 1
            matched_bytes += run * _utf8_len(units[index][0] + units[index][1])
        else:
            kept.extend(units[index:end])
        index = end
    return kept, removed, matched_bytes


def _collapse_periodic(units: list[tuple[str, str]], min_run: int) -> tuple[list[tuple[str, str]], int, int]:
    length = len(units)
    if length < min_run * 2:
        return units, 0, 0
    for block in range(1, length):
        if length % block:
            continue
        repeats = length // block
        if repeats < min_run:
            break  # repeats fall as block grows; nothing left to try
        if units[:block] * repeats == units:
            matched_bytes = sum(_utf8_len(u[0] + u[1]) for u in units)
            return units[:block], length - block, matched_bytes
    return units, 0, 0


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))
