"""Issue #206 Eval 4A: hostile local provider boundary probes."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from mnemoseed_local.llm.drivers.openai_compatible import OpenAICompatibleLLM
from mnemoseed_local.llm.types import LLMUnavailable


class _FaultServer(ThreadingHTTPServer):
    faults: list[str]
    active_requests: int


class _FaultHandler(BaseHTTPRequestHandler):
    server: _FaultServer

    def do_POST(self) -> None:  # noqa: N802
        self.server.active_requests += 1
        try:
            fault = self.server.faults.pop(0)
            if fault == "delay":
                time.sleep(0.12)
            elif fault == "hang":
                time.sleep(0.12)
            elif fault == "disconnect":
                self.connection.close()
                return
            elif fault == "garbage":
                self._write(b"not-json", "text/plain")
                return
            body = json.dumps({"choices": [{"message": {"content": "[]"}}]}).encode()
            self._write(body, "application/json")
        finally:
            self.server.active_requests -= 1

    def _write(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture()
def fault_endpoint() -> Iterator[tuple[str, _FaultServer]]:
    faults = [fault for fault in ("delay", "hang", "disconnect", "garbage") for _ in range(5)]
    endpoint = _FaultServer(("127.0.0.1", 0), _FaultHandler)
    endpoint.faults = faults
    endpoint.active_requests = 0
    thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{endpoint.server_port}", endpoint
    finally:
        endpoint.shutdown()
        endpoint.server_close()
        thread.join(timeout=1)


def test_all_scripted_faults_have_a_bounded_degraded_seat_call(
    fault_endpoint: tuple[str, _FaultServer],
) -> None:
    base_url, endpoint = fault_endpoint
    driver = OpenAICompatibleLLM(base_url=base_url, api_key="test", model="eval", timeout=0.03)
    caller_threads: list[threading.Thread] = []

    def call() -> None:
        with pytest.raises(LLMUnavailable):
            driver.chat(system="seat", user="delta")

    for _ in range(20):
        thread = threading.Thread(target=call)
        caller_threads.append(thread)
        started = time.monotonic()
        thread.start()
        thread.join(timeout=0.08)
        elapsed = time.monotonic() - started
        assert not thread.is_alive(), "seat timeout left a caller thread running"
        assert elapsed < 0.08, "seat call exceeded its client deadline"

    assert endpoint.faults == []
    deadline = time.monotonic() + 0.5
    while endpoint.active_requests and time.monotonic() < deadline:
        time.sleep(0.005)
    assert endpoint.active_requests == 0
    assert not any(thread.is_alive() for thread in caller_threads)
