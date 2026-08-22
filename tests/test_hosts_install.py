"""A3 T2: hook install/uninstall/status logic + the ``hook`` CLI verb.

The OpenCode config root is hermetically redirected by monkeypatching
``Path.home`` and the ``OPENCODE_CONFIG_DIR`` / ``XDG_CONFIG_HOME`` env
overrides, so the tests never touch a real ``~/.config``. The daemon
reachability probe is stubbed at the ``install.daemon_reachable`` seam (and
its httpx call is pinned separately).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mnemoseed_local.cli import main
from mnemoseed_local.hosts import install

ENV_VARS = (
    "OPENCODE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "MNEMOSEED_LOCAL_BASEURL",
    "MNEMOSEED_LOCAL_PROFILE_ID",
)

PLUGIN_RELATIVE = Path(".config") / "opencode" / "plugin" / "mnemoseed-local.ts"

DISABLED_PLUGIN_RELATIVE = Path(".config") / "opencode" / "plugin" / "mnemoseed-local.ts.disabled"


@pytest.fixture
def opencode_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic OpenCode global config root: <tmp home>/.config/opencode."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path / ".config" / "opencode"


def _stub_probe(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(install, "daemon_reachable", lambda base_url, timeout=2.0: value)


# ---------------------------------------------------------------- config root resolution


def test_root_defaults_to_home_dot_config(opencode_home: Path) -> None:
    assert install.resolve_config_root() == opencode_home


def test_root_prefers_xdg_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    assert install.resolve_config_root() == tmp_path / "xdg" / "opencode"


def test_root_prefers_opencode_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "custom"))
    assert install.resolve_config_root() == tmp_path / "custom"


# ---------------------------------------------------------------- install / uninstall


def test_install_creates_plugin_byte_identical(opencode_home: Path) -> None:
    path, changed = install.install_plugin()
    assert changed is True
    assert path == opencode_home / "plugin" / "mnemoseed-local.ts"
    assert path.is_file()
    assert path.read_bytes() == install.plugin_bytes()


def test_second_install_is_idempotent(opencode_home: Path) -> None:
    first_path, first_changed = install.install_plugin()
    second_path, second_changed = install.install_plugin()
    assert first_changed is True
    assert second_path == first_path
    assert second_changed is False
    assert second_path.read_bytes() == install.plugin_bytes()


def test_install_overwrites_a_drifted_file(opencode_home: Path) -> None:
    path, _ = install.install_plugin()
    path.write_bytes(b"drifted")
    path, changed = install.install_plugin()
    assert changed is True
    assert path.read_bytes() == install.plugin_bytes()


def test_uninstall_removes_and_reports(opencode_home: Path) -> None:
    path, existed = install.uninstall_plugin()
    assert existed is False
    install.install_plugin()
    path, existed = install.uninstall_plugin()
    assert existed is True
    assert not path.exists()
    # uninstall never touches anything else (the plugin directory stays).
    assert path.parent.exists()


# ---------------------------------------------------------------- B2.6 rename-switch (disable/enable)


def test_disable_renames_the_plugin_to_the_non_loading_suffix(opencode_home: Path) -> None:
    """B2.6 install-surface switch: the host's plugin glob matches only
    *.ts/*.js, so renaming the file to *.ts.disabled stops it loading without
    deleting it (research doc §1/§5)."""
    install.install_plugin()
    path, changed = install.disable_plugin()
    assert changed is True
    assert path == opencode_home / "plugin" / "mnemoseed-local.ts"
    assert not path.exists()
    assert (opencode_home / "plugin" / "mnemoseed-local.ts.disabled").is_file()


def test_disable_is_idempotent_and_reports_when_absent(opencode_home: Path) -> None:
    assert install.disable_plugin()[1] is False  # nothing installed
    install.install_plugin()
    assert install.disable_plugin()[1] is True
    assert install.disable_plugin()[1] is False  # already disabled


def test_enable_restores_the_disabled_plugin_byte_identical(opencode_home: Path) -> None:
    install.install_plugin()
    install.disable_plugin()
    path, changed = install.enable_plugin()
    assert changed is True
    assert path.is_file()
    assert path.read_bytes() == install.plugin_bytes()
    assert not install.disabled_path().exists()


def test_enable_is_idempotent(opencode_home: Path) -> None:
    install.install_plugin()
    assert install.enable_plugin()[1] is False  # already active
    install.disable_plugin()
    assert install.enable_plugin()[1] is True
    assert install.enable_plugin()[1] is False  # active again


def test_install_clears_a_disabled_remnant(opencode_home: Path) -> None:
    """Install = enabled: a stale disabled remnant is replaced by the active
    file so install/status/uninstall always see one coherent state."""
    install.install_plugin()
    install.disable_plugin()
    path, changed = install.install_plugin()
    assert changed is True
    assert path.is_file()
    assert not install.disabled_path().exists()


def test_uninstall_removes_the_disabled_file_too(opencode_home: Path) -> None:
    """Uninstall means fully gone: a disabled remnant is removed with the
    active file, and the existence report covers either form."""
    install.install_plugin()
    install.disable_plugin()
    path, existed = install.uninstall_plugin()
    assert existed is True
    assert not path.exists()
    assert not install.disabled_path().exists()


def test_status_reports_disabled(opencode_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install.install_plugin()
    install.disable_plugin()
    _stub_probe(monkeypatch, True)
    assert install.hook_status().state == "disabled"


def test_concurrent_disable_enable_is_atomic_and_lossless(
    opencode_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2.6 I2 BLOCKER B1: concurrent disable↔enable must not lose the file.

    20 threads mix disable/enable/status each 50 times; final state is either
    active or disabled, no exception, and the surviving file is byte-identical.
    """
    import concurrent.futures

    _stub_probe(monkeypatch, True)
    # start from installed active state
    install.install_plugin()
    expected = install.plugin_bytes()
    errors: list[BaseException] = []

    def do_disable() -> None:
        try:
            install.disable_plugin()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_enable() -> None:
        try:
            install.enable_plugin()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_status() -> None:
        try:
            install.hook_status()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = []
        for _ in range(50):
            futures.append(pool.submit(do_disable))
            futures.append(pool.submit(do_enable))
            futures.append(pool.submit(do_status))
        concurrent.futures.wait(futures)
        for fut in futures:
            # also surface any future exception
            exc = fut.exception()
            if exc is not None:
                errors.append(exc)

    assert not errors, f"concurrent ops raised: {errors}"
    state = install.hook_status().state
    assert state in {"match", "differs", "disabled"}, f"unexpected terminal state {state}"
    # byte-identical check: whichever file exists must carry the shipped bytes
    active = install.target_path()
    disabled = install.disabled_path()
    if active.is_file():
        assert active.read_bytes() == expected, "active file must stay byte-identical"
        # at most one copy should exist after the storm (atomic replace, no duplication)
        # a stale remnant would mean a lost unlink, but concurrent disable/enable may leave
        # exactly one file (either active or disabled). We tolerate at most one surviving
        # disabled copy when active exists only if a race left both — but we assert no loss.
        assert not (active.is_file() and disabled.is_file() and active.read_bytes() != expected)
    elif disabled.is_file():
        assert disabled.read_bytes() == expected, "disabled file must stay byte-identical"
    else:
        pytest.fail("concurrent storm lost the plugin file entirely")
    # exactly one of the two paths holds the file after the storm (no duplication/no loss)
    assert active.is_file() ^ disabled.is_file(), "exactly one of active/disabled must exist"


# ---------------------------------------------------------------- status


def test_status_not_installed(opencode_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(monkeypatch, False)
    info = install.hook_status()
    assert info.state == "not-installed"
    assert info.path == opencode_home / "plugin" / "mnemoseed-local.ts"
    assert info.daemon_reachable is False
    assert info.base_url == install.DEFAULT_BASE_URL


def test_status_installed_match(opencode_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install.install_plugin()
    _stub_probe(monkeypatch, True)
    info = install.hook_status()
    assert info.state == "match"
    assert info.daemon_reachable is True


def test_status_installed_differs(opencode_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = install.install_plugin()
    path.write_bytes(b"tampered")
    _stub_probe(monkeypatch, True)
    assert install.hook_status().state == "differs"


def test_status_base_url_follows_env_override(opencode_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", "http://localhost:9977")
    _stub_probe(monkeypatch, True)
    assert install.hook_status().base_url == "http://localhost:9977"


# ---------------------------------------------------------------- daemon probe


def test_daemon_probe_hits_healthz_with_2s_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        calls["url"] = url
        calls.update(kwargs)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert install.daemon_reachable("http://localhost:7788/") is True
    assert calls["url"] == "http://localhost:7788/healthz"
    assert calls["timeout"] == 2.0


def test_daemon_probe_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)
    assert install.daemon_reachable("http://localhost:7788") is False


# ---------------------------------------------------------------- CLI verb


def test_cli_hook_install_status_uninstall_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Mirrors the cli_home conventions in test_cli.py: hermetic HOME + env,
    # real main() dispatch, exit-code assertions.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(install, "daemon_reachable", lambda base_url, timeout=2.0: True)

    target = tmp_path / PLUGIN_RELATIVE
    assert main(["hook", "install", "opencode"]) == 0
    out = capsys.readouterr().out
    assert target.is_file()
    assert str(target) in out
    assert "restart opencode" in out

    assert main(["hook", "install", "opencode"]) == 0
    assert "up to date" in capsys.readouterr().out

    assert main(["hook", "status", "opencode"]) == 0
    out = capsys.readouterr().out
    assert "installed (matches shipped plugin)" in out
    assert "daemon: reachable" in out

    assert main(["hook", "disable", "opencode"]) == 0
    assert not target.exists()
    assert (tmp_path / DISABLED_PLUGIN_RELATIVE).is_file()
    assert "disabled hook" in capsys.readouterr().out

    assert main(["hook", "status", "opencode"]) == 0
    assert "installed (disabled)" in capsys.readouterr().out

    assert main(["hook", "enable", "opencode"]) == 0
    assert target.is_file()
    assert "enabled hook" in capsys.readouterr().out

    assert main(["hook", "status", "opencode"]) == 0
    assert "installed (matches shipped plugin)" in capsys.readouterr().out

    assert main(["hook", "uninstall", "opencode"]) == 0
    assert not target.exists()
    assert "uninstalled" in capsys.readouterr().out

    assert main(["hook", "status", "opencode"]) == 0
    out = capsys.readouterr().out
    assert "not installed" in out


def test_cli_hook_requires_an_explicit_host(capsys: pytest.CaptureFixture[str]) -> None:
    """No default host: `hook install` alone must refuse and name the choice —
    the user consciously picks which agent's config they are writing into."""
    with pytest.raises(SystemExit) as excinfo:
        main(["hook", "install"])
    assert excinfo.value.code == 2


def test_cli_hook_rejects_an_unknown_host() -> None:
    """Only shipped adapters are valid; an unknown host is a hard parse error,
    not a silent opencode fallback."""
    with pytest.raises(SystemExit) as excinfo:
        main(["hook", "install", "codex_cli"])
    assert excinfo.value.code == 2


def test_cli_hook_status_reports_unreachable_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(install, "daemon_reachable", lambda base_url, timeout=2.0: False)
    assert main(["hook", "status", "opencode"]) == 0
    out = capsys.readouterr().out
    assert "not installed" in out
    assert "daemon: unreachable" in out
