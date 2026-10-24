"""Host dispatch surfaces: the ``_hook-event`` transformer table and the
``host`` adapter lifecycle skeleton.

Pins: argparse choices derive from the single dispatch table (no drift); a
missing transformer module fails open (a hook never blocks its host) while
the user-facing lifecycle fails loud; the ``[codex]`` extra pins zstandard.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from mnemoseed_local.cli import HOOK_HOSTS, HostBinding, build_parser, cmd_hook_event, main

REPO_ROOT = Path(__file__).resolve().parents[1]


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and name in action.choices:
            return action.choices[name]
    raise AssertionError(f"no {name!r} subparser")


def _choices_of(parser: argparse.ArgumentParser, flag: str) -> list[str]:
    for action in parser._actions:
        if flag in action.option_strings:
            return list(action.choices)
    raise AssertionError(f"no {flag} option")


# ---------------------------------------------------------------- dispatch table


def test_hook_event_choices_derive_from_the_dispatch_table() -> None:
    assert _choices_of(_subparser(build_parser(), "_hook-event"), "--host") == list(HOOK_HOSTS)


def test_host_group_choices_derive_from_the_dispatch_table() -> None:
    group = _subparser(build_parser(), "host")
    for verb in ("install", "uninstall", "status"):
        assert _choices_of(_subparser(group, verb), "--host") == list(HOOK_HOSTS)


def test_hook_event_unknown_host_is_an_argparse_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["_hook-event", "--host", "nope"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_host_group_unknown_host_is_an_argparse_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["host", "status", "--host", "nope"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# ---------------------------------------------------------------- fail-open transformer dispatch


def test_hook_event_missing_module_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    """A host without a transformer module (codex_cli ships none yet) must log
    honestly on stderr, keep stdout empty, and exit 0."""
    assert cmd_hook_event(argparse.Namespace(host="codex_cli")) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "codex_cli" in captured.err


def test_hook_event_missing_module_fails_open_through_main(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["_hook-event", "--host", "codex_cli"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "codex_cli" in captured.err


def test_hook_event_fake_module_name_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(
        HOOK_HOSTS,
        "fake",
        HostBinding(
            events="mnemoseed_local.hosts.__missing_host__.events",
            events_entry="transform_event",
            install="mnemoseed_local.hosts.__missing_host__.install",
        ),
    )
    assert main(["_hook-event", "--host", "fake"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fake" in captured.err


# ---------------------------------------------------------------- host lifecycle skeleton


def test_host_group_help_lists_the_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["host", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for verb in ("install", "uninstall", "status"):
        assert verb in out


def test_host_lifecycle_missing_module_fails_loud(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["host", "install", "--host", "codex_cli"]) == 1
    assert "not implemented yet for host codex_cli" in capsys.readouterr().err
    assert main(["host", "status", "--host", "codex_cli"]) == 1
    assert "not implemented yet for host codex_cli" in capsys.readouterr().err


# ---------------------------------------------------------------- host module surface


def test_events_module_reexports_the_shared_core() -> None:
    from mnemoseed_local.hosts.claude_code import events

    for name in (
        "POST_TIMEOUT_SECONDS",
        "RECALL_BUDGET_KEY",
        "RECALL_DISCLAIMER",
        "RECALL_FENCE_CLOSE",
        "RECALL_FENCE_OPEN",
        "RECALL_FENCE_SANITIZED",
        "RECALL_PULL_MAX_CHARS",
        "RECALL_PULL_TIMEOUT_SECONDS",
        "build_t2_context",
        "capture_count_line",
        "profile_id",
        "recall_count_line",
        "sanitize_recall_text",
        "visibility_mode",
    ):
        assert hasattr(events, name), name


# ---------------------------------------------------------------- [codex] extra


def test_codex_extra_pins_zstandard() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["optional-dependencies"]["codex"] == ["zstandard"]


def test_zstandard_helper_reports_the_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemoseed_local.hosts.common.zstd import require_zstandard

    monkeypatch.setitem(sys.modules, "zstandard", None)
    with pytest.raises(RuntimeError, match=r"mnemoseed-local\[codex\]"):
        require_zstandard()


def test_zstandard_helper_returns_the_lazy_imported_module(monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemoseed_local.hosts.common.zstd import require_zstandard

    fake = ModuleType("zstandard")
    monkeypatch.setitem(sys.modules, "zstandard", fake)
    assert require_zstandard() is fake
