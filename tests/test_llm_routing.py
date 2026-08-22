"""Role routing: [dream.llm] config, lazy resolution, env-var key substitution,
audit logging, and boot-safe isolation (PRD-02 T6; FR-2.14 / design/02 §6-§7),
A2 local trim.

Behavior pinned here: the A2 MVP has ONE dream role ("dream") defaulting to
the local ollama track (openai_compatible kept as the fallback driver); legacy
deep_reflection / short_increment / local_track tables are tolerated and
ignored with a warning; API keys are referenced by env-var NAME and never
stored as values; drivers materialize lazily per-role so a misconfigured role
never breaks boot; and RoleRouter.check() (the console test button) never
raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.config import (
    DEFAULT_LLM_ROUTES,
    LLM_ROLES,
    Config,
    ConfigError,
    RoleLLMConfig,
    load_config,
)
from mnemoseed_local.llm import (
    ChatResult,
    HealthReport,
    LLMDriverInfo,
    LLMRouteError,
    LLMUnavailable,
    RoleRouter,
    UnknownLLMDriverError,
)
from mnemoseed_local.llm.drivers.ollama import OllamaLLM
from mnemoseed_local.llm.drivers.openai_compatible import OpenAICompatibleLLM
from mnemoseed_local.llm.registry import LLMRegistry, register


def _write(path: Any, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- config defaults


def test_default_roles_are_exactly_the_dream_and_verifier_roles() -> None:
    """B5 (vote seat B): the third role is the independent vote generator
    ``dream_vote`` (a separate seat from the judging verifier). Vote is a
    second full generation over the same delta, so it needs its own route —
    never the same model as A and never the cheaper judge model. It has no
    factory route: unconfigured, the daemon falls back to the dream_verifier
    route (still a distinct model from A)."""
    assert LLM_ROLES == ("dream", "dream_verifier", "dream_vote")
    assert set(DEFAULT_LLM_ROUTES) == {"dream", "dream_verifier"}
    assert "dream_vote" not in DEFAULT_LLM_ROUTES


def test_defaults_follow_the_local_ollama_track(monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(Path("/nonexistent/config.toml"))
    dream = cfg.llm["dream"]
    assert dream.driver == "ollama"
    assert dream.model == "qwen3.5:9b"
    assert dream.params["base_url"] == "http://localhost:11434"
    assert set(cfg.llm) == set(DEFAULT_LLM_ROUTES)  # dream_vote has no factory route


def test_verifier_role_defaults_follow_the_small_judge_track(monkeypatch) -> None:
    """B1 (design/01 decision 1): verification is a judging task — simpler and
    cheaper than generation — so the factory verifier route defaults to the
    small judge model on the same local ollama track. Judging is structured
    output: thinking is pinned OFF and a working ctx window ships by default
    (same D4 failure shape as the dream route)."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(Path("/nonexistent/config.toml"))
    verifier = cfg.llm["dream_verifier"]
    assert verifier.driver == "ollama"
    assert verifier.model == "gemma4:e4b"
    assert verifier.params["base_url"] == "http://localhost:11434"
    assert verifier.params["think"] is False
    assert verifier.params["num_ctx"] == 16384


def test_default_key_references_are_env_var_names_never_literals() -> None:
    for role in LLM_ROLES:
        route = DEFAULT_LLM_ROUTES.get(role)
        if route is None:
            continue  # B5 dream_vote has no factory route (resolves via fallback)
        env_name = route.params.get("api_key_env")
        if env_name is not None:
            assert env_name == env_name.upper()
            assert env_name and not any(ch.isspace() for ch in env_name)
    # no literal-looking secret anywhere in the defaults
    blob = str(DEFAULT_LLM_ROUTES).lower()
    assert "sk-" not in blob


# ---------------------------------------------------------------- config parsing


def test_dream_llm_table_parses(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        'preset = "embedded"\n'
        "[dream.llm.dream]\n"
        'driver = "ollama"\n'
        'model = "qwen2.5:7b"\n'
        'base_url = "http://127.0.0.1:11434"\n',
    )
    dream = load_config(p).llm["dream"]
    assert dream.driver == "ollama"
    assert dream.model == "qwen2.5:7b"
    assert dream.params["base_url"] == "http://127.0.0.1:11434"


def test_dream_verifier_llm_table_parses(tmp_path, monkeypatch) -> None:
    """B1: a [dream.llm.dream_verifier] override table parses per role; an
    untouched verifier keeps the factory judge-model route."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        'preset = "embedded"\n'
        "[dream.llm.dream_verifier]\n"
        'driver = "openai_compatible"\n'
        'model = "judge-model"\n'
        'api_key_env = "MNEMOSEED_VERIFIER_API_KEY"\n',
    )
    cfg = load_config(p)
    verifier = cfg.llm["dream_verifier"]
    assert verifier.driver == "openai_compatible"
    assert verifier.model == "judge-model"
    assert verifier.params["api_key_env"] == "MNEMOSEED_VERIFIER_API_KEY"
    assert cfg.llm["dream"].driver == "ollama"


def test_dream_vote_llm_table_parses(tmp_path, monkeypatch) -> None:
    """B5: a [dream.llm.dream_vote] override table parses per role; an
    untouched vote seat keeps the factory independent-generator route."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        'preset = "embedded"\n'
        "[dream.llm.dream_vote]\n"
        'driver = "openai_compatible"\n'
        'model = "vote-model"\n'
        'api_key_env = "MNEMOSEED_VOTE_API_KEY"\n',
    )
    cfg = load_config(p)
    vote = cfg.llm["dream_vote"]
    assert vote.driver == "openai_compatible"
    assert vote.model == "vote-model"
    assert vote.params["api_key_env"] == "MNEMOSEED_VOTE_API_KEY"
    assert cfg.llm["dream"].driver == "ollama"


def test_dream_vote_no_default_requires_explicit_driver_and_model(tmp_path, monkeypatch) -> None:
    """B5: dream_vote has no factory route, so a partial table (driver but no
    model) is a load error naming the role — never a silent empty-model route."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream.llm.dream_vote]\ndriver = "openai_compatible"\n')
    with pytest.raises(ConfigError, match="no default route"):
        load_config(p)


def test_legacy_role_tables_are_accepted_ignored_with_deprecation_warning(
    tmp_path, monkeypatch, caplog
) -> None:
    """A2 trim: legacy deep_reflection / short_increment / local_track tables
    must not break a user's existing config — they are tolerated, logged as a
    deprecation, and ignored."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        'preset = "embedded"\n'
        "[dream.llm.deep_reflection]\n"
        'driver = "openai_compatible"\n'
        'model = "kimi-k3"\n'
        "[dream.llm.short_increment]\n"
        'driver = "openai_compatible"\n'
        'model = "deepseek-v4-flash-0731"\n'
        "[dream.llm.local_track]\n"
        'driver = "ollama"\n'
        'model = "llama3.1:8b"\n',
    )
    with caplog.at_level("WARNING", logger="mnemoseed_local.config"):
        cfg = load_config(p)
    assert set(cfg.llm) == {"dream", "dream_verifier"}
    assert cfg.llm["dream"].driver == "ollama"  # defaults intact
    assert cfg.llm["dream"].model == "qwen3.5:9b"
    assert "deprecated" in caplog.text.lower()


def test_dream_llm_unknown_role_still_names_the_key(tmp_path, monkeypatch) -> None:
    """The deprecation tolerance is scoped to the legacy roles only: any other
    unknown role remains a hard config error."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(
        p,
        'preset = "embedded"\n[dream.llm.extra_role]\ndriver = "openai_compatible"\nmodel = "m"\n',
    )
    with pytest.raises(ConfigError, match=r"config\[dream.llm.extra_role\]"):
        load_config(p)


def test_dream_llm_partial_override_inherits_driver_and_model(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream.llm.dream]\napi_key_env = "MY_ALT_KEY"\n')
    cfg = load_config(p)
    dream = cfg.llm["dream"]
    assert dream.driver == "ollama"  # inherited
    assert dream.model == "qwen3.5:9b"  # inherited
    assert dream.params["api_key_env"] == "MY_ALT_KEY"


def test_dream_llm_must_be_a_table(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream]\nllm = "nope"\n')
    with pytest.raises(ConfigError, match=r"config\[dream.llm\]"):
        load_config(p)


def test_dream_llm_role_must_be_a_table(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream.llm]\ndream = "nope"\n')
    with pytest.raises(ConfigError, match=r"config\[dream.llm.dream\]"):
        load_config(p)


def test_dream_llm_bad_driver_type_names_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream.llm.dream]\ndriver = 42\n')
    with pytest.raises(ConfigError, match=r"config\[dream.llm.dream.driver\]"):
        load_config(p)


def test_programmatic_config_carries_dream_llm_defaults() -> None:
    cfg = Config()
    # every factory-default role is present; B5 dream_vote has no factory route
    assert set(DEFAULT_LLM_ROUTES) == set(cfg.llm)
    assert "dream_vote" not in cfg.llm
    assert cfg.llm["dream"].driver == "ollama"


# ---------------------------------------------------------------- router fakes


class _FakeLLM:
    info = LLMDriverInfo(name="fake_chat", description="test double")

    def __init__(self, **params):
        self.params = params

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system, user
        return ChatResult(
            text="[]",
            model=str(self.params.get("model", "")),
            driver="fake_chat",
        )

    def check(self) -> HealthReport:
        return HealthReport(ok=True, detail={"model": str(self.params.get("model", ""))})


class _BrokenLLM:
    info = LLMDriverInfo(name="broken_chat", description="test double")

    def __init__(self, **params):
        self.params = params

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system, user
        raise LLMUnavailable("provider down")

    def check(self) -> HealthReport:
        return HealthReport(ok=False, detail={"error": "provider down"})


class _AuditSink:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def audit_append(self, entry: Any) -> None:
        self.entries.append(entry)


def _router(
    routes,
    *,
    registry: LLMRegistry | None = None,
    audit: Any = None,
    env: Any = None,
    generation: Any = None,
) -> RoleRouter:
    return RoleRouter(
        routes=routes,
        registry=registry,
        audit=audit,
        env=env if env is not None else (lambda name: None),
        clock=lambda: 42.0,
        generation=generation,
    )


# ---------------------------------------------------------------- router behavior


def test_router_resolves_ollama_driver_for_configured_role() -> None:
    routes = {
        "dream": RoleLLMConfig(
            role="dream",
            driver="ollama",
            model="llama3.1:8b",
            params={"base_url": "http://localhost:11434"},
        )
    }
    router = _router(routes)
    llm = router.resolve("dream")
    assert isinstance(llm, OllamaLLM)
    assert llm.model == "llama3.1:8b"


def test_router_resolves_openai_compatible_with_env_key() -> None:
    routes = {
        "dream": RoleLLMConfig(
            role="dream",
            driver="openai_compatible",
            model="some-model",
            params={"api_key_env": "FIREWORKS_API_KEY", "base_url": "https://api.test/v1"},
        )
    }
    env: dict[str, str] = {"FIREWORKS_API_KEY": "sk-fw-test"}
    router = _router(routes, env=env.get)
    llm = router.resolve("dream")
    assert isinstance(llm, OpenAICompatibleLLM)
    assert llm.api_key == "sk-fw-test"
    assert llm.model == "some-model"


def test_router_missing_env_yet_constructs_no_auth() -> None:
    # an unset key env var never blocks resolve: the driver constructs with an
    # empty key and the provider 401 surfaces as LLMUnavailable at chat time
    router = _router(Config().llm)
    llm = router.resolve("dream")
    assert llm.api_key == ""


def test_router_resolve_caches_same_instance() -> None:
    router = _router(Config().llm)
    a = router.resolve("dream")
    b = router.resolve("dream")
    assert a is b


def test_router_rebuilds_role_when_generation_bumps() -> None:
    """E1-2 (F2): the per-role generation check rebuilds a changed role."""
    generations = {"dream": 0}
    reg = LLMRegistry("test-router-gen")
    register(reg)(_FakeLLM)
    routes = {
        "dream": RoleLLMConfig(role="dream", driver="fake_chat", model="m1"),
    }
    router = _router(routes, registry=reg, generation=lambda role: generations[role])
    first = router.resolve("dream")
    routes["dream"] = RoleLLMConfig(role="dream", driver="fake_chat", model="m2")
    generations["dream"] += 1
    rebuilt = router.resolve("dream")
    assert rebuilt is not first
    assert rebuilt.params["model"] == "m2"


def test_router_reuses_cached_instance_when_generation_unchanged() -> None:
    """E1-2: the generation counter IS the invalidation signal — mutating the
    routes mapping alone never rebuilds a cached instance."""
    reg = LLMRegistry("test-router-gen2")
    register(reg)(_FakeLLM)
    routes = {"dream": RoleLLMConfig(role="dream", driver="fake_chat", model="m1")}
    router = _router(routes, registry=reg, generation=lambda role: 0)
    first = router.resolve("dream")
    routes["dream"] = RoleLLMConfig(role="dream", driver="fake_chat", model="m2")
    assert router.resolve("dream") is first


def test_router_unknown_role_raises_typed_route_error() -> None:
    router = _router(Config().llm)
    with pytest.raises(LLMRouteError, match="no llm route configured for role 'no_such_role'"):
        router.resolve("no_such_role")


def test_router_unknown_driver_only_fails_when_role_resolved() -> None:
    routes = dict(Config().llm)
    routes["dream"] = RoleLLMConfig(role="dream", driver="no_such_driver", model="m")
    router = _router(routes)
    with pytest.raises(UnknownLLMDriverError, match="no_such_driver"):
        router.resolve("dream")


def test_router_check_returns_driver_health() -> None:
    reg = LLMRegistry("test-router")
    register(reg)(_FakeLLM)
    routes = {"dream": RoleLLMConfig(role="dream", driver="fake_chat", model="m")}
    router = _router(routes, registry=reg)
    report = router.check("dream")
    assert isinstance(report, HealthReport)
    assert report.ok is True
    assert report.detail["model"] == "m"


def test_router_check_surfaces_driver_failure_typed() -> None:
    reg = LLMRegistry("test-router-2")
    register(reg)(_BrokenLLM)
    routes = {"dream": RoleLLMConfig(role="dream", driver="broken_chat", model="m")}
    router = _router(routes, registry=reg)
    report = router.check("dream")
    assert report.ok is False
    assert "provider down" in report.detail["error"]


def test_router_check_unconfigured_role_is_failed_health() -> None:
    routes = {"dream": RoleLLMConfig(role="dream", driver="no_such_driver", model="m")}
    router = _router(routes)
    report = router.check("dream")
    assert report.ok is False
    assert "no_such_driver" in report.detail["error"]


def test_router_roles_in_config_order() -> None:
    router = _router(Config().llm)
    assert router.roles() == ("dream", "dream_verifier")  # dream_vote has no factory route


def test_router_audit_logs_role_configured_once_env_name_never_value() -> None:
    sink = _AuditSink()
    routes = {
        "dream": RoleLLMConfig(
            role="dream",
            driver="openai_compatible",
            model="some-model",
            params={"api_key_env": "FIREWORKS_API_KEY", "base_url": "https://api.test/v1"},
        )
    }
    env: dict[str, str] = {"FIREWORKS_API_KEY": "sk-super-secret"}
    router = _router(routes, audit=sink, env=env.get)
    router.resolve("dream")
    router.resolve("dream")  # cached: no second audit entry
    assert len(sink.entries) == 1
    entry = sink.entries[0]
    assert entry.actor == "dream-router"
    assert entry.action == "llm_role_configured"
    assert entry.at == 42.0
    assert entry.detail["role"] == "dream"
    assert entry.detail["api_key_env"] == "FIREWORKS_API_KEY"
    assert "sk-super-secret" not in str(entry.detail)  # never the key value
