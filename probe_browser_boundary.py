"""The Playwright proof for the console's same-origin credential grant.

The design review marked the browser semantics unverified: the cookie grant, the
cross-site refusal and the DNS-rebinding refusal are reasoned from the Fetch and
Cookie specifications, not observed. This module observes them. One browser, one
isolated port, one isolated config home, three probes:

1. a same-origin document load from the loopback address receives the cookie and
   the console's own fetch then succeeds;
2. a cross-origin page (a second loopback origin) fetching the daemon is refused;
3. a DNS-rebinding page (a hostname that resolves to loopback but is not one)
   receives no cookie and is refused.

Nothing here touches the installed runtime: the daemon runs on a spare port with
a temporary ``MNEMOSEED_LOCAL_HOME``.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A public hostname that resolves to loopback: the DNS-rebinding shape.
REBOUND_HOST = "localtest.me"

#: The console's own fetch lane: the first daemon call the page makes.
CONSOLE_ENDPOINT = "/session/recent"


@contextmanager
def _boundary_daemon(home: Path) -> Iterator[dict[str, Any]]:
    """A real daemon on a spare port with its own config home."""
    import subprocess
    import sys
    import sysconfig
    import time

    from mnemoseed_local.credential import read_credential_at
    from mnemoseed_local.rest_client import DaemonClient

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        f'baseurl = "http://127.0.0.1:{port}"\n'
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        '[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    env = {
        **__import__("os").environ,
        "MNEMOSEED_LOCAL_HOME": str(home),
        "PYTHONPATH": __import__("os").pathsep.join(
            [str(REPO_ROOT / "src"), sysconfig.get_paths()["purelib"]]
        ),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "mnemoseed_local.cli", "up", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(REPO_ROOT),
    )
    client = DaemonClient(base_url=f"http://127.0.0.1:{port}", timeout=1.0)
    try:
        deadline = time.monotonic() + 60.0
        while True:
            if proc.poll() is not None:
                raise AssertionError("the boundary daemon exited before readiness")
            try:
                client.get("/healthz")
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise AssertionError("the boundary daemon never answered /healthz") from None
                time.sleep(0.1)
        yield {
            "port": port,
            "base_url": f"http://127.0.0.1:{port}",
            "credential": read_credential_at(home),
        }
    finally:
        try:
            DaemonClient(
                base_url=f"http://127.0.0.1:{port}",
                actor="test",
                token=read_credential_at(home),
            ).post("/daemon/shutdown")
        except Exception:  # pragma: no cover - best-effort teardown
            pass
        try:
            proc.wait(timeout=30.0)
        except Exception:  # pragma: no cover - the sweeper would take it from here
            pass


def _chromium_executable() -> str | None:
    """The installed Chromium build, whatever revision the machine carries."""
    installed = sorted(
        (Path.home() / "AppData/Local/ms-playwright").glob("chromium-*/chrome-win*/chrome.exe")
    )
    return str(installed[-1]) if installed else None


def _browser() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _attacker_page_server() -> Any:
    """A real page on a DIFFERENT origin, the way any website would be."""
    import http.server
    import socketserver
    import threading
    from contextlib import contextmanager

    @contextmanager
    def _serve() -> Iterator[str]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = b"<html><body>attacker page</body></html>"
                self.send_response(200)
                self.send_header("content-type", "text/html")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
                return

        server = socketserver.TCPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.shutdown()
            server.server_close()

    return _serve()


def main() -> int:
    import tempfile

    report: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="mnemoseed-boundary-") as raw:
        home = Path(raw) / "home"
        with _boundary_daemon(home) as daemon:
            base_url = daemon["base_url"]
            with _browser() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=_chromium_executable(), args=["--no-sandbox"]
                )

                # 1. same-origin: the document load is granted the cookie, and
                #    the console's own fetch then reaches the daemon.
                context = browser.new_context(base_url=base_url)
                page = context.new_page()
                page.goto(f"{base_url}/", wait_until="load")
                cookies = context.cookies()
                granted = [cookie for cookie in cookies if cookie["name"] == "mnemoseed_daemon_credential"]
                report["same_origin_granted"] = bool(granted)
                report["same_origin_cookie_value_matches"] = bool(
                    granted and granted[0]["value"] == daemon["credential"]
                )
                report["same_origin_http_only"] = bool(granted and granted[0]["httpOnly"])
                report["same_origin_same_site"] = granted[0]["sameSite"] if granted else None
                result = page.evaluate(
                    """async (endpoint) => {
                        const response = await fetch(endpoint, {
                            method: 'POST',
                            headers: {'content-type': 'application/json'},
                            body: JSON.stringify({
                                profile_id: 'default',
                                sessions: 2,
                                per_session: 2,
                            }),
                        });
                        return {status: response.status, body: await response.text()};
                    }""",
                    CONSOLE_ENDPOINT,
                )
                report["same_origin_fetch_status"] = result["status"]
                context.close()

                # 2. cross-origin: a REAL page served by a different loopback
                #    origin, exactly the "any website can fetch loopback" case.
                with _attacker_page_server() as attacker_origin:
                    report["attacker_origin"] = attacker_origin
                    cross = browser.new_context(base_url=attacker_origin)
                    cross_page = cross.new_page()
                    cross_page.goto(f"{attacker_origin}/", wait_until="load")
                    refused = cross_page.evaluate(
                        """async (target) => {
                            try {
                                const response = await fetch(target + '/session/recent', {
                                    method: 'POST',
                                    headers: {'content-type': 'application/json'},
                                    body: JSON.stringify({
                                        profile_id: 'default',
                                        sessions: 2,
                                        per_session: 2,
                                    }),
                                });
                                const body = await response.text();
                                return {status: response.status, ok: response.ok, body};
                            } catch (error) {
                                return {status: null, ok: false, error: String(error)};
                            }
                        }""",
                        base_url,
                    )
                    report["cross_origin_fetch"] = refused
                    cross.close()

                # 3. DNS rebinding: a non-loopback hostname that resolves to
                #    loopback. The page never receives the cookie and its fetch
                #    is refused, so nothing it reads can reach memory data.
                import socket as _socket

                report["rebound_host"] = REBOUND_HOST
                report["rebound_resolves_to_loopback"] = _socket.gethostbyname(REBOUND_HOST) == "127.0.0.1"
                rebound = browser.new_context(base_url=f"http://{REBOUND_HOST}:{daemon['port']}")
                rebound_page = rebound.new_page()
                rebound_cookies = rebound.cookies(f"http://{REBOUND_HOST}:{daemon['port']}")
                report["rebound_granted"] = bool(
                    [cookie for cookie in rebound_cookies if cookie["name"] == "mnemoseed_daemon_credential"]
                )
                rebound_result = rebound_page.evaluate(
                    """async (target) => {
                        try {
                            const response = await fetch(target + '/session/recent', {
                                method: 'POST',
                                headers: {'content-type': 'application/json'},
                                body: '{}',
                            });
                            return {status: response.status, ok: response.ok};
                        } catch (error) {
                            return {status: null, ok: false, error: String(error)};
                        }
                    }""",
                    f"http://localtest.me:{daemon['port']}",
                )
                # A same-origin document load over the rebound host must not even
                # be granted the cookie.
                rebound_page.goto(f"http://{REBOUND_HOST}:{daemon['port']}/", wait_until="load")
                report["rebound_fetch"] = rebound_result
                report["rebound_cookie_after_document_load"] = bool(
                    [
                        cookie
                        for cookie in rebound.cookies(f"http://{REBOUND_HOST}:{daemon['port']}")
                        if cookie["name"] == "mnemoseed_daemon_credential"
                    ]
                )
                rebound.close()
                browser.close()

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
