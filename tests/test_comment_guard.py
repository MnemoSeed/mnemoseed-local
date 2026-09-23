from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mnemoseed_local.comment_guard import added_lines, main, violations_for_text


def test_rejects_narrative_comments_but_ignores_strings_and_directives() -> None:
    text = """
value = "TODO is ordinary data"
# TODO: remove this workaround
result = call()  # QA workaround for issue #12
# noqa: F401
"""

    violations = violations_for_text(text, "sample.py")

    assert [item.line for item in violations] == [3, 4]


def test_handles_multiline_tail_and_block_comments() -> None:
    text = """
/*
 * HACK: temporary compatibility path
 */
const value = 1; /* FIXME: incident detail */
"""

    violations = violations_for_text(text, "sample.ts")

    assert [item.line for item in violations] == [3, 5]


def test_directive_prefixes_and_jsx_apostrophes_do_not_hide_comments() -> None:
    text = """
const title = "don't use a workaround";
// eslint-disable-next-line -- workaround is not a policy exemption
const view = <p>don't show a workaround</p>; // FIXME: temporary
"""

    violations = violations_for_text(text, "sample.jsx")

    assert [item.line for item in violations] == [3, 4]


def test_shell_css_html_powershell_and_yaml_comments_are_checked() -> None:
    text = "# TODO: workaround\nvalue: " + '"TODO ordinary string"' + "\n"

    assert violations_for_text(text, "sample.yml")[0].line == 1
    assert violations_for_text("/* HACK: temporary */", "sample.css")[0].line == 1
    assert violations_for_text("<!-- FIXME: issue -->", "sample.html")[0].line == 1
    assert violations_for_text("# TODO: workaround", "sample.ps1")[0].line == 1


def test_added_lines_reads_tracked_and_untracked_diff(tmp_path, monkeypatch) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.py").write_text("# clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "tracked.py").write_text("# clean\n# TODO: new\n", encoding="utf-8")
    (tmp_path / "new.ts").write_text("// FIXME: new\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = added_lines("HEAD")

    assert result["tracked.py"] == {2}
    assert result["new.ts"] == {1}


def test_main_fails_cleanly_for_missing_base(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["--base", "missing-base"]) == 2
    assert "missing base" in capsys.readouterr().err


def load_harness_script(name: str):
    import importlib.util

    path = Path(__file__).parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_noqa_prose_is_not_a_directive() -> None:
    violations = violations_for_text("value = 1  # noqa: workaround for issue 12\n", "sample.py")

    assert [item.line for item in violations] == [1]


def test_noqa_codes_stay_allowed() -> None:
    assert violations_for_text("# noqa: F401\n", "sample.py") == []
    assert violations_for_text("# noqa: F401, E501\n", "sample.py") == []
    assert violations_for_text("# noqa: BLE001 - transport failure\n", "sample.py") == []


def test_directive_tokens_do_not_exempt_trailing_narrative() -> None:
    for text in (
        "# noqa: F401 workaround for issue 12\n",
        "# type: ignore[attr-defined] workaround for issue 12\n",
        "# pragma: no cover workaround\n",
    ):
        assert [item.line for item in violations_for_text(text, "sample.py")] == [1]


def test_js_apostrophe_resets_at_newline() -> None:
    text = "<p>the dogs' bones</p>;\n// workaround for issue 12\n"

    assert [item.line for item in violations_for_text(text, "sample.jsx")] == [2]


def test_plain_string_without_comment_stays_clean() -> None:
    assert violations_for_text('const title = "don\'t worry";\n', "sample.jsx") == []


def test_jsx_comments_are_checked_without_flagging_string_or_attribute_text() -> None:
    assert [
        item.line
        for item in violations_for_text("<p>dogs' bones</p>; {/* workaround for issue 12 */}\n", "sample.jsx")
    ] == [1]
    assert violations_for_text('const s = "{/* workaround for issue 12 */}";\n', "sample.jsx") == []
    assert violations_for_text('<p title="{/* workaround for issue 12 */}">ok</p>\n', "sample.jsx") == []


def test_backtick_span_keeps_line_numbers() -> None:
    text = "const t = `a\\\nb`;\n// workaround for issue 12\n"

    assert [item.line for item in violations_for_text(text, "sample.js")] == [3]


def test_css_id_selector_does_not_hide_block_comment() -> None:
    text = ".a { color: #fff; /*\n * workaround for issue 3\n */ }\n"

    assert [item.line for item in violations_for_text(text, "sample.css")] == [2]


def test_browser_discovery_prefers_platform_layout(tmp_path) -> None:
    verify = load_harness_script("atlas_verify.py")
    candidate = tmp_path / "browsers"
    linux_exe = candidate / "chromium-1187" / "chrome-linux" / "headless_shell"
    windows_exe = candidate / "chromium-1187" / "chrome-win" / "headless_shell.exe"
    linux_exe.parent.mkdir(parents=True)
    linux_exe.write_text("x", encoding="utf-8")
    windows_exe.parent.mkdir(parents=True)
    windows_exe.write_text("x", encoding="utf-8")

    assert verify.find_browser_executable(candidate, "posix") == linux_exe
    assert verify.find_browser_executable(candidate, "nt") == windows_exe
    assert verify.find_browser_executable(tmp_path / "missing", "posix") is None


def test_preflight_rejects_real_home_and_reserved_port(tmp_path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "atlas_preflight.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=True)
    assert "PASS: owned run root" in result.stdout


def test_preflight_rejects_outside_root_link_missing_and_decoy(tmp_path, monkeypatch) -> None:
    import os

    preflight = load_harness_script("atlas_preflight.py")
    repo = tmp_path / "repo"
    run = repo / ".verification-runs" / "run"
    home = run / "home"
    run.mkdir(parents=True)
    home.mkdir()
    base_url = "http://127.0.0.1:54321"
    env = {
        "MNEMOSEED_LOCAL_HOME": str(home),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive,
        "HOMEPATH": str(home)[len(home.drive) :],
    }
    good_config = (
        f'baseurl = "{base_url}"\n'
        "[storage.graph.instances.isolated]\n"
        'driver = "sqlite_graph"\n'
        f'path = "{(home / "isolated.db").as_posix()}"\n'
    )

    assert preflight.validate_run_root(repo, run, home, 54321, env) == []
    assert preflight.validate_config(good_config, home, base_url) == []

    outside = tmp_path / "evil" / "run"
    assert preflight.validate_run_root(repo, outside, home, 54321, env)

    real_lstat = os.lstat

    class ReparseStat:
        def __init__(self, original) -> None:
            self._original = original
            self.st_file_attributes = getattr(original, "st_file_attributes", 0) | 0x400

        def __getattr__(self, name: str):
            return getattr(self._original, name)

    def lstat_with_reparse(path):
        original = real_lstat(path)
        if Path(path) == run:
            return ReparseStat(original)
        return original

    monkeypatch.setattr(os, "lstat", lstat_with_reparse)
    assert any("link" in error for error in preflight.validate_run_root(repo, run, home, 54321, env))

    assert any(
        "missing" in error
        for error in preflight.validate_config('baseurl = "http://127.0.0.1:1"', home, base_url)
    )
    decoy = (
        f'baseurl = "{base_url}"\n'
        '[other]\npath = "C:/evil/outside.db"\n'
        "[storage.graph.instances.isolated]\n"
        f'path = "{(home / "isolated.db").as_posix()}"\n'
    )
    assert preflight.validate_config(decoy, home, base_url) == []
    assert any(
        "duplicate" in error
        for error in preflight.validate_config(decoy + "[storage.graph.instances.isolated]\n", home, base_url)
    )
    escaped = good_config.replace("isolated.db", "../outside.db")
    assert any("escapes" in error for error in preflight.validate_config(escaped, home, base_url))
    bad_env = dict(env, HOMEPATH="C:\\evil")
    assert any("HOMEPATH" in error for error in preflight.validate_run_root(repo, run, home, 54321, bad_env))


def test_preflight_accepts_posix_empty_homedrive(tmp_path, monkeypatch) -> None:
    preflight = load_harness_script("atlas_preflight.py")
    repo = tmp_path / "repo"
    run = repo / ".verification-runs" / "run"
    home = run / "home"
    home.mkdir(parents=True)
    env = {
        "MNEMOSEED_LOCAL_HOME": str(home),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": "",
        "HOMEPATH": str(home),
    }

    assert preflight.validate_run_root(repo, run, home, 54321, env, platform="posix") == []


def test_noqa_disguise_fails_the_cli_gate(tmp_path, monkeypatch, capsys) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "guard.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "guard.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "guard.py").write_text("value = 1  # noqa: workaround for issue 12\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["--base", "HEAD"]) == 1
    assert "guard.py:1" in capsys.readouterr().out
