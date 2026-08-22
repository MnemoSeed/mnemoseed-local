"""B5 vote deterministic combiner (mvp-design decision 1, pure).

The vote ensemble runs A and B each fully generating over the same delta, then
this combiner folds the two per-seat reflection results into ONE result the
single merge commits. Pure and deterministic — no LLM judge, no graph writes,
no I/O. The rules (mvp-design decision 1, combiner semantics):

- Agreement = casefold + normalized (subject, predicate, object) equality. An
  agreed key folds into one triple with the existing AC-3 confidence formula
  (max + 0.05 per extra mention, capped 0.95) and the most restrictive route.
- Cross-seat polarity guard (g2): two seats agreeing on the SAME key but with
  contradictory polarity (one positive, one negative) are a contradiction, never
  folded into one reinforced triple — both drop and the key is reported on
  ``conflicts``, mirroring the single-seat negation guard.
- Divergence = the two seats weighed in on the same (subject, predicate) but
  reached different objects. The disputed predicate's parties are preserved as
  ISOLATED (the vote-side mirror of the verify-side "reject -> isolated":
  never voted away, never deleted).
- Single-side = only one seat produced the key. It keeps its seat's route; a
  CORE single-side triple below the low-value floor routes to SALVAGE (the
  design's "single-side-only and low-value" lane).

Per-triple model attribution rides the combined result so provenance can trace
each surviving fact to its generating seat (triple-level attribution, the
journal extension the vote mode exists for).
"""

from __future__ import annotations

from dataclasses import replace

from mnemoseed_local.dream.reflect import ReflectedTriple, ReflectionResult, Route

#: The combiner's prompt/schema lineage marker (mirrors PROMPT_VERSION).
COMBINE_PROMPT_VERSION = "v1"

#: Single-side CORE triples below this confidence are "low value" and route to
#: the salvage lane (mvp-design decision 1: 单方独有且低价值 -> salvage).
SINGLE_SIDE_SALVAGE_FLOOR = 0.5

_ROUTE_ORDER: dict[Route, int] = {Route.CORE: 1, Route.ISOLATED: 2, Route.SALVAGE: 3}


def _subject_predicate(t: ReflectedTriple) -> tuple[str, str]:
    """The (subject, predicate) vote-dispute unit, casefold + stripped."""
    return t.subject.casefold().strip(), t.predicate.casefold().strip()


def _conflict_key(t: ReflectedTriple) -> tuple[str, str, str]:
    """The contradiction key reported on ``conflicts`` (g2's canonical shape)."""
    return (
        t.subject.casefold().strip(),
        t.predicate.casefold().strip(),
        t.object.casefold().strip(),
    )


def _fold_confidence(triples: list[ReflectedTriple]) -> float:
    """The AC-3 fold formula: max + 0.05 per extra mention, capped 0.95."""
    return min(0.95, max(t.confidence for t in triples) + 0.05 * (len(triples) - 1))


def _merge_agreement(a: ReflectedTriple, b: ReflectedTriple) -> ReflectedTriple:
    """Fold one agreed key into a single triple (both seats attributed)."""
    route = max((a.route, b.route), key=lambda r: _ROUTE_ORDER[r])
    model_id = "|".join(m for m in (a.model_id, b.model_id) if m)
    return replace(
        a,
        confidence=_fold_confidence([a, b]),
        route=route,
        model_id=model_id or a.model_id,
        chunk_ids=tuple(sorted(set(a.chunk_ids) | set(b.chunk_ids))),
        tiers=tuple(sorted({t for t in set(a.tiers) | set(b.tiers)}, key=int)),
    )


def _isolate(triples: list[ReflectedTriple]) -> list[ReflectedTriple]:
    """Preserve a disputed predicate's parties in the isolated track, flagged so
    the merge marks them ``needs_reconcile`` (the vote/needs_reconcile
    co-operation)."""
    return [replace(t, route=Route.ISOLATED, vote_disagreement=True) for t in triples]


def _single_side(triple: ReflectedTriple) -> ReflectedTriple:
    """Keep a single-seat triple's route, routing low-value CORE to SALVAGE."""
    if triple.route is Route.CORE and triple.confidence < SINGLE_SIDE_SALVAGE_FLOOR:
        return replace(triple, route=Route.SALVAGE)
    return triple


def combine_results(a: ReflectionResult, b: ReflectionResult) -> ReflectionResult:
    """Fold A's and B's reflection results into one merge-facing result.

    Returns a new ``ReflectionResult`` carrying the combined triples plus the
    union of overflow / consumed allow-lists and conflict keys from both seats.
    """
    # group every seat's triples by (subject, predicate); object keys within a
    # group decide agreement vs divergence vs single-side.
    groups: dict[tuple[str, str], dict[str, dict[str, ReflectedTriple]]] = {}
    for triple, seat in ((t, "a") for t in a.triples):
        groups.setdefault(_subject_predicate(triple), {}).setdefault(triple.object.casefold().strip(), {})[
            seat
        ] = triple
    for triple, seat in ((t, "b") for t in b.triples):
        groups.setdefault(_subject_predicate(triple), {}).setdefault(triple.object.casefold().strip(), {})[
            seat
        ] = triple

    combined: list[ReflectedTriple] = []
    cross_conflicts: list[tuple[str, str, str]] = []
    for sp_group in groups.values():
        object_keys = list(sp_group.values())
        all_seats = {seat for by_obj in object_keys for seat in by_obj}
        # A predicate is cross-seat divergent when the seats produced different
        # objects for it. Agreement still folds PER KEY: only the single-side
        # (divergent) parties of such a group are isolated — an agreed key
        # (both seats on the same object) is never demoted by a sibling's
        # divergence. A group with no cross-seat divergence keeps single-side
        # semantics throughout.
        divergent = len(object_keys) > 1 and len(all_seats) > 1
        for by_seat in object_keys:
            if "a" in by_seat and "b" in by_seat:
                a_t, b_t = by_seat["a"], by_seat["b"]
                if a_t.polarity != b_t.polarity:
                    # cross-seat polarity guard (g2): the same key agreed on
                    # opposite polarity is a contradiction, never folded into
                    # one reinforced triple — both drop and the key is reported.
                    cross_conflicts.append(_conflict_key(a_t))
                    continue
                combined.append(_merge_agreement(a_t, b_t))
            elif divergent:
                # a divergent predicate's single-side party -> isolated
                combined.extend(_isolate(list(by_seat.values())))
            else:
                combined.append(_single_side(next(iter(by_seat.values()))))

    combined.sort(
        key=lambda t: (t.route.value, t.subject.casefold(), t.predicate.casefold(), t.object.casefold())
    )
    return ReflectionResult(
        snapshot_id=a.snapshot_id,
        profile_id=a.profile_id,
        turn_range=a.turn_range,
        prompt_version=COMBINE_PROMPT_VERSION,
        triples=tuple(combined),
        conflicts=tuple(sorted(set(a.conflicts) | set(b.conflicts) | set(cross_conflicts))),
        overflow_chunk_ids=tuple(sorted(set(a.overflow_chunk_ids) | set(b.overflow_chunk_ids))),
        consumed_chunk_ids=tuple(sorted(set(a.consumed_chunk_ids) | set(b.consumed_chunk_ids))),
    )
