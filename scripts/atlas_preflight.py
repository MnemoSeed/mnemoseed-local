"""Prove Atlas harness paths and process environment are worktree-owned."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

REQUIRED_ENV_VARS = ("MNEMOSEED_LOCAL_HOME", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
RESERVED_PORTS = frozenset({7788, 4096})
_ISOLATED_TABLE = r"^\s*\[storage\.graph\.instances\.isolated\]\s*$"
_REAL_HOME_MARKERS = ("~/.mnemoseed-local", "~\\.mnemoseed-local", "%USERPROFILE%", "$HOME")


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def validate_run_root(
    repo_root: Path,
    run_root: Path,
    home: Path,
    port: int,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> list[str]:
    errors: list[str] = []
    source: Mapping[str, str | None] = os.environ if env is None else env
    owned_runs = repo_root.resolve() / ".verification-runs"
    try:
        resolved_run = run_root.resolve()
        resolved_home = home.resolve()
    except OSError as exc:
        return [f"run paths are not resolvable: {exc}"]
    if resolved_run != owned_runs and not resolved_run.is_relative_to(owned_runs):
        errors.append(f"run root escapes owned directory: {run_root}")
    if resolved_run == owned_runs:
        errors.append(f"run root must be a child directory: {run_root}")
    if _is_reparse(run_root):
        errors.append(f"run root is a link: {run_root}")
    if not resolved_home.is_relative_to(resolved_run):
        errors.append(f"home escapes run root: {home}")
    if port in RESERVED_PORTS or not 1024 < port < 65536:
        errors.append(f"unsafe port: {port}")
    for key in REQUIRED_ENV_VARS:
        value = source.get(key)
        if value is None:
            errors.append(f"{key} is not set")
        elif key in {"MNEMOSEED_LOCAL_HOME", "HOME", "USERPROFILE"}:
            try:
                resolved_value = Path(value).resolve()
            except OSError:
                errors.append(f"{key} is not resolvable: {value}")
            else:
                if not resolved_value.is_relative_to(resolved_run):
                    errors.append(f"{key} escapes run root: {value}")
    drive = home.drive if (platform or os.name) == "nt" else ""
    if source.get("HOMEDRIVE", "") != drive:
        errors.append(f"HOMEDRIVE does not match home: {source.get('HOMEDRIVE')}")
    if source.get("HOMEPATH", "") != str(home)[len(drive) :]:
        errors.append(f"HOMEPATH does not match home: {source.get('HOMEPATH')}")
    return errors


def validate_config(config_text: str, home: Path, base_url: str) -> list[str]:
    errors: list[str] = []
    if len(re.findall(_ISOLATED_TABLE, config_text, re.M)) > 1:
        return ["duplicate isolated graph table"]
    try:
        data = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        return [f"config is not valid TOML: {exc}"]
    if data.get("baseurl") != base_url:
        errors.append("config baseurl is not the owned ephemeral URL")
    isolated: object = None
    storage = data.get("storage")
    if isinstance(storage, dict):
        graph = storage.get("graph")
        if isinstance(graph, dict):
            instances = graph.get("instances")
            if isinstance(instances, dict):
                isolated = instances.get("isolated")
    if not isinstance(isolated, dict) or "path" not in isolated:
        errors.append("config is missing the isolated graph path")
    else:
        target = isolated["path"]
        if not isinstance(target, str) or not target:
            errors.append("isolated graph path is not a usable path")
        else:
            try:
                resolved_target = Path(target).resolve()
            except OSError:
                errors.append(f"isolated graph path is not resolvable: {target}")
            else:
                if not resolved_target.is_relative_to(home.resolve()):
                    errors.append(f"isolated graph path escapes home: {target}")
    for marker in _REAL_HOME_MARKERS:
        if marker in config_text:
            errors.append("config contains a real-home path")
            break
    return errors


def main() -> int:
    repo_root = Path(__file__).parent.parent.resolve()
    run_root = repo_root / ".verification-runs" / "preflight-fixture"
    home = run_root / "MNEMOSEED_LOCAL_HOME"
    port = 54321
    owned_env = {key: str(home) for key in ("MNEMOSEED_LOCAL_HOME", "HOME", "USERPROFILE")}
    owned_env["HOMEDRIVE"] = home.drive
    owned_env["HOMEPATH"] = str(home)[len(home.drive) :]
    old = {key: os.environ.get(key) for key in owned_env}
    os.environ.update(owned_env)
    try:
        errors = validate_run_root(repo_root, run_root, home, port, dict(os.environ))
        config = (
            f'baseurl = "http://127.0.0.1:{port}"\n'
            "[storage.graph.instances.isolated]\n"
            'driver = "sqlite_graph"\n'
            f'path = "{(home / "isolated.db").as_posix()}"\n'
        )
        errors.extend(validate_config(config, home, f"http://127.0.0.1:{port}"))
        if errors:
            for error in errors:
                print(f"FAIL: {error}")
            return 1
        print(f"PASS: owned run root {run_root}")
        print(f"PASS: owned environment HOME/USERPROFILE/MNEMOSEED_LOCAL_HOME={home}")
        print(f"PASS: owned ephemeral port {port}; no store opened")
        return 0
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
