"""B1.1: the verifier ctx-window guard + doctor check (live finding Q7).

Live finding from the 2026-08-18 bidirectional pairing: 25 judged candidates
rendered an 18287-token verify prompt — beyond the factory verifier route's
num_ctx=16384, where ollama silently truncates the judging context (the same
D4 trap shape the dream route already guards). The fix is two-sided:

- RUNTIME: the TripleVerifier estimates its own rendered prompt against the
  verifier route's num_ctx BEFORE calling B. Overflow degrades honestly: A's
  original result ships unverified + `window_exceeded` fallback audit (never
  a silently truncated judge, never a blocked merge).
- STATIC (doctor): a config-sanity check for the verify seat mirroring the
  dream-side ctx-window check (advisory formula with its assumptions labeled).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mnemoseed_local.config import Config
from mnemoseed_local.dream import ReflectedTriple, ReflectionResult, Route
from mnemoseed_local.dream.snapshot import Snapshot, SnapshotChunk
from mnemoseed_local.dream.verify import StubVerifyLLM, TripleVerifier
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.ports import TurnRange

_RANGE = TurnRange(0, 4)


class _AuditMeta:
    def __init__(self) -> None:
        self.entries = []

    def audit_append(self, entry) -> None:
        self.entries.append(entry)


class _CountingJudge(StubVerifyLLM):
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *, system: str, user: str) -> str:
        self.calls += 1
        return super().chat(system=system, user=user)


def _stamp(chunk_id: str, text: str) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id="alice",
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        persona_id=None,
        cues=Cues(entities=[]),
        provenance=Provenance(asserted_by="user", session_id="s1", source="manual"),
        turn_start=0,
        turn_end=1,
    )


def _snap(*chunks: ChunkStamp) -> Snapshot:
    return Snapshot(
        snapshot_id="snap-window",
        profile_id="alice",
        turn_range=_RANGE,
        chunks=tuple(SnapshotChunk.from_stamp(c) for c in chunks),
        created_at=1000.0,
        phases=frozenset({"snapshot_done"}),
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


def _core(chunk_id: str) -> ReflectedTriple:
    return ReflectedTriple(
        subject="user",
        predicate="prefer",
        object="pnpm",
        tiers=(CognitiveTier.TIER_1,),
        chunk_ids=(chunk_id,),
        turn_range=_RANGE,
        confidence=0.8,
        route=Route.CORE,
    )


def _config_with_verifier_ctx(num_ctx: int | None, *, driver: str = "ollama") -> Config:
    config = Config()
    config.dream = replace(config.dream, ensemble="verify")
    route = config.llm["dream_verifier"]
    params = dict(route.params)
    if num_ctx is None:
        params.pop("num_ctx", None)
    else:
        params["num_ctx"] = num_ctx
    config.llm["dream_verifier"] = replace(route, driver=driver, params=params)
    return config


# ~70k chars of English -> estimate_tokens far beyond a 16384 window, so the
# rendered verify prompt cannot fit (17088 estimated + margin > 16384).
_BIG_TEXT = "verification window " * 4000


def test_window_exceeded_trips_before_calling_b_with_audit_reason() -> None:
    snap = _snap(_stamp("c1", _BIG_TEXT))
    result = _result(snap, _core("c1"))
    judge = _CountingJudge()
    meta = _AuditMeta()
    verifier = TripleVerifier(llm=judge, config=_config_with_verifier_ctx(16384), meta=meta)
    out = verifier.verify(snap, result)
    assert out is result  # A's original ships unverified (honest degrade)
    assert judge.calls == 0  # never a silently truncated judge
    assert len(meta.entries) == 1
    entry = meta.entries[0]
    assert entry.action == "ensemble_verify_fallback"
    assert entry.detail["reason"] == "window_exceeded"
    assert "num_ctx" in entry.detail["detail"]


def test_window_guard_passes_when_the_prompt_fits() -> None:
    snap = _snap(_stamp("c1", _BIG_TEXT))
    result = _result(snap, _core("c1"))
    judge = _CountingJudge()
    meta = _AuditMeta()
    verifier = TripleVerifier(llm=judge, config=_config_with_verifier_ctx(36864), meta=meta)
    verifier.verify(snap, result)
    assert judge.calls == 1
    assert [e.action for e in meta.entries] == ["ensemble_verified"]


def test_window_guard_skips_when_the_verifier_route_is_not_ollama() -> None:
    """num_ctx is an ollama server knob; a non-ollama verifier route's window
    is out of reach (same precedent as the dream-side checks)."""
    snap = _snap(_stamp("c1", _BIG_TEXT))
    result = _result(snap, _core("c1"))
    judge = _CountingJudge()
    verifier = TripleVerifier(
        llm=judge,
        config=_config_with_verifier_ctx(16384, driver="openai_compatible"),
        meta=_AuditMeta(),
    )
    verifier.verify(snap, result)
    assert judge.calls == 1  # unguarded, verified normally


def test_window_guard_skips_when_num_ctx_is_unconfigured() -> None:
    """Unconfigured num_ctx carries no guard target; the doctor check owns the
    hint text (same split as the dream route)."""
    snap = _snap(_stamp("c1", _BIG_TEXT))
    result = _result(snap, _core("c1"))
    judge = _CountingJudge()
    verifier = TripleVerifier(llm=judge, config=_config_with_verifier_ctx(None), meta=_AuditMeta())
    verifier.verify(snap, result)
    assert judge.calls == 1


def test_window_guard_reads_num_ctx_live() -> None:
    """The ctx gate hot-applies: raising num_ctx via the shared Config flips
    the next run from window_exceeded fallback to judging, no rebuild."""
    small = _snap(_stamp("c1", _BIG_TEXT))
    config = _config_with_verifier_ctx(16384)
    judge = _CountingJudge()
    meta = _AuditMeta()
    verifier = TripleVerifier(llm=judge, config=config, meta=meta)
    verifier.verify(small, _result(small, _core("c1")))
    assert judge.calls == 0
    route = config.llm["dream_verifier"]
    config.llm["dream_verifier"] = replace(route, params={**route.params, "num_ctx": 36864})
    verifier.verify(small, _result(small, _core("c1")))
    assert judge.calls == 1
    assert [e.action for e in meta.entries] == ["ensemble_verify_fallback", "ensemble_verified"]


# ---------------------------------------------------------------- doctor (B1.1 T2)


_ISOLATED_BLOCK = '[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n'


class _FakeDoctorStores:
    def close(self) -> None:
        pass


class _FakeDoctorLLM:
    def check(self):
        from mnemoseed_local.llm.types import HealthReport

        return HealthReport(ok=True, detail={"models": ["qwen3.5:9b", "gemma4:e4b"]})


class _FakeDoctorRouter:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def resolve(self, role: str) -> _FakeDoctorLLM:
        del role
        return _FakeDoctorLLM()


def _mock_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mnemoseed_local.storage.factory.build_stores", lambda config: _FakeDoctorStores())
    monkeypatch.setattr("mnemoseed_local.llm.RoleRouter", _FakeDoctorRouter)
    monkeypatch.setattr("mnemoseed_local.hardware.probe_max_vram_gb", lambda: 0.0)
    monkeypatch.setattr("mnemoseed_local.hardware.probe_ram_gb", lambda: None)


def _write_doctor_config(home: Path, extra: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        'preset = "embedded"\n' + _ISOLATED_BLOCK + extra + "[dream.llm.dream]\nnum_ctx = 40000\n",
        encoding="utf-8",
    )


@pytest.fixture
def doctor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".mnemoseed-local"
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", home / "config.toml")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    _mock_backend(monkeypatch)
    return home


def _ctx_line(out: str) -> str:
    return next(line for line in out.splitlines() if "verifier ctx window" in line)


def test_doctor_verifier_ctx_window_fails_on_factory_gap(doctor_home: Path, capsys) -> None:
    """Q7 made visible: verify ON with the factory verifier num_ctx=16384 vs
    the 32000 delta ceiling is an honest FAIL naming both fix keys."""
    from mnemoseed_local.cli import main

    _write_doctor_config(doctor_home, '[dream]\nensemble = "verify"\n')
    assert main(["doctor"]) == 1
    line = _ctx_line(capsys.readouterr().out)
    assert "FAIL" in line
    assert "num_ctx" in line
    assert "delta_budget_ceiling_tokens" in line


def test_doctor_verifier_ctx_window_passes_when_fitting(doctor_home: Path, capsys) -> None:
    from mnemoseed_local.cli import main

    _write_doctor_config(
        doctor_home,
        '[dream]\nensemble = "verify"\n[dream.llm.dream_verifier]\nnum_ctx = 67000\n',
    )
    assert main(["doctor"]) == 0
    line = _ctx_line(capsys.readouterr().out)
    assert "ok" in line


def test_doctor_verifier_ctx_window_skips_when_ensemble_off(doctor_home: Path, capsys) -> None:
    from mnemoseed_local.cli import main

    _write_doctor_config(doctor_home, "")  # default ensemble = "off"
    assert main(["doctor"]) == 0
    assert "skipped" in _ctx_line(capsys.readouterr().out)


def test_doctor_verifier_ctx_window_skips_non_ollama_route(doctor_home: Path, capsys) -> None:
    from mnemoseed_local.cli import main

    _write_doctor_config(
        doctor_home,
        '[dream]\nensemble = "verify"\n'
        '[dream.llm.dream_verifier]\ndriver = "openai_compatible"\nmodel = "judge"\n',
    )
    assert main(["doctor"]) == 0
    line = _ctx_line(capsys.readouterr().out)
    assert "not ollama" in line
    assert "skipped" in line
