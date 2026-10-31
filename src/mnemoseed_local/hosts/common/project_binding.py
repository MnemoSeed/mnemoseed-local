"""Explicit project-root -> profile binding shared by the CLI and every
supported host: one local registry decides which memory profile a project
uses, so no host re-derives the answer on its own.

The registry is the app home's version-1 ``project-bindings.json`` (local
metadata only — never written into a user project, never part of memory
text). Roots are canonicalized as the realpath of an existing directory;
Windows compares roots case-insensitively and POSIX case-sensitively; lookup
is exact-match only (no parent-directory inheritance, no folder-name
matching). Every failure mode refuses to rewrite the file.
"""

from __future__ import annotations

import json
import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mnemoseed_local.hosts.common.config import write_atomic

BINDINGS_FILENAME = "project-bindings.json"
BINDINGS_VERSION = 1

#: The env var an explicit profile selection travels in; a value that
#: contradicts the binding must refuse the launch, never silently win.
PROFILE_ENV_VAR = "MNEMOSEED_LOCAL_PROFILE_ID"

_BACKUP_SUFFIX = ".bak"


class BindingError(Exception):
    """A bind request or the registry file violates the binding contract;
    the registry file is left byte-for-byte unchanged."""


@dataclass(frozen=True)
class Binding:
    project_root: str
    profile_id: str


@dataclass(frozen=True)
class BindResult:
    """The outcome of one :func:`bind` call.

    ``shared_roots`` is the sharing fact the CLI must show: other roots
    explicitly bound to the same profile (deliberate co-use, never claimed
    as isolation).
    """

    action: str  # created | unchanged | replaced
    project_root: Path
    profile_id: str
    backup_path: Path | None = None
    shared_roots: tuple[Path, ...] = ()


def bindings_path(home: Path) -> Path:
    """The registry file's location inside an app home."""
    return home / BINDINGS_FILENAME


def root_key(root: Path) -> str:
    """Comparison key for stored roots: case-insensitive on Windows,
    case-sensitive on POSIX (``os.path.normcase`` semantics)."""
    return os.path.normcase(str(root))


def canonical_root(raw: str | Path) -> Path:
    """The binding identity of a project directory: the realpath of an
    existing, absolute directory that is itself no symlink/junction. The
    link refusal keeps a path alias from silently changing which root a
    binding names."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise BindingError(f"project root must be an absolute directory path: {path}")
    if os.path.islink(path) or os.path.isjunction(path):
        raise BindingError(f"project root must not be a symlink or junction: {path}")
    if not path.is_dir():
        raise BindingError(f"project root is not an existing directory: {path}")
    return Path(os.path.realpath(path))


def load_bindings(path: Path, *, known_profile_ids: Collection[str] | None = None) -> list[Binding]:
    """Parse and validate the registry; an absent file is an empty registry.

    Unknown versions, malformed structure, empty/unregistered profile ids
    and duplicate normalized roots are rejected — never auto-repaired — and
    a failed read leaves the file untouched.
    """
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BindingError(
            f"{path} is not readable strict JSON ({exc}); fix it manually, then retry"
        ) from exc
    return _validated(data, path, known_profile_ids)


def _validated(data: object, path: Path, known: Collection[str] | None) -> list[Binding]:
    if not isinstance(data, dict):
        raise BindingError(f"{path} must contain a JSON object")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != BINDINGS_VERSION:
        raise BindingError(
            f"{path} has unsupported version {version!r}; expected {BINDINGS_VERSION} "
            "(the file is left unchanged)"
        )
    entries = data.get("bindings")
    if not isinstance(entries, list):
        raise BindingError(f"{path} must carry a 'bindings' list")
    bindings: list[Binding] = []
    seen: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"project_root", "profile_id"}:
            raise BindingError(f"{path} binding entries must hold exactly project_root and profile_id")
        root_raw = entry["project_root"]
        profile_id = entry["profile_id"]
        if not isinstance(root_raw, str) or not root_raw or not os.path.isabs(root_raw):
            raise BindingError(f"{path} holds a non-absolute project_root: {root_raw!r}")
        if not isinstance(profile_id, str):
            raise BindingError(f"{path} holds a non-string profile id: {profile_id!r}")
        _require_registered(profile_id, known, f"{path}: ")
        key = root_key(Path(root_raw))
        if key in seen:
            raise BindingError(f"{path} binds the same normalized root twice: {seen[key]} and {root_raw!r}")
        seen[key] = root_raw
        bindings.append(Binding(project_root=root_raw, profile_id=profile_id))
    return bindings


def _require_registered(profile_id: str, known: Collection[str] | None, context: str) -> None:
    if not profile_id.strip():
        raise BindingError(f"{context}profile id is empty")
    if known is not None and profile_id not in known:
        raise BindingError(
            f"{context}profile {profile_id!r} is not registered; create it first with "
            f"'mnemoseed-local profile create {profile_id}'"
        )


def bind(
    home: Path,
    root: Path,
    profile_id: str,
    *,
    replace: bool = False,
    known_profile_ids: Collection[str] | None = None,
) -> BindResult:
    """Bind one canonical project root to an existing profile.

    Rebinding the same root to the same profile is a byte-identical no-op; a
    different profile without ``replace`` is refused. An explicit replace
    backs the previous registry values up first. The new content is written
    atomically and only after it validates, so a failure anywhere leaves the
    previous file bytes intact.
    """
    canonical = canonical_root(root)
    _require_registered(profile_id, known_profile_ids, "")
    path = bindings_path(home)
    bindings = load_bindings(path, known_profile_ids=known_profile_ids)
    key = root_key(canonical)
    current = next((b for b in bindings if root_key(Path(b.project_root)) == key), None)
    backup: Path | None = None
    if current is not None and current.profile_id == profile_id:
        action, final = "unchanged", bindings
    elif current is not None:
        if not replace:
            raise BindingError(
                f"{canonical} is already bound to profile {current.profile_id!r}; pass --replace to rebind it"
            )
        action = "replaced"
        backup = path.with_name(path.name + _BACKUP_SUFFIX)
        write_atomic(backup, _registry(bindings))
        final = _upsert(bindings, key, canonical, profile_id)
        _validated(_registry(final), path, known_profile_ids)
        write_atomic(path, _registry(final))
    else:
        action = "created"
        final = _upsert(bindings, key, canonical, profile_id)
        _validated(_registry(final), path, known_profile_ids)
        write_atomic(path, _registry(final))
    shared = tuple(
        Path(b.project_root)
        for b in final
        if b.profile_id == profile_id and root_key(Path(b.project_root)) != key
    )
    return BindResult(action, canonical, profile_id, backup, shared)


def _upsert(bindings: Sequence[Binding], key: str, canonical: Path, profile_id: str) -> list[Binding]:
    """Replace a root's binding in place (or append it), keeping order stable."""
    fresh = Binding(project_root=str(canonical), profile_id=profile_id)
    updated = [fresh if root_key(Path(b.project_root)) == key else b for b in bindings]
    if fresh not in updated:
        updated.append(fresh)
    return updated


def _registry(bindings: Sequence[Binding]) -> dict[str, object]:
    """The version-1 document shape for a binding list."""
    return {
        "version": BINDINGS_VERSION,
        "bindings": [{"project_root": b.project_root, "profile_id": b.profile_id} for b in bindings],
    }


def resolve_profile(root: Path, bindings: Sequence[Binding]) -> str | None:
    """The profile of the exactly-matching canonical root; None when unbound.

    No parent-directory inheritance and no folder-name matching: a moved or
    copied project is simply unbound until explicitly rebound.
    """
    key = root_key(root)
    return next((b.profile_id for b in bindings if root_key(Path(b.project_root)) == key), None)


def roots_for_profile(bindings: Sequence[Binding], profile_id: str) -> list[Path]:
    """Every root explicitly bound to a profile — the sharing fact."""
    return [Path(b.project_root) for b in bindings if b.profile_id == profile_id]


def check_launch_profile_env(bound_profile: str, environ: Mapping[str, str]) -> None:
    """Refuse a launch whose explicit profile env var contradicts the binding."""
    override = environ.get(PROFILE_ENV_VAR, "").strip()
    if override and override != bound_profile:
        raise BindingError(
            f"{PROFILE_ENV_VAR}={override!r} conflicts with the bound profile {bound_profile!r}; "
            f"unset {PROFILE_ENV_VAR} (or rebind the project), then relaunch"
        )
