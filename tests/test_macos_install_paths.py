"""macOS install paths for the POSIX orchestrator.

The capture-recall default completes without touching Ollama and marks the
dream route honestly, the refinement path installs Ollama through Homebrew
only when Homebrew is present and the run consents, ``--skip-ollama`` finishes
the install with the dream route explicitly marked unavailable, and every
refusal names the exact next commands instead of dead-ending. The CI matrix
runs the script smoke and the release install proof on a macOS runner, which
stays a runner-level proof and never a real-host or model proof.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SH_TEXT = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
CI_TEXT = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="a POSIX sh runner is required")

try:
    import pty

    _HAS_PTY = hasattr(pty, "openpty")
except ImportError:
    _HAS_PTY = False

needs_pty = pytest.mark.skipif(
    not _HAS_PTY, reason="a POSIX pty is required to answer an interactive consent prompt"
)


# ---------------------------------------------------------------- helpers


def _extract_function(text: str, header: str) -> str:
    """One shell function's source: its header line through its closing brace."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header))
    if lines[start].rstrip().endswith("}"):
        return lines[start]
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _decode(completed: subprocess.CompletedProcess[bytes]) -> subprocess.CompletedProcess[str]:
    completed.stdout = completed.stdout.decode("utf-8", "replace")
    completed.stderr = completed.stderr.decode("utf-8", "replace")
    return completed


def _logging_shim_body(fail_arg: str | None = None) -> str:
    body = '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$SHIM_LOG"\n'
    if fail_arg is not None:
        body += f'case "$*" in *"{fail_arg}"*) exit 3 ;; esac\n'
    return body + "exit 0\n"


def _write_shim(directory: Path, name: str, body: str) -> None:
    shim = directory / name
    shim.write_text(body, encoding="utf-8", newline="\n")
    shim.chmod(0o755)


def _controlled_tools_dir(tmp_path: Path) -> str:
    """The POSIX tools' directory for the controlled PATH. When the shell's own
    directory also carries a system ollama (preinstalled on some CI runners),
    a private bin replaces it, so `have ollama` is false unless a test's own
    shim provides ollama."""
    sh_dir = Path(shutil.which("sh")).parent
    if not any((sh_dir / name).exists() for name in ("ollama", "ollama.exe")):
        return str(sh_dir)
    tools_bin = tmp_path / "tools-bin"
    tools_bin.mkdir(exist_ok=True)
    for name in ("sh", "uname", "sed", "awk", "head", "tr", "cat", "chmod"):
        link = tools_bin / name
        if link.exists():
            continue
        if name == "sh":
            # /bin/sh resolves through the runtime mount even when the PATH
            # lookup for sh itself is shadowed.
            link.write_text('#!/bin/sh\nexec /bin/sh "$@"\n', encoding="utf-8", newline="\n")
        else:
            source = shutil.which(name)
            if source is None:
                continue
            link.write_text(f"#!/bin/sh\nexec '{source}' \"$@\"\n", encoding="utf-8", newline="\n")
        link.chmod(0o755)
    return str(tools_bin)


def _shim_environment(
    tmp_path: Path, names: tuple[str, ...] = (), *, fail_arg: str | None = None
) -> tuple[Path, dict[str, str]]:
    """Executable sh shims on a restricted PATH plus the isolated-home env."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        _write_shim(shim_dir, name, _logging_shim_body(fail_arg))
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    env["MNEMOSEED_LOCAL_HOME"] = str(tmp_path / "home" / ".mnemoseed-local")
    env["SHIM_LOG"] = str(tmp_path / "shim.log")
    env["SHIM_DIR"] = str(shim_dir)
    env["PATH"] = str(shim_dir) + os.pathsep + _controlled_tools_dir(tmp_path)
    return shim_dir, env


def _shim_log_lines(env: dict[str, str]) -> list[str]:
    log = Path(env["SHIM_LOG"])
    if not log.is_file():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _msys_path(path: Path) -> str:
    """A path embeddable in a colon-separated POSIX PATH string."""
    posix = path.as_posix()
    if len(posix) >= 2 and posix[1] == ":":
        return "/" + posix[0].lower() + posix[2:]
    return posix


def _ollama_prelude(
    shim_dir: Path, *, mode: str = "refinement", skip_ollama: bool = False, yes: bool = False
) -> str:
    """The step-1 functions plus the globals they read, with a controlled OS."""
    functions = "\n".join(
        _extract_function(SH_TEXT, header)
        for header in (
            "die() {",
            "have() {",
            "confirm() {",
            "fetch_pipe() {",
            "plan_ollama() {",
            "install_ollama() {",
        )
    )
    return (
        f"{functions}\n"
        f"MODE='{mode}'\n"
        "OS_NAME='Darwin'\n"
        f"SKIP_OLLAMA={1 if skip_ollama else 0}\n"
        f"YES={1 if yes else 0}\n"
        "OLLAMA_URL='https://ollama.com/install.sh'\n"
        f"PATH='{_msys_path(shim_dir)}':\"$PATH\"\n"
    )


def _run_fragment(
    script: str, env: dict[str, str], stdin_data: bytes | None = None
) -> subprocess.CompletedProcess[str]:
    stdin_kwargs = {"input": stdin_data} if stdin_data is not None else {"stdin": subprocess.DEVNULL}
    completed = subprocess.run(
        ["sh", "-c", script],
        env=env,
        capture_output=True,
        timeout=60,
        **stdin_kwargs,
    )
    return _decode(completed)


def _run_fragment_on_pty(script: str, env: dict[str, str], answer: bytes) -> subprocess.CompletedProcess[str]:
    """A fragment whose stdin is a terminal, so an interactive consent can answer."""
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            ["sh", "-c", script],
            env=env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(slave)
        try:
            os.write(master, answer)
        except OSError:
            pass
        out, err = process.communicate(timeout=60)
    finally:
        os.close(master)
    return _decode(subprocess.CompletedProcess(["sh", "-c", script], process.returncode, out, err))


def _make_installer(tmp_path: Path, *, extra_shims: tuple[str, ...] = ()):
    """A full install.sh run against CLI/uv doubles on a restricted PATH."""
    _shim_dir, env = _shim_environment(tmp_path, ("uv", "mnemoseed-local", *extra_shims))

    def run(*args: str, stdin_data: bytes | None = None) -> subprocess.CompletedProcess[str]:
        stdin_kwargs = {"input": stdin_data} if stdin_data is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(
            ["sh", str(REPO_ROOT / "install.sh"), *args],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            **stdin_kwargs,
        )

    return run, env


# ---------------------------------------------------------------- string-level


def test_installer_declares_the_skip_ollama_flag() -> None:
    assert "SKIP_OLLAMA=0" in SH_TEXT
    assert "--skip-ollama) SKIP_OLLAMA=1 ;;" in SH_TEXT
    usage = SH_TEXT[SH_TEXT.index("cat <<'EOF'") : SH_TEXT.index("\nEOF\n")]
    assert "--skip-ollama" in usage
    assert "no install attempt and no model pull" in usage
    assert "the dream route is marked unavailable" in usage


def test_installer_documents_the_macos_brew_consent_flow() -> None:
    header = SH_TEXT[: SH_TEXT.index("set -eu")]
    assert "macOS: `brew install ollama` behind an explicit [y/N]" in header
    assert "confirm() {" in SH_TEXT
    assert "install Ollama now with Homebrew (`brew install ollama`)? [y/N] " in SH_TEXT
    assert "https://ollama.com/download/mac" in SH_TEXT
    # The consent answer never comes from a piped stdin: a `curl | sh` run
    # streams the script itself on stdin, so the sources are terminal stdin
    # or /dev/tty only.
    confirm = _extract_function(SH_TEXT, "confirm() {")
    assert "[ -t 0 ]" in confirm
    assert "/dev/tty" in confirm


def test_ci_matrix_runs_the_smoke_and_release_jobs_on_macos() -> None:
    smoke = CI_TEXT[CI_TEXT.index("  install-script-smoke:") : CI_TEXT.index("  release-install-verify:")]
    release = CI_TEXT[CI_TEXT.index("  release-install-verify:") :]
    for section in (smoke, release):
        assert "os: [ubuntu-latest, windows-latest, macos-latest]" in section
    assert "if: runner.os != 'Windows'" in smoke
    assert "sh -n install.sh && sh install.sh --dry-run" in smoke
    assert "not a real-host or model proof" in release


# ---------------------------------------------------------------- full runs


@needs_sh
def test_default_capture_recall_install_completes_without_ollama_and_marks_the_dream_route(
    tmp_path: Path,
) -> None:
    run, env = _make_installer(tmp_path)
    completed = run()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        "skipped (mode capture-recall runs capture, local embeddings and recall without Ollama)"
        in completed.stdout
    )
    assert (
        "to use the dream engine later: install Ollama (https://ollama.com/download),"
        " then re-run `mnemoseed-local doctor`" in completed.stdout
    )
    assert "installation complete:" in completed.stdout
    assert "init --mode capture-recall" in _shim_log_lines(env)
    assert not any("ollama" in line or line.startswith("brew") for line in _shim_log_lines(env))


@needs_sh
def test_skip_ollama_refinement_install_finishes_and_marks_the_dream_route_unavailable(
    tmp_path: Path,
) -> None:
    run, env = _make_installer(tmp_path)
    completed = run("--mode", "refinement", "--skip-ollama")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        "skipped (--skip-ollama): ollama is not on PATH and no Ollama install was attempted"
        in completed.stdout
    )
    assert "the dream route is unavailable for this install" in completed.stdout
    assert "skipped (--skip-ollama): no dream model pull was attempted" in completed.stdout
    assert "installation complete:" in completed.stdout
    log = _shim_log_lines(env)
    assert "init --mode refinement" in log
    assert not any("install ollama" in line or line.startswith("brew") for line in log)


@needs_sh
def test_skip_ollama_dry_run_has_zero_side_effects(tmp_path: Path) -> None:
    run, env = _make_installer(tmp_path)
    completed = run("--dry-run", "--mode", "refinement", "--skip-ollama")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "skipped (--skip-ollama): no Ollama install attempt and no model pull;" in completed.stdout
    assert (
        "the install finishes and the dream route is marked unavailable when Ollama is absent"
        in completed.stdout
    )
    assert "skipped (--skip-ollama): no dream model pull is attempted" in completed.stdout
    assert (
        "dry-run complete: no installers ran, no init, no doctor, no pull - nothing changed"
        in completed.stdout
    )
    assert _shim_log_lines(env) == []


@needs_sh
def test_skip_ollama_with_ollama_present_skips_the_pull_and_keeps_the_route_honest(
    tmp_path: Path,
) -> None:
    run, env = _make_installer(tmp_path, extra_shims=("ollama",))
    completed = run("--mode", "refinement", "--skip-ollama")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "      found - skipping install" in completed.stdout
    assert "skipped (--skip-ollama): no dream model pull was attempted" in completed.stdout
    assert (
        "ollama is on PATH; pull a dream model later with: ollama pull <dream model>"
        " (see `mnemoseed-local doctor`)" in completed.stdout
    )
    assert "the dream route is unavailable for this install" not in completed.stdout
    log = _shim_log_lines(env)
    assert "init --mode refinement" in log
    assert not any("pull" in line for line in log)


@needs_sh
def test_refinement_without_ollama_still_refuses_on_unsupported_platforms(tmp_path: Path) -> None:
    run, env = _make_installer(tmp_path)
    completed = run("--mode", "refinement")
    assert completed.returncode != 0
    assert "install ollama manually from https://ollama.com/download" in completed.stderr
    assert "re-run with --skip-ollama to finish the install without the dream route" in completed.stderr
    assert _shim_log_lines(env) == []


# ---------------------------------------------------------------- darwin fragments


@needs_sh
def test_darwin_capture_recall_skips_ollama_without_a_dead_end(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    completed = _run_fragment(_ollama_prelude(shim_dir, mode="capture-recall") + "install_ollama\n", env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        "skipped (mode capture-recall runs capture, local embeddings and recall without Ollama)"
        in completed.stdout
    )
    assert "to use the dream engine later" in completed.stdout
    assert _shim_log_lines(env) == []


@needs_sh
def test_darwin_refinement_brew_yes_flag_installs_ollama_via_brew(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    _write_shim(
        shim_dir,
        "brew",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$SHIM_LOG"\n'
        "printf '#!/bin/sh\\nexit 0\\n' > \"$SHIM_DIR/ollama\"\n"
        'chmod +x "$SHIM_DIR/ollama"\n'
        "exit 0\n",
    )
    completed = _run_fragment(_ollama_prelude(shim_dir, yes=True) + "install_ollama\n", env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "install ollama" in _shim_log_lines(env)
    assert "installing Ollama via Homebrew..." in completed.stdout
    assert "      installed" in completed.stdout


@needs_sh
@needs_pty
def test_darwin_refinement_brew_consent_via_terminal_installs_ollama(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    _write_shim(
        shim_dir,
        "brew",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$SHIM_LOG"\n'
        "printf '#!/bin/sh\\nexit 0\\n' > \"$SHIM_DIR/ollama\"\n"
        'chmod +x "$SHIM_DIR/ollama"\n'
        "exit 0\n",
    )
    completed = _run_fragment_on_pty(_ollama_prelude(shim_dir) + "install_ollama\n", env, b"y\n")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "install Ollama now with Homebrew (`brew install ollama`)? [y/N] " in completed.stdout
    assert "install ollama" in _shim_log_lines(env)
    assert "      installed" in completed.stdout


@needs_sh
@needs_pty
def test_darwin_refinement_brew_declined_consent_names_the_exact_next_steps(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    _write_shim(shim_dir, "brew", _logging_shim_body())
    completed = _run_fragment_on_pty(_ollama_prelude(shim_dir) + "install_ollama\n", env, b"n\n")
    assert completed.returncode != 0
    assert "re-run with --skip-ollama to finish the install without the dream route" in completed.stderr
    assert "brew install ollama" in completed.stderr
    assert "https://ollama.com/download/mac" in completed.stderr
    assert _shim_log_lines(env) == []


@needs_sh
def test_darwin_refinement_without_an_answer_source_declines_instead_of_guessing(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    _write_shim(shim_dir, "brew", _logging_shim_body())
    completed = _run_fragment(_ollama_prelude(shim_dir) + "install_ollama\n", env)
    assert completed.returncode != 0
    assert "re-run with --skip-ollama to finish the install without the dream route" in completed.stderr
    assert _shim_log_lines(env) == []


@needs_sh
def test_darwin_brew_consent_never_consumes_a_piped_stdin(tmp_path: Path) -> None:
    # A piped answer must stay unread: a `curl | sh` run streams the script
    # itself on stdin, so reading it would corrupt the install; the consent
    # may only come from a terminal stdin or /dev/tty. A non-EOF piped "y"
    # must therefore still end in the refusal with no brew invocation.
    shim_dir, env = _shim_environment(tmp_path)
    _write_shim(shim_dir, "brew", _logging_shim_body())
    completed = _run_fragment(_ollama_prelude(shim_dir) + "install_ollama\n", env, stdin_data=b"y\n")
    assert completed.returncode != 0
    assert "re-run with --skip-ollama to finish the install without the dream route" in completed.stderr
    assert _shim_log_lines(env) == []


@needs_sh
def test_darwin_refinement_without_brew_names_homebrew_ollama_and_the_skip_path(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    completed = _run_fragment(_ollama_prelude(shim_dir) + "install_ollama\n", env)
    assert completed.returncode != 0
    assert "ollama not found and Homebrew is not installed" in completed.stderr
    assert "https://brew.sh" in completed.stderr
    assert "https://ollama.com/download/mac" in completed.stderr
    assert "re-run with --skip-ollama to finish the install without the dream route" in completed.stderr
    assert _shim_log_lines(env) == []


@needs_sh
def test_darwin_skip_ollama_marks_the_dream_route_unavailable_without_invoking_brew(
    tmp_path: Path,
) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("brew",))
    completed = _run_fragment(_ollama_prelude(shim_dir, skip_ollama=True) + "install_ollama\n", env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        "skipped (--skip-ollama): ollama is not on PATH and no Ollama install was attempted"
        in completed.stdout
    )
    assert "the dream route is unavailable for this install" in completed.stdout
    assert "brew install ollama" in completed.stdout
    assert _shim_log_lines(env) == []


@needs_sh
def test_darwin_brew_failure_reports_the_manual_download(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("brew",), fail_arg="install ollama")
    completed = _run_fragment(_ollama_prelude(shim_dir, yes=True) + "install_ollama\n", env)
    assert completed.returncode != 0
    assert "'brew install ollama' failed" in completed.stderr
    assert "install it manually from https://ollama.com/download/mac" in completed.stderr
    assert "install ollama" in _shim_log_lines(env)


# ---------------------------------------------------------------- linux regressions


@needs_sh
def test_linux_refinement_with_ollama_present_skips_the_install(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path, ("ollama",))
    prelude = _ollama_prelude(shim_dir).replace("OS_NAME='Darwin'", "OS_NAME='Linux'")
    completed = _run_fragment(prelude + "install_ollama\n", env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "      found - skipping install" in completed.stdout
    assert _shim_log_lines(env) == []


@needs_sh
def test_linux_refinement_installs_ollama_via_the_official_script(tmp_path: Path) -> None:
    shim_dir, env = _shim_environment(tmp_path)
    _write_shim(
        shim_dir,
        "curl",
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$SHIM_LOG"\n'
        "cat <<'INSTALLER'\n"
        "#!/bin/sh\n"
        "printf '#!/bin/sh\\nexit 0\\n' > \"$SHIM_DIR/ollama\"\n"
        'chmod +x "$SHIM_DIR/ollama"\n'
        "exit 0\n"
        "INSTALLER\n",
    )
    prelude = _ollama_prelude(shim_dir).replace("OS_NAME='Darwin'", "OS_NAME='Linux'")
    completed = _run_fragment(prelude + "install_ollama\n", env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert any("https://ollama.com/install.sh" in line for line in _shim_log_lines(env))
    assert "not found - installing via the official script..." in completed.stdout
    assert "      installed" in completed.stdout


# ---------------------------------------------------------------- dry-run plans


@needs_sh
def test_dry_run_plan_reports_the_darwin_skip_brew_and_no_brew_paths(tmp_path: Path) -> None:
    skipped_dir, skipped_env = _shim_environment(tmp_path / "skipped")
    skipped = _run_fragment(_ollama_prelude(skipped_dir, skip_ollama=True) + "plan_ollama\n", skipped_env)
    assert skipped.returncode == 0, skipped.stdout + skipped.stderr
    assert "skipped (--skip-ollama): no Ollama install attempt and no model pull;" in skipped.stdout
    assert (
        "the install finishes and the dream route is marked unavailable when Ollama is absent"
        in skipped.stdout
    )

    brew_dir, brew_env = _shim_environment(tmp_path / "brew", ("brew",))
    planned = _run_fragment(_ollama_prelude(brew_dir) + "plan_ollama\n", brew_env)
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert "would ask [y/N] consent, then install via `brew install ollama`" in planned.stdout
    assert (
        "(--yes grants the consent; declining exits non-zero with the exact manual commands)"
        in planned.stdout
    )

    nobrew_dir, nobrew_env = _shim_environment(tmp_path / "nobrew")
    refused = _run_fragment(_ollama_prelude(nobrew_dir) + "plan_ollama\n", nobrew_env)
    assert refused.returncode == 0, refused.stdout + refused.stderr
    assert "would exit non-zero with the exact manual steps: install Homebrew" in refused.stdout
    assert (
        "(https://brew.sh) or Ollama (https://ollama.com/download/mac), or re-run with --skip-ollama"
        in refused.stdout
    )
