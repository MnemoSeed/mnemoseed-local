"""Ensemble verify pass (B1; design/01 decision 1, honest-cost edition).

Model A reflects; model B judges A's folded CORE triples one by one against
their evidence chunks. A rejected triple is deterministically rerouted to
ISOLATED — divergence is preserved, never voted away. Every B failure shape
(transport, malformed output, verdict coverage mismatch) falls back to A's
original result plus an audit record; the verify pass never raises into the
reflect boundary and never blocks a merge.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from mnemoseed_local.config import Config
from mnemoseed_local.dream import (
    ReflectedTriple,
    ReflectionResult,
    ReflectOrchestrator,
    Route,
    SnapshotPhase,
    load_snapshot_file,
)
from mnemoseed_local.dream.delta import estimate_tokens
from mnemoseed_local.dream.ledger import TokenLedger
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.dream.verify import (
    VERIFY_PROMPT_VERSION,
    StubVerifyLLM,
    TripleVerifier,
)
from mnemoseed_local.llm.types import ChatResult, LLMUnavailable, Usage
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import TurnRange

_RANGE = TurnRange(0, 4)


# ---------------------------------------------------------------- fakes


class _CannedLLM:
    """Returns a fixed verdict payload; records every prompt it receives."""

    def __init__(
        self,
        verdicts: list[dict[str, object]] | str,
        *,
        completion_tokens: int | None = None,
    ) -> None:
        self._text = verdicts if isinstance(verdicts, str) else json.dumps(verdicts)
        self._completion_tokens = completion_tokens
        self.calls: list[dict[str, str]] = []
        self.model = "judge-model"

    def chat(self, *, system: str, user: str) -> ChatResult:
        self.calls.append({"system": system, "user": user})
        usage = (
            Usage(prompt_tokens=None, completion_tokens=self._completion_tokens)
            if self._completion_tokens is not None
            else None
        )
        return ChatResult(text=self._text, usage=usage, model=self.model, driver="stub")


class _UnavailableLLM:
    model = "judge-model"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system, user
        self.calls += 1
        raise LLMUnavailable("verifier route unreachable")


class _AuditMeta:
    """MetaStore-shaped audit sink double."""

    def __init__(self) -> None:
        self.entries = []
        self.token_usage: list[tuple[str, str, int]] = []

    def audit_append(self, entry) -> None:
        self.entries.append(entry)

    def add_token_usage(self, profile_id: str, year_month: str, tokens: int) -> None:
        self.token_usage.append((profile_id, year_month, tokens))


class _ExplodingMeta(_AuditMeta):
    def audit_append(self, entry) -> None:
        raise RuntimeError("audit store on fire")


# ---------------------------------------------------------------- builders


def _stamp(
    chunk_id: str,
    text: str,
    *,
    tier: CognitiveTier = CognitiveTier.TIER_1,
    origin: str = "user",
    turn_start: int | None = 0,
    turn_end: int | None = 1,
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id="alice",
        text=text,
        cognitive_tier=tier,
        model_id="anima-model" if origin == "agent" else "test-model",
        persona_id=None,
        cues=Cues(entities=[]),
        provenance=Provenance(
            asserted_by="user" if origin == "user" else "anima-model",
            session_id="s1",
            source="manual",
        ),
        turn_start=turn_start,
        turn_end=turn_end,
    )


def _snap(*chunks: ChunkStamp) -> Snapshot:
    return Snapshot(
        snapshot_id="snap-p1",
        profile_id="alice",
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in chunks),
        created_at=1000.0,
        phases=frozenset({"snapshot_done"}),
    )


def _triple(
    subject: str,
    predicate: str,
    obj: str,
    *,
    chunk_ids: tuple[str, ...],
    route: Route = Route.CORE,
    confidence: float = 0.8,
) -> ReflectedTriple:
    return ReflectedTriple(
        subject=subject,
        predicate=predicate,
        object=obj,
        tiers=(CognitiveTier.TIER_1,),
        chunk_ids=chunk_ids,
        turn_range=_RANGE,
        confidence=confidence,
        route=route,
    )


def _result(snap: Snapshot, *triples: ReflectedTriple) -> ReflectionResult:
    return ReflectionResult(
        snapshot_id=snap.snapshot_id,
        profile_id=snap.profile_id,
        turn_range=snap.turn_range,
        prompt_version="v1",
        triples=tuple(triples),
        consumed_chunk_ids=tuple(c.chunk_id for c in snap.chunks),
    )


def _config(ensemble: str = "verify") -> Config:
    config = Config()
    config.dream = replace(config.dream, ensemble=ensemble)
    return config


def _verifier(
    llm,
    *,
    config: Config | None = None,
    meta: _AuditMeta | None = None,
    resolve_llm=None,
) -> TripleVerifier:
    return TripleVerifier(
        llm=llm,
        resolve_llm=resolve_llm,
        config=config or _config(),
        meta=meta if meta is not None else _AuditMeta(),
        ledger=None,
    )


# ---------------------------------------------------------------- mode gate


def test_off_short_circuits_without_calling_b() -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"))
    result = _result(snap, _triple("user", "prefer", "pnpm", chunk_ids=("c1",)))
    llm = _CannedLLM([{"index": 0, "verdict": "reject"}])
    meta = _AuditMeta()
    verifier = _verifier(llm, config=_config("off"), meta=meta)
    out = verifier.verify(snap, result)
    assert out is result
    assert llm.calls == []
    assert meta.entries == []


def test_zero_core_triples_short_circuits_without_calling_b() -> None:
    snap = _snap(_stamp("c1", "noise"))
    result = _result(
        snap,
        _triple("assistant", "asserts", "noise", chunk_ids=("c1",), route=Route.ISOLATED),
        _triple("assistant", "claims", "junk", chunk_ids=("c1",), route=Route.SALVAGE),
    )
    llm = _CannedLLM([])
    meta = _AuditMeta()
    out = _verifier(llm, meta=meta).verify(snap, result)
    assert out is result
    assert llm.calls == []
    assert meta.entries == []


# ---------------------------------------------------------------- happy path


def test_all_accept_keeps_result_and_audits_verified() -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"), _stamp("c2", "I decided uv", turn_start=1, turn_end=2))
    t1 = _triple("user", "prefer", "pnpm", chunk_ids=("c1",))
    t2 = _triple("user", "decided", "uv", chunk_ids=("c2",))
    result = _result(snap, t1, t2)
    llm = _CannedLLM([{"index": 0, "verdict": "accept"}, {"index": 1, "verdict": "accept"}])
    meta = _AuditMeta()
    out = _verifier(llm, meta=meta).verify(snap, result)
    assert out == result
    assert len(meta.entries) == 1
    entry = meta.entries[0]
    assert entry.actor == "dream"
    assert entry.action == "ensemble_verified"
    assert entry.detail["run_id"] == snap.snapshot_id
    assert entry.detail["verifier_model"] == "judge-model"
    assert entry.detail["verify_prompt_version"] == VERIFY_PROMPT_VERSION
    assert entry.detail["judged"] == 2
    assert entry.detail["accepted"] == 2
    assert entry.detail["rejected"] == 0
    assert entry.detail["rejected_keys"] == []


def test_candidate_prompt_carries_each_core_triples_evidence() -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"))
    result = _result(snap, _triple("user", "prefer", "pnpm", chunk_ids=("c1",)))
    llm = _CannedLLM([{"index": 0, "verdict": "accept"}])
    _verifier(llm).verify(snap, result)
    assert len(llm.calls) == 1
    user = llm.calls[0]["user"]
    assert "index: 0" in user
    assert "subject: user" in user
    assert "predicate: prefer" in user
    assert "object: pnpm" in user
    assert "chunk_id: c1" in user  # evidence block rendered for the judge


def test_reject_reroutes_core_to_isolated_preserving_everything_else() -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"), _stamp("c2", "the sky is plaid", turn_start=1, turn_end=2))
    good = _triple("user", "prefer", "pnpm", chunk_ids=("c1",))
    hallucination = _triple("user", "claims", "the sky is plaid", chunk_ids=("c2",))
    result = _result(snap, good, hallucination)
    llm = _CannedLLM([{"index": 0, "verdict": "accept"}, {"index": 1, "verdict": "reject"}])
    meta = _AuditMeta()
    out = _verifier(llm, meta=meta).verify(snap, result)
    assert out.triples[0].route is Route.CORE
    rerouted = out.triples[1]
    assert rerouted.route is Route.ISOLATED  # divergence isolated, never deleted
    assert rerouted.subject == hallucination.subject
    assert rerouted.confidence == hallucination.confidence
    assert rerouted.chunk_ids == hallucination.chunk_ids
    entry = meta.entries[0]
    assert entry.action == "ensemble_verified"
    assert entry.detail["judged"] == 2
    assert entry.detail["accepted"] == 1
    assert entry.detail["rejected"] == 1
    assert entry.detail["rejected_keys"] == ["user|claims|the sky is plaid"]


def test_only_core_triples_are_judged_and_nothing_upgrades() -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"))
    core = _triple("user", "prefer", "pnpm", chunk_ids=("c1",))
    isolated = _triple("assistant", "asserts", "noise", chunk_ids=("c1",), route=Route.ISOLATED)
    salvage = _triple("system", "states", "config", chunk_ids=("c1",), route=Route.SALVAGE)
    result = _result(snap, core, isolated, salvage)
    llm = _CannedLLM([{"index": 0, "verdict": "accept"}])
    out = _verifier(llm).verify(snap, result)
    assert len(llm.calls) == 1
    assert llm.calls[0]["user"].count("<candidate>") == 1  # core-only judging
    assert out.triples[1].route is Route.ISOLATED
    assert out.triples[2].route is Route.SALVAGE


def test_verdict_word_tolerance_is_bounded() -> None:
    """Small local judges render verdicts loosely (D4 live matrix): accepted /
    rejected map into the verdict; words outside the bounded map fail coverage."""
    snap = _snap(_stamp("c1", "I prefer pnpm"), _stamp("c2", "I decided uv", turn_start=1, turn_end=2))
    result = _result(
        snap,
        _triple("user", "prefer", "pnpm", chunk_ids=("c1",)),
        _triple("user", "decided", "uv", chunk_ids=("c2",)),
    )
    llm = _CannedLLM([{"index": 0, "verdict": "Accepted"}, {"index": 1, "verdict": "REJECTED"}])
    out = _verifier(llm).verify(snap, result)
    assert out.triples[0].route is Route.CORE
    assert out.triples[1].route is Route.ISOLATED


def test_string_digit_index_coerces_and_applies() -> None:
    """The same bounded D4 coercion as the reflect lane: a judge rendering its
    index as a digit string ("1") is usable; the verdict applies positionally."""
    snap = _snap(_stamp("c1", "I prefer pnpm"), _stamp("c2", "I decided uv", turn_start=1, turn_end=2))
    result = _result(
        snap,
        _triple("user", "prefer", "pnpm", chunk_ids=("c1",)),
        _triple("user", "decided", "uv", chunk_ids=("c2",)),
    )
    llm = _CannedLLM([{"index": "0", "verdict": "accept"}, {"index": "1", "verdict": "reject"}])
    out = _verifier(llm).verify(snap, result)
    assert out.triples[0].route is Route.CORE
    assert out.triples[1].route is Route.ISOLATED


# ---------------------------------------------------------------- fallback shapes


@pytest.mark.parametrize(
    ("llm", "reason"),
    [
        (_UnavailableLLM(), "llm_unavailable"),
        (_CannedLLM("certainly not a json array"), "malformed_output"),
        # non-array top level — the widest-span repair lane salvages the inner
        # [] (same D4 tolerance the reflect lane gets); the recovered empty
        # array then fails to cover the judged set
        (_CannedLLM('{"verdicts": []}'), "coverage_mismatch"),
        (_CannedLLM([{"index": 0, "verdict": "accept"}]), "coverage_mismatch"),  # missing index
        (
            _CannedLLM(
                [
                    {"index": 0, "verdict": "accept"},
                    {"index": 1, "verdict": "accept"},
                    {"index": 2, "verdict": "accept"},
                ]
            ),
            "coverage_mismatch",
        ),  # extra index
        (
            _CannedLLM(
                [
                    {"index": 0, "verdict": "accept"},
                    {"index": 0, "verdict": "reject"},
                    {"index": 1, "verdict": "accept"},
                ]
            ),
            "coverage_mismatch",
        ),  # duplicate index
        (
            _CannedLLM(
                [
                    {"index": 0, "verdict": "maybe"},
                    {"index": 1, "verdict": "accept"},
                ]
            ),
            "coverage_mismatch",
        ),  # verdict word outside the bounded map
        (
            _CannedLLM(
                [
                    {"index": True, "verdict": "accept"},
                    {"index": 1, "verdict": "accept"},
                ]
            ),
            "coverage_mismatch",
        ),  # a bool is never an index
        (
            _CannedLLM(
                [
                    {"index": 0, "verdict": "accept"},
                    {"index": -1, "verdict": "accept"},
                ]
            ),
            "coverage_mismatch",
        ),  # negative index
    ],
)
def test_every_b_failure_falls_back_to_a_original_with_audit(llm, reason: str) -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"), _stamp("c2", "I decided uv", turn_start=1, turn_end=2))
    t1 = _triple("user", "prefer", "pnpm", chunk_ids=("c1",))
    t2 = _triple("user", "decided", "uv", chunk_ids=("c2",))
    result = _result(snap, t1, t2)
    meta = _AuditMeta()
    out = _verifier(llm, meta=meta).verify(snap, result)
    assert out is result  # A's original, untouched
    assert len(meta.entries) == 1
    entry = meta.entries[0]
    assert entry.actor == "dream"
    assert entry.action == "ensemble_verify_fallback"
    assert entry.detail["run_id"] == snap.snapshot_id
    assert entry.detail["verifier_model"] == "judge-model"
    assert entry.detail["reason"] == reason
    assert isinstance(entry.detail["detail"], str) and entry.detail["detail"]


def test_audit_sink_failure_never_breaks_the_fallback() -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"))
    result = _result(snap, _triple("user", "prefer", "pnpm", chunk_ids=("c1",)))
    out = _verifier(_UnavailableLLM(), meta=_ExplodingMeta()).verify(snap, result)
    assert out is result


def test_fallback_records_no_verdict_audit_and_no_ledger() -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"))
    result = _result(snap, _triple("user", "prefer", "pnpm", chunk_ids=("c1",)))
    meta = _AuditMeta()
    verifier = TripleVerifier(
        llm=_UnavailableLLM(), config=_config(), meta=meta, ledger=TokenLedger(meta=meta)
    )
    verifier.verify(snap, result)
    assert [e.action for e in meta.entries] == ["ensemble_verify_fallback"]
    assert meta.token_usage == []  # no completed call, nothing attributable


# ---------------------------------------------------------------- seams


def test_resolver_is_pinned_per_run_and_hot_apply_flips_the_mode() -> None:
    """The ensemble mode is read live off the shared Config (configwrite
    hot-apply): off -> verify between two runs flips B on the next run, and a
    set dream.llm.dream_verifier materializes through the per-run resolver."""
    snap = _snap(_stamp("c1", "I prefer pnpm"))
    result = _result(snap, _triple("user", "prefer", "pnpm", chunk_ids=("c1",)))
    config = _config("off")
    resolutions: list[str] = []

    def _resolve() -> _CannedLLM:
        resolutions.append("resolved")
        return _CannedLLM([{"index": 0, "verdict": "accept"}])

    verifier = TripleVerifier(llm=_CannedLLM([]), resolve_llm=_resolve, config=config, meta=_AuditMeta())
    verifier.verify(snap, result)
    assert resolutions == []  # off: B not even resolved
    config.dream = replace(config.dream, ensemble="verify")
    out = verifier.verify(snap, result)
    assert resolutions == ["resolved"]
    assert out == result


def test_verifier_tokens_are_recorded_additively() -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"))
    result = _result(snap, _triple("user", "prefer", "pnpm", chunk_ids=("c1",)))
    llm = _CannedLLM([{"index": 0, "verdict": "accept"}], completion_tokens=7)
    meta = _AuditMeta()
    verifier = TripleVerifier(llm=llm, config=_config(), meta=meta, ledger=TokenLedger(meta=meta))
    verifier.verify(snap, result)
    assert len(meta.token_usage) == 1
    _, _, tokens = meta.token_usage[0]
    prompt_estimate = estimate_tokens(llm.calls[0]["system"] + llm.calls[0]["user"])
    assert tokens == prompt_estimate + 7
    entry = meta.entries[0]
    assert entry.action == "ensemble_verified"
    assert entry.detail["tokens"] == prompt_estimate + 7


# ---------------------------------------------------------------- stub judge


def test_stub_judge_accepts_evidenced_and_rejects_evidenceless_candidates() -> None:
    snap = _snap(_stamp("c1", "I prefer pnpm"))
    evidenced = _triple("user", "prefer", "pnpm", chunk_ids=("c1",))
    dangling = _triple("user", "prefer", "yarn", chunk_ids=("missing-chunk",))
    result = _result(snap, evidenced, dangling)
    verifier = _verifier(StubVerifyLLM())
    out = verifier.verify(snap, result)
    assert out.triples[0].route is Route.CORE
    assert out.triples[1].route is Route.ISOLATED  # no evidence to judge against


# ---------------------------------------------------------------- reflect integration


def test_reflect_runs_the_verifier_before_finalize(tmp_path) -> None:
    """The verifier seat sits between assembly and the atomic journal write:
    the REFLECT_DONE payload carries the VERIFIED result (reroutes included),
    so merge and crash-resume consume exactly what B judged."""
    from mnemoseed_local.dream import StubReflectLLM

    snap = _snap(_stamp("c1", "I decided uv"))

    class _SpyVerifier:
        def __init__(self) -> None:
            self.seen: list[ReflectionResult] = []

        def verify(self, snapshot: Snapshot, result: ReflectionResult) -> ReflectionResult:
            self.seen.append(result)
            rerouted = tuple(
                replace(t, route=Route.ISOLATED) if t.route is Route.CORE else t for t in result.triples
            )
            return replace(result, triples=rerouted)

    spy = _SpyVerifier()
    orchestrator = ReflectOrchestrator(
        llm=StubReflectLLM(), directory=tmp_path, sleep=lambda _: None, verifier=spy
    )
    outcome = orchestrator.reflect(snap)
    assert outcome.ok is True
    assert spy.seen, "the verifier never ran"
    on_disk = load_snapshot_file(tmp_path / f"{snap.snapshot_id}.json")
    assert on_disk is not None
    assert SnapshotPhase.REFLECT_DONE.value in on_disk.phases
    assert outcome.result is not None
    for triple in outcome.result.triples:
        assert triple.route is Route.ISOLATED


def test_reflect_without_verifier_journals_the_plain_result(tmp_path) -> None:
    from mnemoseed_local.dream import StubReflectLLM

    snap = _snap(_stamp("c1", "I decided uv"))
    orchestrator = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path, sleep=lambda _: None)
    outcome = orchestrator.reflect(snap)
    assert outcome.ok is True
    assert outcome.result is not None
    assert any(t.route is Route.CORE for t in outcome.result.triples)


# ---------------------------------------------------------------- stub driver (B1 T3)


def test_stub_verifier_driver_resolves_and_judges_offline() -> None:
    """B1 T3: driver = "stub_verifier" wraps the deterministic StubVerifyLLM in
    the full DreamLLM port, so a config route can select it by name — daemon
    integration tests judge offline, never touching a network."""
    from mnemoseed_local.llm.registry import LLM_DRIVERS

    llm = LLM_DRIVERS.build("stub_verifier", {"model": "stubjudge"})
    assert llm.check().ok is True
    assert llm.model == "stubjudge"
    snap = _snap(_stamp("c1", "I prefer pnpm"))
    evidenced = _triple("user", "prefer", "pnpm", chunk_ids=("c1",))
    dangling = _triple("user", "prefer", "yarn", chunk_ids=("gone",))
    out = _verifier(llm).verify(snap, _result(snap, evidenced, dangling))
    assert out.triples[0].route is Route.CORE
    assert out.triples[1].route is Route.ISOLATED
