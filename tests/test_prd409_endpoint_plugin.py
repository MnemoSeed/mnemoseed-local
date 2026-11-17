"""The TypeScript hook routes through endpoint verification before its bearer."""

from __future__ import annotations

from pathlib import Path

import pytest
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


def test_plugin_reverifies_each_turn_and_stops_after_instance_rotation(tmp_path: Path) -> None:
    bundle = ts._bundle(tmp_path)
    transcript = ts._run(bundle, "endpoint-rotated")

    health = [p for p in transcript["posts"] if p["url"].endswith("/healthz")]
    ingests = [p for p in transcript["posts"] if p["url"].endswith("/ingest")]
    assert len(health) == 2, "each bearer-carrying turn must re-check the instance, not reuse a cached URL"
    assert all(_is_bearer(p["headers"]) is False for p in health), "no credential belongs on /healthz"
    assert len(ingests) == 1, "a rotated instance must receive no bearer-carrying ingest"
    assert _is_bearer(ingests[0]["headers"]) is True


@pytest.mark.parametrize("scenario", ["endpoint-record-missing", "endpoint-record-malformed"])
def test_plugin_fails_closed_without_a_valid_record(tmp_path: Path, scenario: str) -> None:
    bundle = ts._bundle(tmp_path)
    transcript = ts._run(bundle, scenario)

    ingests = [p for p in transcript["posts"] if p["url"].endswith("/ingest")]
    assert ingests == [], "no ingest may carry a credential without a verified endpoint record"
