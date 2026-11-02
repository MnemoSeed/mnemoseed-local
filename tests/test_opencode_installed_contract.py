"""Pinned contract between the shipped opencode plugin and the INSTALLED
opencode (issue #337's bar): every hook name and bus event name plugin.ts
registers must EXIST in the installed opencode binary, and the retired V1
hook names must NOT reappear. The V305 verification round proved the shipped
V1 plugin was silently dead on opencode 2.x (V1 plugin implementations do not
run in V2), so the contract is read against the real installed package —
never against a docs snapshot.

Read-only against the installed package; skipped honestly when no opencode
installation can be discovered on this machine."""

from __future__ import annotations

import json
import re
import shutil
from functools import lru_cache
from importlib import resources
from pathlib import Path

import pytest

PLUGIN_NAME = "plugin.ts"

#: Hook designators the shipped plugin registers on the V2 plugin ctx.
EXPECTED_HOOK_NAMES = ["prompt", "context", "compaction", "execute.after"]

#: Bus event designators the shipped plugin's event loop dispatches on.
EXPECTED_EVENT_NAMES = [
    "session.created",
    "session.forked",
    "session.agent.selected",
    "session.model.selected",
    "session.text.ended",
    "session.step.ended",
    "session.step.failed",
    "session.idle",
    "session.execution.failed",
    "session.deleted",
]

#: The retired V1 hook designators — the exact drift that killed capture. They must
#: never reappear in the plugin source NOR exist in the V2 binary.
RETIRED_V1_HOOK_NAMES = [
    "chat.message",
    "chat.system.transform",
    "tool.execute.after",
    "experimental.session.compacting",
]


def _plugin_source() -> str:
    return resources.files("mnemoseed_local.hosts.opencode").joinpath(PLUGIN_NAME).read_text(encoding="utf-8")


def _strip_ts_comments(source: str) -> str:
    """Remove // and /* */ comments (string-aware) so comment text — the header
    wire-contract table in particular — can never satisfy the name contract."""
    out: list[str] = []
    i = 0
    n = len(source)
    in_str: str | None = None
    while i < n:
        ch = source[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'`":
            in_str = ch
            out.append(ch)
            i += 1
            continue
        if source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _registered_event_names(source: str) -> list[str]:
    cases = re.findall(r'case "([a-z][a-z.]*(?:\.[a-z]+)+)":', _strip_ts_comments(source))
    seen: list[str] = []
    for name in cases:
        if name not in seen:
            seen.append(name)
    return seen


def _registered_hook_names(source: str) -> list[str]:
    names = re.findall(r'(?:ctx\.session|ctx\.tool)\.hook\("([^"]+)"', _strip_ts_comments(source))
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def _discover_installed_binary() -> tuple[Path, str] | None:
    """Locate the installed opencode and its platform binary via the npm shim
    on PATH (read-only): <shim dir>/node_modules/@opencode/cli is the npm
    package whose optionalDependencies carry the per-platform binaries."""
    shim = shutil.which("opencode")
    if shim is None:
        return None
    shim_dir = Path(shim).parent
    cli_pkg = shim_dir / "node_modules" / "@opencode" / "cli"
    manifest = cli_pkg / "package.json"
    if not manifest.is_file():
        return None
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = str(meta.get("version", "unknown"))
    binary = cli_pkg / "bin" / "opencode.exe"
    if binary.is_file():
        return binary, version
    for sub in sorted((cli_pkg / "node_modules" / "@opencode").glob("*/bin/opencode.exe")):
        return sub, version
    return None


@lru_cache(maxsize=1)
def _installed_binary_bytes() -> tuple[bytes, str] | None:
    found = _discover_installed_binary()
    if found is None:
        return None
    binary, version = found
    return binary.read_bytes(), version


@pytest.fixture(scope="module")
def installed() -> tuple[bytes, str]:
    found = _installed_binary_bytes()
    if found is None:
        pytest.skip(
            "no installed opencode package discovered (opencode shim and its "
            "node_modules/@opencode/cli package are required for this contract)"
        )
    return found


def test_installed_opencode_is_discovered(installed: tuple[bytes, str]) -> None:
    _, version = installed
    assert re.match(r"\d+\.\d+\.\d+", version), f"unexpected installed version: {version}"


def test_plugin_registers_only_names_the_installed_binary_knows(
    installed: tuple[bytes, str],
) -> None:
    data, version = installed
    source = _plugin_source()
    hooks = _registered_hook_names(source)
    events = _registered_event_names(source)
    assert hooks, "the plugin must register session/tool hooks"
    assert events, "the plugin must dispatch bus events"
    for name in sorted(set(hooks) | set(events)):
        count = data.count(name.encode())
        assert count > 0, (
            f"plugin registers {name!r} but the installed opencode {version} "
            f"binary never mentions it — silent-capture drift (issue #337 class)"
        )


def test_retired_v1_hook_names_never_reappear_in_the_plugin() -> None:
    source = _plugin_source()
    for name in RETIRED_V1_HOOK_NAMES:
        assert f'"{name}"' not in source, (
            f"the retired V1 hook {name!r} is back in plugin.ts — it is dead "
            f"on opencode 2.x (V1 plugin implementations do not run in V2)"
        )


def test_retired_v1_hook_names_do_not_exist_in_the_installed_binary(
    installed: tuple[bytes, str],
) -> None:
    """The negative half of the #337 drift: the V1 hook names have ZERO
    occurrences in the V2 binary, so any future re-registration of them must
    fail this gate instead of dying silently in a real session."""
    data, version = installed
    for name in RETIRED_V1_HOOK_NAMES:
        assert data.count(name.encode()) == 0, (
            f"{name!r} unexpectedly appears in opencode {version}'s binary — "
            f"re-verify the V1/V2 drift before re-pinning this contract"
        )


def test_plugin_event_names_match_the_pinned_contract_list() -> None:
    """The registered event set is pinned literally so a new event case cannot
    silently bypass the installed-binary contract above."""
    source = _plugin_source()
    assert _registered_event_names(source) == EXPECTED_EVENT_NAMES
    assert _registered_hook_names(source) == EXPECTED_HOOK_NAMES
