"""The TypeScript hook routes through endpoint verification before its bearer."""

from __future__ import annotations

from pathlib import Path

import test_hook_ts_behavior as ts


def _is_bearer(headers: dict | None) -> bool:
    if not isinstance(headers, dict):
        return False
    return any(str(key).lower() == "authorization" for key in headers)


def test_plugin_sends_no_authorization_on_stale_instance(tmp_path: Path) -> None:
    bundle = ts._bundle(tmp_path)
    transcript = ts._run(bundle, "endpoint-stale")

    health = [p for p in transcript["posts"] if p["url"].endswith("/healthz")]
    ingests = [p for p in transcript["posts"] if p["url"].endswith("/ingest")]
    assert health and _is_bearer(health[0]["headers"]) is False
    assert ingests == [], "a bearer-carrying ingest was sent after stale verification"


def test_plugin_attaches_bearer_after_valid_instance(tmp_path: Path) -> None:
    bundle = ts._bundle(tmp_path)
    transcript = ts._run(bundle, "endpoint-valid")

    health = [p for p in transcript["posts"] if p["url"].endswith("/healthz")]
    ingests = [p for p in transcript["posts"] if p["url"].endswith("/ingest")]
    assert health and _is_bearer(health[0]["headers"]) is False
    assert ingests, "the valid handshake should allow the ingest POST"
    assert _is_bearer(ingests[0]["headers"]) is True
