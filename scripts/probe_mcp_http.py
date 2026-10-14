"""Acceptance probe: a real OpenCode client onto the daemon's HTTP MCP surface.

Stands an isolated daemon up on a spare loopback port under a temporary config
home (never the installed runtime, never a reserved port), points a real
OpenCode run at the remote MCP entry the first-party plugin injects, and
proves the whole loop: the client connects, lists the gateway tools, calls
one, and the stored fact is readable back through the daemon REST surface.

This is a manual probe whose transcript is review evidence, not a CI gate.

    uv run python scripts/probe_mcp_http.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_REPO_ROOT / "src")
_LOOPBACK = "127.0.0.1"
_READY_TIMEOUT_S = 60.0
_OPENCODE_TIMEOUT_S = 300.0
_PROBE_FACT = "http mcp acceptance probe fact"

#: The model the probe run uses (overridable through the environment); the
#: probe is about the MCP loop, not about a specific provider.
_PROBE_MODEL = os.environ.get("MNEMOSEED_LOCAL_PROBE_MODEL", "opencode/step-5-preview-free")

_PROMPT = (
    "Use the MCP tools from the connected 'mnemoseed' server only. "
    "First call its remember tool with the exact text "
    f"'{_PROBE_FACT}'. Then call its session_windows tool with no arguments. "
    "Finish with one short line naming both tools you called."
)


def _unused_loopback_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_LOOPBACK, 0))
        return int(probe.getsockname()[1])


def _embedded_config_text(workdir: Path, port: int) -> str:
    return (
        f'baseurl = "http://{_LOOPBACK}:{port}"\n'
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(workdir / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(workdir / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(workdir / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(workdir / "meta.db").as_posix()}"\n'
        '[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
    )


def _await_ready(base_url: str) -> None:
    import httpx

    deadline = time.monotonic() + _READY_TIMEOUT_S
    while True:
        try:
            if httpx.get(f"{base_url}/healthz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            raise AssertionError(f"the probe daemon never answered /healthz on {base_url}")
        time.sleep(0.2)


def _opencode_config(oc_home: Path, credential: str, profile_id: str, url: str) -> Path:
    """The host home an OpenCode run reads: a config of its own, the model
    setup and provider credential copied from the host's, and the first-party
    remote MCP entry the plugin's config hook writes (same entry bytes).

    A home of its own keeps the run off the host's servers, plugins and
    sessions entirely: nothing outside the probe's temporary directory is
    read for configuration and nothing is written to it.
    """
    from mnemoseed_local.rest_client import ACTOR_HEADER

    config_dir = oc_home / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {"$schema": "https://opencode.ai/config.json", "share": "disabled"}
    host_config = _host_model_setup()
    for key in ("model", "small_model", "providers"):
        if key in host_config:
            config[key] = host_config[key]
    config["mcp"] = {
        "mnemoseed": {
            "type": "remote",
            "url": url,
            "headers": {
                "Authorization": f"Bearer {credential}",
                "X-MnemoSeed-Profile": profile_id,
                ACTOR_HEADER: "mcp",
            },
            "oauth": False,
            "enabled": True,
            "timeout": 30000,
        }
    }
    path = config_dir / "opencode.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def _host_model_setup() -> dict[str, Any]:
    """The host's own model and provider declarations, so the probe's model
    call authenticates and resolves exactly like the host's runs do. A
    missing or unreadable host config leaves the probe's model line bare."""
    try:
        host_config = Path.home() / ".config" / "opencode" / "opencode.json"
        data = json.loads(host_config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stage_provider_credential(oc_home: Path) -> bool:
    """Copy the host's provider credential into the probe home's data dir.

    The run is real, so it authenticates like any other run on this machine;
    the copy lives and dies with the probe's temporary home.
    """
    source = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if not source.is_file():
        return False
    target = oc_home / ".local" / "share" / "opencode" / "auth.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return True


def _run_opencode(oc_home: Path, workdir: Path) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ}
    # A home of its own relocates every opencode path (config, data, state)
    # under the probe home, so the host's servers and plugins never load.
    environment.update(
        {
            "HOME": str(oc_home),
            "USERPROFILE": str(oc_home),
            "HOMEDRIVE": oc_home.drive,
            "HOMEPATH": str(oc_home)[len(oc_home.drive) :],
        }
    )
    for name in ("XDG_CONFIG_HOME", "OPENCODE_CONFIG"):
        environment.pop(name, None)
    argv = ["opencode", "run", "--standalone", "--auto", "--model", _PROBE_MODEL, _PROMPT]
    if os.name == "nt":
        # the npm shim is a .cmd: CreateProcess needs the shell to run it
        argv = ["cmd", "/c", *argv]
    return subprocess.run(
        argv,
        env=environment,
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_OPENCODE_TIMEOUT_S,
    )


def _shutdown(base_url: str, credential: str) -> Any:
    import httpx

    response = httpx.post(
        f"{base_url}/daemon/shutdown",
        headers={"Authorization": f"Bearer {credential}", "X-MnemoSeed-Actor": "cli"},
        timeout=30.0,
    )
    return response.json() if response.status_code == 200 else {"status": response.status_code}


def main() -> int:
    if shutil.which("opencode") is None or shutil.which("node") is None:
        print("probe unavailable: the opencode CLI is not on PATH")
        return 2
    from mnemoseed_local.credential import read_credential_at
    from mnemoseed_local.rest_client import DaemonClient

    workdir = Path(tempfile.mkdtemp(prefix="mnemoseed-mcp-probe-"))
    try:
        oc_home = workdir / "oc-home"
        if not _stage_provider_credential(oc_home):
            print("probe unavailable: no provider credential under the host's opencode data dir")
            return 2
        home = workdir / "home"
        home.mkdir()
        port = _unused_loopback_port()
        base_url = f"http://{_LOOPBACK}:{port}"
        (home / "config.toml").write_text(_embedded_config_text(workdir, port), encoding="utf-8")
        environment = {
            **os.environ,
            "MNEMOSEED_LOCAL_HOME": str(home),
            "PYTHONPATH": os.pathsep.join(
                filter(None, [_SRC, sysconfig.get_paths()["purelib"], os.environ.get("PYTHONPATH", "")])
            ),
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "mnemoseed_local.cli", "up", "--host", _LOOPBACK, "--port", str(port)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        credential = ""
        transcript: dict[str, Any] = {"base_url": base_url, "isolated_home": str(home)}
        try:
            _await_ready(base_url)
            credential = read_credential_at(home)
            config_path = _opencode_config(oc_home, credential, "default", f"{base_url}/mcp")
            run = _run_opencode(oc_home, workdir)
            # An explicit token, not client_for: client_for resolves the process's
            # own config home, and this probe reads the isolated daemon's home.
            client = DaemonClient(base_url=base_url, profile_id="default", actor="cli", token=credential)
            activity = client.get("/api/v1/observability")
            recalled = client.post("/memory/recall", {"profile_id": "default", "query": _PROBE_FACT})
            transcript.update(
                {
                    "opencode_config": str(config_path),
                    "opencode_exit_code": run.returncode,
                    "opencode_output_tail": (run.stdout or "").strip().splitlines()[-6:],
                    "authenticated_actor_counts": activity["authenticated_actor_counts"],
                    "recalled_fact": any(
                        _PROBE_FACT in json.dumps(entry, ensure_ascii=False)
                        for entry in recalled["memory"]["entries"]
                    ),
                }
            )
            transcript["status"] = "PASS" if (run.returncode == 0 and transcript["recalled_fact"]) else "FAIL"
        finally:
            transcript["shutdown"] = _shutdown(base_url, credential)
            try:
                proc.wait(timeout=30.0)
            except subprocess.TimeoutExpired:
                transcript["daemon_exit_code"] = None
        print(json.dumps(transcript, indent=2, ensure_ascii=False))
        return 0 if transcript.get("status") == "PASS" else 1
    finally:
        # The workdir holds the staged provider credential and the isolated
        # daemon's credential, so it leaves no trace once the transcript is out.
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
