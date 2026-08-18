"""B3.1 T3 — cloud anchor seats in the matrix (PRD-B3 addendum).

Any OpenAI-compatible provider (Modal-hosted Kimi-K3, DeepSeek, ...) joins the
roster as extra routes: same cell expansion (off/verify), same rig, and an
HONEST availability probe — a route whose driver check fails is a loud
``route_unavailable`` failure (exit 1), never a silently-missing anchor. API
keys are referenced by env-var NAME only (the RoleRouter/env resolution
chain), never materialized in cells, reports, or logs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.eval.harness import EvalCell, EvalRoute
from mnemoseed_local.eval.materials import material_catalog
from mnemoseed_local.eval.matrix import (
    default_matrix,
    matrix_exit_code,
    parse_extra_route,
    probe_routes,
    run_matrix,
)

STUB_A = EvalRoute(driver="stub", model="stub-a")
STUB_B = EvalRoute(driver="stub_verifier", model="stub-b")

KIMI_SPEC = "openai_compatible|moonshotai/Kimi-K3|https://example.invalid/v1|KIMI_API_KEY|90|8192"


def test_parse_extra_route_full() -> None:
    route = parse_extra_route(KIMI_SPEC)
    assert route.driver == "openai_compatible"
    assert route.model == "moonshotai/Kimi-K3"
    params = dict(route.params)
    assert params["base_url"] == "https://example.invalid/v1"
    assert params["api_key_env"] == "KIMI_API_KEY"
    assert params["timeout"] == 90.0
    assert params["max_tokens"] == 8192


def test_parse_extra_route_defaults() -> None:
    route = parse_extra_route("ollama|qwen3:0.6b|http://localhost:11434")
    params = dict(route.params)
    assert "api_key_env" not in params
    assert params["timeout"] == 60.0
    assert "num_predict" not in params  # ollama generation cap only when named
    assert params["think"] is False
    assert params["num_ctx"] == 16384
    capped = parse_extra_route("ollama|qwen3:0.6b|http://localhost:11434||45|4096")
    assert dict(capped.params)["num_predict"] == 4096
    assert dict(capped.params)["timeout"] == 45.0


def test_parse_extra_route_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError):
        parse_extra_route("no-model-parts")
    with pytest.raises(ValueError):
        parse_extra_route("driver|model")  # missing base_url


def test_default_matrix_includes_extra_seats() -> None:
    cells = default_matrix(roster=("qwen3.5:9b",), extra_routes=(parse_extra_route(KIMI_SPEC),))
    assert len(cells) == 2 * 2  # (1 roster + 1 extra) x (off + verify)
    kimi_cells = [c for c in cells if c.reflect.driver == "openai_compatible"]
    assert len(kimi_cells) == 2
    assert {c.ensemble for c in kimi_cells} == {"off", "verify"}
    verify = next(c for c in kimi_cells if c.ensemble == "verify")
    assert verify.verifier is not None and verify.verifier.model == "gemma4:e4b"
    assert all("secrets" not in str(c) for c in cells)  # cell payloads never carry key material


def test_probe_routes_non_ollama_via_driver_check() -> None:
    route = parse_extra_route(KIMI_SPEC)
    seen: list[tuple[str, str | None]] = []

    def fake_checker(route_: EvalRoute, api_key: str | None) -> str | None:
        seen.append((route_.model, api_key))
        return None  # healthy

    probe = probe_routes(
        [route],
        checker=fake_checker,
        env=lambda name: "sk-test" if name == "KIMI_API_KEY" else None,
    )
    assert probe[route.model] is None
    assert seen == [("moonshotai/Kimi-K3", "sk-test")]


def test_probe_routes_missing_key_env_is_loud_failure() -> None:
    route = parse_extra_route(KIMI_SPEC)
    probe = probe_routes([route], env=lambda name: None)
    assert probe[route.model] is not None
    assert "KIMI_API_KEY" in probe[route.model]  # type: ignore[operator]


def test_probe_routes_check_failure_is_route_unavailable() -> None:
    route = parse_extra_route(KIMI_SPEC)

    def bad_checker(route_: EvalRoute, api_key: str | None) -> str | None:
        return "HTTP 401 unauthorized"

    probe = probe_routes(
        [route], checker=bad_checker, env=lambda name: "sk-test" if name == "KIMI_API_KEY" else None
    )
    assert probe[route.model] == "HTTP 401 unauthorized"


def test_run_matrix_cloud_skip_marks_and_fails(tmp_path: Path) -> None:
    cells = [
        EvalCell(reflect=STUB_A, ensemble="off", verifier=STUB_B),
        EvalCell(reflect=parse_extra_route(KIMI_SPEC), ensemble="off"),
    ]

    def bad_checker(route_: EvalRoute, api_key: str | None) -> str | None:
        return "unreachable"

    report = run_matrix(
        cells,
        material_catalog(None, canary_seed=1, canary_count=1),
        root=tmp_path,
        route_checker=bad_checker,
        env=lambda name: "sk-test" if name == "KIMI_API_KEY" else None,
    )
    assert len(report.cells) == 1
    assert report.skipped[0].reason.startswith("route_unavailable: moonshotai/Kimi-K3")
    # a downed CLOUD anchor is a loud failure (exit 1), unlike a missing local tag
    assert matrix_exit_code(report) == 1
