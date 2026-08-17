"""CapturePipeline seam — where the F1-F3 capture funnel plugs in.

F1 (Stripper) is wired as StrippingPipeline, the embedded default below; F2
(persistence classifier) and F3 (scoring) run as ScoringPipeline, the funnel
tail that scores drained turns into a score pool. Submit must never block the
ingest hot path: every stage is an O(1) append, and F1-F3 process on the
consumer side (``drain`` / ``turns``), never inside the HTTP handler.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, cast

from mnemoseed_local.capture.pool import ScorePool
from mnemoseed_local.capture.rulesets_v1 import RULESET_V1
from mnemoseed_local.capture.scorer import Durability, ScoredTurn, TurnScorer
from mnemoseed_local.capture.stamper import (
    StampWriter,
    WriteContext,
    WriteOutcome,
    WriteOutcomeKind,
)
from mnemoseed_local.capture.stripper import (
    RuleSet,
    StrippedTurn,
    Stripper,
    StripStats,
)
from mnemoseed_local.schema.turn import Turn, TurnRole
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import Embedder, TurnRange, VectorStore


class CapturePipeline(Protocol):
    """Consumer contract for structured turns and session settlements."""

    def submit_turn(self, turn: Turn) -> None: ...

    def end_session(self, session_id: str, turn_range: TurnRange) -> None: ...


class InMemoryCapturePipeline:
    """In-process CapturePipeline (embedded default): buffers per session.

    Ordering per session is submission order. ``settled`` records the closed
    turn range so later stages know the session boundary.
    """

    def __init__(self) -> None:
        self._turns: dict[str, list[Turn]] = {}
        self._settled: dict[str, TurnRange] = {}

    def submit_turn(self, turn: Turn) -> None:
        self._turns.setdefault(turn.session_id, []).append(turn)

    def end_session(self, session_id: str, turn_range: TurnRange) -> None:
        self._settled[session_id] = turn_range

    def turns(self, session_id: str) -> list[Turn]:
        return list(self._turns.get(session_id, []))

    def settled(self, session_id: str) -> TurnRange | None:
        return self._settled.get(session_id)

    def sessions(self) -> tuple[str, ...]:
        return tuple(self._turns)


class StrippingPipeline:
    """F1 seam: O(1) submit on the HTTP path; the stripper drains on read.

    ``submit_turn`` is a plain append to the delegate buffer, so the /ingest
    handler never runs the stripper. The consumer side (``turns`` / ``drain``)
    strips each buffered turn exactly once with the ruleset current at that
    moment — a hot reload therefore takes effect on the next turn, and already
    drained turns are never reprocessed. The delegate keeps the raw provenance
    copy; ``stats`` expose cumulative compression telemetry for the benchmark.
    """

    def __init__(
        self,
        delegate: InMemoryCapturePipeline | None = None,
        stripper: Stripper | None = None,
    ) -> None:
        self._delegate = delegate if delegate is not None else InMemoryCapturePipeline()
        self._stripper = stripper if stripper is not None else Stripper(RULESET_V1)
        self._stripped: dict[str, list[Turn]] = {}
        self._bytes_in = 0
        self._bytes_out = 0
        self._rules_hit: dict[str, int] = {}
        self._matched_by_rule: dict[str, int] = {}

    def submit_turn(self, turn: Turn) -> None:
        self._delegate.submit_turn(turn)

    def end_session(self, session_id: str, turn_range: TurnRange) -> None:
        self._delegate.end_session(session_id, turn_range)

    def reload_rules(self, ruleset: RuleSet) -> None:
        """Swap the ruleset; governs turns drained after this call."""
        self._stripper.reload_rules(ruleset)

    def drain(self, session_id: str) -> list[StrippedTurn]:
        """Strip not-yet-processed turns of one session; returns per-turn results."""
        raw = self._delegate.turns(session_id)
        done = len(self._stripped.get(session_id, []))
        results: list[StrippedTurn] = []
        for turn in raw[done:]:
            result = self._stripper.strip_turn(turn)
            self._bytes_in += result.stats.bytes_in
            self._bytes_out += result.stats.bytes_out
            for rule_id, count in result.stats.rules_hit.items():
                self._rules_hit[rule_id] = self._rules_hit.get(rule_id, 0) + count
            for rule_id, size in result.stats.matched_by_rule.items():
                self._matched_by_rule[rule_id] = self._matched_by_rule.get(rule_id, 0) + size
            results.append(result)
            self._stripped.setdefault(session_id, []).append(result.turn)
        return results

    def turns(self, session_id: str) -> list[Turn]:
        """Drain pending turns lazily and return the stripped versions."""
        self.drain(session_id)
        return list(self._stripped.get(session_id, []))

    def settled(self, session_id: str) -> TurnRange | None:
        return self._delegate.settled(session_id)

    def sessions(self) -> tuple[str, ...]:
        return self._delegate.sessions()

    @property
    def stats(self) -> StripStats:
        """Cumulative stripping telemetry across every drained turn."""
        return StripStats(
            bytes_in=self._bytes_in,
            bytes_out=self._bytes_out,
            rules_hit=dict(self._rules_hit),
            matched_by_rule=dict(self._matched_by_rule),
        )


def _user_text(turn: Turn) -> str:
    """Forward, joined content of the USER steps (the scanner's raw input)."""
    parts = [step.content for step in turn.steps if step.role is TurnRole.USER]
    return " ".join(parts)


@dataclass
class ScoringStats:
    """Cumulative F1-F3 funnel telemetry across every drained turn of a
    ScoringPipeline. ``dropped_reasons`` counts DISPOSABLE verdicts per reason;
    ``pool_triggers`` counts dream/forced events fired by the score pool."""

    turns_in: int = 0
    durable_kept: int = 0
    dropped: int = 0
    dropped_reasons: dict[str, int] = field(default_factory=dict)
    pool_triggers: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    rules_hit: dict[str, int] = field(default_factory=dict)
    matched_by_rule: dict[str, int] = field(default_factory=dict)

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
        """NFR-1.2 noise-class stripping rate; 0 when nothing was matched."""
        matched = self.noise_matched_bytes
        if matched <= 0:
            return 0.0
        return self.noise_removed_bytes / matched


class ScoringPipeline:
    """Full funnel tail: O(1) submit, then strip -> score -> pool on drain.

    ``submit_turn`` appends to the delegate buffer, so /ingest stays O(1). The
    consumer side strips each pending turn (F1), classifies and scores it
    (F2/F3), keeps DURABLE turns in the buffer, drops DISPOSABLE turns with a
    reason, and credits the score pool with each durable turn's S. Recent text
    per profile feeds the scorer's novelty / repetition terms.
    """

    def __init__(
        self,
        delegate: InMemoryCapturePipeline | None = None,
        *,
        stripper: Stripper | None = None,
        scorer: TurnScorer | None = None,
        pool: ScorePool | None = None,
        recent_capacity: int = 16,
    ) -> None:
        self._delegate = delegate if delegate is not None else InMemoryCapturePipeline()
        self._stripper = stripper if stripper is not None else Stripper(RULESET_V1)
        self._scorer = (
            scorer if scorer is not None else TurnScorer(embedder=cast(Embedder, SyntheticEmbedder()))
        )
        self._pool = pool if pool is not None else ScorePool(clock=time.monotonic)
        self._recent_capacity = recent_capacity
        self._stripped: dict[str, list[Turn]] = {}
        self._scored: dict[str, list[ScoredTurn]] = {}
        self._recent: dict[str, list[str]] = {}
        self._stats = ScoringStats()

    def submit_turn(self, turn: Turn) -> None:
        self._delegate.submit_turn(turn)

    def end_session(self, session_id: str, turn_range: TurnRange) -> None:
        self._delegate.end_session(session_id, turn_range)

    def reload_rules(self, ruleset: RuleSet) -> None:
        """Swap the stripper ruleset; governs turns drained after this call."""
        self._stripper.reload_rules(ruleset)

    def drain(self, session_id: str) -> list[ScoredTurn]:
        """Process pending turns of one session; returns newly durable results."""
        raw = self._delegate.turns(session_id)
        stripped_done = len(self._stripped.get(session_id, []))
        results: list[ScoredTurn] = []
        for turn in raw[stripped_done:]:
            stripped = self._stripper.strip_turn(turn)
            self._stats.bytes_in += stripped.stats.bytes_in
            self._stats.bytes_out += stripped.stats.bytes_out
            for rule_id, count in stripped.stats.rules_hit.items():
                self._stats.rules_hit[rule_id] = self._stats.rules_hit.get(rule_id, 0) + count
            for rule_id, size in stripped.stats.matched_by_rule.items():
                self._stats.matched_by_rule[rule_id] = self._stats.matched_by_rule.get(rule_id, 0) + size
            self._stripped.setdefault(session_id, []).append(stripped.turn)
            recent = self._recent.setdefault(turn.profile_id, [])
            scored = self._scorer.score_turn(
                stripped.turn,
                recent_texts=tuple(recent),
                importance_hint=turn.importance_hint,
            )
            self._stats.turns_in += 1
            next_recent = [*recent, _user_text(stripped.turn)]
            self._recent[turn.profile_id] = next_recent[-self._recent_capacity :]
            if scored.durability.durability is Durability.DURABLE:
                self._stats.durable_kept += 1
                self._scored.setdefault(session_id, []).append(scored)
                fired = self._pool.add_points(
                    turn.profile_id,
                    scored.importance,
                    TurnRange(turn.turn_index, turn.turn_index),
                )
                self._stats.pool_triggers += len(fired)
                results.append(scored)
            else:
                self._stats.dropped += 1
                reason = scored.durability.reasons[0] if scored.durability.reasons else "default-deferral"
                self._stats.dropped_reasons[reason] = self._stats.dropped_reasons.get(reason, 0) + 1
        return results

    def turns(self, session_id: str) -> list[ScoredTurn]:
        """Drain pending turns lazily and return the buffered durable results."""
        self.drain(session_id)
        return list(self._scored.get(session_id, []))

    def settled(self, session_id: str) -> TurnRange | None:
        return self._delegate.settled(session_id)

    def sessions(self) -> tuple[str, ...]:
        return self._delegate.sessions()

    @property
    def pool(self) -> ScorePool:
        return self._pool

    @property
    def stats(self) -> ScoringStats:
        return self._stats


@dataclass
class WritingStats:
    """Cumulative stamp-writer telemetry across every drained turn."""

    turns_written: int = 0
    new_chunks: int = 0
    reinforced: int = 0
    needs_reconcile: int = 0


def _default_write_context(turn: Turn) -> WriteContext:
    """Bare write context: only the identity the wire model always carries."""
    return WriteContext(profile_id=turn.profile_id)


class WritingPipeline:
    """ScoringPipeline plus the stamp writer (FR-1.6/FR-1.8/FR-1.9).

    ``submit_turn`` stays an O(1) append through the inner ScoringPipeline —
    the /ingest hot path never touches embeddings or the store. The consumer
    side (``drain`` / ``turns``) first scores the pending turns, then sends the
    durable ones through the StampWriter, recording per-outcome telemetry.
    Read paths drain first so a producer cannot bypass the write path.
    """

    def __init__(
        self,
        store: VectorStore,
        inner: ScoringPipeline | None = None,
        *,
        writer: StampWriter | None = None,
        context: Callable[[Turn], WriteContext] | None = None,
        embedder: Embedder | None = None,
        # epoch domain: the clock stamps persisted fields (ingested_at /
        # provenance times), which every downstream consumer (decay sweep,
        # ingest windows, audit) reads as epoch (D3)
        clock: Callable[[], float] = time.time,
    ) -> None:
        resolved_embedder = embedder if embedder is not None else cast(Embedder, SyntheticEmbedder())
        self._inner = (
            inner if inner is not None else ScoringPipeline(scorer=TurnScorer(embedder=resolved_embedder))
        )
        self._context = context if context is not None else _default_write_context
        if writer is None:
            writer = StampWriter(
                store,
                embedder=resolved_embedder,
                clock=clock,
                pool=self._inner.pool,
            )
        self._writer = writer
        self._stats = WritingStats()

    def submit_turn(self, turn: Turn) -> None:
        self._inner.submit_turn(turn)

    def end_session(self, session_id: str, turn_range: TurnRange) -> None:
        self._inner.end_session(session_id, turn_range)

    def reload_rules(self, ruleset: RuleSet) -> None:
        self._inner.reload_rules(ruleset)

    def drain(self, session_id: str) -> list[WriteOutcome]:
        """Score pending turns, then write the durable ones to the store."""
        scored = self._inner.drain(session_id)
        outcomes: list[WriteOutcome] = []
        for item in scored:
            outcome = self._writer.write(item, self._context(item.turn))
            outcomes.append(outcome)
            self._stats.turns_written += 1
            if outcome.kind is WriteOutcomeKind.NEW_CHUNK:
                self._stats.new_chunks += 1
            elif outcome.kind is WriteOutcomeKind.REINFORCED:
                self._stats.reinforced += 1
            else:
                self._stats.needs_reconcile += 1
        return outcomes

    def turns(self, session_id: str) -> list[ScoredTurn]:
        """Drain pending turns (writing them) and return the scored view."""
        self.drain(session_id)
        return self._inner.turns(session_id)

    def settled(self, session_id: str) -> TurnRange | None:
        return self._inner.settled(session_id)

    def sessions(self) -> tuple[str, ...]:
        return self._inner.sessions()

    @property
    def stats(self) -> WritingStats:
        return self._stats

    @property
    def pool(self) -> ScorePool:
        """The inner funnel's score pool (daemon boot restore + observability)."""
        return self._inner.pool
