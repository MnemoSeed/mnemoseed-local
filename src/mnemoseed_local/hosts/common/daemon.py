"""Shared daemon POST discipline for host hooks: env base URL, per-lane
timeouts, fire-and-forget failure reporting, the dedicated recall pull, and
the per-event project binding gate."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from httpx import Timeout

from mnemoseed_local import config as config_mod
from mnemoseed_local import endpoint
from mnemoseed_local.config import live_home_dir
from mnemoseed_local.hosts.common.project_binding import (
    PROFILE_ENV_VAR,
    BindingError,
    bindings_path,
    contains_root,
    load_bindings,
    resolve_profile,
    roots_for_profile,
)
from mnemoseed_local.rest_client import DaemonClient, DaemonUnavailableError, client_for

#: Daemon base URL a hook posts to when no env override is set.
DEFAULT_BASE_URL = "http://localhost:7788"

#: Fire-and-forget POST budget (mirrors the opencode plugin's 2s discipline).
POST_TIMEOUT_SECONDS = 2.0

#: Dedicated mid-session pull budget: a distinct constant — reusing the flat
#: 2s host budget would be too heavy for a per-turn pull.
RECALL_PULL_TIMEOUT_SECONDS = 0.3

#: Fallback item budget when the daemon omits ``budget_chars`` (older daemon,
#: or a malformed payload). The daemon is the ONLY budget authority in the
#: normal path; this mirrors the opencode plugin's ``RECALL_PULL_MAX_CHARS``.
RECALL_PULL_MAX_CHARS = 1200

#: The daemon reports its effective item budget on the wire under this key.
RECALL_BUDGET_KEY = "budget_chars"

#: Hook outbox: one JSONL line per failed POST, flushed in order on recovery.
#: Retention defaults live here and are reversible: 24h / 10MB, oldest dropped.
OUTBOX_FILENAME = "hook-outbox.jsonl"
OUTBOX_MAX_AGE_SECONDS = 24 * 60 * 60
OUTBOX_MAX_BYTES = 10 * 1024 * 1024

#: Always-on stderr prefix (never gated by the debug lane).
WARNING_PREFIX = "mnemoseed-local:"


def resolve_base_url(*, verify: bool = False) -> str:
    """Daemon base URL the hook posts to (env override, else the default).

    With ``verify`` the endpoint record and instance handshake gate the URL.
    Installers probe reachability without a credential and keep the raw URL.
    """
    override = (os.environ.get("MNEMOSEED_LOCAL_BASEURL") or "").strip()
    raw = (override or DEFAULT_BASE_URL).rstrip("/")
    if not verify:
        return raw
    return endpoint.resolve(raw, override=bool(override), allow_missing=True)


def profile_id() -> str:
    """Profile binding convention: env override or the default profile."""
    return os.environ.get("MNEMOSEED_LOCAL_PROFILE_ID") or "default"


#: Visible restart instruction every binding refusal carries: the controlled
#: launch is the only way a host instance may pin root and profile together.
RESTART_INSTRUCTION = (
    "restart the host with 'mnemoseed-local project run --root <project> -- <host argv>' "
    "so the root and the profile stay pinned together"
)

_GATE_MODES = ("legacy", "bound", "manual", "rejected")


@dataclass(frozen=True)
class BindingDecision:
    """One host event's outcome under the project binding contract.

    ``profile`` is the namespace the operation must use — always the
    launcher's env selection, never a rewritten value. ``mode`` is one of
    :data:`_GATE_MODES`: the legacy unbound/default entry, a verified
    controlled launch, an explicit env selection on an unbound root, or a
    refusal. ``rejected`` carries the visible refusal reason (with restart
    instructions) when the event must be dropped without any daemon write.
    """

    mode: str
    profile: str
    bound_roots: tuple[Path, ...] = ()
    rejected: str | None = None


def _under(root: Path, directory: Path) -> bool:
    """Component-boundary containment of ``directory`` under ``root``
    (case-folded where the platform folds paths)."""
    return contains_root(root, directory)


def _rejection(profile: str, roots: tuple[Path, ...], reason: str) -> BindingDecision:
    shown = ", ".join(str(root) for root in roots) if roots else "none"
    return BindingDecision(
        mode="rejected",
        profile=profile,
        bound_roots=roots,
        rejected=(
            f"event dropped: {reason}; bound root(s) for profile {profile!r}: {shown}; {RESTART_INSTRUCTION}"
        ),
    )


def binding_gate(cwd: object) -> BindingDecision:
    """Resolve one host event against the project binding registry.

    With no profile env var the event is the legacy unbound/default entry and
    the registry is never consulted. Under an explicit profile the registry
    only verifies consistency: a cwd bound to another profile, a cwd outside
    every root bound to the active profile, a missing cwd when bound roots
    exist, and an unreadable registry each reject the event — never a silent
    namespace switch. A subdirectory of a bound root stays inside its
    project's namespace; an unbound cwd under an explicit selection with
    nothing bound anywhere is the honored manual path.
    """
    env = os.environ.get(PROFILE_ENV_VAR, "").strip()
    if not env:
        return BindingDecision(mode="legacy", profile=profile_id())
    try:
        bindings = load_bindings(bindings_path(live_home_dir()))
    except BindingError as exc:
        return _rejection(env, (), f"project binding registry is unreadable ({exc})")
    roots = tuple(roots_for_profile(bindings, env))
    if not isinstance(cwd, str) or not cwd.strip():
        if not roots:
            return BindingDecision(mode="manual", profile=env)
        return _rejection(
            env, roots, "this host event carries no cwd, so the controlled launch cannot be verified"
        )
    directory = Path(os.path.realpath(cwd))
    exact = resolve_profile(directory, bindings)
    if exact is not None:
        if exact == env:
            return BindingDecision(mode="bound", profile=env, bound_roots=roots)
        return _rejection(
            env, roots, f"cwd {directory} is bound to profile {exact!r}, not the active profile {env!r}"
        )
    if any(_under(root, directory) for root in roots):
        return BindingDecision(mode="bound", profile=env, bound_roots=roots)
    if not roots:
        return BindingDecision(mode="manual", profile=env)
    return _rejection(
        env, roots, f"cwd {directory} does not match the bound project for the active profile {env!r}"
    )


def _binding_state_line(decision: BindingDecision) -> str:
    """The one opt-in debug line a hook may render about the binding state."""
    if decision.mode == "legacy":
        return f"binding: profile {decision.profile} unbound (legacy default entry; no project binding)"
    if decision.mode == "manual":
        return f"binding: profile {decision.profile} unbound (explicit env selection; no project root bound)"
    shown = ", ".join(str(root) for root in decision.bound_roots)
    line = f"binding: profile {decision.profile} bound to {shown}"
    if len(decision.bound_roots) > 1:
        line += " (shared: these roots deliberately use the same memory namespace)"
    return line


def enforce_project_binding(cwd: object) -> bool:
    """The per-event adapter gate every hook transformer runs first.

    Returns ``False`` when the event must be dropped: the refusal (with
    restart instructions) is already printed to stderr — never stdout, never
    a daemon write. Otherwise the event proceeds under the launcher-pinned
    profile and one opt-in debug line records the binding state (legacy
    "unbound", a verified bound root, or a deliberately shared profile).
    """
    decision = binding_gate(cwd)
    if decision.rejected is not None:
        print(f"mnemoseed-local: {decision.rejected}", file=sys.stderr)
        return False
    debug(_binding_state_line(decision))
    return True


def debug_enabled() -> bool:
    return bool(os.environ.get("MNEMOSEED_LOCAL_DEBUG"))


def debug(message: str) -> None:
    """Opt-in failure lane: stderr only (never stdout), silent unless enabled."""
    if debug_enabled():
        print(f"mnemoseed-local: {message}", file=sys.stderr)


def hook_client(timeout: float | Timeout = POST_TIMEOUT_SECONDS) -> DaemonClient:
    """The hook POST client: verified base URL, env profile binding, hook actor."""
    return client_for(resolve_base_url(verify=True), profile_id=profile_id(), actor="hook", timeout=timeout)


def hook_client_or_none(timeout: float | Timeout = POST_TIMEOUT_SECONDS) -> DaemonClient | None:
    """A hook client, or None when the endpoint record is stale.

    A stale record means the daemon is down (hard kill leaves the record behind)
    or a different instance owns the port. No credential is presented in that
    case; the caller routes the turn to the outbox via :func:`post_capture`.
    """
    try:
        return hook_client(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a stale endpoint must never lose a turn
        debug(f"hook client unavailable: {exc}")
        return None


def outbox_path() -> Path:
    """The spool path, resolved from the config home at call time."""
    return config_mod.CONFIG_DIR / OUTBOX_FILENAME


def _warn(message: str) -> None:
    """Emit an always-on stderr line (never gated by MNEMOSEED_LOCAL_DEBUG)."""
    print(f"{WARNING_PREFIX} {message}", file=sys.stderr)


def _read_outbox(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError, ValueError:
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "path" in entry and "body" in entry:
            entries.append(entry)
    return entries


def _write_outbox(path: Path, entries: list[dict[str, Any]]) -> None:
    payload = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        _warn(f"hook outbox could not be written at {path}: {exc}")


def _prune(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    now = time.time()
    kept = [
        entry
        for entry in entries
        if isinstance(entry.get("ts"), (int, float)) and now - float(entry["ts"]) <= OUTBOX_MAX_AGE_SECONDS
    ]
    dropped = len(entries) - len(kept)
    while kept and sum(len(json.dumps(entry)) + 1 for entry in kept) > OUTBOX_MAX_BYTES:
        kept.pop(0)
        dropped += 1
    return kept, dropped


def _spool(host: str, path: str, body: dict[str, Any]) -> None:
    target = outbox_path()
    entries = _read_outbox(target)
    entries.append({"ts": time.time(), "host": host, "path": path, "body": body})
    kept, dropped = _prune(entries)
    _write_outbox(target, kept)
    _warn(f"{host} {path} turn spooled to {target} (daemon unreachable; will flush on recovery)")
    if dropped:
        _warn(f"hook outbox overflow: dropped {dropped} oldest turn(s) from {target}")


def _flush_outbox() -> bool:
    """Re-POST spooled turns in order; True only when the spool is fully drained."""
    target = outbox_path()
    entries = _read_outbox(target)
    if not entries:
        return True
    try:
        client = hook_client()
    except Exception:  # noqa: BLE001 - still down or the endpoint is still stale
        return False
    remaining: list[dict[str, Any]] = []
    for entry in entries:
        try:
            client.post(str(entry["path"]), entry["body"])
        except Exception:  # noqa: BLE001 - keep the line for the next attempt
            remaining.append(entry)
    _write_outbox(target, remaining)
    return not remaining


def post_capture(
    client: DaemonClient | None,
    path: str,
    body: dict[str, Any],
    *,
    host: str,
) -> dict[str, Any]:
    """One bounded hook POST with durable degradation.

    Flushes the spool first (in order), so recovered turns precede this one;
    daemon ordinal dedupe absorbs any duplicate. If the spool cannot drain or
    the POST fails, appends one line, emits an always-on stderr warning and
    re-raises so the host's fire-and-forget guard aborts the lane (no T2 pull
    on a failed ingest). No retries are added.
    """
    if not _flush_outbox():
        _spool(host, path, body)
        raise DaemonUnavailableError(f"{host} {path} spooled; daemon unreachable")
    if client is None:
        _spool(host, path, body)
        raise DaemonUnavailableError(f"{host} {path} spooled; daemon endpoint unavailable")
    try:
        return client.post(path, body)
    except Exception as exc:  # noqa: BLE001 - spool, warn, then let the host guard abort
        _spool(host, path, body)
        debug(f"{host} {path} post failed: {exc}")
        raise


def pull_pending_recall(session_id: str, client: DaemonClient) -> dict[str, Any] | None:
    """Bounded ``POST /session/recall-pending`` pull (300ms, fail-open).

    A dedicated await with its own timeout, never the fire-and-forget
    ``post()`` budget. ``seen_chunk_ids`` is empty — hosts without a T1
    session-start replay have nothing before the T2 pull to de-duplicate
    against.
    """
    pull_client = DaemonClient(
        base_url=client.base_url,
        profile_id=client.profile_id,
        actor=client.actor,
        timeout=Timeout(RECALL_PULL_TIMEOUT_SECONDS),
        token=client.token,
    )
    try:
        return pull_client.post(
            "/session/recall-pending",
            {"profile_id": client.profile_id, "session_id": session_id, "seen_chunk_ids": []},
        )
    except Exception as exc:  # noqa: BLE001 - fail-open pull lane
        debug(f"recall-pending pull failed: {exc}")
        return None
