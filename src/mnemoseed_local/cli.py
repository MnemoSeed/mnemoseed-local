"""mnemoseed-local CLI entry point (A2 MVP).

Verbs: init / up / status / doctor / recall / remember / dream (--once,
status) / forget / config (get | set | rollback) / uninstall (--purge).
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
from mnemoseed_local.config import CONFIG_DIR, CONFIG_PATH, ConfigError, default_config_toml, load_config

#: Default profile at the application boundary (no identity in the local MVP).
DEFAULT_PROFILE = "default"


def cmd_init(args: argparse.Namespace) -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not args.force:
        print(f"config already exists: {CONFIG_PATH} (use --force to overwrite)")
        return 1
    CONFIG_PATH.write_text(default_config_toml(), encoding="utf-8")
    print(f"initialized {CONFIG_DIR}")
    print(f"config: {CONFIG_PATH}")
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
    parser.print_help(file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
