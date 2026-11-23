"""Consumption-instrument oracle (S1): append-only evidence ledger, additive
serve_id echo, host emission, and the read-side honesty rules.

Temp home + spare-loopback isolation; no S0 harness reuse.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import auth_support
import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.observability import Observability
from mnemoseed_local.storage.drivers import sqlite_meta
from mnemoseed_local.storage.drivers._migrations import MIGRATIONS, latest_version
from mnemoseed_local.storage.ports import (
    ConsumptionEvent,
    ConsumptionEventFilter,
    ConsumptionEventKind,
    ConsumptionEvidenceKind,
    ConsumptionMethod,
    ConsumptionSource,
    Page,
    consumption_join,
    consumption_status,
    derive_consumption_dedup_key,
)

REPO = Path(__file__).resolve().parents[1]

OBSERVED_AT = "2026-11-18T12:34:56+00:00"


def _driver(tmp_path: Path) -> sqlite_meta.SqliteMetaDriver:
    return sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")


def _close(driver: sqlite_meta.SqliteMetaDriver) -> None:
    asyncio.run(driver.close())


def _event(
    *,
    profile: str = "default",
    correlation: str = "serve-1",
    kind: ConsumptionEventKind = ConsumptionEventKind.SERVED,
    evidence: str = "chunk-1",
    observed: float = 1000.0,
    session: str | None = "sess-1",
    method: ConsumptionMethod | None = ConsumptionMethod.UNSPECIFIED,
    source: ConsumptionSource = ConsumptionSource.DAEMON,
    evidence_kind: ConsumptionEvidenceKind = ConsumptionEvidenceKind.CHUNK,
    rank: int | None = 0,
) -> ConsumptionEvent:
    return ConsumptionEvent(
        profile_id=profile,
        correlation_id=correlation,
        event_kind=kind,
        evidence_kind=evidence_kind,
        evidence_id=evidence,
        source=source,
        observed_at=observed,
        method=method,
        rank=rank,
        session_id=session,
    )


def _rows(client: TestClient, action: str = "recall_feedback") -> list[dict[str, Any]]:
    response = client.get("/api/v1/audit", params={"action": action})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _consumption_events(client: TestClient, *, kind: str | None = None) -> list[ConsumptionEvent]:
    store = client.app.state.stores.meta
    flt = ConsumptionEventFilter(
        profile_id="default",
        event_kind=ConsumptionEventKind(kind) if kind else None,
    )
    return store.query_consumption_events(flt, Page(0, 100)).items


def _consumption_rows(client: TestClient, *, kind: str | None = None) -> list[dict[str, Any]]:
    page_items = _consumption_events(client, kind=kind)
    return [
        {
            "event_kind": event.event_kind.value,
            "correlation_id": event.correlation_id,
            "evidence_id": event.evidence_id,
            "method": event.method.value if event.method else None,
            "source": event.source.value,
        }
        for event in page_items
    ]


def _bootable_home(home: Path, monkeypatch: pytest.MonkeyPatch, *, auto_recall: bool = False) -> None:
    home.mkdir(parents=True, exist_ok=True)
    capture = "[capture]\nauto_recall = true\n" if auto_recall else ""
    (home / "config.toml").write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        '[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        '[dream.llm.dream]\ndriver = "stub"\nmodel = "stub"\n' + capture,
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", home)


# ---------------------------------------------------------------- AC-1: schema


def test_migration_v18_creates_the_append_only_consumption_table(tmp_path: Path) -> None:
    assert latest_version() == 18
    driver = _driver(tmp_path)
    try:
        assert driver.schema_version() == 18
        conn = driver._conn
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(consumption_events)")}
        expected = {
            "id",
            "profile_id",
            "observed_at",
            "correlation_id",
            "event_kind",
            "evidence_kind",
            "evidence_id",
            "source",
            "method",
            "rank",
            "needle_len",
            "needle_hash",
            "session_id",
            "query_hash",
            "dedup_key",
        }
        assert set(cols) == expected, set(cols)
        assert cols["profile_id"][3] == 1, "profile_id must be NOT NULL"
        assert cols["observed_at"][3] == 1, "observed_at must be NOT NULL"
        assert cols["correlation_id"][3] == 1, "correlation_id must be NOT NULL"
        # No verdict/boolean column: the event_kind is a fact, not a judgment.
        assert not ({"used", "consumed", "verdict", "not_used"} & set(cols))
        triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert {"trg_consumption_no_update", "trg_consumption_no_delete"} <= triggers
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_consumption_profile_time", "idx_consumption_corr"} <= indexes
        unique_sql = next(
            row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE name='consumption_events'")
        )
        assert "UNIQUE (profile_id, dedup_key)" in unique_sql
        kinds = sorted(m.version for m in MIGRATIONS if m.applies_to("meta"))
        assert kinds[-1] == 18
    finally:
        _close(driver)


def test_consumption_events_reject_update_and_delete(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    try:
        assert driver.append_consumption_event(_event()) is True
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            driver._conn.execute(
                "UPDATE consumption_events SET method='unspecified' WHERE profile_id='default'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            driver._conn.execute("DELETE FROM consumption_events")
    finally:
        _close(driver)


def test_observed_at_is_monotonic_within_a_session(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    try:
        assert driver.append_consumption_event(_event(observed=2000.0, evidence="c1")) is True
        with pytest.raises(ValueError, match="observed_at"):
            driver.append_consumption_event(_event(observed=1000.0, evidence="c2"))
    finally:
        _close(driver)


# ---------------------------------------------------------------- AC-7: idempotency


def test_duplicate_append_is_idempotent(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    try:
        event = _event()
        assert driver.append_consumption_event(event) is True
        assert driver.append_consumption_event(event) is False
        page = driver.query_consumption_events(ConsumptionEventFilter(profile_id="default"), Page())
        assert page.total == 1
        assert derive_consumption_dedup_key(
            event.profile_id,
            event.correlation_id,
            event.event_kind.value,
            event.evidence_id,
            event.method.value if event.method else None,
        )
    finally:
        _close(driver)


# ---------------------------------------------------------------- AC-2: three write kinds


def test_dedup_key_is_stable_over_attribution_fields() -> None:
    first = derive_consumption_dedup_key("default", "s1", "served", "c1", "unspecified")
    second = derive_consumption_dedup_key("default", "s1", "served", "c1", "unspecified")
    assert first == second
    assert first != derive_consumption_dedup_key("default", "s2", "served", "c1", "unspecified")


def test_endpoint_appends_injected_and_consumed_attested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _bootable_home(home, monkeypatch)
    with auth_support.boot(home) as client:
        body = {
            "profile_id": "default",
            "host": "opencode",
            "session_id": "sess-1",
            "items": [
                {
                    "event_kind": "injected",
                    "evidence_kind": "chunk",
                    "evidence_id": "c-1",
                    "method": "unspecified",
                    "observed_at": 1000.0,
                    "correlation_id": "serve-1",
                },
                {
                    "event_kind": "consumed_attested",
                    "evidence_kind": "chunk",
                    "evidence_id": "c-1",
                    "method": "citation_substring_v1",
                    "needle_len": 24,
                    "needle_hash": "a" * 64,
                    "observed_at": 1001.0,
                    "correlation_id": "serve-1",
                },
            ],
        }
        response = client.post("/memory/consumption", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 2
        kinds = {row["event_kind"] for row in _consumption_rows(client)}
        assert kinds == {"injected", "consumed_attested"}


def test_explicit_feedback_appends_a_consumed_explicit_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _bootable_home(home, monkeypatch)
    with auth_support.boot(home) as client:
        response = client.post(
            "/memory/recall_feedback",
            json={
                "profile_id": "default",
                "kind": "not_useful",
                "observed_at": OBSERVED_AT,
                "correlation_id": "serve-9",
            },
        )
        assert response.status_code == 200, response.text
        rows = _consumption_rows(client, kind="consumed_explicit")
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == "serve-9"
        assert rows[0]["method"] == "explicit_not_useful"
        assert rows[0]["source"] == "agent_tool"


# ---------------------------------------------------------------- AC-4: additive serve_id


def test_serve_id_is_additive_and_strippable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    _bootable_home(home, monkeypatch)
    from mnemoseed_local.daemon.memory import MemoryService
    from mnemoseed_local.retrieve.assemble import AssembledEntry

    entry = AssembledEntry(
        kind="chunk",
        id="c-1",
        text="t" * 40,
        score=1.0,
        tokens=10,
        flags=(),
        source="manual",
        asserted_by="user",
        provenance_source="manual",
        explicit_pin=True,
        ingested_at=1.0,
    )
    without = MemoryService._entry_payload(entry)
    with_serve = MemoryService._entry_payload(entry, "serve-xyz")
    assert "serve_id" not in without
    assert with_serve["serve_id"] == "serve-xyz"
    stripped = dict(with_serve)
    stripped.pop("serve_id")
    assert stripped == without, "stripping serve_id must restore the baseline entry byte-for-byte"


# ---------------------------------------------------------------- AC-5 / AC-9: read-side honesty


def test_consumption_status_is_unknown_without_a_consumed_row() -> None:
    served_only = [_event(kind=ConsumptionEventKind.SERVED)]
    assert consumption_status(served_only) == "unknown"
    assert consumption_status([]) == "unknown"
    assert consumption_status(served_only) not in {"used", "not_used"}


def test_consumption_status_reflects_consumed_method() -> None:
    events = [
        _event(kind=ConsumptionEventKind.SERVED),
        _event(
            kind=ConsumptionEventKind.CONSUMED_ATTESTED,
            method=ConsumptionMethod.CITATION_SUBSTRING_V1,
        ),
    ]
    assert consumption_status(events) == "consumed_attested"


def test_consumption_join_is_exact_only_for_a_unique_serve_evidence_pair() -> None:
    served = [_event(kind=ConsumptionEventKind.SERVED, correlation="s1", evidence="c1")]
    consumed = _event(
        kind=ConsumptionEventKind.CONSUMED_ATTESTED,
        correlation="s1",
        evidence="c1",
        method=ConsumptionMethod.CITATION_SUBSTRING_V1,
    )
    assert consumption_join(consumed, served) == "exact"
    # no echoed serve id -> weak
    weak = _event(
        kind=ConsumptionEventKind.CONSUMED_ATTESTED,
        correlation="",
        evidence="c1",
        method=ConsumptionMethod.CITATION_SUBSTRING_V1,
    )
    assert consumption_join(weak, served) == "weak"
    # ambiguous candidate set (the same pair served twice) -> weak
    ambiguous = [
        _event(kind=ConsumptionEventKind.SERVED, correlation="s1", evidence="c1"),
        _event(kind=ConsumptionEventKind.SERVED, correlation="s1", evidence="c1", observed=1001.0),
    ]
    assert consumption_join(consumed, ambiguous) == "weak"


# ---------------------------------------------------------------- AC-6: fire-and-forget


def test_observability_counts_each_appended_kind() -> None:
    obs = Observability()
    for kind in (
        ConsumptionEventKind.SERVED,
        ConsumptionEventKind.INJECTED,
        ConsumptionEventKind.CONSUMED_ATTESTED,
    ):
        obs.note_consumption_event(kind.value)
    snapshot = obs.snapshot()
    counts = snapshot["consumption_events"]
    assert counts["served"] == 1
    assert counts["injected"] == 1
    assert counts["consumed_attested"] == 1


# ---------------------------------------------------------------- AC-3: no body exfiltration


def test_consumption_ledger_never_stores_natural_language_text(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    try:
        needle = "自然语言的原始针脚内容不应被存储"
        event = ConsumptionEvent(
            profile_id="default",
            correlation_id="serve-1",
            event_kind=ConsumptionEventKind.CONSUMED_ATTESTED,
            evidence_kind=ConsumptionEvidenceKind.CHUNK,
            evidence_id="c-1",
            source=ConsumptionSource.PLUGIN_HOOK,
            observed_at=1000.0,
            method=ConsumptionMethod.CITATION_SUBSTRING_V1,
            needle_len=len(needle),
            needle_hash=hashlib.sha256(needle.encode()).hexdigest(),
            query_hash=hashlib.sha256(b"natural query").hexdigest(),
        )
        assert driver.append_consumption_event(event) is True
        row = driver.query_consumption_events(ConsumptionEventFilter(profile_id="default"), Page()).items[0]
        assert row.needle_hash == hashlib.sha256(needle.encode()).hexdigest()
        assert row.needle_len == len(needle)
        stored = str(row.__dict__)
        assert needle not in stored
        assert "natural query" not in stored
    finally:
        _close(driver)


def test_consumption_wire_model_has_no_text_fields() -> None:
    from mnemoseed_local.daemon.memory import ConsumptionItem

    names = set(ConsumptionItem.model_fields)
    assert not ({"text", "query", "content", "reply"} & names)


def test_ranking_modules_have_zero_consumption_readers() -> None:
    sources = [
        REPO / "src/mnemoseed_local/retrieve/cues.py",
        REPO / "src/mnemoseed_local/retrieve/readview.py",
        REPO / "src/mnemoseed_local/retrieve/hybrid.py",
        REPO / "src/mnemoseed_local/retrieve/assemble.py",
        REPO / "src/mnemoseed_local/decay/reinforce.py",
    ]
    forbidden = ("consumption_events", "ConsumptionEvent", "query_consumption", "append_consumption")
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), f"{path} reads the consumption ledger"


def test_plugin_consumption_builder_carries_only_pointers() -> None:
    plugin = (REPO / "src/mnemoseed_local/hosts/opencode/plugin.ts").read_text(encoding="utf-8")
    start = plugin.index("function buildConsumptionBody")
    end = plugin.index("\n}", start)
    builder = plugin[start:end]
    assert "needle_hash" in builder and "needle_len" in builder
    assert ".text" not in builder and "reply" not in builder


def _ingest_chunk(client: TestClient, text: str) -> None:
    from mnemoseed_local.schema.turn import HostId

    ingest = client.post(
        "/ingest",
        json={
            "host": HostId.CLAUDE_CODE.value,
            "event": "user_prompt",
            "session_id": "sess-consume",
            "profile_id": "default",
            "ts": 1.0,
            "content": {"text": text},
        },
    )
    assert ingest.status_code == 202, ingest.text
    settle = client.post("/session/end", json={"session_id": "sess-consume", "profile_id": "default"})
    assert settle.status_code == 200, settle.text


def test_recall_survives_a_ledger_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    _bootable_home(home, monkeypatch)
    with auth_support.boot(home) as client:
        _ingest_chunk(client, "记忆消费链路的缓存完整性检查流程")
        store = client.app.state.stores.meta

        def boom(event: ConsumptionEvent) -> bool:
            raise RuntimeError("ledger down")

        monkeypatch.setattr(store, "append_consumption_event", boom)
        response = client.post(
            "/memory/recall",
            json={"profile_id": "default", "query": "缓存完整性", "top_k": 5},
        )
        assert response.status_code == 200, response.text


def test_feedback_never_writes_reinforcement_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemoseed_local.storage.ports import ChunkFilter

    home = tmp_path / "home"
    _bootable_home(home, monkeypatch)
    with auth_support.boot(home) as client:
        _ingest_chunk(client, "一项用于验证反馈零排序写入的记忆内容")
        store = client.app.state.stores.vector
        before = store.list_chunks(ChunkFilter(profile_id="default"), Page(0, 50)).items
        assert before
        snapshot = [(chunk.chunk_id, chunk.decay_weight, chunk.last_reinforced) for chunk in before]

        response = client.post(
            "/memory/recall_feedback",
            json={
                "profile_id": "default",
                "kind": "not_useful",
                "observed_at": OBSERVED_AT,
                "correlation_id": "serve-x",
            },
        )
        assert response.status_code == 200, response.text
        after = store.list_chunks(ChunkFilter(profile_id="default"), Page(0, 50)).items
        assert [(chunk.chunk_id, chunk.decay_weight, chunk.last_reinforced) for chunk in after] == snapshot


def test_feedback_old_caller_without_correlation_id_stays_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _bootable_home(home, monkeypatch)
    with auth_support.boot(home) as client:
        response = client.post(
            "/memory/recall_feedback",
            json={"profile_id": "default", "kind": "not_useful", "observed_at": OBSERVED_AT},
        )
        assert response.status_code == 200, response.text
        audit = _rows(client)
        assert len(audit) == 1
        assert audit[0]["detail"] == {
            "profile_id": "default",
            "kind": "not_useful",
            "observed_at": OBSERVED_AT,
        }
        rows = _consumption_rows(client, kind="consumed_explicit")
        assert len(rows) == 1
        assert rows[0]["correlation_id"] == ""
        assert rows[0]["method"] == "explicit_unknown"


# ---------------------------------------------------------------- AC-2: served writers


def test_recall_writes_a_served_row_per_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_similarity_floor: None
) -> None:
    home = tmp_path / "home"
    _bootable_home(home, monkeypatch)
    query = "缓存完整性"
    with auth_support.boot(home) as client:
        _ingest_chunk(client, "记忆消费链路的缓存完整性检查流程")
        response = client.post(
            "/memory/recall",
            json={"profile_id": "default", "query": query, "top_k": 5},
        )
        assert response.status_code == 200, response.text
        entries = response.json()["memory"]["entries"]
        assert entries

        served = _consumption_events(client, kind="served")
        assert len(served) == len(entries)
        for rank, (event, entry) in enumerate(zip(served, entries, strict=True)):
            assert event.profile_id == "default"
            assert event.event_kind is ConsumptionEventKind.SERVED
            assert event.source is ConsumptionSource.DAEMON
            assert event.correlation_id == entry["serve_id"]
            assert event.evidence_id == entry["id"]
            assert event.rank == rank
            assert event.query_hash == hashlib.sha256(query.encode("utf-8")).hexdigest()


def test_recall_pending_writes_a_served_row_per_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mnemoseed_local.schema.turn import HostId

    home = tmp_path / "home"
    _bootable_home(home, monkeypatch, auto_recall=True)
    with auth_support.boot(home) as client:
        _ingest_chunk(client, "上一轮我们把 LanceDb 定为向量存储")
        ingest = client.post(
            "/ingest",
            json={
                "host": HostId.CLAUDE_CODE.value,
                "event": "user_prompt",
                "session_id": "sess-pull",
                "profile_id": "default",
                "ts": 2.0,
                "content": {"text": "LanceDb 现在处于什么阶段"},
            },
        )
        assert ingest.status_code == 202, ingest.text

        response = client.post(
            "/session/recall-pending",
            json={"profile_id": "default", "session_id": "sess-pull"},
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert items

        served = _consumption_events(client, kind="served")
        assert len(served) == len(items)
        for rank, (event, item) in enumerate(zip(served, items, strict=True)):
            assert event.profile_id == "default"
            assert event.event_kind is ConsumptionEventKind.SERVED
            assert event.source is ConsumptionSource.DAEMON
            assert event.correlation_id == item["serve_id"]
            assert event.evidence_id == item["id"]
            assert event.rank == rank
            assert event.query_hash is None
