"""Ownership and removal of the Unix login autostart registrations.

install.sh writes the systemd user unit (Linux) and the LaunchAgent plist
(macOS); this module owns the uninstall side only. The identity marker decides
ownership: a same-path file without it is foreign and is never modified or
deleted. Every tool failure fails closed with the exact manual commands,
mirroring the win32 branch's helper-missing envelope in ``cmd_uninstall``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

AUTOSTART_IDENTITY_MARKER = "mnemoseed-local:autostart:unix-daemon:v1"
SYSTEMD_UNIT_NAME = "mnemoseed-local.service"
LAUNCHAGENT_LABEL = "mnemoseed-local-daemon"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def launchagent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHAGENT_LABEL}.plist"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _uid() -> int:
    if sys.platform == "win32":
        raise OSError("the launchd gui domain requires a macOS user session")
    return os.getuid()


def _launchagent_domain() -> str:
    return f"gui/{_uid()}/{LAUNCHAGENT_LABEL}"


def _owned(path: Path) -> bool:
    return AUTOSTART_IDENTITY_MARKER in path.read_text(encoding="utf-8", errors="replace")


def _report_foreign(path: Path) -> None:
    print(
        f"error: {path} exists but is not provably owned by MnemoSeed (identity marker missing); "
        "inspect it manually and remove or rename it, then re-run",
        file=sys.stderr,
    )


def _print_manual_removal(reason: str, commands: tuple[str, ...]) -> None:
    print(f"error: {reason}", file=sys.stderr)
    print("remove the autostart registration manually with:", file=sys.stderr)
    for command in commands:
        print(f"  {command}", file=sys.stderr)


def _systemd_manual_commands(unit: Path) -> tuple[str, ...]:
    return (
        f"systemctl --user disable --now {SYSTEMD_UNIT_NAME}",
        f"rm {unit}",
        "systemctl --user daemon-reload",
    )


def _launchagent_manual_commands(domain: str, plist: Path) -> tuple[str, ...]:
    return (
        f"launchctl bootout {domain}",
        f"rm {plist}",
    )


def remove_unix_autostart() -> int:
    """Remove the platform's autostart registration. A missing registration
    continues, a foreign same-path file is reported and kept, and every tool
    failure fails closed; the caller treats non-zero as "stop before the
    adapter flow", exactly like the win32 branch."""
    if sys.platform == "darwin":
        return _remove_launchagent()
    return _remove_systemd_unit()


def _remove_systemd_unit() -> int:
    unit = systemd_unit_path()
    if not unit.is_file():
        print(f"no systemd user unit registered at {unit}")
        return 0
    if not _owned(unit):
        _report_foreign(unit)
        return 1
    manual = _systemd_manual_commands(unit)
    try:
        disable = _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
    except OSError as exc:
        _print_manual_removal(f"systemctl is unavailable: {exc}", manual)
        return 1
    if disable.returncode != 0:
        detail = disable.stderr.strip() or f"exit code {disable.returncode}"
        _print_manual_removal(
            f"'systemctl --user disable --now {SYSTEMD_UNIT_NAME}' failed: {detail}", manual
        )
        return 1
    try:
        unit.unlink()
    except OSError as exc:
        _print_manual_removal(f"could not remove {unit}: {exc}", manual)
        return 1
    try:
        reload_result = _run(["systemctl", "--user", "daemon-reload"])
    except OSError as exc:
        _print_manual_removal(
            f"'systemctl --user daemon-reload' failed: {exc}",
            ("systemctl --user daemon-reload",),
        )
        return 1
    if reload_result.returncode != 0:
        detail = reload_result.stderr.strip() or f"exit code {reload_result.returncode}"
        _print_manual_removal(
            f"'systemctl --user daemon-reload' failed: {detail}",
            ("systemctl --user daemon-reload",),
        )
        return 1
    print(f"removed the systemd user unit {unit}")
    return 0


def _remove_launchagent() -> int:
    plist = launchagent_plist_path()
    if not plist.is_file():
        print(f"no LaunchAgent plist registered at {plist}")
        return 0
    if not _owned(plist):
        _report_foreign(plist)
        return 1
    domain = _launchagent_domain()
    manual = _launchagent_manual_commands(domain, plist)
    try:
        loaded = _run(["launchctl", "print", domain])
    except OSError as exc:
        _print_manual_removal(f"launchctl is unavailable: {exc}", manual)
        return 1
    if loaded.returncode == 0:
        try:
            bootout = _run(["launchctl", "bootout", domain])
        except OSError as exc:
            _print_manual_removal(f"'launchctl bootout {domain}' could not run: {exc}", manual)
            return 1
        if bootout.returncode != 0:
            detail = bootout.stderr.strip() or f"exit code {bootout.returncode}"
            _print_manual_removal(f"'launchctl bootout {domain}' failed: {detail}", manual)
            return 1
    try:
        plist.unlink()
    except OSError as exc:
        _print_manual_removal(f"could not remove {plist}: {exc}", manual)
        return 1
    print(f"removed the LaunchAgent plist {plist}")
    return 0
