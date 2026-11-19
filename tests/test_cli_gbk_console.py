"""UTF-8-safe CLI rendering on a console code page that cannot encode stored
text, and UTF-8 decoding of daemon responses on any locale."""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

from mnemoseed_local.cli import main
from mnemoseed_local.rest_client import DaemonClient, DaemonUnavailableError

_STORED = "现在吗 \u00b2"


class _GbkConsole(io.TextIOBase):
    """Text stream that encodes like cp936 until its entry reconfigure runs."""

    def __init__(self) -> None:
        self._encoding = "gbk"
        self._errors = "strict"
        self._bytes = io.BytesIO()
        self.reconfigured: list[tuple[str | None, str | None]] = []

    @property
    def encoding(self) -> str:
        return self._encoding

    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None) -> None:
        self.reconfigured.append((encoding, errors))
        if encoding is not None:
            self._encoding = encoding
        if errors is not None:
            self._errors = errors

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._bytes.write(text.encode(self._encoding, self._errors))
        return len(text)

    def output(self) -> str:
        return self._bytes.getvalue().decode("utf-8")


class _StubClient:
    profile_id = "default"

    def __init__(self, body: dict[str, Any], *, error: Exception | None = None) -> None:
        self._body = body
        self._error = error

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return self._body


class _MojibakeResponse:
    status_code = 200
    content = json.dumps({"text": _STORED}, ensure_ascii=False).encode("utf-8")

    def json(self) -> dict[str, Any]:
        return {"text": self.content.decode("gbk", errors="replace")}


def _install_gbk_console(monkeypatch: pytest.MonkeyPatch) -> _GbkConsole:
    console = _GbkConsole()
    monkeypatch.setattr(sys, "stdout", console)
    return console


@pytest.mark.parametrize(
    ("argv", "body", "expected"),
    [
        (
            ["recall", "hook-less 表面 方案"],
            {
                "memory": {
                    "entries": [{"kind": "fact", "score": 0.9, "text": _STORED, "flags": []}],
                    "coverage": {},
                }
            },
            _STORED,
        ),
        (["remember", _STORED], {"outcome": _STORED, "chunk_id": "c1"}, _STORED),
        (["dream", "once"], {"launched": True, "reason": _STORED, "state": "idle"}, _STORED),
        (
            ["forget", _STORED, "--kind", "entity"],
            {"removed": {"chunks": ["a"], "nodes": ["b"]}},
            "forgotten:",
        ),
    ],
)
def test_stored_text_renders_on_a_gbk_console(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    body: dict[str, Any],
    expected: str,
) -> None:
    gbk_console = _install_gbk_console(monkeypatch)
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: _StubClient(body))

    assert main(argv) == 0
    assert gbk_console.reconfigured == [("utf-8", "replace")]
    assert expected in gbk_console.output()


def test_client_error_renders_on_a_gbk_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    console = _GbkConsole()
    monkeypatch.setattr(sys, "stderr", console)
    error = DaemonUnavailableError(f"cannot reach {_STORED}")
    monkeypatch.setattr(
        "mnemoseed_local.rest_client.resolve_client", lambda args: _StubClient({}, error=error)
    )

    assert main(["recall", "x"]) == 1
    assert console.reconfigured == [("utf-8", "replace")]
    assert _STORED in console.output()


def test_daemon_response_is_decoded_as_utf8_on_a_gbk_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mnemoseed_local.rest_client.httpx.post", lambda *a, **k: _MojibakeResponse())

    body = DaemonClient(base_url="http://127.0.0.1:1").post("/memory/recall", {})

    assert body == {"text": _STORED}


def test_redirected_json_stays_parseable(monkeypatch: pytest.MonkeyPatch) -> None:
    gbk_console = _install_gbk_console(monkeypatch)
    client = DaemonClient(base_url="http://127.0.0.1:1")
    monkeypatch.setattr("mnemoseed_local.rest_client.resolve_client", lambda args: client)
    monkeypatch.setattr("mnemoseed_local.rest_client.httpx.post", lambda *a, **k: _MojibakeResponse())

    assert main(["recall", "现在吗", "--json"]) == 0
    assert json.loads(gbk_console.output()) == {"text": _STORED}
