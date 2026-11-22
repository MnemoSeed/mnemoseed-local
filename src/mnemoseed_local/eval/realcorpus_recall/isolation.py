"""Spare-port daemon lifecycle for the isolated evaluation home."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from mnemoseed_local.eval.realcorpus_recall.manifest import InvalidRun

RESERVED_PORTS = (7788, 4096)
LOOPBACK_HOST = "127.0.0.1"


def assert_port_is_spare(port: int) -> None:
    if port in RESERVED_PORTS:
        raise InvalidRun(f"reserved port {port}")
    if not 1024 < port < 65536:
        raise InvalidRun(f"port out of range: {port}")


def allocate_spare_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        port = int(probe.getsockname()[1])
    assert_port_is_spare(port)
    return port


def daemon_command(port: int, *, host: str = LOOPBACK_HOST) -> list[str]:
    assert_port_is_spare(port)
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "mnemoseed_local.daemon.app:app",
        "--host",
        host,
        "--port",
        str(port),
    ]


def daemon_env(home: Path, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["MNEMOSEED_LOCAL_HOME"] = str(home)
    return env


@dataclass
class DaemonHandle:
    home: Path
    port: int
    base_url: str
    process: subprocess.Popen[bytes]


def _wait_ready(base_url: str, process: subprocess.Popen[bytes], timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise InvalidRun("daemon exited before readiness")
        try:
            if httpx.get(f"{base_url}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise InvalidRun("daemon readiness timeout")


def launch_daemon(home: Path, port: int) -> DaemonHandle:
    assert_port_is_spare(port)
    base_url = f"http://{LOOPBACK_HOST}:{port}"
    process = subprocess.Popen(
        daemon_command(port),
        env=daemon_env(home),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handle = DaemonHandle(home=home, port=port, base_url=base_url, process=process)
    try:
        _wait_ready(base_url, process)
    except InvalidRun:
        stop_daemon(handle)
        raise
    return handle


def stop_daemon(handle: DaemonHandle) -> None:
    if handle.process.poll() is None:
        handle.process.terminate()
        try:
            handle.process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            handle.process.wait(timeout=10.0)


def pin_home_in_config(home: Path, *, live_home: Path | None = None) -> None:
    live = live_home if live_home is not None else Path.home() / ".mnemoseed-local"
    config = home / "config.toml"
    if not config.exists():
        return
    home_value = str(home).replace("\\", "/")
    live_value = str(live).replace("\\", "/")
    text = config.read_text(encoding="utf-8")
    for source in ("~/.mnemoseed-local", live_value, str(live)):
        text = text.replace(source, home_value)
    config.write_text(text, encoding="utf-8")
    remaining = config.read_text(encoding="utf-8")
    if any(source in remaining for source in ("~/.mnemoseed-local", live_value, str(live))):
        raise InvalidRun("live home reference remains in evaluation config")


def resolve_home_from_env(env: dict[str, str]) -> Path:
    value = env.get("MNEMOSEED_LOCAL_HOME", "")
    if not value.strip():
        raise InvalidRun("MNEMOSEED_LOCAL_HOME is not pinned")
    home = Path(value)
    if home == Path.home() / ".mnemoseed-local":
        raise InvalidRun("live home is not an evaluation home")
    return home


@contextmanager
def daemon_context(home: Path) -> Iterator[DaemonHandle]:
    port = allocate_spare_port()
    handle = launch_daemon(home, port)
    try:
        yield handle
    finally:
        stop_daemon(handle)


def port_of_base_url(base_url: str) -> int:
    port = urlparse(base_url).port
    if port is None:
        raise InvalidRun("base url has no port")
    assert_port_is_spare(port)
    return port
