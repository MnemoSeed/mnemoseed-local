"""`dream status` answers the owner's data-status question: pending pool vs
threshold, digested watermark, and dream history (committed runs + failed
extractions by class). Missing data renders honest zeros/nulls."""

from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.routing import APIRoute, APIRouter
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.dream import consumer, nominate
from mnemoseed_local.mcp_gateway.server import handle_message
from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import Provenance
from mnemoseed_local.storage.ports import (
    AuditEntry,
    DreamRun,
    GraphStore,
    ReconciliationApplication,
    StorageError,
    TurnRange,
    reconciliation_deferred_dedup_key,
)

PROFILE = "default"


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


def _client(config_path: Path) -> TestClient:
    return TestClient(create_app())


def _reconcile_node(node_id: str, *, profile_id: str, peer: str) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=profile_id,
        node_type=NodeType.PREFERENCE,
        props={
            "domain": "editor",
            "statement": f"statement-{node_id}",
            "valence": 0.4,
            "prior_width": 0.2,
            "trait_anchor": "stable-anchor",
            "evidence_chain": [{"chunk_id": f"chunk-{node_id}"}],
        },
        entities=["editor"],
        confidence=0.73,
        decay_weight=0.8,
        needs_reconcile=True,
        conflict_flag=True,
        read_conflict_id=peer,
        provenance=Provenance(
            asserted_by="source-model",
            session_id="source-session",
            source=f"chunk://{node_id}",
            confidence=0.61,
        ),
    )


def test_dream_status_reports_pool_watermark_and_history(config_path: Path) -> None:
    with _client(config_path) as client:
        stores = client.app.state.stores  # type: ignore[attr-defined]
        stores.meta.pool_credit(PROFILE, 2.05, TurnRange(start=3, end=3))
        finished = time.time() - 60.0
        stores.meta.record_dream_run(
            DreamRun(run_id="r1", started_at=finished - 30.0, finished_at=finished, tokens=100)
        )
        for cls in ("truncated_delta_deferred", "llm_unreachable", "llm_unreachable"):
            stores.meta.audit_append(
                AuditEntry(
                    actor="dream",
                    action="dream_extract_failed",
                    detail={"failure_class": cls},
                    at=time.time(),
                )
            )

        body = client.post("/memory/dream_status", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        payload = body.json()

        assert payload["pool"]["balance"] == pytest.approx(2.05)
        assert payload["pool"]["threshold"] > 0.0
        assert payload["watermark"] == {"start": 3, "end": 3}
        assert payload["history"]["committed_runs"] >= 1
        assert payload["history"]["last_commit_at"] is not None
        assert payload["history"]["extract_failures"] == {
            "truncated_delta_deferred": 1,
            "llm_unreachable": 2,
        }
        # legacy lines survive
        assert "state" in payload


def test_dream_status_reports_pending_and_lifetime_filed(config_path: Path) -> None:
    """The pool block splits the pending gauge from the lifetime filed total."""
    with _client(config_path) as client:
        stores = client.app.state.stores  # type: ignore[attr-defined]
        stores.meta.pool_credit(PROFILE, 2.05, TurnRange(start=3, end=3))

        body = client.post("/memory/dream_status", json={"profile_id": PROFILE})
        payload = body.json()
        assert payload["pool"]["balance"] == pytest.approx(2.05)
        assert payload["pool"]["pending"] == pytest.approx(payload["pool"]["balance"])
        assert payload["pool"]["lifetime_filed"] == 0.0

        stores.meta.pool_drain(PROFILE, TurnRange(start=3, end=3))
        body = client.post("/memory/dream_status", json={"profile_id": PROFILE})
        payload = body.json()
        assert payload["pool"]["pending"] == 0.0
        assert payload["pool"]["lifetime_filed"] == pytest.approx(2.05)


def test_dream_status_empty_profile_renders_honest_nulls(config_path: Path) -> None:
    with _client(config_path) as client:
        body = client.post("/memory/dream_status", json={"profile_id": "fresh-profile"})
        assert body.status_code == 200, body.text
        payload = body.json()

        assert payload["pool"]["balance"] == 0.0
        assert payload["watermark"] is None
        assert payload["history"]["committed_runs"] == 0
        assert payload["history"]["last_commit_at"] is None
        assert payload["history"]["extract_failures"] == {}


def test_cli_dream_status_prints_the_status_block(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "state": "idle",
        "pending_queue": 0,
        "pending_manual": 0,
        "pool": {"balance": 2.05, "threshold": 1.0},
        "watermark": {"start": 3, "end": 3},
        "history": {
            "committed_runs": 2,
            "last_commit_at": "2026-08-24T07:39:04Z",
            "extract_failures": {"truncated_delta_deferred": 1},
        },
    }

    class _StubClient:
        profile_id = PROFILE

        def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
            assert path == "/memory/dream_status"
            return payload

    from mnemoseed_local import cli, rest_client

    monkeypatch.setattr(rest_client, "resolve_client", lambda args: _StubClient())
    assert cli.main(["dream", "status"]) == 0
    out = capsys.readouterr().out
    assert "pool: 2.05 / 1.0 pts" in out
    assert "digested turns: 3..3" in out
    assert "dreams committed: 2" in out
    assert "last dream: 2026-08-24T07:39:04Z" in out
    assert "extraction failures: truncated_delta_deferred=1" in out


def test_dream_status_counts_exact_beyond_single_page(config_path: Path) -> None:
    with _client(config_path) as client:
        stores = client.app.state.stores  # type: ignore[attr-defined]
        for i in range(505):
            stores.meta.audit_append(
                AuditEntry(
                    actor="dream",
                    action="dream_extract_failed",
                    detail={"failure_class": "llm_unreachable" if i % 2 else "over_budget"},
                    at=time.time(),
                )
            )
        for i in range(205):
            stores.meta.record_dream_run(
                DreamRun(run_id=f"r{i}", started_at=float(i), finished_at=float(i) + 1.0, tokens=1)
            )

        body = client.post("/memory/dream_status", json={"profile_id": PROFILE})
        payload = body.json()
        assert payload["history"]["extract_failures"] == {
            "over_budget": 253,
            "llm_unreachable": 252,
        }
        assert payload["history"]["committed_runs"] == 205
        # newest run wins (list is started_at DESC; last finish = 204+1)
        assert payload["history"]["last_commit_at"] is not None


def _commit_reconciliation(graph: GraphStore, nomination_id: str, profile_id: str, disposition: str) -> None:
    left, right = f"{nomination_id}-a", f"{nomination_id}-b"
    for node_id, peer in ((left, right), (right, left)):
        graph.upsert_node(_reconcile_node(node_id, profile_id=profile_id, peer=peer))
    application = ReconciliationApplication(
        nomination_id=nomination_id,
        profile_id=profile_id,
        disposition=disposition,
        reason_code={
            "accepted": "conflict_confirmed",
            "rejected": "not_conflict_confirmed",
            "unresolved": "missing_endpoint",
        }[disposition],
        canonical_kind="read_conflict",
        composite_group_id=f"group-{nomination_id}",
        evidence_event_ids=(),
        verdict="conflict",
        quality="verified",
        left_node_id=left,
        left_version=1,
        left_expected_peer_id=right,
        right_node_id=right,
        right_version=1,
        right_expected_peer_id=left,
        winner_node_id=left if disposition == "accepted" else None,
        loser_node_id=right if disposition == "accepted" else None,
        loser_prior_version=None,
        loser_new_version=None,
        applied_at=100.0,
    )
    factor = 0.5 if disposition == "accepted" else None
    receipt = graph.apply_reconciliation(application, downweight_factor=factor)
    assert receipt.disposition == disposition
    assert graph.apply_reconciliation(application, downweight_factor=factor) == receipt


def test_dream_status_reports_reconcile_counts(config_path: Path) -> None:
    with _client(config_path) as client:
        stores = client.app.state.stores
        for nomination_id, run_id in (("accepted-0", "r1"), ("accepted-0", "r2"), ("pending", "r1")):
            entry = AuditEntry(
                actor="dream-engine",
                action="reconcile_deferred",
                detail={"profile_id": PROFILE, "nomination_id": nomination_id},
                at=time.time(),
                dedup_key=reconciliation_deferred_dedup_key(nomination_id, run_id, "reconcile_deferred"),
            )
            stores.meta.audit_append(entry)
            stores.meta.audit_append(entry)
        for index, changes in enumerate(
            (
                {"dedup_key": None},
                {"actor": "console-user"},
                {"action": "reconcile_accepted"},
                {"detail": {"nomination_id": "missing-profile"}},
                {"detail": {"profile_id": "DEFAULT"}},
                {"detail": {"profile_id": f" {PROFILE} "}},
            )
        ):
            distractor = replace(entry, dedup_key=f"distractor-{index}")
            stores.meta.audit_append(replace(distractor, **changes))
        stores.meta.audit_append(
            AuditEntry(
                actor="dream-engine",
                action="reconcile_deferred",
                detail={"profile_id": "other-profile", "nomination_id": "nom-foreign"},
                at=time.time(),
                dedup_key=reconciliation_deferred_dedup_key(
                    "nom-foreign", "run-foreign", "reconcile_deferred"
                ),
            )
        )
        stores.meta.audit_append(
            AuditEntry(
                actor="dream-engine",
                action="reconcile_deferred",
                detail={"nomination_id": "nom-unkeyed"},
                at=time.time(),
            )
        )
        stores.meta.audit_append(
            AuditEntry(
                actor="console-user",
                action="reconcile_deferred",
                detail={"profile_id": PROFILE, "nomination_id": "nom-actor"},
                at=time.time(),
            )
        )
        for disposition, count in (("accepted", 2), ("rejected", 1), ("unresolved", 4)):
            for i in range(count):
                _commit_reconciliation(stores.graph, f"{disposition}-{i}", PROFILE, disposition)
            _commit_reconciliation(stores.graph, f"foreign-{disposition}", "other-profile", disposition)
        for outbox in stores.graph.pending_reconciliation_audits(100):
            if outbox.nomination_id == "accepted-0":
                stores.graph.mark_reconciliation_audit_delivered(outbox.outbox_id, time.time())

        body = client.post("/memory/dream_status", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        payload = body.json()

        assert payload["reconcile"] == {
            "accepted": 2,
            "rejected": 1,
            "deferred": 3,
            "unresolved": 4,
        }

        body = client.post("/memory/dream_status", json={"profile_id": "fresh-profile"})
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["reconcile"] == {
            "accepted": 0,
            "rejected": 0,
            "deferred": 0,
            "unresolved": 0,
        }


@pytest.mark.parametrize(
    ("layer", "method"),
    [("graph", "count_reconciliation_receipts"), ("meta", "count_reconciliation_deferred")],
)
def test_dream_status_reconciliation_errors_are_visible(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, layer: str, method: str
) -> None:
    def fail(*, profile_id: str) -> None:
        raise StorageError(f"{profile_id}: reconciliation count unavailable")

    with _client(config_path) as client:
        stores = client.app.state.stores
        monkeypatch.setattr(getattr(stores, layer), method, fail)
        with pytest.raises(StorageError, match="reconciliation count unavailable"):
            client.post("/memory/dream_status", json={"profile_id": PROFILE})


_CONSUMER_REPAIR_SITES = (
    (consumer.ReconciliationConsumer, "process"),
    (consumer.ReconciliationConsumer, "repair"),
)

_NOMINATION_SITES = (
    (nominate, "after_commit"),
    (nominate, "materialize_nominations"),
    (nominate, "run_committed_nominations"),
)


def test_dream_status_counting_wrappers_perform_zero_writes_and_no_invocations(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative-control pin for the #198 status-compose counting seam.

    Both counting wrappers must answer with plain reads only: zero audit_log
    writes, zero outbox mutations, and zero consumer/repair/nominate-scanner
    invocations while dream_status composes. A mutant wiring any of those
    callables into the status path fails on the journal or the Mock call
    counts (the SIDE_EFFECT mutant class).
    """
    with _client(config_path) as client:
        stores = client.app.state.stores
        journal = [
            (stores.graph, stores.graph.append_version),
            (stores.graph, stores.graph.invalidate),
            (stores.graph, stores.graph.tombstone),
            (stores.graph, stores.graph.supersede_link),
            (stores.graph, stores.graph.apply_reconciliation),
            (stores.meta, stores.meta.audit_append),
            (stores.meta, stores.meta.set_config),
            (stores.meta, stores.meta.rollback_config),
            (stores.meta, stores.meta.pool_credit),
            (stores.meta, stores.meta.pool_drain),
            (stores.meta, stores.meta.advance_watermark),
        ]
        sentinels: dict[str, Mock] = {}
        for store, fn in journal:
            sentinel = Mock(wraps=fn)
            sentinels[fn.__name__] = sentinel
            monkeypatch.setattr(type(store), fn.__name__, sentinel)
        embed_sentinel = Mock(wraps=stores.embed.embed)
        monkeypatch.setattr(type(stores.embed), "embed", embed_sentinel)
        invoke_mocks: dict[tuple[Any, str], Mock] = {}
        with monkeypatch.context() as consumer_site:
            for site, name in (*_CONSUMER_REPAIR_SITES, *_NOMINATION_SITES):
                invoke_mocks[(id(site), name)] = Mock()
                consumer_site.setattr(site, name, invoke_mocks[(id(site), name)])

            ingest = client.post(
                "/ingest",
                json={
                    "host": "opencode",
                    "event": "user_prompt",
                    "session_id": "embedder-control",
                    "profile_id": PROFILE,
                    "ts": 1.0,
                    "content": {"text": "embedder control"},
                },
            )
            assert ingest.status_code == 202, ingest.text
            settled = client.post(
                "/session/end",
                json={"session_id": "embedder-control", "profile_id": PROFILE, "ts": 2.0},
            )
            assert settled.status_code == 200, settled.text
            assert embed_sentinel.call_count >= 1
            embed_sentinel.reset_mock()
            for sentinel in sentinels.values():
                sentinel.reset_mock()
            for sentinel in invoke_mocks.values():
                sentinel.reset_mock()
            client.post("/memory/dream_status", json={"profile_id": PROFILE})

        for _store, fn in journal:
            assert not sentinels[fn.__name__].call_count, f"{fn.__name__} must stay untouched"
        for site, name in (*_CONSUMER_REPAIR_SITES, *_NOMINATION_SITES):
            assert not invoke_mocks[(id(site), name)].called, f"{name} must never fire during status compose"
        assert not embed_sentinel.called, "embedder must stay untouched during status compose"


@pytest.mark.parametrize(
    ("tool_names", "expected_paths"),
    [
        (
            ["recall", "remember", "supersede", "dream_once", "recent_sessions", "session_windows"],
            [
                "/memory/recall",
                "/memory/remember",
                "/memory/supersede",
                "/memory/dream_once",
                "/memory/dream_status",
            ],
        ),
    ],
)
def test_external_surface_inventory_equals_frozen_baseline(
    tool_names: list[str], expected_paths: list[str]
) -> None:
    """#198 additive-absence pin: the external HTTP route table, the MCP tool
    name set and the console page routes stay at the frozen baseline — the
    reconcile counts ride existing surfaces only."""

    routes = create_app().routes
    http_paths: set[str] = set()
    for route in routes:
        if isinstance(route, APIRoute):
            http_paths.add(route.path)
        elif isinstance(route, APIRouter):
            http_paths.update(r.path for r in route.routes if isinstance(r, APIRoute))
        else:
            included = getattr(route, "include_context", None)
            original = getattr(included, "included_router", None) if included is not None else None
            if original is not None:
                http_paths.update(r.path for r in original.routes if isinstance(r, APIRoute))
    assert http_paths == {
        "/healthz",
        "/health",
        "/mcp/handshake",
        "/api/v1/observability",
        "/api/v1/audit",
        "/api/v1/profiles",
        "/api/v1/profiles/archive",
        "/api/v1/config",
        "/api/v1/config/set",
        "/api/v1/config/versions",
        "/api/v1/config/rollback",
        "/daemon/shutdown",
        "/ingest",
        "/session/end",
        "/flush",
        "/session/recall-pending",
        "/session/recent",
        "/session/windows",
        "/memory/atlas",
        "/memory/audit",
        "/memory/error_events",
        "/memory/export",
        "/memory/forget_this",
        "/memory/reinforce",
        "/memory/timeline",
        *expected_paths,
    }

    response = handle_message(_RecordingClient(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"] for tool in (response or {}).get("result", {}).get("tools", [])}
    assert tools == set(tool_names)
    console_js = Path(__file__).parents[1] / "src" / "mnemoseed_local" / "console" / "static" / "app.js"
    html = Path(__file__).parents[1] / "src" / "mnemoseed_local" / "console" / "static" / "index.html"
    js = console_js.read_text(encoding="utf-8")
    page_routes = set(re.findall(r"#/([a-z_]+)", js)) - {"memory"}
    page_routes.update(re.findall(r'data-route="([a-z_]+)"', html.read_text(encoding="utf-8")))
    assert page_routes == {"overview", "atlas", "config", "profiles", "dream"}


class _RecordingClient:
    profile_id = PROFILE

    def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        return {}


def test_cli_prints_none_for_zero_failures(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "state": "idle",
        "pending_queue": 0,
        "pending_manual": 0,
        "pool": {"balance": 0.0, "threshold": 1.0},
        "watermark": None,
        "history": {"committed_runs": 1, "last_commit_at": "t", "extract_failures": {}},
    }

    class _StubClient:
        profile_id = PROFILE

        def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
            return payload

    from mnemoseed_local import cli, rest_client

    monkeypatch.setattr(rest_client, "resolve_client", lambda args: _StubClient())
    assert cli.main(["dream", "status"]) == 0
    out = capsys.readouterr().out
    assert "extraction failures: none" in out
    assert "digested turns: none yet" in out


def test_cli_json_mode_emits_extended_fields(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    payload = {
        "state": "idle",
        "pool": {"balance": 1.5, "threshold": 1.0},
        "watermark": {"start": 1, "end": 2},
        "history": {"committed_runs": 1, "last_commit_at": "t", "extract_failures": {}},
    }

    class _StubClient:
        profile_id = PROFILE

        def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
            return payload

    from mnemoseed_local import cli, rest_client

    monkeypatch.setattr(rest_client, "resolve_client", lambda args: _StubClient())
    assert cli.main(["dream", "status", "--json"]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["pool"]["balance"] == 1.5
    assert emitted["history"]["committed_runs"] == 1
