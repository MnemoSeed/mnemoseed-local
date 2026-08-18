"""Ensemble verify pass (B1; design/01 decision 1, honest-cost edition).

Model A reflects; model B judges A's folded CORE triples one by one against
their evidence chunks. The honest-cost contract:

- Judging is a simpler task than generation — one bounded verdict per
  candidate, not a second full generation.
- A rejected triple is deterministically rerouted to ISOLATED: divergence is
  preserved (never voted away, never deleted), the merge path stays untouched.
- EVERY B failure shape (transport outage, malformed output, verdict coverage
  mismatch) falls back to A's original result plus an audit record. B is a
  verification layer, not a critical path: one attempt, no retries, the
  reflect boundary always finalizes.

The pass is dormant unless ``dream.ensemble == "verify"`` (live-read off the
shared Config, so configwrite hot-apply flips it on the next dream run).

Verdict grammar tolerance is bounded exactly like the reflect output lane
(D4): indices are digit-coercible, verdict words come from a fixed map —
anything else is garbage and fails the run back to A.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from mnemoseed_local.config import Config
from mnemoseed_local.dream.delta import estimate_tokens
from mnemoseed_local.dream.ledger import TokenLedger
from mnemoseed_local.dream.prompts import parse_chunk_blocks, render_chunk_block
from mnemoseed_local.dream.reflect import (
    ChatLLM,
    ReflectedTriple,
    ReflectionResult,
    Route,
    _loads_json_array,
    _split_response,
)
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.llm.types import LLMUnavailable
from mnemoseed_local.storage.ports import AuditEntry, MetaStore

logger = logging.getLogger("mnemoseed_local.dream.verify")

VERIFY_PROMPT_VERSION = "v1"

#: Room left under the verifier window for the verdict array itself (the same
#: "generation margin" shape the dream-side ctx check uses).
VERIFY_MARGIN_TOKENS = 2048

_VERIFIED_ACTION = "ensemble_verified"
_FALLBACK_ACTION = "ensemble_verify_fallback"

#: The config role the judging seat materializes from (config.py LLM_ROLES).
_VERIFIER_ROLE = "dream_verifier"

#: Bounded verdict word map (casefolded). Words outside it are garbage.
_VERDICT_WORDS: dict[str, bool] = {
    "accept": True,
    "accepted": True,
    "reject": False,
    "rejected": False,
}

_SYSTEM_TEMPLATE = """\
You are the verification pass of a cross-model neutral memory engine.

Another model extracted each candidate triple below from the evidence chunks
rendered with it. Judge every candidate on one question only: is this triple
supported by its evidence?

Rules (mandatory):
1. Reject a candidate ONLY when the evidence does not support it, contradicts
   it, or the triple is garbled. When in doubt, accept.
2. Judge factual support, not style: never reject over wording, tone, or the
   triple's claimed confidence.
3. Judge each candidate independently — one verdict per candidate.

Output ONLY a JSON array of objects, one per candidate, in candidate order,
with these exact fields: index, verdict. verdict is one of: accept | reject.
Do not output any other text, explanation, or markdown.
"""

_USER_HEADER = "Verify these candidate triples against their evidence (deterministic candidate order):\n\n"


def build_verify_prefix() -> str:
    """The fixed side of the verify prompt (system + user header), exactly as
    sent to the judge — the doctor ctx-window check's estimate target (mirrors
    prompts.build_cache_prefix)."""
    return _SYSTEM_TEMPLATE + _USER_HEADER


class _CoverageError(Exception):
    """The verdict payload parsed but does not cover the judged set exactly
    (missing / extra / duplicate index, or a garbage field value)."""


def _coerce_index(value: Any) -> int:
    """Coerce one verdict index: an int (strict path) or a digit string.
    Anything else is garbage (covers the run back to A)."""
    if isinstance(value, bool):
        raise _CoverageError(f"index must not be a bool: {value!r}")
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        raise _CoverageError(f"index is not coercible: {value!r}") from None


def _parse_verdicts(payload: list[Any], judged_count: int) -> list[bool]:
    """Parse the judge's verdict array into per-candidate accept flags, in
    candidate order. The judged set must be covered EXACTLY once each —
    anything else covers the run back to A's original result."""
    verdicts: dict[int, bool] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise _CoverageError(f"verdict item is not an object: {item!r}")
        index = _coerce_index(item.get("index"))
        word = str(item.get("verdict", "")).strip().casefold()
        if word not in _VERDICT_WORDS:
            raise _CoverageError(f"verdict is not accept/reject: {word!r}")
        if index >= judged_count or index < 0:
            raise _CoverageError(f"verdict index {index} outside the judged set of {judged_count}")
        if index in verdicts:
            raise _CoverageError(f"duplicate verdict for index {index}")
        verdicts[index] = _VERDICT_WORDS[word]
    if len(verdicts) != judged_count:
        missing = sorted(set(range(judged_count)) - set(verdicts))
        raise _CoverageError(f"missing verdicts for indices {missing}")
    return [verdicts[i] for i in range(judged_count)]


def _render_candidates(judged: list[ReflectedTriple], by_id: dict[str, SnapshotChunk]) -> str:
    """Render the candidate-block grammar the judge consumes. Candidate order
    is the folded-triple order (deterministic); each candidate carries exactly
    its provenance-pinned evidence chunks (missing ids are skipped, never
    invented)."""
    parts: list[str] = []
    for index, triple in enumerate(judged):
        evidence = "".join(
            render_chunk_block(by_id[chunk_id]) for chunk_id in triple.chunk_ids if chunk_id in by_id
        )
        parts.append(
            "<candidate>\n"
            f"index: {index}\n"
            f"subject: {triple.subject}\n"
            f"predicate: {triple.predicate}\n"
            f"object: {triple.object}\n"
            f"route: {triple.route.value}\n"
            f"confidence: {triple.confidence:.2f}\n"
            "evidence:\n"
            f"{evidence}"
            "</candidate>\n"
        )
    return "".join(parts)


_CANDIDATE_RE = re.compile(
    r"<candidate>\n"
    r"index: (?P<index>\d+)\n"
    r"subject: (?P<subject>.*?)\n"
    r"predicate: (?P<predicate>.*?)\n"
    r"object: (?P<object>.*?)\n"
    r"route: (?P<route>[a-z]+)\n"
    r"confidence: (?P<confidence>[\d.]+)\n"
    r"evidence:\n"
    r"(?P<evidence>.*?)</candidate>",
    re.DOTALL,
)


class TripleVerifier:
    """The ensemble verify phase: model B judges model A's folded CORE triples.

    Constructed unconditionally by the daemon; the live-read
    ``dream.ensemble`` gate keeps it dormant until a configwrite flips the
    mode on (no restart, mirrors the Merger floor / DeltaPacker ceiling
    seams). Never raises into the reflect boundary.
    """

    def __init__(
        self,
        *,
        llm: ChatLLM,
        resolve_llm: Callable[[], ChatLLM] | None = None,
        config: Config,
        meta: MetaStore | None = None,
        ledger: TokenLedger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._llm = llm
        # Per-run hot-apply seam (mirrors the reflect boundary's F2 resolver):
        # materialize the verifier route AT RUN START so a configwrite change
        # lands on the next dream run without a daemon restart.
        self._resolve_llm = resolve_llm
        self._config = config
        self._meta = meta
        self._ledger = ledger
        self._clock = clock

    def verify(self, snapshot: Snapshot, result: ReflectionResult) -> ReflectionResult:
        """Judge A's folded core triples; return the result merge should see.

        Returns A's original result unchanged when the ensemble is off, when
        nothing core-routed exists to judge, or when B fails in any way (the
        fallback carries an audit record describing why)."""
        if self._config.dream.ensemble != "verify":
            return result
        judged = [t for t in result.triples if t.route is Route.CORE]
        if not judged:
            return result
        llm = self._resolve_llm() if self._resolve_llm is not None else self._llm
        model = str(getattr(llm, "model", "") or "")
        by_id = {c.chunk_id: c for c in snapshot.chunks}
        user = _USER_HEADER + _render_candidates(judged, by_id)
        window_error = self._window_overflow(user)
        if window_error is not None:
            # B1.1: never hand ollama a prompt it will silently truncate — a
            # truncated judge is worse than none, so overflow degrades honestly
            # (A's original + audit), exactly like every other B failure shape.
            return self._fallback(snapshot, result, model, "window_exceeded", window_error)
        try:
            response = llm.chat(system=_SYSTEM_TEMPLATE, user=user)
        except LLMUnavailable as exc:
            return self._fallback(snapshot, result, model, "llm_unavailable", str(exc))
        except Exception as exc:  # noqa: BLE001 - any driver error degrades as unreachable
            return self._fallback(snapshot, result, model, "llm_unavailable", str(exc))
        text, usage = _split_response(response)
        try:
            payload = _loads_json_array(text)
        except ValueError as exc:  # strict parse AND the widest-span repair lane failed
            return self._fallback(snapshot, result, model, "malformed_output", str(exc))
        try:
            verdicts = _parse_verdicts(payload, len(judged))
        except _CoverageError as exc:
            return self._fallback(snapshot, result, model, "coverage_mismatch", str(exc))

        completion = usage.completion_tokens if usage is not None and usage.completion_tokens else 0
        prompt_estimate = estimate_tokens(_SYSTEM_TEMPLATE + user)
        if self._ledger is not None:
            # Append-only telemetry (no budgets): the verify call consumed the
            # rendered candidates plus the provider-reported completion tokens.
            self._ledger.record(
                snapshot.profile_id,
                delta_tokens=prompt_estimate,
                output_tokens=completion,
            )

        core_position = 0
        rejected_keys: list[str] = []
        new_triples: list[ReflectedTriple] = []
        for triple in result.triples:
            if triple.route is not Route.CORE:
                new_triples.append(triple)
                continue
            accepted = verdicts[core_position]
            core_position += 1
            if accepted:
                new_triples.append(triple)
            else:
                # Divergence is preserved, never voted away: a rejection
                # reroutes the triple into the physical isolation track with
                # provenance and confidence untouched.
                new_triples.append(replace(triple, route=Route.ISOLATED))
                rejected_keys.append(f"{triple.subject}|{triple.predicate}|{triple.object}")
        self._audit(
            _VERIFIED_ACTION,
            {
                "run_id": snapshot.snapshot_id,
                "profile_id": snapshot.profile_id,
                "verifier_model": model,
                "verify_prompt_version": VERIFY_PROMPT_VERSION,
                "judged": len(judged),
                "accepted": len(judged) - len(rejected_keys),
                "rejected": len(rejected_keys),
                "rejected_keys": rejected_keys,
                "tokens": prompt_estimate + completion,
            },
        )
        return replace(result, triples=tuple(new_triples))

    def _window_overflow(self, user: str) -> str | None:
        """The pre-call ctx guard (B1.1, live finding Q7): estimate the rendered
        verify prompt against the verifier route's num_ctx, live-read from the
        shared Config so a configwrite window raise hot-applies to the next run.
        Ollama-only (its server-side knob); unconfigured num_ctx carries no
        guard target — the doctor check owns that hint (dream-route precedent)."""
        route = self._config.llm.get(_VERIFIER_ROLE)
        if route is None or route.driver != "ollama":
            return None
        num_ctx = route.params.get("num_ctx")
        if not isinstance(num_ctx, int) or isinstance(num_ctx, bool):
            return None
        needed = estimate_tokens(_SYSTEM_TEMPLATE + user) + VERIFY_MARGIN_TOKENS
        if needed <= num_ctx:
            return None
        return (
            f"estimated verify prompt {needed} tokens exceeds the verifier route's "
            f"num_ctx={num_ctx}; raise dream.llm.dream_verifier num_ctx or lower "
            "dream.delta_budget_ceiling_tokens"
        )

    def _fallback(
        self,
        snapshot: Snapshot,
        result: ReflectionResult,
        model: str,
        reason: str,
        detail: str,
    ) -> ReflectionResult:
        """The honest-cost fallback (design/01 decision 1): B failed, so the
        dream ships A's original unverified result — never a blocked merge —
        plus an audit record saying why."""
        logger.warning(
            "ensemble verify fell back to the unverified reflect for %s: %s (%s)",
            snapshot.snapshot_id,
            reason,
            detail,
        )
        first_line = detail.strip().splitlines()[0][:200] if detail.strip() else reason
        self._audit(
            _FALLBACK_ACTION,
            {
                "run_id": snapshot.snapshot_id,
                "profile_id": snapshot.profile_id,
                "verifier_model": model,
                "reason": reason,
                "detail": first_line,
            },
        )
        return result

    def _audit(self, action: str, detail: dict[str, Any]) -> None:
        if self._meta is None:
            return
        try:
            self._meta.audit_append(AuditEntry(actor="dream", action=action, detail=detail, at=self._clock()))
        except Exception as exc:  # noqa: BLE001 - an audit failure never breaks the dream
            logger.warning("verify audit append failed (%s): %s", action, exc)


class StubVerifyLLM:
    """Deterministic, offline verifier for tests and the manual review phase.

    Parses the candidate-block grammar back out of the user prompt and judges
    by evidence presence: a candidate with at least one evidence chunk is
    accepted, an evidenceless candidate is rejected — so the prompt and the
    harness stub can never drift apart (same contract shape as StubReflectLLM).
    """

    def chat(self, *, system: str, user: str) -> str:
        del system
        verdicts: list[dict[str, Any]] = []
        for match in _CANDIDATE_RE.finditer(user):
            accepted = len(parse_chunk_blocks(match.group("evidence"))) > 0
            verdicts.append(
                {"index": int(match.group("index")), "verdict": "accept" if accepted else "reject"}
            )
        return json.dumps(verdicts, ensure_ascii=False)
