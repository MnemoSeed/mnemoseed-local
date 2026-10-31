#!/usr/bin/env python3
"""Clean-runner verification of the built release artifact.

The harness builds the wheel and sdist from this checkout with uv, validates
their metadata (including the ``codex`` extra requirement marker in either
quote style), installs the wheel with that extra into an isolated ``uv tool``
directory (the installers' own install style), and proves the installed surface
from outside the checkout: CLI version, codex-extra import, the MCP stdio
handshake, default and explicit-false config behaviour, and the Codex hook
lifecycle against a sandboxed host home and a fake ``/healthz`` listener. Every
child process runs with a bounded timeout; a failed assertion exits non-zero and
the structured report keeps the evidence.

Coverage limits are stated in the report itself: no model inference, no real
Codex hook trust, and no end-to-end run of the install orchestrators.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCHEMA = "mnemoseed-release-install-verify/1"

PROJECT = "mnemoseed-local"
CODEX_EXTRA = "codex"
CODEX_HOST = "codex_cli"
HOOK_TAIL = "_hook-event --host codex_cli"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
MCP_REQUIRED_TOOLS = ("recall", "remember", "dream_once", "recent_sessions", "session_windows")
FORBIDDEN_PORTS = (7788, 4096)

BUILD_TIMEOUT_S = 600.0
INSTALL_TIMEOUT_S = 900.0
CLI_TIMEOUT_S = 120.0
MCP_TIMEOUT_S = 90.0
HOOK_TIMEOUT_S = 120.0

COVERAGE = (
    "wheel and sdist built from this checkout with `uv build`",
    "wheel METADATA and sdist PKG-INFO checked for name, version, console script and the codex extra",
    "wheel installed with its codex extra into an isolated `uv tool` directory and bin dir",
    "installed CLI --version equals the artifact version",
    "`import zstandard` and package metadata resolved from the installed tool environment",
    "MCP initialize + tools/list over newline-delimited JSON-RPC on stdio (no tools/call sent)",
    "fresh `init` defaults dream.auto_trigger and capture.auto_recall ON, loaded from the installed wheel",
    "an existing explicit-false config keeps byte-identical content, is not overwritten, and loads as false",
    "Codex hook install/status/uninstall in a sandboxed CODEX_HOME: nested hooks schema, trust-unverified "
    "install state (host trust unknown), the generated command executed through the host shell, and config "
    "bytes restored on uninstall",
)

LIMITATIONS = (
    "no model inference: no Ollama model was pulled, loaded or called",
    "no real Codex host trust: the run observes the trust-unverified state only, "
    "so host trust stays unknown; "
    "the one-time /hooks trust step stays user-side and was not performed",
    "no full doctor run: the model-dependent doctor checks need a real Ollama daemon and were excluded",
    "the MCP handshake proves the stdio wire surface only; tools/call was deliberately not exercised and no "
    "daemon was reachable",
    "the install orchestrators (install.sh / install.ps1) are not executed end to end; their syntax and "
    "dry-run coverage stays in the existing install-script-smoke job",
    "a fresh artifact install is not a full model or real-host activation",
)

MCP_REQUESTS = (
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05",'
    '"capabilities":{},"clientInfo":{"name":"release-install-verify","version":"1"}}}\n'
    '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
)

_HOOK_EVENT = {
    "hook_event_name": "ReleaseVerifyUnsupportedEvent",
    "session_id": "release-install-verify",
    "cwd": ".",
}

HOOK_PAYLOAD = json.dumps(_HOOK_EVENT) + "\n"

_CONFIG_PROBE = """
import json

from mnemoseed_local import __version__
from mnemoseed_local.config import CONFIG_PATH, load_config

config = load_config()
print(json.dumps({
    "version": __version__,
    "config_path": str(CONFIG_PATH),
    "preset": config.preset,
    "baseurl": config.baseurl,
    "dream_auto_trigger": config.dream.auto_trigger,
    "capture_auto_recall": config.capture.auto_recall,
    "isolated_graph": "isolated" in config.layer_instances("graph"),
}))
"""

_PROVENANCE_PROBE = """
import importlib.metadata as metadata
import json
import sys

import mnemoseed_local
import zstandard

distribution = metadata.distribution("mnemoseed-local")
print(json.dumps({
    "interpreter": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "package_file": mnemoseed_local.__file__,
    "dist_location": str(distribution.locate_file("")),
    "installed_version": metadata.version("mnemoseed-local"),
    "zstandard_version": getattr(zstandard, "__version__", ""),
    "zstandard_file": zstandard.__file__,
}))
"""

_EXPLICIT_FALSE_CONFIG = """\
preset = "embedded"
baseurl = "{baseurl}"

[dream]
auto_trigger = false

[capture]
auto_recall = false
"""

_CODEX_FOREIGN_CONFIG = """\
model = "gpt-5-codex"

[projects."sandbox"]
trust_level = "trusted"
"""

_FOREIGN_HOOKS: dict[str, Any] = {
    "hooks": {
        "Stop": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": "third-party-noop"}],
            }
        ]
    },
    "third_party": {"keep": True},
}

_SANDBOX_CONFIG = """\
preset = "embedded"
baseurl = "{baseurl}"

[storage.graph.instances.isolated]
driver = "sqlite_graph"
path = "{isolated}"
"""

_DROPPED_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
    }
)


class Failure(RuntimeError):
    """A verification assertion or step failed."""


@dataclass
class Run:
    """One bounded child process and its captured output."""

    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float


@dataclass
class Context:
    """Paths and identity shared by every verification step."""

    repo_root: Path
    work_root: Path
    uv: Path
    version: str
    dist_dir: Path
    wheel: Path
    sdist: Path
    tool_dir: Path
    tool_bin: Path
    tool_python: Path | None = None
    cli: Path | None = None

    @property
    def installed_python(self) -> Path:
        if self.tool_python is None:
            raise Failure("the installed tool interpreter was not resolved")
        return self.tool_python

    @property
    def installed_cli(self) -> Path:
        if self.cli is None:
            raise Failure("the installed CLI shim was not resolved")
        return self.cli

    def cli_env(self, home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = _base_env(home)
        env["MNEMOSEED_LOCAL_HOME"] = str(home / ".mnemoseed-local")
        env["UV_TOOL_DIR"] = str(self.tool_dir)
        env["UV_TOOL_BIN_DIR"] = str(self.tool_bin)
        env["PATH"] = str(self.tool_bin) + os.pathsep + env["PATH"]
        if extra:
            env.update(extra)
        return env

    def uv_env(self) -> dict[str, str]:
        env = _base_env(self.work_root / "uv-home")
        env["UV_CACHE_DIR"] = str(self.work_root / "uv-cache")
        env["UV_TOOL_DIR"] = str(self.tool_dir)
        env["UV_TOOL_BIN_DIR"] = str(self.tool_bin)
        env["UV_PYTHON_DOWNLOADS"] = "automatic"
        env["UV_PYTHON_INSTALL_DIR"] = str(self.work_root / "uv-python")
        return env


@dataclass
class Session:
    """One proof's isolated home: its cwd, environment and installed entry points."""

    verifier: Verifier
    home: Path
    cwd: Path
    env: dict[str, str]
    shim: Path
    python: Path

    def cli(self, *argv: object, name: str, exit_code: int = 0) -> Run:
        """Run the installed CLI here, record it, and assert its exit code."""
        return self._run([self.shim, *argv], name=name, exit_code=exit_code)

    def host(self, *argv: object, name: str) -> Run:
        """Run one ``host`` subcommand against the sandboxed host home."""
        return self.cli("host", *argv, "--host", CODEX_HOST, name=name)

    def probe(self, source: str, *, name: str) -> Run:
        """Run one assertion probe under the isolated tool interpreter."""
        return self._run([self.python, "-I", "-c", source], name=name)

    def _run(self, cmd: Sequence[object], *, name: str, exit_code: int = 0) -> Run:
        """Record one bounded invocation in this session's environment."""
        return self.verifier.run(
            cmd,
            cwd=self.cwd,
            env=self.env,
            label=name,
            name=name,
            timeout=CLI_TIMEOUT_S,
            exit_code=exit_code,
        )


def _session(verifier: Verifier, context: Context, name: str) -> Session:
    """An isolated home, its scratch cwd and environment for one proof."""
    home = context.work_root / name
    cwd = home / "scratch"
    cwd.mkdir(parents=True, exist_ok=True)
    return Session(
        verifier=verifier,
        home=home,
        cwd=cwd,
        env=context.cli_env(home),
        shim=context.installed_cli,
        python=context.installed_python,
    )


class Verifier:
    """Runs bounded commands, records one JSON report, fails on a false check."""

    def __init__(self, report_path: Path) -> None:
        self.report_path = report_path
        self.steps: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "running",
            "started_at": _utc_now(),
            "platform": _platform_facts(),
            "artifact": {},
            "tools": {},
            "steps": self.steps,
            "coverage": list(COVERAGE),
            "limitations": list(LIMITATIONS),
            "failures": [],
        }
        self._current: dict[str, Any] | None = None
        self.flush()

    def flush(self) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(self.report, indent=2, ensure_ascii=False) + "\n"
        self.report_path.write_text(body, encoding="utf-8")

    @contextmanager
    def step(self, name: str) -> Iterator[dict[str, Any]]:
        entry: dict[str, Any] = {"step": name, "status": "running", "assertions": [], "commands": []}
        self.steps.append(entry)
        previous = self._current
        self._current = entry
        started = time.monotonic()
        try:
            yield entry
        except Exception:
            entry["status"] = "failed"
            raise
        else:
            entry["status"] = "passed"
        finally:
            entry["duration_s"] = round(time.monotonic() - started, 3)
            self._current = previous
            self.flush()

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        record: dict[str, Any] = {"check": name, "ok": bool(ok)}
        if detail:
            record["detail"] = detail
        self._container("assertions").append(record)
        self.flush()
        if not ok:
            raise Failure(f"{name}: {detail}" if detail else name)

    def note(self, name: str, detail: str = "") -> None:
        record: dict[str, Any] = {"note": name, "ok": True}
        if detail:
            record["detail"] = detail
        self._container("assertions").append(record)
        self.flush()

    def contains(
        self,
        run: Run,
        needle: str,
        name: str,
        *,
        stream: str = "stdout",
        present: bool = True,
    ) -> None:
        text = run.stderr if stream == "stderr" else run.stdout
        self.check(name, (needle in text) is present, _tail(text, 800))

    def run(
        self,
        argv: Sequence[object],
        *,
        cwd: Path,
        env: Mapping[str, str],
        label: str,
        name: str,
        timeout: float,
        stdin_text: str | None = None,
        exit_code: int = 0,
    ) -> Run:
        """Run one bounded child process, record it, and assert the expected exit code."""
        argv_text = [str(item) for item in argv]
        record: dict[str, Any] = {
            "label": label,
            "argv": argv_text,
            "cwd": str(cwd),
            "timeout_s": timeout,
        }
        self._container("commands").append(record)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv_text,
                cwd=str(cwd),
                env=dict(env),
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            record["exit_code"] = None
            record["timed_out"] = True
            self.flush()
            raise Failure(f"{label}: timed out after {timeout:.0f}s ({' '.join(argv_text)})") from None
        except OSError as exc:
            record["exit_code"] = None
            record["error"] = str(exc)
            self.flush()
            raise Failure(f"{label}: could not start {' '.join(argv_text)}: {exc}") from exc
        duration = round(time.monotonic() - started, 3)
        record["exit_code"] = completed.returncode
        record["duration_s"] = duration
        record["stdout"] = _tail(completed.stdout)
        record["stderr"] = _tail(completed.stderr)
        self.flush()
        run = Run(
            argv=argv_text,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_s=duration,
        )
        self.check(name, completed.returncode == exit_code, _failure_detail(run))
        return run

    def flag(self, name: str, payload: Mapping[str, Any], key: str, expected: bool) -> None:
        """Assert one probe field is exactly the expected boolean."""
        value = payload.get(key)
        self.check(name, value is expected, str(value))

    def contained(self, name: str, path: Path, root: Path, *, expected: bool = True) -> None:
        """Assert one reported path resolves inside a root directory, or outside it."""
        self.check(name, _under(path, root) is expected, str(path))

    def _container(self, key: str) -> list[dict[str, Any]]:
        if self._current is not None:
            current: list[dict[str, Any]] = self._current[key]
            return current
        fallback: list[dict[str, Any]] = self.report.setdefault(key, [])
        return fallback


class _FakeHealthz:
    """A loopback-only /healthz listener standing in for the daemon."""

    def __init__(self) -> None:
        self._server: _HealthzServer | None = None
        self._thread: threading.Thread | None = None
        self._last_received = 0
        self.port = 0

    @property
    def requests_received(self) -> int:
        """Requests received by the running listener; the last count once stopped."""
        if self._server is not None:
            return self._server.requests_received
        return self._last_received

    def start(self) -> int:
        self._server = _HealthzServer(("127.0.0.1", 0))
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._last_received = self._server.requests_received
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None


class _HealthzHandler(BaseHTTPRequestHandler):
    """Answers GET /healthz with a healthy payload; anything else is 404."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self.server.count_request()
        if self.path.split("?", 1)[0] != "/healthz":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"ok": True, "gate": {"ok": True}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class _HealthzServer(ThreadingHTTPServer):
    """Carries the received-request count for the fake /healthz listener."""

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _HealthzHandler)
        self.requests_received = 0
        self._lock = threading.Lock()

    def count_request(self) -> None:
        with self._lock:
            self.requests_received += 1


def _base_env(home: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _DROPPED_ENV_KEYS or upper.startswith(("PYTHON", "UV_", "CODEX_", "MNEMOSEED")):
            continue
        env[key] = value
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "MNEMOSEED_LOCAL_PROFILE_ID": "default",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_NO_PROGRESS": "1",
            "NO_COLOR": "1",
        }
    )
    env["PATH"] = os.environ.get("PATH") or os.environ.get("Path") or ""
    return env


def _platform_facts() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "runner": os.environ.get("RUNNER_OS", ""),
        "sha": os.environ.get("GITHUB_SHA", ""),
    }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _tail(text: str, limit: int = 4000) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return "..." + stripped[-limit:]


def _json_text(value: object, limit: int = 500) -> str:
    """Compact JSON evidence for one assertion detail."""
    return json.dumps(value)[:limit]


def _failure_detail(run: Run) -> str:
    parts: list[str] = []
    if run.exit_code is not None:
        parts.append(f"exit {run.exit_code}")
    if run.stderr.strip():
        parts.append("stderr: " + _tail(run.stderr, 1500))
    if run.stdout.strip():
        parts.append("stdout: " + _tail(run.stdout, 1500))
    return " | ".join(parts) if parts else "no output"


def _preserved_bytes_detail(label: str, before: bytes, after: bytes, changed: str) -> str:
    """Observe preserved bytes when the comparison passes; keep the failure wording fixed."""
    if after == before:
        return f"{label} bytes preserved ({len(before)})"
    return changed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _normcase(path: object) -> str:
    return os.path.normcase(str(path))


def _same_path(value: object, expected: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return _normcase(Path(value).resolve()) == _normcase(expected.resolve())
    except OSError:
        return False


def _under(path: Path, root: Path) -> bool:
    try:
        target = _normcase(path.resolve())
        base = _normcase(root.resolve())
    except OSError:
        return False
    return target == base or target.startswith(base + os.sep)


def _spare_port() -> int:
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in FORBIDDEN_PORTS:
            return port
    raise Failure("could not reserve a spare loopback port")


def _parse_json_stdout(verifier: Verifier, run: Run, label: str) -> dict[str, Any]:
    lines = [line for line in run.stdout.splitlines() if line.strip()]
    verifier.check(f"{label}: produced one JSON line", len(lines) == 1, _failure_detail(run))
    try:
        payload = json.loads(lines[-1])
    except (IndexError, ValueError) as exc:
        raise Failure(f"{label}: stdout is not JSON ({exc}): {_tail(run.stdout)}") from exc
    verifier.check(f"{label}: printed a JSON object", isinstance(payload, dict), _tail(run.stdout))
    return payload


def project_version(repo_root: Path) -> tuple[str, str]:
    try:
        data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        return str(project["name"]), str(project["version"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise Failure(f"cannot read the project version from pyproject.toml: {exc}") from exc


def _artifact_version(name: str, suffix: str) -> str:
    stem = name[: -len(suffix)]
    parts = stem.split("-")
    return parts[1] if len(parts) > 1 else ""


_MARKER_ATOM = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*"
    r"(?P<op>===|==|!=|<=|>=|~=|<|>)\s*"
    r"""(?P<value>'[^']*'|"[^"]*")"""
)
_DIST_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MARKER_EQUALITY = ("==", "===")


def _marker_extras(requirement: str) -> frozenset[str]:
    """Extras a requirement's environment marker selects, in either quote style."""
    selected: set[str] = set()
    denied: set[str] = set()
    for atom in _MARKER_ATOM.finditer(requirement.partition(";")[2]):
        if atom["name"] != "extra":
            continue
        value = atom["value"][1:-1]
        if atom["op"] in _MARKER_EQUALITY:
            selected.add(value)
        elif atom["op"] == "!=":
            denied.add(value)
    return frozenset(selected - denied)


def _extra_requirements(requires: Sequence[str], extra: str) -> list[str]:
    """Requirements gated on one extra by their marker."""
    return [item for item in requires if extra in _marker_extras(item)]


def _requirement_name(requirement: str) -> str:
    """PEP 503-normalized distribution name of a requirement string."""
    match = _DIST_NAME.match(requirement.strip())
    return re.sub(r"[-_.]+", "-", match.group(0)).lower() if match else ""


def _member(names: Sequence[str], suffix: str) -> str | None:
    """First archive member carrying one dist-info suffix."""
    return next((item for item in names if item.endswith(suffix)), None)


def _is_pkg_info(name: str) -> bool:
    """True for a top-level sdist PKG-INFO member name."""
    return name.count("/") == 1 and name.endswith("/PKG-INFO")


def wheel_metadata(wheel: Path) -> dict[str, Any]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = _member(names, ".dist-info/METADATA")
        if metadata_name is None:
            raise Failure(f"{wheel.name}: no .dist-info/METADATA member")
        entry_points_name = _member(names, ".dist-info/entry_points.txt")
        record_name = _member(names, ".dist-info/RECORD")
        message = BytesParser().parsebytes(archive.read(metadata_name))
        entry_points = archive.read(entry_points_name).decode("utf-8") if entry_points_name else ""
    return {
        "name": message.get("Name"),
        "version": message.get("Version"),
        "requires": message.get_all("Requires-Dist") or [],
        "extras": message.get_all("Provides-Extra") or [],
        "entry_points": entry_points,
        "has_record": record_name is not None,
        "member_count": len(names),
    }


def sdist_metadata(sdist: Path) -> dict[str, Any]:
    with tarfile.open(sdist) as archive:
        members = archive.getmembers()
        member = next((item for item in members if _is_pkg_info(item.name)), None)
        if member is None:
            raise Failure(f"{sdist.name}: no top-level PKG-INFO member")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise Failure(f"{sdist.name}: PKG-INFO member is not a file")
        message = BytesParser().parsebytes(extracted.read())
    return {"name": message.get("Name"), "version": message.get("Version"), "member_count": len(members)}


def prepare(verifier: Verifier, repo_root: Path, work_root: Path) -> Context:
    uv = shutil.which("uv")
    verifier.check("uv is available on PATH", uv is not None, "install uv first (CI sets it up)")
    uv_path = Path(uv or "")
    name, version = project_version(repo_root)
    verifier.check("the checkout declares the expected project name", name == PROJECT, name)
    verifier.report["artifact"]["declared_version"] = version
    dist_dir = work_root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    context = Context(
        repo_root=repo_root,
        work_root=work_root,
        uv=uv_path,
        version=version,
        dist_dir=dist_dir,
        wheel=dist_dir / "pending.whl",
        sdist=dist_dir / "pending.tar.gz",
        tool_dir=work_root / "uv-tools",
        tool_bin=work_root / "uv-tools-bin",
    )
    uv_version = verifier.run(
        [uv_path, "--version"],
        cwd=work_root,
        env=context.uv_env(),
        label="uv --version",
        name="uv answers --version",
        timeout=CLI_TIMEOUT_S,
    )
    verifier.report["tools"]["uv"] = uv_version.stdout.strip()

    verifier.run(
        [uv_path, "build", "--out-dir", str(dist_dir)],
        cwd=repo_root,
        env=context.uv_env(),
        label="uv build",
        name="uv build produced the release artifacts",
        timeout=BUILD_TIMEOUT_S,
    )
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    verifier.check(
        "exactly one wheel was built",
        len(wheels) == 1,
        f"wheels: {[item.name for item in wheels]}",
    )
    verifier.check(
        "exactly one sdist was built",
        len(sdists) == 1,
        f"sdists: {[item.name for item in sdists]}",
    )
    context.wheel = wheels[0]
    context.sdist = sdists[0]
    artifact = verifier.report["artifact"]
    artifact["wheel"] = {
        "name": context.wheel.name,
        "sha256": _sha256(context.wheel),
        "size_bytes": context.wheel.stat().st_size,
    }
    artifact["sdist"] = {
        "name": context.sdist.name,
        "sha256": _sha256(context.sdist),
        "size_bytes": context.sdist.stat().st_size,
    }
    verifier.check(
        "the wheel filename carries the declared version",
        _artifact_version(context.wheel.name, ".whl") == version,
        context.wheel.name,
    )
    verifier.check(
        "the sdist filename carries the declared version",
        _artifact_version(context.sdist.name, ".tar.gz") == version,
        context.sdist.name,
    )
    return context


def validate_artifact_metadata(verifier: Verifier, context: Context) -> None:
    wheel = wheel_metadata(context.wheel)
    verifier.check("wheel METADATA names the project", wheel["name"] == PROJECT, str(wheel["name"]))
    verifier.check(
        "wheel METADATA carries the declared version",
        wheel["version"] == context.version,
        str(wheel["version"]),
    )
    verifier.check("wheel carries a RECORD member", wheel["has_record"] is True)
    verifier.check(
        "wheel declares the codex extra",
        CODEX_EXTRA in {str(extra) for extra in wheel["extras"]},
        f"extras: {wheel['extras']}",
    )
    codex_requirements = _extra_requirements(wheel["requires"], CODEX_EXTRA)
    codex_names = {_requirement_name(item) for item in codex_requirements}
    verifier.check(
        "the codex extra requires zstandard",
        "zstandard" in codex_names,
        f"codex requirements: {codex_requirements}",
    )
    verifier.check(
        "wheel declares the console script",
        f"{PROJECT} = mnemoseed_local.cli:main" in wheel["entry_points"],
        _tail(wheel["entry_points"], 500),
    )
    sdist = sdist_metadata(context.sdist)
    verifier.check("sdist PKG-INFO names the project", sdist["name"] == PROJECT, str(sdist["name"]))
    verifier.check(
        "sdist PKG-INFO carries the declared version",
        sdist["version"] == context.version,
        str(sdist["version"]),
    )
    verifier.report["artifact"]["wheel_metadata"] = {
        "extras": wheel["extras"],
        "codex_requirements": codex_requirements,
        "member_count": wheel["member_count"],
    }


def install_artifact(verifier: Verifier, context: Context) -> None:
    scratch = context.work_root / "install-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    requirement = f"{PROJECT}[{CODEX_EXTRA}] @ {context.wheel.resolve().as_uri()}"
    verifier.run(
        [context.uv, "tool", "install", "--python", "3.12", requirement],
        cwd=scratch,
        env=context.uv_env(),
        label="uv tool install (built wheel with codex extra)",
        name="uv tool install accepted the built wheel with its codex extra",
        timeout=INSTALL_TIMEOUT_S,
    )
    tool_root = context.tool_dir / PROJECT
    verifier.check(
        "the tool environment landed in the isolated UV_TOOL_DIR",
        tool_root.is_dir(),
        f"expected {tool_root}",
    )
    candidates = (
        tool_root / "Scripts" / "python.exe",
        tool_root / "bin" / "python",
        tool_root / "bin" / "python3",
    )
    interpreter = next((item for item in candidates if item.is_file()), None)
    verifier.check(
        "the tool environment exposes its own interpreter",
        interpreter is not None,
        f"looked for {[str(item) for item in candidates]}",
    )
    context.tool_python = interpreter
    cli_names = ("mnemoseed-local.exe", "mnemoseed-local") if os.name == "nt" else ("mnemoseed-local",)
    shim = next(
        (context.tool_bin / item for item in cli_names if (context.tool_bin / item).is_file()),
        None,
    )
    verifier.check(
        "the isolated bin dir carries the CLI shim",
        shim is not None,
        f"looked for {[item for item in cli_names]} in {context.tool_bin}",
    )
    context.cli = shim
    verifier.report["installed"] = {
        "tool_dir": str(tool_root),
        "bin_dir": str(context.tool_bin),
        "interpreter": str(interpreter),
        "cli": str(shim),
    }


def proof_installed_surface(verifier: Verifier, context: Context) -> None:
    session = _session(verifier, context, "installed-surface")
    version_run = session.cli("--version", name="the installed CLI answers --version")
    expected = f"{PROJECT} {context.version}"
    verifier.check(
        "the installed CLI reports the artifact version",
        version_run.stdout.strip() == expected,
        f"expected {expected!r}, got {version_run.stdout.strip()!r}",
    )
    provenance = session.probe(
        _PROVENANCE_PROBE,
        name="the installed interpreter imports the codex extra and package metadata",
    )
    payload = _parse_json_stdout(verifier, provenance, "installed interpreter probe")
    path_fields = ("interpreter", "prefix", "base_prefix", "package_file", "dist_location", "zstandard_file")
    for field in path_fields:
        value = payload.get(field)
        verifier.check(
            f"the interpreter probe reports an absolute {field} path",
            isinstance(value, str) and bool(value.strip()) and "\0" not in value and os.path.isabs(value),
            repr(value),
        )
    paths = {field: Path(payload[field]) for field in path_fields}
    tool_root = context.tool_dir / PROJECT
    try:
        prefix = paths["prefix"].resolve()
        base_prefix = paths["base_prefix"].resolve()
        expected_prefix = tool_root.resolve()
    except (OSError, RuntimeError) as exc:
        raise Failure(f"the interpreter environment paths could not be resolved: {exc}") from exc
    verifier.check(
        "the probe prefix is the exact installed tool environment",
        _normcase(prefix) == _normcase(expected_prefix),
        f"{prefix} vs {expected_prefix}",
    )
    verifier.check(
        "the installed interpreter runs in a virtual environment",
        _normcase(prefix) != _normcase(base_prefix),
        f"{prefix} vs {base_prefix}",
    )
    try:
        probe_dir = paths["interpreter"].parent.resolve()
        invoked_dir = context.installed_python.parent.resolve()
    except (OSError, RuntimeError) as exc:
        raise Failure(f"the interpreter paths could not be resolved: {exc}") from exc
    verifier.check(
        "the probe ran under the invoked tool interpreter",
        _normcase(probe_dir / paths["interpreter"].name)
        == _normcase(invoked_dir / context.installed_python.name),
        str(paths["interpreter"]),
    )
    for field, label in (
        ("package_file", "the imported package"),
        ("dist_location", "the installed distribution"),
        ("zstandard_file", "zstandard"),
    ):
        verifier.contained(f"{label} resolves inside the installed tool environment", paths[field], tool_root)
        verifier.contained(
            f"{label} resolves outside the checkout", paths[field], context.repo_root, expected=False
        )
    verifier.check(
        "the installed metadata reports the artifact version",
        payload.get("installed_version") == context.version,
        str(payload.get("installed_version")),
    )
    verifier.report["tools"]["zstandard"] = payload.get("zstandard_version", "")
    verifier.note("package file", str(paths["package_file"]))


def _jsonrpc_answers(verifier: Verifier, run: Run) -> dict[str, dict[str, Any]]:
    """Parse newline-framed JSON-RPC answers, keyed by request id."""
    responses: list[dict[str, Any]] = []
    malformed: list[str] = []
    for line in run.stdout.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            malformed.append(_tail(line, 200))
            continue
        if isinstance(parsed, dict):
            responses.append(parsed)
        else:
            malformed.append(_tail(line, 200))
    verifier.check(
        "every stdout line is one newline-framed JSON-RPC message",
        not malformed,
        f"unparsed lines: {malformed}",
    )
    verifier.check(
        "initialize and tools/list were both answered",
        len(responses) == 2,
        f"responses: {[item.get('id') for item in responses]}",
    )
    return {str(item.get("id")): item for item in responses}


def proof_mcp_handshake(verifier: Verifier, context: Context) -> None:
    session = _session(verifier, context, "mcp")
    session.cli("init", name="init succeeds in the isolated home")
    port = _spare_port()
    verifier.check("the MCP baseurl uses a spare port", port not in FORBIDDEN_PORTS, f"port {port}")
    handshake = verifier.run(
        [session.shim, "mcp", "--baseurl", f"http://127.0.0.1:{port}"],
        cwd=session.cwd,
        env=session.env,
        label="MCP stdio handshake",
        name="the MCP gateway exits cleanly on stdin EOF",
        timeout=MCP_TIMEOUT_S,
        stdin_text=MCP_REQUESTS,
    )
    answers = _jsonrpc_answers(verifier, handshake)
    initialize = answers.get("1", {})
    tools_list = answers.get("2", {})
    result = initialize.get("result")
    verifier.check("initialize returned a result", isinstance(result, dict), _json_text(initialize))
    init_result: dict[str, Any] = result if isinstance(result, dict) else {}
    protocol = init_result.get("protocolVersion")
    verifier.check(
        "initialize reports the MCP protocol version",
        isinstance(protocol, str) and bool(protocol),
        _json_text(init_result, 300),
    )
    server_info = init_result.get("serverInfo") or {}
    verifier.check(
        "initialize names the gateway and the artifact version",
        server_info.get("name") == PROJECT and server_info.get("version") == context.version,
        _json_text(server_info),
    )
    capabilities = init_result.get("capabilities") or {}
    verifier.check(
        "initialize advertises the tools capability",
        isinstance(capabilities.get("tools"), dict),
        _json_text(capabilities),
    )
    list_result = tools_list.get("result")
    listed = list_result.get("tools") if isinstance(list_result, dict) else None
    tools = listed if isinstance(listed, list) else []
    verifier.check("tools/list returned a tool array", bool(tools), _json_text(list_result, 300))
    names = {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}
    missing = sorted(set(MCP_REQUIRED_TOOLS) - names)
    verifier.check("tools/list carries the memory and session tools", not missing, f"missing: {missing}")
    verifier.check(
        "every listed tool carries an input schema",
        all(isinstance(tool.get("inputSchema"), dict) for tool in tools if isinstance(tool, dict)),
        _json_text(sorted(names)),
    )
    verifier.note("no tools/call was issued; the handshake proves the stdio wire surface only")


def proof_config_defaults(verifier: Verifier, context: Context) -> None:
    session = _session(verifier, context, "config-defaults")
    session.cli("init", name="init succeeds in a fresh isolated home")
    config_file = session.home / ".mnemoseed-local" / "config.toml"
    verifier.check("init wrote the config file", config_file.is_file(), str(config_file))
    probe = session.probe(_CONFIG_PROBE, name="the installed loader reads the fresh config")
    payload = _parse_json_stdout(verifier, probe, "config defaults probe")
    verifier.check(
        "the loader resolved the init config path",
        _same_path(payload.get("config_path"), config_file),
        f"{payload.get('config_path')} vs {config_file}",
    )
    verifier.check(
        "the loaded package version matches the artifact",
        payload.get("version") == context.version,
        str(payload.get("version")),
    )
    verifier.flag("dream.auto_trigger defaults ON", payload, "dream_auto_trigger", True)
    verifier.flag("capture.auto_recall defaults ON", payload, "capture_auto_recall", True)
    verifier.flag("init keeps the isolated graph instance", payload, "isolated_graph", True)


def proof_config_explicit_false(verifier: Verifier, context: Context) -> None:
    session = _session(verifier, context, "config-explicit-false")
    config_dir = session.home / ".mnemoseed-local"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.toml"
    port = _spare_port()
    baseurl = f"http://127.0.0.1:{port}"
    fixture = _EXPLICIT_FALSE_CONFIG.format(baseurl=baseurl)
    verifier.check("the explicit-false fixture points at a spare loopback port", baseurl in fixture, baseurl)
    config_file.write_text(fixture, encoding="utf-8", newline="\n")
    before = config_file.read_bytes()
    init = session.cli(
        "init",
        name="init refuses to overwrite an existing config",
        exit_code=1,
    )
    verifier.contains(init, "config already exists", "init reports that the config already exists")
    after = config_file.read_bytes()
    verifier.check(
        "the explicit-false config bytes are unchanged",
        after == before,
        _preserved_bytes_detail("config.toml", before, after, changed="config.toml was rewritten"),
    )
    probe = session.probe(_CONFIG_PROBE, name="the installed loader reads the explicit-false config")
    payload = _parse_json_stdout(verifier, probe, "explicit-false config probe")
    verifier.flag("dream.auto_trigger stays explicitly false", payload, "dream_auto_trigger", False)
    verifier.flag("capture.auto_recall stays explicitly false", payload, "capture_auto_recall", False)
    verifier.check(
        "the explicit-false config keeps the spare-port baseurl",
        f":{port}" in str(payload.get("baseurl", "")),
        str(payload.get("baseurl")),
    )
    verifier.note("config bytes before the run", hashlib.sha256(before).hexdigest())


def _load_hooks_document(hooks_file: Path) -> dict[str, Any]:
    try:
        document = json.loads(hooks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Failure(f"{hooks_file}: hooks.json is not readable JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise Failure(f"{hooks_file}: hooks.json root is not an object")
    return document


def _our_hook_commands(document: object) -> dict[str, list[str]]:
    """Every marker handler command, keyed by event, out of the nested container."""
    if not isinstance(document, dict):
        raise Failure("hooks.json root is not a JSON object")
    root = document.get("hooks")
    if not isinstance(root, dict):
        raise Failure("hooks.json has no nested 'hooks' container")
    found: dict[str, list[str]] = {}
    for event, groups in root.items():
        if not isinstance(groups, list):
            raise Failure(f"hooks.json hooks.{event} is not a list")
        commands: list[str] = []
        for group in groups:
            if not isinstance(group, dict):
                raise Failure(f"hooks.json hooks.{event} holds a non-object group")
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                raise Failure(f"hooks.json hooks.{event} group has no handler list")
            for handler in handlers:
                if not isinstance(handler, dict):
                    raise Failure(f"hooks.json hooks.{event} holds a non-object handler")
                if handler.get("type") != "command":
                    continue
                command = handler.get("command")
                if isinstance(command, str) and HOOK_TAIL in command:
                    commands.append(command)
        if commands:
            found[str(event)] = commands
    return found


def _has_handler(group: object, handler: dict[str, Any]) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(item == handler for item in handlers)


def _host_shell_command(command: str) -> list[str]:
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")
        if shell is None:
            raise Failure("no PowerShell host shell found for the generated hook command")
        return [shell, "-NoProfile", "-NonInteractive", "-Command", f"{command}; exit $LASTEXITCODE"]
    return ["sh", "-c", f"{command}; exit $?"]


def _check_daemon_reachable(verifier: Verifier, run: Run, port: int, name: str) -> None:
    """Assert the sandboxed host sees the fake daemon on the spare port."""
    text = run.stdout
    verifier.check(name, "daemon: reachable" in text and f":{port}" in text, _tail(text, 500))


def _status_health_count(
    verifier: Verifier,
    session: Session,
    fake_daemon: _FakeHealthz,
    checkpoint: str,
    name: str,
) -> Run:
    """Run one host status check and record the health requests the fake daemon received."""
    before = fake_daemon.requests_received
    run = session.host("status", name=name)
    counts: dict[str, int] = verifier.report.setdefault("health_requests", {})
    counts[checkpoint] = fake_daemon.requests_received - before
    verifier.report["health_requests_note"] = "per-checkpoint delta, not cumulative"
    return run


def proof_codex_sandbox(verifier: Verifier, context: Context) -> None:
    session = _session(verifier, context, "codex")
    codex_home = session.home / "codex-home"
    mnemoseed_home = session.home / "mnemoseed-home"
    for path in (codex_home, mnemoseed_home):
        path.mkdir(parents=True, exist_ok=True)
    session.env.update({"CODEX_HOME": str(codex_home), "MNEMOSEED_LOCAL_HOME": str(mnemoseed_home)})
    mnemoseed_config = mnemoseed_home / "config.toml"
    codex_config = codex_home / "config.toml"
    hooks_file = codex_home / "hooks.json"
    fake_daemon = _FakeHealthz()
    try:
        port = fake_daemon.start()
        session.env["MNEMOSEED_LOCAL_BASEURL"] = f"http://127.0.0.1:{port}"
        verifier.check(
            "the fake daemon listens on a spare port",
            port not in FORBIDDEN_PORTS,
            f"port {port}",
        )
        mnemoseed_config.write_text(
            _SANDBOX_CONFIG.format(
                baseurl=f"http://127.0.0.1:{port}",
                isolated=str(session.home / "isolated.db").replace("\\", "/"),
            ),
            encoding="utf-8",
            newline="\n",
        )
        mnemoseed_before = mnemoseed_config.read_bytes()
        codex_config.write_text(_CODEX_FOREIGN_CONFIG, encoding="utf-8", newline="\n")
        codex_before = codex_config.read_bytes()
        hooks_file.write_text(json.dumps(_FOREIGN_HOOKS, indent=2) + "\n", encoding="utf-8", newline="\n")

        status_before = _status_health_count(
            verifier, session, fake_daemon, "before_install", "host status runs before install"
        )
        verifier.contains(status_before, "host: not-installed", "the sandbox starts not-installed")
        _check_daemon_reachable(verifier, status_before, port, "status reports the fake daemon as reachable")

        install = session.host("install", name="host install succeeds")
        verifier.check("host install wrote hooks.json", hooks_file.is_file(), str(hooks_file))
        verifier.contains(install, "via /hooks", "host install points at the one-time /hooks trust step")

        document = _load_hooks_document(hooks_file)
        commands = _our_hook_commands(document)
        verifier.check(
            "all four Codex events carry exactly one marker handler",
            sorted(commands) == sorted(HOOK_EVENTS) and all(len(items) == 1 for items in commands.values()),
            _json_text({event: len(items) for event, items in commands.items()}),
        )
        flat = [event for event in HOOK_EVENTS if isinstance(document.get(event), list)]
        verifier.check("events live in the nested hooks container only", not flat, f"flat event keys: {flat}")
        unique_commands = sorted({command for items in commands.values() for command in items})
        verifier.check(
            "every event runs the same canonical command",
            len(unique_commands) == 1,
            _json_text(unique_commands),
        )
        shim = _normcase(context.installed_cli.resolve())
        verifier.check(
            "the canonical command references the installed shim",
            all(shim in _normcase(command) for command in unique_commands),
            _json_text(unique_commands),
        )
        stop_groups = (document.get("hooks") or {}).get("Stop") or []
        foreign_handler = {"type": "command", "command": "third-party-noop"}
        foreign_groups = [group for group in stop_groups if _has_handler(group, foreign_handler)]
        verifier.check(
            "install keeps the foreign Stop handler value",
            bool(foreign_groups),
            _json_text(stop_groups, 1000),
        )
        verifier.check(
            "install keeps the foreign group metadata",
            all(isinstance(group, dict) and group.get("matcher") == "*" for group in foreign_groups),
            _json_text(foreign_groups, 1000),
        )
        verifier.check(
            "install keeps unrelated root fields",
            document.get("third_party") == _FOREIGN_HOOKS["third_party"],
            _json_text(document.get("third_party")),
        )

        codex_text = codex_config.read_text(encoding="utf-8")
        parsed_codex = tomllib.loads(codex_text)
        entry = (parsed_codex.get("mcp_servers") or {}).get("mnemoseed")
        verifier.check(
            "the Codex config carries the canonical MCP gateway entry",
            isinstance(entry, dict) and sorted(entry) == ["args", "command"] and entry.get("args") == ["mcp"],
            _json_text(entry),
        )
        verifier.check(
            "the MCP gateway entry points at the installed shim",
            _same_path((entry or {}).get("command"), context.installed_cli),
            str((entry or {}).get("command")),
        )
        verifier.check(
            "install writes no hook trust record",
            "trusted_hash" not in codex_text and "[hooks.state]" not in codex_text,
            _tail(codex_text, 800),
        )

        status_after = _status_health_count(
            verifier, session, fake_daemon, "after_install", "host status runs after install"
        )
        for needle in ("host: installed,", "unverified", "via /hooks"):
            verifier.contains(status_after, needle, f"status reports {needle}")
        _check_daemon_reachable(verifier, status_after, port, "status keeps the fake daemon reachability")

        executed = verifier.run(
            _host_shell_command(unique_commands[0]),
            cwd=session.cwd,
            env=session.env,
            label="generated hook command through the host shell",
            name="the generated hook command exits cleanly on an unsupported event",
            timeout=HOOK_TIMEOUT_S,
            stdin_text=HOOK_PAYLOAD,
        )
        verifier.contains(
            executed,
            "Traceback",
            "the generated hook command does not crash",
            stream="stderr",
            present=False,
        )
        verifier.check(
            "the generated hook command keeps host stdout clean",
            executed.stdout.strip() == "",
            _tail(executed.stdout, 800),
        )

        session.host("uninstall", name="host uninstall succeeds")
        verifier.check(
            "uninstall restores the foreign Codex config bytes",
            codex_config.read_bytes() == codex_before,
            _tail(codex_config.read_text(encoding="utf-8"), 800),
        )
        mnemoseed_after = mnemoseed_config.read_bytes()
        verifier.check(
            "the host adapter never touched the mnemoseed config home",
            mnemoseed_after == mnemoseed_before,
            _preserved_bytes_detail(
                "mnemoseed config.toml", mnemoseed_before, mnemoseed_after, changed="config.toml changed"
            ),
        )
        hooks_text = hooks_file.read_text(encoding="utf-8") if hooks_file.is_file() else ""
        verifier.check(
            "uninstall removes every marker handler",
            HOOK_TAIL not in hooks_text,
            _tail(hooks_text, 800),
        )
        verifier.check(
            "uninstall keeps the foreign hook",
            "third-party-noop" in hooks_text,
            _tail(hooks_text, 800),
        )
        status_final = _status_health_count(
            verifier, session, fake_daemon, "after_uninstall", "host status runs after uninstall"
        )
        verifier.contains(status_final, "host: not-installed", "the sandbox returns to not-installed")
        _check_daemon_reachable(
            verifier,
            status_final,
            port,
            "status keeps the fake daemon reachability after uninstall",
        )
        verifier.note("hook payload", HOOK_PAYLOAD.strip())
        verifier.note("hook command", unique_commands[0])
    finally:
        fake_daemon.stop()


PROOFS: tuple[tuple[str, Callable[[Verifier, Context], None]], ...] = (
    ("install-artifact", install_artifact),
    ("installed-cli-and-codex-extra", proof_installed_surface),
    ("mcp-handshake", proof_mcp_handshake),
    ("config-defaults", proof_config_defaults),
    ("config-explicit-false", proof_config_explicit_false),
    ("codex-hook-sandbox", proof_codex_sandbox),
)


def _run_all(verifier: Verifier, repo_root: Path, work_root: Path) -> None:
    with verifier.step("build-and-validate"):
        context = prepare(verifier, repo_root, work_root)
        validate_artifact_metadata(verifier, context)
    for name, proof in PROOFS:
        with verifier.step(name):
            proof(verifier, context)


def _print_summary(verifier: Verifier, exit_code: int) -> None:
    for step in verifier.steps:
        print(f"[{step['status']:>6}] {step['step']} ({step.get('duration_s', 0)}s)")
    artifact = verifier.report.get("artifact", {})
    wheel = artifact.get("wheel") or {}
    print(f"artifact: {wheel.get('name', '?')} sha256={wheel.get('sha256', '?')}")
    print(f"status: {'PASS' if exit_code == 0 else 'FAIL'}")
    print(f"report: {verifier.report_path}")
    print("limits: no model inference, no host trust (unknown), no end-to-end installer run")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the built release wheel on a clean runner")
    parser.add_argument(
        "--report",
        default="release-proof/verify-release-install.json",
        help="path of the structured proof report (default: release-proof/verify-release-install.json)",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="reuse this work dir instead of a fresh temp dir; an explicit dir is left in place",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="keep the auto-created temp work dir (debugging aid)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    work_root = (
        Path(args.workdir).resolve()
        if args.workdir
        else Path(tempfile.mkdtemp(prefix="mnemoseed-release-verify-"))
    )
    work_root.mkdir(parents=True, exist_ok=True)
    verifier = Verifier(report_path)
    verifier.report["repo_root"] = str(repo_root)
    verifier.report["work_dir"] = str(work_root)
    verifier.flush()
    status = "fail"
    exit_code = 1
    try:
        _run_all(verifier, repo_root, work_root)
        status = "pass"
        exit_code = 0
    except Failure as exc:
        verifier.report["failures"].append(str(exc))
        print(f"release install verification FAILED: {exc}", file=sys.stderr)
    except Exception as exc:
        verifier.report["failures"].append(f"unexpected error: {exc!r}")
        print(f"release install verification FAILED: unexpected error: {exc!r}", file=sys.stderr)
    finally:
        removed = False
        if args.workdir is None and not args.keep_workdir:
            shutil.rmtree(work_root, ignore_errors=True)
            removed = not work_root.exists()
        verifier.report["status"] = status
        verifier.report["finished_at"] = _utc_now()
        verifier.report["work_dir_removed"] = removed
        verifier.flush()
        _print_summary(verifier, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
