"""Role routing: [dream.llm] config, lazy resolution, env-var key substitution,
audit logging, and boot-safe isolation (PRD-02 T6; FR-2.14 / design/02 §6-§7).

Behavior pinned here: fresh installs ship with NO factory dream route (an
explicitly configured role table must carry both driver and model); legacy
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


def test_roles_are_unchanged_and_factory_routes_are_empty() -> None:
    """The role names stay the pipeline-internal contract, but fresh installs
    ship with NO factory dream route: dreams defer honestly until a route is
    bound (existing installs are unaffected — their routes live in the
    settings DB and win at boot)."""
    assert LLM_ROLES == ("dream", "dream_verifier", "dream_vote")
    assert DEFAULT_LLM_ROUTES == {}


def test_fresh_config_has_no_factory_routes(monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(Path("/nonexistent/config.toml"))
    assert cfg.llm == {}


def test_default_routes_carry_no_secrets() -> None:
    # an empty default table has nothing to leak; the pin keeps a future
    # default from reintroducing a literal-looking secret
    assert "sk-" not in str(DEFAULT_LLM_ROUTES).lower()
    assert "api_key" not in str(DEFAULT_LLM_ROUTES).lower()


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
    assert "dream" not in cfg.llm  # unconfigured roles carry no route


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
    assert "dream" not in cfg.llm


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
    assert cfg.llm == {}  # legacy tables are tolerated, never applied
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


def test_partial_role_table_is_a_named_load_error(tmp_path, monkeypatch) -> None:
    """With no factory defaults, an explicitly configured role table must carry
    BOTH driver and model (the wizard's write contract); a partial table is a
    named load error, never a silent fallback to removed defaults."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    _write(p, 'preset = "embedded"\n[dream.llm.dream]\napi_key_env = "MY_ALT_KEY"\n')
    with pytest.raises(ConfigError, match=r"config\[dream\.llm\.dream\]"):
        load_config(p)
    p2 = tmp_path / "config2.toml"
    _write(p2, 'preset = "embedded"\n[dream.llm.dream]\ndriver = "ollama"\n')
    with pytest.raises(ConfigError, match=r"config\[dream\.llm\.dream\]"):
        load_config(p2)


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


def test_programmatic_config_has_no_factory_routes() -> None:
    cfg = Config()
    assert cfg.llm == {}
    assert DEFAULT_LLM_ROUTES == {}


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


def _explicit_routes() -> dict[str, RoleLLMConfig]:
    return {
        "dream": RoleLLMConfig(
            role="dream",
            driver="openai_compatible",
            model="some-model",
            params={"base_url": "https://api.test/v1"},
        ),
        "dream_verifier": RoleLLMConfig(
            role="dream_verifier",
            driver="openai_compatible",
            model="judge-model",
            params={"base_url": "https://api.test/v1"},
        ),
    }


def test_router_missing_env_yet_constructs_no_auth() -> None:
    # an unset key env var never blocks resolve: the driver constructs with an
    # empty key and the provider 401 surfaces as LLMUnavailable at chat time
    router = _router(_explicit_routes())
    llm = router.resolve("dream")
    assert llm.api_key == ""


def test_router_resolve_caches_same_instance() -> None:
    router = _router(_explicit_routes())
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
    router = _router(_explicit_routes())
    with pytest.raises(LLMRouteError, match="no llm route configured for role 'no_such_role'"):
        router.resolve("no_such_role")


def test_router_builds_only_from_explicit_config(monkeypatch) -> None:
    """With empty factory defaults the router has nothing to build from: a
    fresh config resolves no role and the daemon degrades typed (never
    crashes); only explicit configuration gives resolve a route."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(Path("/nonexistent/config.toml"))
    router = _router(cfg.llm)
    assert router.roles() == ()
    with pytest.raises(LLMRouteError, match="no llm route configured for role 'dream'"):
        router.resolve("dream")


def test_router_unknown_driver_only_fails_when_role_resolved() -> None:
    routes = dict(_explicit_routes())
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
    router = _router(_explicit_routes())
    assert router.roles() == ("dream", "dream_verifier")


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
