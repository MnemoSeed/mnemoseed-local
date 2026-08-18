"""mnemoseed-local CLI entry point (A2 MVP).

Verbs: init / up / status / doctor / recall / remember / dream (--once,
status) / forget / config (get | set | rollback) / uninstall (--purge) /
hook (install | uninstall | status) / mcp (MCP stdio gateway).
Local loopback by default; every state-changing verb talks to the daemon REST
(FR-7.12); no identity/accounts/tokens in the local MVP.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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

#: Default profile at the application boundary (no identity in the local MVP).
DEFAULT_PROFILE = "default"

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
    from mnemoseed_local.storage.factory import build_stores
    from mnemoseed_local.storage.ports import StorageError

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

    try:
        stores = build_stores(config)
        checks.append(("storage", True, "all layers resolved; capability gate passed"))
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
    return _doctor_report(checks)


def _doctor_report(checks: list[tuple[str, bool, str]]) -> int:
    failed = 0
    for name, ok, detail in checks:
        state_char = "ok" if ok else "FAIL"
        print(f"[{state_char:>4}] {name}: {detail}")
        if not ok:
            failed += 1
    if failed:
        print(f"doctor: {failed} check(s) failed")
    else:
        print("doctor: all checks passed")
    return 1 if failed else 0


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
    """B1 T3: the ensemble verify judging seat's model must be pulled when the
    user opted into verify mode.

    Dormant (ensemble off) or non-ollama routes skip. A missing model FAILS
    doctor with the pull hint — never a silent pull — while the runtime
    fallback stays the safety net (the dream still ships A's unverified result
    + audit; doctor is the honest report, not a boot gate)."""
    if config.dream.ensemble != "verify":
        return (
            "ensemble verifier",
            True,
            f"ensemble mode {config.dream.ensemble!r}; verifier model check skipped",
        )
    ok, detail = _role_model_check(config, "dream_verifier")
    return ("ensemble verifier", ok, detail)


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


# ------------------------------------------------------------ host hook (A3 T2)


def cmd_hook(args: argparse.Namespace) -> int:
    """Host hook management (design/01 §4.5).

    ``args.host`` selects the adapter (only "opencode" ships today — the
    parser's choices enforce it). Local filesystem operations only — the
    daemon REST write path is never touched. ``status`` adds a read-only
    /healthz reachability probe.
    """
    from mnemoseed_local.hosts import install as hook

    assert args.host == "opencode"  # parser choices pin this
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
    info = hook.hook_status()
    state_label = {
        "not-installed": "not installed",
        "match": "installed (matches shipped plugin)",
        "differs": "installed (differs from shipped plugin)",
    }[info.state]
    print(f"hook: {state_label}")
    print(f"path: {info.path}")
    reach = "reachable" if info.daemon_reachable else "unreachable"
    print(f"daemon: {reach} ({info.base_url})")
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

    p_hook = sub.add_parser("hook", help="manage a host hook (host adapter plugin lifecycle)")
    p_hook.add_argument(
        "hook_command",
        choices=("install", "uninstall", "status"),
        help="install writes the plugin into the host config root; "
        "uninstall removes it; status reports the install state and daemon reachability",
    )
    p_hook.add_argument(
        "host",
        choices=("opencode",),
        help="the host whose hook to manage (no default — installing a hook "
        "writes into that host's config directory, so the choice is always "
        "explicit; only opencode ships today, claude_code/codex planned)",
    )

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
    if args.command == "uninstall":
        return cmd_uninstall(args)
    if args.command == "hook":
        return cmd_hook(args)
    if args.command == "mcp":
        return cmd_mcp(args)
    parser.print_help(file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
