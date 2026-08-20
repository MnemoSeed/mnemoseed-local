"""B2.6 probe plugin (T0-style, observation-only): static pins.

The probe ``mnemoseed_b26_probe.ts`` is a sibling of the shipped
``plugin.ts`` that opencode loads side-by-side (the loader treats every
exported function-valued module under the scanned plugin dirs as a plugin
instance). It exists to answer the B2.6 research questions — does the
``config`` hook fire, do ``cfg.mcp`` mutations stick, does an options tuple
reach the plugin — and must stay behavior-free. These pins guard the probe's
shape, its isolation from the shipped plugin and the installer, and its
parseability (same esbuild syntax gate as ``test_hosts_opencode.py``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from mnemoseed_local.hosts import install

PROBE_NAME = "mnemoseed_b26_probe.ts"


def _probe_source() -> str:
    return resources.files("mnemoseed_local.hosts.opencode").joinpath(PROBE_NAME).read_text(encoding="utf-8")


def test_probe_is_shipped_as_package_data() -> None:
    probe = resources.files("mnemoseed_local.hosts.opencode").joinpath(PROBE_NAME)
    assert probe.is_file()


def test_probe_pins_the_observation_surface() -> None:
    """Sentinel append through the config hook, options-tuple logging, the
    50-event cap and the DEBUG-gated JSONL sink; every shipped-plugin hook
    surface is mirrored with seq+name logging."""
    source = _probe_source()
    for token in (
        "b26-probe-sentinel",
        "b26-probe-noop",
        "enabled: false",
        "config: async",
        "probe-b26.jsonl",
        "MNEMOSEED_LOCAL_DEBUG",
        "EVENT_CAP = 50",
    ):
        assert token in source, token
    assert "options === undefined ? null : options" in source, "the options arg must be logged fully"
    for hook in (
        '"chat.message"',
        '"chat.system.transform"',
        '"tool.execute.after"',
        '"experimental.session.compacting"',
    ):
        assert hook in source, hook


def test_probe_has_no_process_or_network_capabilities() -> None:
    """Observation only: no child process, no shell, no socket, no fetch.
    The only side effect is the JSONL append under the DEBUG gate."""
    source = _probe_source()
    for pattern in (
        r"fetch\(",
        r"child_process",
        r"\bspawn\b",
        r"\bexec\b",
        r"http://",
        r"https://",
        r"node:net",
        r"\bWebSocket\b",
    ):
        assert re.search(pattern, source) is None, pattern


def test_shipped_plugin_does_not_reference_the_probe() -> None:
    shipped = (
        resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts").read_text(encoding="utf-8")
    )
    assert "mnemoseed_b26_probe" not in shipped
    assert "b26-probe-sentinel" not in shipped


def test_hook_install_writes_only_the_shipped_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The installer deploys exactly one file (plugin.ts as mnemoseed-local.ts);
    the probe is a manual-copy artifact and must never ride hook install."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    path, changed = install.install_plugin()
    assert changed is True
    assert path.name == "mnemoseed-local.ts"
    assert path.is_file()
    assert not (path.parent / PROBE_NAME).exists()
    installer = Path(install.__file__).read_text(encoding="utf-8")
    assert "mnemoseed_b26_probe" not in installer


def test_probe_ts_parses_clean_under_esbuild(tmp_path: Path) -> None:
    """Same syntax gate as the shipped plugin: a parse-broken probe would
    fail silently at host load, so the parse gate must cover it too."""
    if shutil.which("npx") is None:
        pytest.skip("npx unavailable on this machine")
    probe = resources.files("mnemoseed_local.hosts.opencode").joinpath(PROBE_NAME)
    out = tmp_path / "probe.js"
    result = subprocess.run(
        f'npx --yes esbuild "{probe}" --outfile="{out}" --log-level=error',
        shell=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"mnemoseed_b26_probe.ts must parse clean: {result.stderr}"
    assert out.is_file()
