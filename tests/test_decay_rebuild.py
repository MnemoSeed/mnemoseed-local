"""One-time pin-weight rebuild (design/09 §4.1, retention redesign).

Existing chunks whose ``provenance.source`` is the explicit-pin marker spent
their early life decaying at the ordinary verbatim-chunk rate; the flashbulb
tier is much slower, so the migration recomputes each pin's effective weight
from its ``last_reinforced`` baseline (ingested_at fallback, the sweep rule)
under the pin λ and writes it back through the existing batch port. The pass
is deterministic and idempotent: a meta-store marker makes it strictly
one-time, and a crash before the marker write reruns harmlessly (same inputs,
same targets). One audit entry carries the stats — never per-chunk noise.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from mnemoseed_local.config import Config, load_config
from mnemoseed_local.decay.rebuild import (
    PIN_REBUILD_MARKER_KEY,
    PinRebuildStats,
    rebuild_pin_weights,
)
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed_local.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.factory import build_stores
from mnemoseed_local.storage.ports import AuditFilter, Page, StoredProfile
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_DAY = 86400.0
_PROFILE = "p1"
_NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _ensure_real_drivers() -> None:
    """test_daemon clears the shared registries; re-register the real drivers."""
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)


def _config_toml(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    return cfg


def _stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, object]:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config = load_config(_config_toml(tmp_path))
    stores = build_stores(config)
    return config, stores


def _seed_profile(stores: object, profile: str = _PROFILE) -> None:
    stores.meta.upsert_profile(StoredProfile(profile_id=profile))


def _chunk(
    chunk_id: str,
    *,
    source: str,
    ingested_at: float,
    last_reinforced: float | None = None,
    confidence: float = 1.0,
    decay_weight: float = 1.0,
    profile: str = _PROFILE,
) -> ChunkStamp:
    return ChunkStamp(
        chunk_id=chunk_id,
        profile_id=profile,
        text=f"body of {chunk_id}",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=["ui"]),
        provenance=Provenance(
            asserted_by="user",
            session_id=None,
            source=source,
            confidence=confidence,
            asserted_at=ingested_at,
            history=[],
        ),
        decay_weight=decay_weight,
        ingested_at=ingested_at,
        last_reinforced=last_reinforced,
    )


def _seed(stores: object, stamp: ChunkStamp) -> None:
    vector = SyntheticEmbedder(dimension=64).embed(stamp.text)
    stores.vector.upsert_chunk(stamp, vector.dense, vector.sparse)


# ---------------------------------------------------------------- behavior


def test_rebuild_raises_faded_pins_to_the_flashbulb_curve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin 100 days past its last reinforcement sits at the ordinary-rate
    value exp(-3.0) ≈ 0.05; the rebuild recomputes it under λ_pin=0.005 from
    the SAME baseline, recovering exp(-0.5) ≈ 0.607."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    faded = math.exp(-0.03 * 100.0)
    _seed(
        stores,
        _chunk(
            "old-pin",
            source="memory.remember",
            ingested_at=_NOW - 200 * _DAY,
            last_reinforced=_NOW - 100 * _DAY,
            decay_weight=faded,
        ),
    )

    stats = rebuild_pin_weights(stores, config, clock=lambda: _NOW)

    assert isinstance(stats, PinRebuildStats)
    assert stats.pins_scanned == 1
    assert stats.pins_updated == 1
    expected = math.exp(-0.005 * 100.0)
    assert expected > faded, "the flashbulb curve must sit above the faded value"
    assert stores.vector.get_chunk("old-pin").decay_weight == pytest.approx(expected, abs=1e-6)


def test_rebuild_falls_back_to_ingested_at_without_a_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``last_reinforced`` means the sweep's fallback baseline: ingested_at."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    _seed(
        stores,
        _chunk(
            "fresh-pin",
            source="memory.remember",
            ingested_at=_NOW - 10 * _DAY,
            decay_weight=math.exp(-0.03 * 10.0),
        ),
    )

    rebuild_pin_weights(stores, config, clock=lambda: _NOW)

    expected = math.exp(-0.005 * 10.0)
    assert stores.vector.get_chunk("fresh-pin").decay_weight == pytest.approx(expected, abs=1e-6)


def test_rebuild_leaves_plain_chunks_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the explicit-pin source participates: ordinary chunks keep their
    current weight and their ordinary-rate future."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    _seed(
        stores,
        _chunk(
            "plain",
            source="capture.session",
            ingested_at=_NOW - 100 * _DAY,
            last_reinforced=_NOW - 100 * _DAY,
            decay_weight=0.123,
        ),
    )

    stats = rebuild_pin_weights(stores, config, clock=lambda: _NOW)

    assert stats.pins_scanned == 0
    assert stats.pins_updated == 0
    assert stores.vector.get_chunk("plain").decay_weight == pytest.approx(0.123)


def test_rebuild_is_one_time_via_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed rebuild never runs again: the second call reports None and
    leaves weights untouched even when the world moved on underneath it."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    _seed(
        stores,
        _chunk(
            "old-pin",
            source="memory.remember",
            ingested_at=_NOW - 200 * _DAY,
            last_reinforced=_NOW - 100 * _DAY,
            decay_weight=math.exp(-0.03 * 100.0),
        ),
    )
    rebuild_pin_weights(stores, config, clock=lambda: _NOW)
    rebuilt = stores.vector.get_chunk("old-pin").decay_weight

    # the pin fades further after the migration; a re-run must not resurrect it
    later = _NOW + 30 * _DAY
    assert rebuild_pin_weights(stores, config, clock=lambda: later) is None
    assert stores.meta.get_config(PIN_REBUILD_MARKER_KEY) is not None
    assert stores.vector.get_chunk("old-pin").decay_weight == pytest.approx(rebuilt)


def test_rebuild_crash_before_marker_write_reruns_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash window: a previous pass wrote the weights but died before the
    marker landed — the store shows pins already sitting at their flashbulb
    targets. The rerun recomputes the SAME targets from the same baselines,
    skips the no-op writes, and completes the marker."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    rebuilt = math.exp(-0.005 * 100.0)
    _seed(
        stores,
        _chunk(
            "old-pin",
            source="memory.remember",
            ingested_at=_NOW - 200 * _DAY,
            last_reinforced=_NOW - 100 * _DAY,
            decay_weight=rebuilt,
        ),
    )

    stats = rebuild_pin_weights(stores, config, clock=lambda: _NOW)

    assert stats is not None
    assert stats.pins_scanned == 1
    assert stats.pins_updated == 0  # the recomputed target already sits in the row
    assert stores.meta.get_config(PIN_REBUILD_MARKER_KEY) is not None
    assert stores.vector.get_chunk("old-pin").decay_weight == pytest.approx(rebuilt)


def test_rebuild_spans_every_known_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D5: every profile with rows is covered; each keeps its own pins."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores, "p1")
    _seed_profile(stores, "p2")
    for profile in ("p1", "p2"):
        _seed(
            stores,
            _chunk(
                f"pin-{profile}",
                source="memory.remember",
                profile=profile,
                ingested_at=_NOW - 200 * _DAY,
                last_reinforced=_NOW - 100 * _DAY,
                decay_weight=math.exp(-0.03 * 100.0),
            ),
        )

    stats = rebuild_pin_weights(stores, config, clock=lambda: _NOW)

    assert stats.profiles_scanned == 2
    assert stats.pins_scanned == 2
    for profile in ("p1", "p2"):
        chunk = stores.vector.get_chunk(f"pin-{profile}")
        assert chunk.decay_weight == pytest.approx(math.exp(-0.005 * 100.0), abs=1e-6)


def test_rebuild_audits_one_summary_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Observability: ONE audit row with the migration stats, actor=daemon."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    _seed(
        stores,
        _chunk(
            "old-pin",
            source="memory.remember",
            ingested_at=_NOW - 200 * _DAY,
            last_reinforced=_NOW - 100 * _DAY,
            decay_weight=math.exp(-0.03 * 100.0),
        ),
    )

    rebuild_pin_weights(stores, config, clock=lambda: _NOW)

    entries = stores.meta.audit_query(AuditFilter(action="pin_weight_rebuild"), Page(limit=10)).items
    assert len(entries) == 1
    assert entries[0].actor == "daemon"
    assert entries[0].detail["pins_scanned"] == 1
    assert entries[0].detail["pins_updated"] == 1


def test_rebuild_crash_between_audit_and_marker_keeps_exactly_one_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash window: the summary audit row landed but the completion marker did
    not. The rerun completes the marker WITHOUT appending a second summary row
    (dedupe guard), so the design's exactly-one accounting survives."""
    config, stores = _stack(tmp_path, monkeypatch)
    _seed_profile(stores)
    _seed(
        stores,
        _chunk(
            "old-pin",
            source="memory.remember",
            ingested_at=_NOW - 200 * _DAY,
            last_reinforced=_NOW - 100 * _DAY,
            decay_weight=math.exp(-0.03 * 100.0),
        ),
    )
    real_set_config = stores.meta.set_config
    crashed = False

    def crashing_set_config(key: str, value: dict) -> int:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated crash between audit and marker")
        return real_set_config(key, value)

    monkeypatch.setattr(stores.meta, "set_config", crashing_set_config)
    with pytest.raises(RuntimeError):
        rebuild_pin_weights(stores, config, clock=lambda: _NOW)

    stats = rebuild_pin_weights(stores, config, clock=lambda: _NOW)

    assert stats is not None  # no marker yet -> the pass legitimately reruns
    entries = stores.meta.audit_query(AuditFilter(action="pin_weight_rebuild"), Page(limit=10)).items
    assert len(entries) == 1
    assert stores.meta.get_config(PIN_REBUILD_MARKER_KEY) is not None
