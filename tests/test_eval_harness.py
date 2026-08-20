"""B3 T2 — scratch eval rig: production dream chain over disposable stores (PRD-B3).

The rig mirrors `daemon.app._build_capture` wiring (graph main+isolated double
instance, shared ledger, one live Config) over stores rooted ENTIRELY under a
caller-given directory — a run never touches the live config/data dirs, so a
live ollama matrix can run next to a live daemon. Unit tests drive the rig
over the deterministic stub seats; live seats only swap routes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.eval.canary import canary_session
from mnemoseed_local.eval.harness import (
    COLLAPSE_MAX_COMPLETION_TOKENS,
    EvalCell,
    EvalRig,
    EvalRoute,
    RigPaths,
    is_reflect_collapse,
)
from mnemoseed_local.llm.types import ChatResult, Usage

STUB_A = EvalRoute(driver="stub", model="stub-a")
STUB_B = EvalRoute(driver="stub_verifier", model="stub-b")


def _stub_cell(**overrides: object) -> EvalCell:
    base = {
        "reflect": STUB_A,
        "ensemble": "off",
        "verifier": STUB_B,
        "delta_budget_tokens": 32000,
        "core_confidence_floor": 0.0,
    }
    base.update(overrides)
    return EvalCell(**base)  # type: ignore[arg-type]


@pytest.fixture
def rig(tmp_path: Path) -> EvalRig:
    rig = EvalRig(RigPaths(root=tmp_path / "rig"), _stub_cell())
    yield rig
    rig.close()


def _signatures(run) -> list[tuple[str, str, str, str, str]]:
    nodes = sorted(
        (*run.core_nodes, *run.isolated_nodes),
        key=lambda n: (n.graph, n.subject, n.predicate, n.object),
    )
    return [(n.graph, n.subject, n.predicate, n.object, n.polarity) for n in nodes]


def test_stub_full_pass_writes_core_nodes(rig: EvalRig) -> None:
    session = canary_session(21, facts=4, noise=2)
    run = rig.run_canary(session)
    assert run.merge_committed
    assert run.merge_summary is not None
    assert run.core_nodes, "stub dream wrote no core nodes"
    predicates = {n.predicate for n in run.core_nodes}
    assert predicates & {"prefers", "has_habit", "decided", "believes"}
    # safe clear: every chunk the model consumed is marked consolidated
    assert run.chunks, "no chunks captured"
    assert all(chunk.consolidated for chunk in run.chunks)
    # turn<->chunk attribution is exact under the one-item-per-session shape
    assert len(run.turn_sessions) == len(session.turns) == len(run.chunks)


def test_verify_mode_judges_and_audits(tmp_path: Path) -> None:
    rig = EvalRig(RigPaths(root=tmp_path / "rig"), _stub_cell(ensemble="verify"))
    try:
        session = canary_session(22, facts=4, noise=2)
        run = rig.run_canary(session)
    finally:
        rig.close()
    verified = [a for a in run.audit if a.action == "ensemble_verified"]
    assert verified, "no ensemble_verified audit entry"
    detail = verified[0].detail
    assert detail["judged"] > 0
    assert detail["accepted"] == detail["judged"] - detail["rejected"]
    assert detail["verifier_model"] == "stub-b"


def test_off_mode_never_calls_verifier(rig: EvalRig) -> None:
    run = rig.run_canary(canary_session(23, facts=4, noise=2))
    ensemble_actions = {a.action for a in run.audit if a.action.startswith("ensemble_")}
    assert ensemble_actions == set()


def test_floor_downgrade_routes_isolated(tmp_path: Path) -> None:
    rig = EvalRig(RigPaths(root=tmp_path / "rig"), _stub_cell(core_confidence_floor=0.99))
    try:
        run = rig.run_canary(canary_session(24, facts=4, noise=2))
    finally:
        rig.close()
    assert run.core_nodes == (), "below-floor triples must not land in core"
    assert run.isolated_nodes, "below-floor triples must be preserved in the isolated graph"


def test_token_usage_and_duration_recorded(rig: EvalRig) -> None:
    run = rig.run_canary(canary_session(25, facts=4, noise=2))
    assert run.token_usage > 0
    assert run.duration_s > 0.0
    assert run.reflect_result is not None  # the journaled (post-verify) result


def test_zero_write_outside_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = tmp_path / "forbidden-config-home"
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", forbidden)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", forbidden)
    monkeypatch.setattr("mnemoseed_local.dream.reflect.CONFIG_DIR", forbidden)
    rig = EvalRig(RigPaths(root=tmp_path / "rig"), _stub_cell())
    try:
        rig.run_canary(canary_session(26, facts=4, noise=2))
    finally:
        rig.close()
    assert not forbidden.exists(), "rig wrote outside its root (live config/data dir shape)"


def test_repeat_runs_semantically_deterministic(tmp_path: Path) -> None:
    runs = []
    for index in range(2):
        rig = EvalRig(RigPaths(root=tmp_path / f"rig-{index}"), _stub_cell())
        try:
            runs.append(rig.run_canary(canary_session(27, facts=4, noise=2)))
        finally:
            rig.close()
    assert _signatures(runs[0]) == _signatures(runs[1])
    assert sorted(a.action for a in runs[0].audit) == sorted(a.action for a in runs[1].audit)
    assert [(c.session_id, c.text) for c in runs[0].chunks] == [
        (c.session_id, c.text) for c in runs[1].chunks
    ]


def test_cell_id_deterministic_and_discriminating() -> None:
    a = _stub_cell()
    assert a.cell_id == _stub_cell().cell_id
    assert a.cell_id != _stub_cell(reflect=EvalRoute(driver="ollama", model="qwen3.5:9b")).cell_id
    assert a.cell_id != _stub_cell(ensemble="verify").cell_id
    assert a.cell_id != _stub_cell(verifier=EvalRoute(driver="stub_verifier", model="stub-c")).cell_id
    assert a.cell_id != _stub_cell(delta_budget_tokens=8192).cell_id
    assert a.cell_id != _stub_cell(core_confidence_floor=0.5).cell_id


# ---------------------------------------------------------------- B4a collapse guard


def _collapse_stub_chat(original, collapse: int = 1):
    """StubLLM.chat stand-in: collapses (literal [] + completion=2) for the
    first ``collapse`` calls, then delegates to the real deterministic stub.
    Returns (fake, calls) — calls() reads the attempt counter."""
    calls = 0

    def fake(self, *, system: str, user: str) -> ChatResult:
        nonlocal calls
        calls += 1
        if calls <= collapse:
            return ChatResult(text="[]", usage=Usage(completion_tokens=2), model=self.model, driver="stub")
        return original(self, system=system, user=user)

    return fake, lambda: calls


def test_collapse_fingerprint_boundary() -> None:
    """The classifier fires ONLY on the RCA fingerprint: verbatim [] with a
    tiny completion count. Legit empty extractions never match."""
    base = dict(model="m", driver="ollama")
    collapse = ChatResult(text="[]", usage=Usage(completion_tokens=2), **base)
    assert is_reflect_collapse(collapse)
    # boundary pin: one token above the collapse fingerprint is already normal
    assert not is_reflect_collapse(
        ChatResult(text="[]", usage=Usage(completion_tokens=COLLAPSE_MAX_COMPLETION_TOKENS + 1), **base)
    )
    assert not is_reflect_collapse(ChatResult(text="[]", usage=Usage(completion_tokens=12), **base))
    # formatting is not the verbatim fingerprint
    assert not is_reflect_collapse(ChatResult(text="[ ]", usage=Usage(completion_tokens=2), **base))
    # no provider usage (plain-text / stub seats) is never a collapse
    assert not is_reflect_collapse("[]")
    assert not is_reflect_collapse(ChatResult(text="[]", usage=None, **base))
    # a real extraction is never a collapse
    assert not is_reflect_collapse(
        ChatResult(text='[{"predicate": "prefers"}]', usage=Usage(completion_tokens=60), **base)
    )


def test_collapse_retry_recovers_and_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse fingerprint -> typed exception -> retry loop engages -> second
    call extracts fully -> cell completes with attempts + recovery recorded."""
    from mnemoseed_local.llm.drivers.stub import StubLLM

    fake, calls = _collapse_stub_chat(StubLLM.chat, collapse=1)
    monkeypatch.setattr(StubLLM, "chat", fake)
    rig = EvalRig(RigPaths(root=tmp_path / "rig"), _stub_cell(), sleep=lambda _: None)
    try:
        run = rig.run_canary(canary_session(51, facts=4, noise=2))
    finally:
        rig.close()
    assert calls() == 2  # one collapse, then one full-extraction retry
    assert run.merge_committed
    assert run.core_nodes, "the recovered attempt must extract the facts"
    assert run.reflect_collapse_attempts == 1
    assert run.reflect_recovered is True


def test_collapse_every_retry_records_honestly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse on every retry: no crash, honest counts and a failed outcome."""
    from mnemoseed_local.llm.drivers.stub import StubLLM

    fake, calls = _collapse_stub_chat(StubLLM.chat, collapse=99)
    monkeypatch.setattr(StubLLM, "chat", fake)
    rig = EvalRig(RigPaths(root=tmp_path / "rig"), _stub_cell(), sleep=lambda _: None)
    try:
        run = rig.run_canary(canary_session(52, facts=4, noise=2))
    finally:
        rig.close()
    assert calls() == 3  # max_retries=2 -> exactly 3 attempts, no crash
    assert run.reflect_collapse_attempts == 3
    assert run.reflect_recovered is False
    assert run.merge_committed is False
    assert run.reflect_outcome is not None
    assert run.reflect_outcome.ok is False
    assert "collapse" in (run.reflect_outcome.error or "")


def test_legit_empty_extraction_not_classified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed empty extraction under NORMAL token counts is not a
    collapse: accepted without retry, no collapse fields set."""
    from mnemoseed_local.llm.drivers.stub import StubLLM

    calls = 0

    def legit_empty(self, *, system: str, user: str) -> ChatResult:
        nonlocal calls
        calls += 1
        return ChatResult(text="[]", usage=Usage(completion_tokens=12), model=self.model, driver="stub")

    monkeypatch.setattr(StubLLM, "chat", legit_empty)
    rig = EvalRig(RigPaths(root=tmp_path / "rig"), _stub_cell(), sleep=lambda _: None)
    try:
        run = rig.run_canary(canary_session(53, facts=4, noise=2))
    finally:
        rig.close()
    assert calls == 1  # accepted as a legit empty extraction: no retry
    assert run.reflect_collapse_attempts == 0
    assert run.reflect_recovered is False
    assert run.merge_committed  # a genuinely empty (no overflow) merge still commits


def test_rig_records_seat_seed_from_reflect_route(tmp_path: Path) -> None:
    cell = EvalCell(
        reflect=EvalRoute(driver="stub", model="stub-a", params=(("seed", 42),)),
        ensemble="off",
        verifier=STUB_B,
    )
    rig = EvalRig(RigPaths(root=tmp_path / "rig"), cell)
    try:
        run = rig.run_canary(canary_session(54, facts=4, noise=2))
    finally:
        rig.close()
    assert run.seat_seed == 42
    assert run.reflect_collapse_attempts == 0
    assert run.reflect_recovered is False
