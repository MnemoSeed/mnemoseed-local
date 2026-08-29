"""mnemoseed-local CLI entry point (A2 MVP).

Verbs: init / up / on / off / status / doctor / recall / remember / dream
(--once, status) / forget / profile (create | list | archive | unarchive) /
config (get | set | rollback) / uninstall (--purge) / hook (install |
uninstall | status) / mcp (MCP stdio gateway). Local loopback by default;
every state-changing verb talks to the daemon REST (FR-7.12); no
identity/accounts/tokens in the local MVP.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from mnemoseed_local import __version__
from mnemoseed_local.config import (
    CONFIG_DIR,
    CONFIG_PATH,
    DEFAULT_LLM_ROUTES,
    Config,
    ConfigError,
    default_config_toml,
    load_config,
)
from mnemoseed_local.rest_client import DaemonClient

#: Default profile at the application boundary (no identity in the local MVP).
DEFAULT_PROFILE = "default"

#: Host config the MCP-injection doctor check inspects (B2.12): an
#: ``mcp.mnemoseed`` entry here means opencode was told about our MCP server.
OPENCODE_CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"

#: Doctor ctx-window check (design/01 §4.8): the generation margin assumed when
#: the dream route does not configure ``num_predict``.
DREAM_MARGIN_TOKENS_DEFAULT = 2048


def cmd_init(args: argparse.Namespace) -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not args.force:
        print(f"config already exists: {CONFIG_PATH} (use --force to overwrite)")
        return 1
    CONFIG_PATH.write_text(default_config_toml(), encoding="utf-8")
    print(f"initialized {CONFIG_DIR}")
    print(f"config: {CONFIG_PATH}")
    # A3 T5 (design/01 §6 Phase A3): point at self-check and the one-time model
    # pull. Guidance only — the CLI never pulls a model silently.
    model = DEFAULT_LLM_ROUTES["dream"].model
    print("next steps:")
    print("  1. mnemoseed-local doctor   (self-check incl. hardware tier)")
    print(f"  2. ollama pull {model}   (dream model, first time only)")
    print("  3. mnemoseed-local up")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    from mnemoseed_local.daemon.runner import run_server
    from mnemoseed_local.daemon_state import is_disabled
    from mnemoseed_local.storage.factory import build_stores
    from mnemoseed_local.storage.ports import StorageError

    if is_disabled():
        print(
            "error: memory service is disabled (run 'mnemoseed-local on' to re-enable)",
            file=sys.stderr,
        )
        return 1
    host, port = args.host, args.port
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"preset: {config.preset}")
    if config.preset == "embedded":
        print("embedded single-process daemon - all drivers in-process, zero external services")

    # A3 T5 model-missing UX: an ollama dream route needs its model pulled
    # before boot — fail fast with the fix hint, never a silent `ollama pull`
    # (the bge-m3 lazy-load precedent). A non-ollama route skips the pre-flight
    # entirely: the provider's model inventory is out of doctor's reach.
    if config.llm["dream"].driver == "ollama":
        model_ok, model_detail = _dream_model_check(config)
        if not model_ok:
            if model_detail.startswith("model "):
                print(f"error: dream {model_detail}", file=sys.stderr)
            else:
                print(f"error: {model_detail}", file=sys.stderr)
            return 1

    # Resolve the storage stack up front so a bad driver key or invalid params
    # fail with a clean one-line error instead of a uvicorn startup traceback.
    try:
        stores = build_stores(config)
        asyncio.run(stores.close())
    except StorageError as exc:
        print(f"error: storage stack failed to build: {exc}", file=sys.stderr)
        return 1

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"daemon on http://{host}:{port}")
    return run_server(host, port)


#: The off poll waits this long for the listener to disappear; a longer drain
#: is reported honestly instead of being blocked on.
_OFF_POLL_TIMEOUT_S = 15.0
_OFF_POLL_INTERVAL_S = 0.5
#: Per-call cap for liveness probes: a half-dead daemon (accepts but never
#: answers) must not park the poll for the client's 30s timeout.
_OFF_PROBE_TIMEOUT_S = 1.0


def _probe_client(client: DaemonClient) -> DaemonClient:
    """A short-timeout copy of the client for liveness probes. Test fakes are
    plain objects that control their own timing — only real clients carry a
    settable timeout."""
    if not isinstance(client, DaemonClient):
        return client
    return replace(client, timeout=_OFF_PROBE_TIMEOUT_S)


def _daemon_reachable(client: DaemonClient) -> bool:
    """True when the daemon answers /healthz — the running signal."""
    try:
        client.get("/healthz")
        return True
    except Exception:
        return False


def _wait_for_daemon_gone(client: DaemonClient, timeout_s: float) -> bool:
    """Poll until the daemon stops answering /healthz; False on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _daemon_reachable(client):
            return True
        time.sleep(_OFF_POLL_INTERVAL_S)
    return False


def cmd_off(args: argparse.Namespace) -> int:
    """Stop the daemon and persist the disabled state. The marker lands FIRST
    — during the poll a watcher / up must not boot a fresh daemon, and the
    marker must never land on a revived one."""
    from mnemoseed_local.daemon_state import is_disabled, set_disabled
    from mnemoseed_local.rest_client import DaemonRestError, DaemonUnavailableError, resolve_client

    if is_disabled():
        print("already off: memory service is disabled")
        try:
            client = resolve_client(args)
        except Exception as exc:
            return _client_error(exc)
        if _daemon_reachable(_probe_client(client)):
            print(
                "note: a daemon is currently running but the memory service is disabled; "
                "it will not be restarted by 'up' (stop it manually or run 'mnemoseed-local on')"
            )
        return 0
    try:
        client = resolve_client(args)
    except Exception as exc:
        return _client_error(exc)
    probe = _probe_client(client)
    try:
        set_disabled()
    except OSError as exc:
        print(f"error: could not write the disabled marker: {exc}", file=sys.stderr)
        return 1
    try:
        status: str
        try:
            client.post("/daemon/shutdown")
            status = "requested"
        except DaemonUnavailableError:
            status = "gone"  # already stopped: the POST is best-effort
        except DaemonRestError:
            status = "refused"  # answered but did not accept the request (older build)
        if status == "requested":
            if _wait_for_daemon_gone(probe, _OFF_POLL_TIMEOUT_S):
                print("daemon stopped; memory service disabled")
            elif _daemon_reachable(probe):
                print(
                    "daemon is still running; memory service disabled "
                    "(stop it manually, or run 'mnemoseed-local on' to re-enable)"
                )
            else:
                print("daemon may still be shutting down; memory service disabled")
        elif status == "gone":
            print("daemon not running; memory service disabled")
        elif _daemon_reachable(probe):
            print(
                "daemon is still running and did not accept the shutdown request; "
                "stop it manually (Ctrl+C in its console, or Task Manager); "
                "memory service disabled and stays off (run 'mnemoseed-local on' to re-enable)"
            )
        else:
            print("daemon did not accept the shutdown request; memory service disabled")
    except Exception as exc:
        # The marker is already written (state converged); an unexpected
        # shutdown-flow failure must still end with honest guidance, not a
        # traceback.
        print(
            f"error: shutdown request failed: {exc}; memory service is disabled — "
            "if the daemon is still running, stop it manually",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_on(args: argparse.Namespace) -> int:
    """Re-enable the memory service and start the daemon unless it is already
    running (a running daemon is reported, never restarted)."""
    from mnemoseed_local.daemon_state import set_enabled
    from mnemoseed_local.rest_client import resolve_client

    set_enabled()
    try:
        client = resolve_client(args)
    except Exception as exc:
        return _client_error(exc)
    if _daemon_reachable(client):
        print("already on: memory service is running")
        return 0
    return cmd_up(args)


def cmd_status(args: argparse.Namespace) -> int:
    from mnemoseed_local.rest_client import resolve_client

    client = resolve_client(args)
    try:
        health = client.get("/healthz")
        config = client.get("/api/v1/config")["config"]
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json({"health": health, "config": config})
    gate = health.get("gate", {})
    gate_state = "ok" if gate.get("ok") else "FAIL"
    print(f"daemon: mnemoseed-local {__version__} (preset {health.get('preset', '?')}, gate {gate_state})")
    print(f"dream auto_trigger: {config['dream']['auto_trigger']}")
    print(f"dream llm driver:  {config['dream']['llm']['dream']['driver']}")
    print(f"profile: {client.profile_id}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []
    warnings: list[tuple[str, str]] = []
    try:
        config = load_config()
        checks.append(("config", True, f"loaded from {config.source}"))
    except ConfigError as exc:
        checks.append(("config", False, str(exc)))
        return _doctor_report(checks)

    from mnemoseed_local.rest_client import is_loopback

    loopback = is_loopback(config.baseurl)
    checks.append(("loopback-only", loopback, config.baseurl))
    if not loopback:
        checks.append(("daemon", False, "the local MVP refuses a non-loopback baseurl at boot"))
        return _doctor_report(checks)

    checks.append(_dream_ctx_window_check(config))

    # T3b (design/01 §4.8): the isolated graph instance is mandatory. Checked
    # on the config's declared graph instances (before store resolution), so a
    # config that would boot the daemon but strand tier-3 output is reported
    # here with a fix hint, never silently.
    graph_instances = config.layer_instances("graph")
    has_isolated = "isolated" in graph_instances
    isolated_detail = (
        "isolated graph instance present"
        if has_isolated
        else "missing; add a [storage.graph.instances.isolated] table "
        "(driver = \"sqlite_graph\") or run 'mnemoseed init'"
    )
    checks.append(("isolated graph", has_isolated, isolated_detail))

    # A3 T5 (design/01 §4.8 decision 8): informational tier recommendation.
    # Config-only, so it always reports — even when the storage stack or the
    # dream llm probe would fail later in the checklist.
    checks.append(_hardware_tier_check(config))

    from mnemoseed_local.llm import LLMError, RoleRouter
    from mnemoseed_local.storage.factory import build_stores
    from mnemoseed_local.storage.ports import StorageError

    unregistered_profiles: list[str] = []
    try:
        stores = build_stores(config)
        checks.append(("storage", True, "all layers resolved; capability gate passed"))
        unregistered_profiles = _unregistered_profile_ids(stores)
    except StorageError as exc:
        checks.append(("storage", False, str(exc)))
        return _doctor_report(checks)
    finally:
        try:
            if "stores" in locals():
                asyncio.run(stores.close())
        except Exception:
            pass

    router = RoleRouter(routes=config.llm, audit=None)
    try:
        llm = router.resolve("dream")
        report = llm.check()
        checks.append(("dream llm", report.ok, f"{config.llm['dream'].driver} {report.detail}"))
    except LLMError as exc:
        checks.append(("dream llm", False, str(exc)))

    # A3 T5 model-missing UX: the ollama dream route's model must be pulled.
    model_ok, model_detail = _dream_model_check(config)
    checks.append(("dream model", model_ok, model_detail))

    # B1 T3: same model-missing honesty for the verify judging seat when the
    # user opted into ensemble=verify.
    checks.append(_ensemble_verifier_check(config))

    # B1.1 (live finding Q7): the verify seat's context window must fit its
    # judging load — the check that would have caught the 16384-vs-18287 gap.
    checks.append(_verifier_ctx_window_check(config))

    # B2.12 (#117): a registered MCP server that never connected while the
    # capture hooks clearly work is the silent missing-tools state — surface
    # it. The since-boot qualifier keeps a daemon restart from crying wolf
    # over a healthy setup whose gateway simply has not re-announced yet.
    if _opencode_mcp_registered(OPENCODE_CONFIG_PATH):
        activity = _daemon_activity(config)
        if activity is not None:
            if activity.get("capture_ingest_count", 0) > 0 and activity.get("mcp_handshake_count", 0) == 0:
                warnings.append(
                    (
                        "mcp injection",
                        "MCP server registered but never connected since the daemon "
                        "booted - tools likely not injected into sessions; restart "
                        "opencode or re-register the 'mnemoseed' MCP server",
                    )
                )

    # B2.12 (#110): captured namespaces with no profiles-table row are how a
    # typo'd profile_id presents (an empty namespace, "fake amnesia"). The
    # implicit "default" id is exempt (#109): the conventional single-user
    # namespace needs no profiles-table row.
    if unregistered_profiles:
        warnings.append(
            (
                "unknown profiles",
                f"captured profile_ids with no profiles-table row: "
                f"{', '.join(unregistered_profiles)} - non-default namespaces may be "
                "intentional (MNEMOSEED_LOCAL_PROFILE_ID); register them with "
                "'mnemoseed-local profile create <id>'; typo'd ids present as empty memory",
            )
        )
    return _doctor_report(checks, warnings)


def _doctor_report(checks: list[tuple[str, bool, str]], warnings: list[tuple[str, str]] | None = None) -> int:
    failed = 0
    for name, ok, detail in checks:
        state_char = "ok" if ok else "FAIL"
        print(f"[{state_char:>4}] {name}: {detail}")
        if not ok:
            failed += 1
    for name, detail in warnings or []:
        print(f"[warn] {name}: {detail}")
    if failed:
        print(f"doctor: {failed} check(s) failed")
    else:
        print("doctor: all checks passed")
    return 1 if failed else 0


def _opencode_mcp_registered(path: Path) -> bool:
    """True when the host config registers an enabled ``mcp.mnemoseed`` entry.

    A missing/corrupt file (or a malformed entry) means the check cannot say
    anything — quiet False, never an error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    mcp = data.get("mcp") if isinstance(data, dict) else None
    entry = mcp.get("mnemoseed") if isinstance(mcp, dict) else None
    if not isinstance(entry, dict):
        return False
    return entry.get("enabled") is not False


def _daemon_activity(config: Config) -> dict[str, Any] | None:
    """The daemon's since-boot observability counters; None when unreachable
    (a down daemon says nothing about MCP injection)."""
    try:
        return DaemonClient(base_url=config.baseurl).get("/api/v1/observability")
    except Exception:  # noqa: BLE001 - any transport failure means "unknown"
        return None


def _unregistered_profile_ids(stores: Any) -> list[str]:
    """Captured profile_ids with no row in the profiles table (#110).

    The implicit "default" id is the conventionally-known namespace and is
    never reported (#109).
    """
    try:
        distinct = getattr(stores.vector, "distinct_profile_ids", None)
        if not callable(distinct):
            return []
        known = {profile.profile_id for profile in stores.meta.list_profiles()}
        known.add(DEFAULT_PROFILE)
        return sorted(set(distinct()) - known)
    except Exception:  # noqa: BLE001 - purely observational check
        return []


def _delta_budget_ceiling_tokens(config: Config) -> int:
    """Single-point read of the dream delta ceiling for the doctor check.

    The check's delta component reads the ``dream.delta_budget_ceiling_tokens``
    config key (T3a), so lowering the ceiling via configwrite makes a tight
    window pass without touching num_ctx.
    """
    return config.dream.delta_budget_ceiling_tokens


def _dream_ctx_window_check(config: Config) -> tuple[str, bool, str]:
    """Design/01 §4.8: the dream route's context window must fit the cached
    prefix, the packed delta ceiling, and the generation margin.

    An unconfigured ``num_ctx`` is only a hint (the default route works without
    it); a configured one is checked against
    ``estimate_tokens(cache_prefix) + delta ceiling + margin`` and fails with a
    fix hint when the window is exceeded.
    """
    from mnemoseed_local.dream.delta import estimate_tokens
    from mnemoseed_local.dream.prompts import build_cache_prefix

    route = config.llm["dream"]
    # AC5 (T3a): the ctx-window check (and its num_ctx / num_predict hints) is
    # ollama-specific — num_ctx is an ollama server knob. A non-ollama route is
    # skipped: the provider's own context window is out of doctor's reach, so a
    # failure here would be a false alarm on a healthy route.
    if route.driver != "ollama":
        return (
            "dream ctx window",
            True,
            f"route driver {route.driver!r} is not ollama; ctx-window check skipped",
        )
    params = route.params
    num_ctx_raw = params.get("num_ctx")
    if num_ctx_raw is None:
        return (
            "dream ctx window",
            True,
            "num_ctx is not configured; set num_ctx under [dream.llm.dream] "
            "so doctor can verify the window fits",
        )
    try:
        num_ctx = int(num_ctx_raw)
    except (TypeError, ValueError):
        return (
            "dream ctx window",
            False,
            f"num_ctx must be an integer, got {num_ctx_raw!r}",
        )
    num_predict_raw = params.get("num_predict", DREAM_MARGIN_TOKENS_DEFAULT)
    try:
        margin = int(num_predict_raw)
    except (TypeError, ValueError):
        return (
            "dream ctx window",
            False,
            f"num_predict must be an integer, got {num_predict_raw!r}",
        )
    prefix_tokens = estimate_tokens(build_cache_prefix())
    ceiling = _delta_budget_ceiling_tokens(config)
    needed = prefix_tokens + ceiling + margin
    if needed <= num_ctx:
        return (
            "dream ctx window",
            True,
            f"prefix+delta+margin={needed} <= num_ctx={num_ctx}",
        )
    return (
        "dream ctx window",
        False,
        f"prefix+delta+margin={needed} > num_ctx={num_ctx}; lower the delta ceiling or raise num_ctx",
    )


def models_contain(models: list[str], configured: str) -> bool:
    """Ollama model-name normalization (A3 T5): is `configured` in the pulled list?

    A configured name WITH a tag matches exactly ``name:tag`` (plus a bare
    ``name`` server entry when the tag is ``latest`` — some ollama builds elide
    the default tag in /api/tags). A configured name WITHOUT a tag matches
    either ``name`` or ``name:latest``, but never a pinned non-latest tag.
    """
    if ":" not in configured:
        wanted = {configured, f"{configured}:latest"}
    else:
        name, _, tag = configured.partition(":")
        wanted = {configured, name} if tag == "latest" else {configured}
    return any(model in wanted for model in models)


def _role_model_check(config: Config, role: str) -> tuple[bool, str]:
    """The ollama model-presence check for one LLM role, reused by every seat.

    Compare the configured ``route.model`` against the server's ``GET
    /api/tags`` inventory (via the driver's ``check()`` probe; name comparison
    goes through ``models_contain``). A missing model fails with the pull hint
    and an unreachable server fails with a start-ollama hint — never a silent
    pull. A non-ollama route skips the check (same precedent as the ctx-window
    check: the provider's model inventory is out of doctor's reach). Single
    wording source for the dream-model and ensemble-verifier checks.
    """
    route = config.llm[role]
    if route.driver != "ollama":
        return True, f"route driver {route.driver!r} is not ollama; model presence check skipped"
    from mnemoseed_local.llm import LLMError, RoleRouter

    try:
        llm = RoleRouter(routes=config.llm, audit=None).resolve(role)
    except LLMError as exc:
        return False, str(exc)
    report = llm.check()
    if not report.ok:
        error = report.detail.get("error", "unknown error")
        return False, f"ollama server unreachable ({error}); start ollama first"
    models = [str(name) for name in (report.detail.get("models") or []) if isinstance(name, str)]
    if models_contain(models, route.model):
        return True, f"model {route.model!r} present"
    return False, f"model {route.model!r} not pulled; run: ollama pull {route.model}"


def _dream_model_check(config: Config) -> tuple[bool, str]:
    """A3 T5 (design/01 §6 Phase A3): the ollama dream route's model must be pulled.

    ``up`` calls the same helper as a pre-flight before booting the daemon.
    """
    return _role_model_check(config, "dream")


def _ensemble_verifier_check(config: Config) -> tuple[str, bool, str]:
    """B1 T3: the verify judging seat's model must be pulled when the user
    opted into verify mode.

    Dormant (ensemble off) skips. In vote mode seat B is an independent
    generator — ``dream_vote`` when configured, otherwise it falls back to the
    ``dream_verifier`` judging route (daemon ``_build_vote_llm`` — still a
    distinct model from the dream generator). Doctor validates the effective
    vote seat's model so the preflight stays consistent with the runtime
    fallback; a missing model FAILS with the pull hint while the runtime
    fallback remains the safety net."""
    if config.dream.ensemble == "off":
        return (
            "ensemble verifier",
            True,
            f"ensemble mode {config.dream.ensemble!r}; verifier model check skipped",
        )
    if config.dream.ensemble == "vote":
        vote_role = "dream_vote" if "dream_vote" in config.llm else "dream_verifier"
        ok, detail = _role_model_check(config, vote_role)
        if ok:
            try:
                vote_model = config.llm[vote_role].model
                dream_model = config.llm["dream"].model
                if vote_model == dream_model:
                    return (
                        "ensemble verifier",
                        False,
                        f"vote seat model {vote_model!r} must be distinct from dream model {dream_model!r}",
                    )
            except Exception:
                pass
        return ("ensemble verifier", ok, detail)
    ok, detail = _role_model_check(config, "dream_verifier")
    return ("ensemble verifier", ok, detail)


def _verifier_ctx_window_check(config: Config) -> tuple[str, bool, str]:
    """B1.1: the verify judging seat's ctx window must fit its load.

    Static sanity with its assumption labeled: candidates repeat their evidence
    blocks per judge item (2026-08-18 live: 25 candidates over a 1.7k delta
    rendered an 18.3k verify prompt — nearly 11x the delta), so the formula
    double-covers the delta ceiling, and the TripleVerifier's per-run estimate
    guard is the exact instrument on top of it. Dormant (ensemble off) skips.
    In vote mode seat B is an independent generator — ``dream_vote`` when
    configured, otherwise the ``dream_verifier`` fallback (daemon
    ``_build_vote_llm``). Its window is validated against the same
    double-ceiling estimate as the verify seat so a too-small num_ctx is
    reported before the daemon would degrade.
    """
    if config.dream.ensemble == "off":
        return (
            "verifier ctx window",
            True,
            f"ensemble mode {config.dream.ensemble!r}; verifier ctx-window check skipped",
        )
    if config.dream.ensemble == "vote":
        vote_role = "dream_vote" if "dream_vote" in config.llm else "dream_verifier"
        route = config.llm[vote_role]
        if route.driver != "ollama":
            return (
                "verifier ctx window",
                True,
                f"route driver {route.driver!r} is not ollama; ctx-window check skipped",
            )
        num_ctx_raw = route.params.get("num_ctx")
        if num_ctx_raw is None:
            return (
                "verifier ctx window",
                True,
                f"num_ctx is not configured; set num_ctx under [dream.llm.{vote_role}] "
                "so doctor can verify the window fits",
            )
        try:
            num_ctx = int(num_ctx_raw)
        except (TypeError, ValueError):
            return ("verifier ctx window", False, f"num_ctx must be an integer, got {num_ctx_raw!r}")
        from mnemoseed_local.dream.delta import estimate_tokens
        from mnemoseed_local.dream.verify import VERIFY_MARGIN_TOKENS, build_verify_prefix

        prefix_tokens = estimate_tokens(build_verify_prefix())
        ceiling = config.dream.delta_budget_ceiling_tokens
        needed = prefix_tokens + 2 * ceiling + VERIFY_MARGIN_TOKENS
        if needed <= num_ctx:
            return (
                "verifier ctx window",
                True,
                f"prefix+2x ceiling+margin={needed} <= num_ctx={num_ctx}",
            )
        return (
            "verifier ctx window",
            False,
            f"prefix+2x delta ceiling+margin={needed} > num_ctx={num_ctx}; raise "
            f"dream.llm.{vote_role} num_ctx or lower dream.delta_budget_ceiling_tokens "
            "(large extractions otherwise fall back unverified: window_exceeded)",
        )
    route = config.llm["dream_verifier"]
    if route.driver != "ollama":
        return (
            "verifier ctx window",
            True,
            f"route driver {route.driver!r} is not ollama; ctx-window check skipped",
        )
    num_ctx_raw = route.params.get("num_ctx")
    if num_ctx_raw is None:
        return (
            "verifier ctx window",
            True,
            "num_ctx is not configured; set num_ctx under [dream.llm.dream_verifier] "
            "so doctor can verify the window fits",
        )
    try:
        num_ctx = int(num_ctx_raw)
    except (TypeError, ValueError):
        return ("verifier ctx window", False, f"num_ctx must be an integer, got {num_ctx_raw!r}")
    from mnemoseed_local.dream.delta import estimate_tokens
    from mnemoseed_local.dream.verify import VERIFY_MARGIN_TOKENS, build_verify_prefix

    prefix_tokens = estimate_tokens(build_verify_prefix())
    ceiling = config.dream.delta_budget_ceiling_tokens
    needed = prefix_tokens + 2 * ceiling + VERIFY_MARGIN_TOKENS
    if needed <= num_ctx:
        return (
            "verifier ctx window",
            True,
            f"prefix+2x ceiling+margin={needed} <= num_ctx={num_ctx}",
        )
    return (
        "verifier ctx window",
        False,
        f"prefix+2x delta ceiling+margin={needed} > num_ctx={num_ctx}; raise "
        "dream.llm.dream_verifier num_ctx or lower dream.delta_budget_ceiling_tokens "
        "(large extractions otherwise fall back unverified: window_exceeded)",
    )


def _hardware_tier_check(config: Config) -> tuple[str, bool, str]:
    """A3 T5 (design/01 §4.8 decision 8): informational tier recommendation.

    Always ``ok=True``: a recommended/current tier mismatch is a hint, never a
    failure. The detail format is a pinned machine-readable contract — the
    install script extracts the recommended tier from this exact line.
    """
    from mnemoseed_local import hardware

    vram_gb = hardware.probe_max_vram_gb()
    ram_gb = hardware.probe_ram_gb()
    tier = hardware.recommended_tier(vram_gb, ram_gb)
    detail = (
        f'recommended tier "{tier}" (vram={int(vram_gb)}GB, ram={int(ram_gb or 0.0)}GB); '
        f'current tier "{config.dream.hardware_tier}"'
    )
    return ("hardware tier", True, detail)


def cmd_recall(args: argparse.Namespace) -> int:
    from mnemoseed_local.rest_client import resolve_client

    try:
        client = resolve_client(args)
        body = client.post(
            "/memory/recall",
            {
                "profile_id": client.profile_id,
                "query": args.query,
                **({"top_k": args.top_k} if args.top_k is not None else {}),
            },
        )
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(body)
    memory = body.get("memory", {})
    for entry in memory.get("entries", []):
        kind = entry.get("kind")
        score = entry.get("score")
        flags = ",".join(entry.get("flags", []))
        suffix = f" [{flags}]" if flags else ""
        print(f"[{kind}] ({score:.2f}) {entry.get('text')}{suffix}")
    coverage = memory.get("coverage", {})
    if coverage:
        print(
            f"coverage: vector_hits={coverage.get('vector_hits')} "
            f"graph_hits={coverage.get('graph_hits')} "
            f"profile_chunks={coverage.get('profile_chunks')}"
        )
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    from mnemoseed_local.rest_client import resolve_client

    try:
        client = resolve_client(args)
        body = client.post(
            "/memory/remember",
            {"profile_id": client.profile_id, "text": args.text},
        )
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(body)
    print(f"remembered: {body.get('outcome')} (chunk {body.get('chunk_id')})")
    return 0


def cmd_dream(args: argparse.Namespace) -> int:
    from mnemoseed_local.rest_client import resolve_client

    try:
        client = resolve_client(args)
        if getattr(args, "dream_command", None) == "status":
            body = client.post("/memory/dream_status", {"profile_id": client.profile_id})
        else:
            body = client.post("/memory/dream_once", {"profile_id": client.profile_id})
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(body)
    if getattr(args, "dream_command", None) == "status":
        print(f"state: {body.get('state')}")
        print(f"pending_queue: {body.get('pending_queue')}")
        print(f"pending_manual: {body.get('pending_manual')}")
        pool = body.get("pool") or {}
        if pool:
            print(f"pool: {pool.get('balance')} / {pool.get('threshold')} pts")
        watermark = body.get("watermark")
        if watermark:
            print(f"digested turns: {watermark.get('start')}..{watermark.get('end')}")
        elif "watermark" in body:
            print("digested turns: none yet")
        history = body.get("history") or {}
        if history:
            failures = history.get("extract_failures") or {}
            failure_text = ", ".join(f"{k}={v}" for k, v in sorted(failures.items())) or "none"
            print(f"dreams committed: {history.get('committed_runs', 0)}")
            last = history.get("last_commit_at")
            print(f"last dream: {last if last else 'never'}")
            print(f"extraction failures: {failure_text}")
    else:
        print(f"launched: {body.get('launched')}")
        print(f"state: {body.get('state')}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    from mnemoseed_local.rest_client import resolve_client

    body: dict[str, Any] = {"profile_id": None}
    if args.kind == "chunk":
        body["chunk_id"] = args.target
    elif args.kind == "entity":
        body["entity"] = args.target
    else:
        body["node_id"] = args.target
    try:
        client = resolve_client(args)
        body["profile_id"] = client.profile_id
        payload = client.post("/memory/forget_this", body)
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(payload)
    removed = payload.get("removed", {})
    print(f"forgotten: {len(removed.get('chunks', []))} chunk(s), {len(removed.get('nodes', []))} node(s)")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove the local config home. ``--purge`` also deletes the data files
    (the app's own directory only — never anything outside it)."""
    target = CONFIG_DIR
    if not target.exists():
        print("no mnemoseed-local data directory; nothing to uninstall")
        return 0
    print(f"data dir: {target}")
    if args.purge:
        _purge(target, yes=args.yes)
    else:
        print("run `mnemoseed-local uninstall --purge` to also delete the data files")
    return 0


def _purge(target: Path, *, yes: bool) -> None:
    """Delete the app's own config home (purge). The path is validated to live
    exactly under the resolved config home before anything is removed."""
    resolved = target.resolve()
    if not str(resolved).startswith(str(CONFIG_DIR.resolve())):
        print(f"error: refusing to delete {resolved} (outside the config home)", file=sys.stderr)
        sys.exit(1)
    print("purge will delete:")
    for child in sorted(resolved.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        print(f"  {child}")
    print(f"  {resolved}")
    if not yes:
        answer = input("delete these mnemoseed-local data files? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("purge cancelled")
            return
    import shutil

    shutil.rmtree(resolved)
    print(f"data dir deleted: {resolved}")


# ------------------------------------------------------------ config ops


def _parse_config_value(raw: str) -> Any:
    """Parse a CLI value: JSON scalars win, otherwise keep the raw string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def cmd_config(args: argparse.Namespace) -> int:
    from mnemoseed_local.rest_client import (
        DaemonUnavailableError,
        is_loopback,
        resolve_client,
    )

    try:
        client = resolve_client(args)
    except Exception as exc:
        return _client_error(exc)

    def _require_loopback() -> int | None:
        if not is_loopback(client.base_url):
            print(
                f"error: config operations are loopback-only; refusing {client.base_url}",
                file=sys.stderr,
            )
            return 1
        return None

    if args.config_command == "get":
        refused = _require_loopback()
        if refused is not None:
            return refused
        try:
            body = client.get("/api/v1/config")
        except (DaemonUnavailableError, Exception) as exc:
            return _client_error(exc)
        if getattr(args, "json", False):
            return _emit_json(body)
        config = body.get("config", {})
        if args.key:
            found: Any = config
            for segment in args.key.split("."):
                if not isinstance(found, dict) or segment not in found:
                    print(f"key {args.key!r} not present in the resolved config")
                    return 1
                found = found[segment]
            print(json.dumps(found, ensure_ascii=False, default=str))
            return 0
        print(json.dumps(config, indent=2, ensure_ascii=False, default=str))
        return 0

    value = _parse_config_value(args.value)
    refused = _require_loopback()
    if refused is not None:
        return refused
    try:
        body = client.post("/api/v1/config/set", {"key_path": args.key_path, "value": value})
    except (DaemonUnavailableError, Exception) as exc:
        return _client_error(exc)
    if getattr(args, "json", False):
        return _emit_json(body)
    print(f"config set {args.key_path} = {value!r} (version {body.get('version_id')})")
    restart = body.get("restart_required")
    if restart:
        print("restart required to apply")
    return 0


def cmd_config_rollback(args: argparse.Namespace) -> int:
    from mnemoseed_local.rest_client import DaemonUnavailableError, is_loopback, resolve_client

    try:
        client = resolve_client(args)
    except Exception as exc:
        return _client_error(exc)
    if not is_loopback(client.base_url):
        print(
            f"error: config operations are loopback-only; refusing {client.base_url}",
            file=sys.stderr,
        )
        return 1
    try:
        body = client.post("/api/v1/config/rollback", {"version_id": args.version_id})
    except (DaemonUnavailableError, Exception) as exc:
        return _client_error(exc)
    if getattr(args, "json", False):
        return _emit_json(body)
    print(f"config rolled back to version {body.get('version_id')}")
    return 0


# ------------------------------------------------------------ profile ops


def cmd_profile(args: argparse.Namespace) -> int:
    """Profile lifecycle verbs (#109): create / list / archive / unarchive,
    all over the daemon REST (loopback-trusted like the memory surface)."""
    from mnemoseed_local.rest_client import resolve_client

    try:
        client = resolve_client(args)
        if args.profile_command == "create":
            body = client.post(
                "/api/v1/profiles",
                {
                    "profile_id": args.profile_id,
                    **({"display_name": args.display_name} if args.display_name else {}),
                },
            )
            if not getattr(args, "json", False):
                print(f"created profile {body.get('profile_id')}")
                return 0
        elif args.profile_command == "list":
            body = client.get("/api/v1/profiles")
            if getattr(args, "json", False):
                return _emit_json(body)
            profiles = body.get("profiles", [])
            for profile in profiles:
                archived = " [archived]" if profile.get("archived") else ""
                name = f"  {profile['display_name']}" if profile.get("display_name") else ""
                print(f"{profile['profile_id']}{archived}{name}")
            print(f"{len(profiles)} profile(s)")
            return 0
        else:  # archive | unarchive
            body = client.post(
                "/api/v1/profiles/archive",
                {"profile_id": args.profile_id, "archived": args.profile_command == "archive"},
            )
            if not getattr(args, "json", False):
                state = "archived" if body.get("archived") else "unarchived"
                print(f"profile {body.get('profile_id')} {state}")
                return 0
    except Exception as exc:
        return _client_error(exc)
    return _emit_json(body)


# ------------------------------------------------------------ host hook (A3 T2)


def cmd_hook(args: argparse.Namespace) -> int:
    """Host hook management (design/01 §4.5).

    ``args.host`` selects the adapter (opencode file-copy lifecycle or
    claude_code settings.json merge). Local filesystem operations only — the
    daemon REST write path is never touched. ``status`` adds a read-only
    /healthz reachability probe.
    """
    if args.host == "claude_code":
        return _cmd_hook_claude_code(args)
    return _cmd_hook_opencode(args)


def _cmd_hook_opencode(args: argparse.Namespace) -> int:
    from mnemoseed_local.hosts import install as hook

    if args.hook_command == "install":
        path, changed = hook.install_plugin()
        if changed:
            print(f"installed hook: {path}")
        else:
            print(f"hook already up to date: {path}")
        # OpenCode auto-discovers plugin files at startup, so a running host
        # only picks the hook up on its next boot.
        print("restart opencode to pick up the hook (plugin files load at startup)")
        return 0
    if args.hook_command == "uninstall":
        path, existed = hook.uninstall_plugin()
        if existed:
            print(f"uninstalled hook: {path}")
        else:
            print(f"hook not installed: {path}")
        print("restart opencode for the removal to take effect")
        return 0
    if args.hook_command == "disable":
        path, changed = hook.disable_plugin()
        if changed:
            print(f"disabled hook: {path}")
        else:
            print(f"hook not installed or already disabled: {path}")
        print("restart opencode for the switch to take effect (plugin files load at startup)")
        return 0
    if args.hook_command == "enable":
        path, changed = hook.enable_plugin()
        if changed:
            print(f"enabled hook: {path}")
        else:
            print(f"hook not disabled or not installed: {path}")
        print("restart opencode for the switch to take effect (plugin files load at startup)")
        return 0
    info = hook.hook_status()
    state_label = {
        "not-installed": "not installed",
        "match": "installed (matches shipped plugin)",
        "differs": "installed (differs from shipped plugin)",
        "disabled": "installed (disabled)",
    }[info.state]
    print(f"hook: {state_label}")
    print(f"path: {info.path}")
    reach = "reachable" if info.daemon_reachable else "unreachable"
    print(f"daemon: {reach} ({info.base_url})")
    return 0


def _cmd_hook_claude_code(args: argparse.Namespace) -> int:
    from mnemoseed_local.hosts.claude_code import install as hook

    command = args.hook_command
    try:
        if command == "install":
            path, changed = hook.install()
            print(f"{'installed hook' if changed else 'hook already up to date'}: {path}")
        elif command == "uninstall":
            path, existed = hook.uninstall()
            print(f"{'uninstalled hook' if existed else 'hook not installed'}: {path}")
        elif command == "disable":
            path, changed = hook.disable()
            print(f"{'disabled hook' if changed else 'hook not installed or already disabled'}: {path}")
        elif command == "enable":
            path, changed = hook.enable()
            print(f"{'enabled hook' if changed else 'hook not disabled or not installed'}: {path}")
        else:
            info = hook.status()
            state_label = {
                "not-installed": "not installed",
                "installed": "installed",
                "disabled": "installed (disabled)",
                "partial": "partially installed",
                "differs": "present (settings not strict JSON — fix manually)",
            }[info.state]
            print(f"hook: {state_label}")
            print(f"path: {info.path}")
            reach = "reachable" if info.daemon_reachable else "unreachable"
            print(f"daemon: {reach} ({info.base_url})")
    except hook.SettingsParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _read_stdin_text() -> str:
    """UTF-8 decode of the raw stdin bytes.

    The host pipes UTF-8 regardless of the process locale, so the stdin TEXT
    layer (locale-decoded, e.g. GBK on zh-CN Windows) must be bypassed: read
    ``stdin.buffer`` and decode as UTF-8 ourselves.
    """
    raw = getattr(sys.stdin, "buffer", None)
    if raw is not None:
        data: bytes = raw.read()
        return data.decode("utf-8", errors="replace")
    return sys.stdin.read()


def cmd_hook_event(args: argparse.Namespace) -> int:
    """Hidden transformer: host hook stdin JSON -> normalized daemon POST.

    Zero stdout on EVERY path EXCEPT a served B2.1 T2 mid-session recall pull:
    an acked UserPromptSubmit emits one JSON ``{hookSpecificOutput.additionalContext}``
    object so Claude Code injects the pending recall alongside the submitted
    prompt. Fire-and-forget with a ~2s timeout; failures swallowed into the
    opt-in stderr debug lane.
    """
    from mnemoseed_local.hosts import install as shared
    from mnemoseed_local.hosts.claude_code import events

    try:
        payload = json.loads(_read_stdin_text())
    except ValueError:
        events.debug("dropped malformed stdin payload")
        return 0
    if not isinstance(payload, dict):
        events.debug("dropped non-object stdin payload")
        return 0
    try:
        action = events.normalize_event(payload, now=time.time())
    except Exception as exc:  # noqa: BLE001 - tolerant-by-contract lane
        events.debug(f"normalization failed: {exc}")
        return 0
    if action is None:
        events.debug(f"dropped unmapped or identity-less event {payload.get('hook_event_name')!r}")
        return 0
    kind, body = action
    client = DaemonClient(
        base_url=shared.resolve_base_url(),
        profile_id=events.profile_id(),
        actor="hook",
        timeout=events.endpoint_budget(kind),
    )
    try:
        client.post(events.ENDPOINTS[kind], body.model_dump(mode="json"))
        # B2.1 T2: only an ACKED user ingest parks the focal slot, so the pull
        # runs strictly after the 2xx (ack-implies-ready); a served selection is
        # emitted as additionalContext — the ONE stdout this lane may write.
        session_id = events.injection_session_id(body)
        if session_id is not None:
            block = events.inject_recall_context(session_id, client)
            if block is not None:
                print(json.dumps(events.additional_context_payload(block)))
    except Exception as exc:  # noqa: BLE001 - fire-and-forget contract
        events.debug(f"{kind} post failed: {exc}")
    return 0


# ------------------------------------------------------------ MCP gateway (A3 T3)


def cmd_mcp(args: argparse.Namespace) -> int:
    """MCP stdio gateway (design/01 §4.5, ingestion channel ③).

    Blocking JSON-RPC loop over stdin/stdout. A down daemon is NOT a startup
    error: the handshake works regardless; only tools/call surfaces the
    connectivity failure as a structured isError result.
    """
    from mnemoseed_local.mcp_gateway import server as mcp_server

    return mcp_server.serve(client=mcp_server.build_client(args))


# ------------------------------------------------------------ plumbing


def _emit_json(payload: Any) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def _client_error(exc: Exception) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mnemoseed-local",
        description="MnemoSeed Local - local single-user AI memory layer",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create ~/.mnemoseed-local with a default config")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config")

    p_up = sub.add_parser("up", help="start the daemon (embedded single-process)")
    p_up.add_argument("--host", default="127.0.0.1")
    p_up.add_argument("--port", type=int, default=7788)
    p_up.add_argument("--baseurl", default=None, help="daemon base URL override")

    p_on = sub.add_parser("on", help="re-enable the memory service and start the daemon")
    p_on.add_argument("--host", default="127.0.0.1")
    p_on.add_argument("--port", type=int, default=7788)
    p_on.add_argument("--baseurl", default=None, help="daemon base URL override")

    p_off = sub.add_parser("off", help="stop the daemon and disable the memory service")
    p_off.add_argument("--baseurl", default=None, help="daemon base URL override")

    p_status = sub.add_parser("status", help="daemon health + resolved config")
    p_status.add_argument("--baseurl", default=None)
    p_status.add_argument("--json", action="store_true")

    p_doctor = sub.add_parser("doctor", help="run the self-check checklist")
    p_doctor.add_argument("--baseurl", default=None)

    p_recall = sub.add_parser("recall", help='recall memories: mnemoseed-local recall "<query>"')
    p_recall.add_argument("query")
    p_recall.add_argument("--top-k", type=int, default=None)
    p_recall.add_argument("--baseurl", default=None)
    p_recall.add_argument("--json", action="store_true")

    p_remember = sub.add_parser("remember", help='pin a fact: mnemoseed-local remember "<fact>"')
    p_remember.add_argument("text")
    p_remember.add_argument("--baseurl", default=None)
    p_remember.add_argument("--json", action="store_true")

    p_dream = sub.add_parser("dream", help="manual consolidation: dream --once or dream status")
    dream_sub = p_dream.add_subparsers(dest="dream_command")
    p_dream_once = dream_sub.add_parser("once", help="run exactly one dream cycle")
    p_dream_once.add_argument("--baseurl", default=None)
    p_dream_once.add_argument("--json", action="store_true")
    p_dream_status = dream_sub.add_parser("status", help="read the trigger state / pending queue")
    p_dream_status.add_argument("--baseurl", default=None)
    p_dream_status.add_argument("--json", action="store_true")

    p_forget = sub.add_parser("forget", help='delete a memory: mnemoseed-local forget "<target>"')
    p_forget.add_argument("target")
    p_forget.add_argument(
        "--kind",
        choices=("node", "chunk", "entity"),
        default="node",
        help="what the target names (default: node)",
    )
    p_forget.add_argument("--baseurl", default=None)
    p_forget.add_argument("--json", action="store_true")

    p_config = sub.add_parser("config", help="config get | set | rollback (loopback-only)")
    config_sub = p_config.add_subparsers(dest="config_command")
    p_config_get = config_sub.add_parser("get", help="show the resolved config (or one dotted key)")
    p_config_get.add_argument("key", nargs="?")
    p_config_get.add_argument("--baseurl", default=None)
    p_config_get.add_argument("--json", action="store_true")
    p_config_set = config_sub.add_parser("set", help="write one config key")
    p_config_set.add_argument("key_path")
    p_config_set.add_argument("value")
    p_config_set.add_argument("--baseurl", default=None)
    p_config_set.add_argument("--json", action="store_true")
    p_config_rollback = config_sub.add_parser("rollback", help="revert the config to a prior version")
    p_config_rollback.add_argument("version_id")
    p_config_rollback.add_argument("--baseurl", default=None)
    p_config_rollback.add_argument("--json", action="store_true")

    p_uninstall = sub.add_parser("uninstall", help="remove the local daemon / data directory")
    p_uninstall.add_argument("--purge", action="store_true", help="delete the data files too")
    p_uninstall.add_argument("--yes", action="store_true", help="skip the purge confirmation")

    p_profile = sub.add_parser("profile", help="manage memory profiles: create | list | archive | unarchive")
    profile_sub = p_profile.add_subparsers(dest="profile_command", required=True)
    p_profile_create = profile_sub.add_parser("create", help="register a profile namespace")
    p_profile_create.add_argument("profile_id")
    p_profile_create.add_argument("--display-name", default="")
    p_profile_create.add_argument("--baseurl", default=None)
    p_profile_create.add_argument("--json", action="store_true")
    p_profile_list = profile_sub.add_parser("list", help="list the registered profiles")
    p_profile_list.add_argument("--baseurl", default=None)
    p_profile_list.add_argument("--json", action="store_true")
    p_profile_archive = profile_sub.add_parser("archive", help="archive a profile")
    p_profile_archive.add_argument("profile_id")
    p_profile_archive.add_argument("--baseurl", default=None)
    p_profile_archive.add_argument("--json", action="store_true")
    p_profile_unarchive = profile_sub.add_parser("unarchive", help="unarchive a profile")
    p_profile_unarchive.add_argument("profile_id")
    p_profile_unarchive.add_argument("--baseurl", default=None)
    p_profile_unarchive.add_argument("--json", action="store_true")

    p_hook = sub.add_parser("hook", help="manage a host hook (host adapter plugin lifecycle)")
    p_hook.add_argument(
        "hook_command",
        choices=("install", "uninstall", "status", "disable", "enable"),
        help="install writes the plugin into the host config root; "
        "uninstall removes it; disable/enable rename it to the non-loading "
        "*.ts.disabled suffix (the bundle switch); status reports the "
        "install state and daemon reachability",
    )
    p_hook.add_argument(
        "host",
        choices=("opencode", "claude_code"),
        help="the host whose hook to manage (no default — installing a hook "
        "writes into that host's config directory/files, so the choice is "
        "always explicit)",
    )

    p_hook_event = sub.add_parser("_hook-event", help=argparse.SUPPRESS)
    p_hook_event.add_argument("--host", required=True, choices=("claude_code",))

    p_mcp = sub.add_parser("mcp", help="run the MCP stdio gateway (JSON-RPC over stdin/stdout)")
    p_mcp.add_argument("--baseurl", default=None, help="daemon base URL override")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "up":
        return cmd_up(args)
    if args.command == "on":
        return cmd_on(args)
    if args.command == "off":
        return cmd_off(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "recall":
        return cmd_recall(args)
    if args.command == "remember":
        return cmd_remember(args)
    if args.command == "dream":
        return cmd_dream(args)
    if args.command == "forget":
        return cmd_forget(args)
    if args.command == "config":
        return cmd_config(args) if args.config_command != "rollback" else cmd_config_rollback(args)
    if args.command == "profile":
        return cmd_profile(args)
    if args.command == "uninstall":
        return cmd_uninstall(args)
    if args.command == "hook":
        return cmd_hook(args)
    if args.command == "_hook-event":
        return cmd_hook_event(args)
    if args.command == "mcp":
        return cmd_mcp(args)
    parser.print_help(file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
