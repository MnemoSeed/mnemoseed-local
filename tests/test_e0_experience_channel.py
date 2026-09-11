"""E-0 experience-channel foundation (PRD-B2.13): composite ledger groups,
read-side evidence-fate resolution, and the default-off channel flag.

These tests are the behavior fence for the foundation only — no detectors, no
model calls, no adjudication. A composite signal is represented as one ledger
row per source sharing a deterministic ``composite_group_id``; legacy rows keep
NULL. Reading the ledger resolves each chunk evidence pointer against the live
chunk store so a purged source surfaces as ``evidence_retired`` instead of a
dangling pointer (the ledger row itself is never rewritten).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.config import ConfigError, load_config
from mnemoseed_local.configwrite.service import (
    CONFIG_KEY_REGISTRY,
    ConfigWriteError,
    ConfigWriteService,
)
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.storage.drivers._migrations import (
    MIGRATIONS,
    apply_migrations,
    latest_version,
)
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import (
    ErrorEvent,
    ErrorEventFilter,
    ErrorSignalType,
    EvidenceKind,
    EvidencePointer,
    Page,
)

PROFILE = "default"


# ---------------------------------------------------------------- fixtures


def _config_toml(tmp_path: Path) -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
    )


@pytest.fixture
def e0_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


# ---------------------------------------------------------------- (A) composite


def test_composite_group_rows_roundtrip_and_filter(tmp_path) -> None:
    """A composite signal writes N rows sharing one composite_group_id; the
    group filter returns exactly those rows and legacy rows stay NULL."""
    driver = SqliteMetaDriver(path=tmp_path / "meta.db")
    try:
        for source in ("src-a", "src-b"):
            driver.append_error_event(
                ErrorEvent(
                    profile_id="u1",
                    signal_type=ErrorSignalType.COMPOSITE,
                    observed_at=100.0,
                    evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id=source),
                    session_id="s1",
                    composite_group_id="grp-1",
                )
            )
        # a legacy single-source row keeps the NULL carrier
        driver.append_error_event(
            ErrorEvent(
                profile_id="u1",
                signal_type=ErrorSignalType.COMPOSITE,
                observed_at=200.0,
                evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id="src-c"),
                session_id="s1",
            )
        )
        page = driver.query_error_events(ErrorEventFilter(profile_id="u1"), Page(0, 50))
        assert page.total == 3
        assert [e.composite_group_id for e in page.items] == ["grp-1", "grp-1", None]

        grouped = driver.query_error_events(
            ErrorEventFilter(profile_id="u1", composite_group_id="grp-1"), Page(0, 50)
        )
        assert grouped.total == 2
        assert {e.evidence_ptr.id for e in grouped.items} == {"src-a", "src-b"}
    finally:
        asyncio.run(driver.close())


def test_composite_multi_row_write_shares_one_observed_at_within_stream(tmp_path) -> None:
    """Multi-row composite writes inside one (profile, session) share an
    observed_at; the #178 monotonic gate allows equal timestamps."""
    driver = SqliteMetaDriver(path=tmp_path / "meta.db")
    try:
        for source in ("src-a", "src-b"):
            driver.append_error_event(
                ErrorEvent(
                    profile_id="u1",
                    signal_type=ErrorSignalType.COMPOSITE,
                    observed_at=500.0,
                    evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id=source),
                    session_id="s1",
                    composite_group_id="grp-eq",
                )
            )
        page = driver.query_error_events(
            ErrorEventFilter(profile_id="u1", composite_group_id="grp-eq"), Page(0, 50)
        )
        assert page.total == 2
    finally:
        asyncio.run(driver.close())


def test_blank_composite_group_normalizes_to_null(tmp_path) -> None:
    """A blank composite_group_id is not a group: it stores as NULL so the
    "NULL means single-source" contract cannot be broken by whitespace."""
    driver = SqliteMetaDriver(path=tmp_path / "meta.db")
    try:
        driver.append_error_event(
            ErrorEvent(
                profile_id="u1",
                signal_type=ErrorSignalType.COMPOSITE,
                observed_at=100.0,
                evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id="src-a"),
                composite_group_id="   ",
            )
        )
        page = driver.query_error_events(ErrorEventFilter(profile_id="u1"), Page(0, 50))
        assert page.items[0].composite_group_id is None
        assert (
            driver.query_error_events(
                ErrorEventFilter(profile_id="u1", composite_group_id="   "), Page(0, 50)
            ).total
            == 0
        )
    finally:
        asyncio.run(driver.close())


# ---------------------------------------------------------------- (A) migration v13


def test_migration_v13_is_the_head() -> None:
    assert latest_version() == 13
    meta_versions = sorted(m.version for m in MIGRATIONS if m.applies_to("meta"))
    assert meta_versions == [1, 3, 4, 6, 7, 8, 9, 11, 12, 13]


def test_v12_install_upgrades_to_v13_with_null_group(tmp_path) -> None:
    """An existing v12 meta file gains the nullable composite_group_id column;
    every legacy error_events row back-fills NULL (single-source)."""
    path = tmp_path / "meta.db"
    conn = sqlite3.connect(path, isolation_level=None)
    apply_migrations(conn, "meta", target=12)
    conn.execute(
        "INSERT INTO error_events (profile_id, signal_type, observed_at, evidence_kind, "
        "evidence_id, session_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("u1", "user_correction", "2026-01-01T00:00:00.000Z", "chunk", "legacy-c", "s1"),
    )
    conn.close()

    driver = SqliteMetaDriver(path=path)
    try:
        assert driver.schema_version() == 13
        cols = [row[1] for row in driver._conn.execute("PRAGMA table_info(error_events)")]
        assert "composite_group_id" in cols
        page = driver.query_error_events(ErrorEventFilter(profile_id="u1"), Page(0, 50))
        assert page.total == 1
        assert page.items[0].composite_group_id is None
    finally:
        asyncio.run(driver.close())


def test_fresh_install_lands_at_v13(tmp_path) -> None:
    driver = SqliteMetaDriver(path=tmp_path / "fresh.db")
    try:
        assert driver.schema_version() == 13
    finally:
        asyncio.run(driver.close())


# ---------------------------------------------------------------- (B) read-side retired


def test_read_route_marks_purged_chunk_evidence_retired(e0_config: Path) -> None:
    """A ledger row pointing at a chunk that no longer exists surfaces with
    evidence_retired=true; the append-only row itself is never rewritten."""
    with TestClient(create_app()) as client:
        meta = client.app.state.stores.meta
        meta.append_error_event(
            ErrorEvent(
                profile_id=PROFILE,
                signal_type=ErrorSignalType.COMPOSITE,
                observed_at=100.0,
                evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id="gone-chunk"),
                session_id="s1",
                composite_group_id="grp-retired",
            )
        )
        data = client.post("/memory/error_events", json={"profile_id": PROFILE}).json()
        assert len(data["items"]) == 1
        assert data["items"][0]["evidence_retired"] is True

        # read-side resolution is a pure read: the ledger row is unchanged and
        # still refuses a rewrite (append-only trigger)
        page = meta.query_error_events(ErrorEventFilter(profile_id=PROFILE), Page(0, 50))
        assert page.items[0].evidence_ptr.id == "gone-chunk"
        assert page.items[0].composite_group_id == "grp-retired"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            meta._conn.execute("UPDATE error_events SET evidence_id = 'rewritten'")


def test_read_route_marks_live_chunk_evidence_not_retired(e0_config: Path) -> None:
    """A pointer resolving to a live chunk reads evidence_retired=false."""
    from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Provenance

    with TestClient(create_app()) as client:
        stores = client.app.state.stores
        stamp = ChunkStamp(
            chunk_id="live-chunk",
            profile_id=PROFILE,
            text="live evidence",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="stub",
            provenance=Provenance(asserted_by="stub", source="manual"),
        )
        stores.vector.upsert_chunk(stamp, stores.embed.embed("live evidence").dense)
        stores.meta.append_error_event(
            ErrorEvent(
                profile_id=PROFILE,
                signal_type=ErrorSignalType.USER_CORRECTION,
                observed_at=100.0,
                evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id="live-chunk"),
            )
        )
        data = client.post("/memory/error_events", json={"profile_id": PROFILE}).json()
        assert len(data["items"]) == 1
        assert data["items"][0]["evidence_retired"] is False


def test_composite_read_resolves_live_and_gone_in_one_page(e0_config: Path) -> None:
    """Two ledger rows in one composite group — one chunk live, the other never
    created — resolve independently in the same read page, proving the
    chunk_ids IN-clause is actively narrowing (not just a no-op)."""
    from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Provenance

    with TestClient(create_app()) as client:
        stores = client.app.state.stores
        embed = stores.embed.embed("dummy").dense
        stores.vector.upsert_chunk(
            ChunkStamp(
                chunk_id="live-src",
                profile_id=PROFILE,
                text="live evidence",
                cognitive_tier=CognitiveTier.TIER_1,
                model_id="stub",
                provenance=Provenance(asserted_by="stub", source="manual"),
            ),
            embed,
        )
        stores.vector.upsert_chunk(
            ChunkStamp(
                chunk_id="distractor-a",
                profile_id=PROFILE,
                text="distractor a",
                cognitive_tier=CognitiveTier.TIER_1,
                model_id="stub",
                provenance=Provenance(asserted_by="stub", source="manual"),
            ),
            embed,
        )
        stores.vector.upsert_chunk(
            ChunkStamp(
                chunk_id="distractor-b",
                profile_id=PROFILE,
                text="distractor b",
                cognitive_tier=CognitiveTier.TIER_1,
                model_id="stub",
                provenance=Provenance(asserted_by="stub", source="manual"),
            ),
            embed,
        )
        for src_id in ("live-src", "gone-src"):
            stores.meta.append_error_event(
                ErrorEvent(
                    profile_id=PROFILE,
                    signal_type=ErrorSignalType.COMPOSITE,
                    observed_at=100.0,
                    evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id=src_id),
                    session_id="s1",
                    composite_group_id="composite-oracle",
                )
            )

        data = client.post("/memory/error_events", json={"profile_id": PROFILE}).json()
        items = {item["evidence_id"]: item for item in data["items"]}
        assert items["live-src"]["evidence_retired"] is False
        assert items["gone-src"]["evidence_retired"] is True


def test_read_route_retires_only_after_the_chunk_is_deleted(e0_config: Path) -> None:
    """The marker flips true only once the referenced chunk is physically gone
    (forget_this deletes chunk rows), never by rewriting the ledger."""
    from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Provenance

    with TestClient(create_app()) as client:
        stores = client.app.state.stores
        stamp = ChunkStamp(
            chunk_id="doomed",
            profile_id=PROFILE,
            text="about to be forgotten",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="stub",
            provenance=Provenance(asserted_by="stub", source="manual"),
        )
        stores.vector.upsert_chunk(stamp, stores.embed.embed("about to be forgotten").dense)
        stores.meta.append_error_event(
            ErrorEvent(
                profile_id=PROFILE,
                signal_type=ErrorSignalType.USER_CORRECTION,
                observed_at=100.0,
                evidence_ptr=EvidencePointer(kind=EvidenceKind.CHUNK, id="doomed"),
            )
        )
        before = client.post("/memory/error_events", json={"profile_id": PROFILE}).json()
        assert before["items"][0]["evidence_retired"] is False

        forget = client.post("/memory/forget_this", json={"profile_id": PROFILE, "chunk_id": "doomed"})
        assert forget.status_code == 200, forget.text

        after = client.post("/memory/error_events", json={"profile_id": PROFILE}).json()
        assert after["items"][0]["evidence_retired"] is True


# ---------------------------------------------------------------- (C) config flag


def test_experience_channel_flag_registered_and_defaults_false(tmp_path) -> None:
    assert "dream.experience_channel.enabled" in CONFIG_KEY_REGISTRY
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.dream.experience_channel.enabled is False


def test_experience_channel_flag_is_readable_and_writable(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n[dream]\n',
        encoding="utf-8",
    )
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0)
    assert service.get()["config"]["dream"]["experience_channel"]["enabled"] is False
    result = service.set("dream.experience_channel.enabled", True, actor="console")
    assert result["ok"] is True
    assert service._config.dream.experience_channel.enabled is True
    assert load_config(path).dream.experience_channel.enabled is True


def test_experience_channel_flag_rejects_non_bool(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n[dream]\n',
        encoding="utf-8",
    )
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0)
    # the key itself is writable before the rejection is asserted, so this
    # cannot pass via the "unknown config key" branch
    assert service.set("dream.experience_channel.enabled", True, actor="console")["ok"] is True
    with pytest.raises(ConfigWriteError, match=r"must be a boolean"):
        service.set("dream.experience_channel.enabled", "yes", actor="console")


def test_experience_channel_flag_loader_rejects_non_bool(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n'
        '[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n'
        '[dream]\n[dream.experience_channel]\nenabled = "on"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"config\[dream\.experience_channel\.enabled\]"):
        load_config(path)


def test_experience_channel_flag_is_documented_in_default_config() -> None:
    from mnemoseed_local.config import default_config_toml

    assert "[dream.experience_channel]" in default_config_toml()
    assert "experience_channel" in default_config_toml()


def test_nothing_consumes_the_experience_channel_flag_yet() -> None:
    """E-0 is foundation wiring only: the flag is referenced by the config
    loader and the configwrite registry, and by no hot path (no daemon, dream,
    capture, retrieve or storage module reads it)."""
    root = Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local"
    referencing = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "experience_channel" in path.read_text(encoding="utf-8")
    }
    assert referencing == {"config.py", "configwrite/service.py"}
