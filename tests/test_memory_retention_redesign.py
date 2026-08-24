"""Retention redesign daemon surface (design/09): supersede-in-place on
re-pin, index residue on the MCP explicit recall response, and the rescue ->
normal-reinforcement path.

The near-duplicate geometry is seeded directly through the store (the probe is
a pure dense-cosine re-score), so the remember() branches are exercised under
controlled similarity values without depending on embedder semantics.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from mnemoseed_local.config import Config, load_config
from mnemoseed_local.daemon.memory import MemoryService
from mnemoseed_local.schema.stamp import (
    EXPLICIT_PIN_SOURCE,
    ChunkStamp,
    CognitiveTier,
    Cues,
    Provenance,
    ProvenanceEvent,
)
from mnemoseed_local.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.factory import Stores, build_stores
from mnemoseed_local.storage.ports import ChunkFilter, Page, StoredProfile
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_PROFILE = "default"
_EMBEDDER = SyntheticEmbedder(dimension=64)


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


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MemoryService, Stores, Config]:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    config = load_config(cfg)
    stores = build_stores(config)
    stores.meta.upsert_profile(StoredProfile(profile_id=_PROFILE))
    svc = MemoryService(stores, config)
    yield svc, stores, config
    import asyncio

    svc.close()  # sync: shuts the retriever's track executor down
    asyncio.run(stores.close())


# ---------------------------------------------------------------- helpers


def _dense(text: str) -> list[float]:
    return _EMBEDDER.embed(text).dense


def _blend(base_text: str, away_text: str, cosine: float) -> list[float]:
    """A dense vector at an exact cosine to ``base_text``'s embedding (the
    orthogonalized component of ``away_text`` supplies the angular distance)."""
    base = _dense(base_text)
    away = _dense(away_text)
    norm_b = math.sqrt(sum(v * v for v in base))
    dot = sum(a * b for a, b in zip(base, away, strict=True))
    proj = [dot / (norm_b * norm_b) * v for v in base]
    ortho = [a - b for a, b in zip(away, proj, strict=True)]
    norm_o = math.sqrt(sum(v * v for v in ortho))
    scale_o = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return [cosine * b / norm_b + scale_o * o / norm_o for b, o in zip(base, ortho, strict=True)]


def _seed_chunk(
    stores: Stores,
    chunk_id: str,
    text: str,
    *,
    dense: list[float],
    decay_weight: float = 1.0,
    source: str = EXPLICIT_PIN_SOURCE,
    entities: tuple[str, ...] = (),
) -> str:
    sparse = _EMBEDDER.embed(text).sparse
    stores.vector.upsert_chunk(
        ChunkStamp(
            chunk_id=chunk_id,
            profile_id=_PROFILE,
            text=text,
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="test-model",
            cues=Cues(entities=list(entities)),
            provenance=Provenance(
                asserted_by="user",
                session_id=None,
                source=source,
                confidence=1.0,
                history=[ProvenanceEvent(action="created", actor="user", at=1_700_000_000.0)],
            ),
            decay_weight=decay_weight,
            ingested_at=1_700_000_000.0,
        ),
        dense,
        sparse,
    )
    return chunk_id


# ------------------------------------------------------------ supersede-in-place


class TestSupersedeInPlace:
    """design/09 §3.3: within-topic re-pins collapse onto one entity."""

    NEW_TEXT = "AtlasDb keeps the nightly export window at 02:00 UTC"
    OLD_TEXT = "AtlasDb used to keep the nightly export window at 03:00 UTC"

    def test_reworded_consistent_repinsupersedes_in_place(self, service) -> None:
        """A consistent verdict over materially different wording replaces the
        stored text on the SAME chunk and appends a superseded revision event;
        no second entry is created."""
        svc, stores, _config = service
        _seed_chunk(stores, "old", self.OLD_TEXT, dense=_dense(self.NEW_TEXT))
        before = stores.vector.list_chunks(ChunkFilter(profile_id=_PROFILE), Page(offset=0, limit=100)).total

        result = svc.remember(profile_id=_PROFILE, text=self.NEW_TEXT)

        assert result["outcome"] == "superseded"
        assert result["chunk_id"] == "old"
        after = stores.vector.list_chunks(ChunkFilter(profile_id=_PROFILE), Page(offset=0, limit=100)).total
        assert after == before
        stored = stores.vector.get_chunk("old")
        assert stored.text == self.NEW_TEXT
        actions = [event.action for event in stored.provenance.history]
        assert actions[0] == "created"
        assert actions[-1] == "superseded"
        detail = stored.provenance.history[-1].detail
        assert detail["superseded_text"] == self.OLD_TEXT

    def test_weak_band_repin_supersedes_instead_of_duplicating(self, service) -> None:
        """The duplicate gap: a re-pin landing between the conflict threshold
        and the reinforce threshold (consistent verdict, not strong) previously
        fell through to a second chunk; it now supersedes in place."""
        svc, stores, _config = service
        blended = _blend(self.NEW_TEXT, "unrelated filler body text", cosine=0.87)
        _seed_chunk(stores, "weak-hit", self.OLD_TEXT, dense=blended)

        result = svc.remember(profile_id=_PROFILE, text=self.NEW_TEXT)

        assert result == {"outcome": "superseded", "chunk_id": "weak-hit"}
        stored = stores.vector.get_chunk("weak-hit")
        assert stored.text == self.NEW_TEXT
        assert [e.action for e in stored.provenance.history][-1] == "superseded"

    def test_identical_repin_keeps_pure_reinforcement(self, service) -> None:
        """Identical wording keeps today's behavior: rebound in place, text and
        history untouched beyond the original created event."""
        svc, stores, _config = service
        _seed_chunk(stores, "same", self.NEW_TEXT, dense=_dense(self.NEW_TEXT), decay_weight=0.5)

        result = svc.remember(profile_id=_PROFILE, text=self.NEW_TEXT)

        assert result == {"outcome": "reinforced", "chunk_id": "same"}
        stored = stores.vector.get_chunk("same")
        assert stored.decay_weight == pytest.approx(0.6)
        assert stored.text == self.NEW_TEXT
        assert [e.action for e in stored.provenance.history] == ["created"]

    def test_conflicting_repin_still_flags_needs_reconcile(self, service) -> None:
        """True statement contradictions are never silently overwritten: the
        conflict branch keeps flagging needs_reconcile on the stored text."""
        svc, stores, _config = service
        old = "AtlasDb prefers pnpm for package management"
        new = "AtlasDb stopped using pnpm and switched to npm for package management"
        _seed_chunk(stores, "conflict", old, dense=_dense(new))

        result = svc.remember(profile_id=_PROFILE, text=new)

        assert result == {"outcome": "needs_reconcile", "chunk_id": "conflict"}
        assert stores.vector.get_chunk("conflict").text == old

    def test_capture_class_band_hit_is_never_superseded(self, service) -> None:
        """The supersede branch is flashbulb-class only: a re-pin landing in
        band over an ordinary CAPTURE chunk must fall through to a fresh chunk
        and leave the captured verbatim session text byte-untouched — capture
        provenance never gets rewritten by the pin path."""
        svc, stores, _config = service
        _seed_chunk(
            stores,
            "cap1",
            self.OLD_TEXT,
            dense=_dense(self.NEW_TEXT),
            source="capture.session",
        )

        result = svc.remember(profile_id=_PROFILE, text=self.NEW_TEXT)

        assert result["outcome"] == "new_chunk"
        assert result["chunk_id"] != "cap1"
        stored = stores.vector.get_chunk("cap1")
        assert stored.text == self.OLD_TEXT
        assert [event.action for event in stored.provenance.history] == ["created"]
        assert stored.provenance.source == "capture.session"
        # never reinforced: the baseline still equals the ingestion fallback
        assert stored.last_reinforced == stored.ingested_at

    def test_identical_repin_in_weak_band_reports_reinforced(self, service) -> None:
        """Identical wording is the strongest possible consistency evidence: an
        exact re-pin that lands ONLY in the weak band reinforces in place — it
        must not append a self-referential superseded event."""
        svc, stores, _config = service
        blended = _blend(self.NEW_TEXT, "unrelated filler body text", cosine=0.87)
        _seed_chunk(stores, "weak-same", self.NEW_TEXT, dense=blended, decay_weight=0.5)

        result = svc.remember(profile_id=_PROFILE, text=self.NEW_TEXT)

        assert result == {"outcome": "reinforced", "chunk_id": "weak-same"}
        stored = stores.vector.get_chunk("weak-same")
        assert stored.decay_weight == pytest.approx(0.6)
        assert [event.action for event in stored.provenance.history] == ["created"]


# ---------------------------------------------------------------- index residue


class TestIndexResidue:
    """design/09 §3.6: dead-zone pins stay reachable as one-line residues."""

    DEAD_PIN = "AtlasDb ships the audit bundle every Friday"
    QUERY = "what is the current status of AtlasDb"

    def test_dead_zone_pin_renders_a_residue_line_on_explicit_recall(self, service) -> None:
        svc, stores, _config = service
        _seed_chunk(stores, "dead", self.DEAD_PIN, dense=_dense(self.DEAD_PIN), decay_weight=0.05)

        result = svc.recall(profile_id=_PROFILE, query=self.QUERY)

        block = result["memory"]["index_residue"]
        assert block["window_truncated"] is False  # one row, scan window far from full
        residue = block["rows"]
        assert len(residue) == 1
        row = residue[0]
        assert row["chunk_id"] == "dead"
        assert row["head"].startswith(self.DEAD_PIN[:20])
        assert self.DEAD_PIN.startswith(row["head"])
        assert "[pin residue" in row["line"]
        assert row["date"][:4] == "2023"  # ingested_at rendered as ISO date
        # the dead pin itself is never a served entry
        assert all(entry["id"] != "dead" for entry in result["memory"]["entries"])

    def test_healthy_pin_produces_no_residue(self, service) -> None:
        svc, stores, _config = service
        _seed_chunk(stores, "alive", self.DEAD_PIN, dense=_dense(self.DEAD_PIN), decay_weight=0.9)

        result = svc.recall(profile_id=_PROFILE, query=self.QUERY)

        block = result["memory"]["index_residue"]
        assert block["rows"] == []
        assert block["window_truncated"] is False

    def test_scan_window_beyond_the_limit_is_honestly_marked_truncated(
        self, service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The residue scan is bounded (newest _RESIDUE_SCAN_LIMIT rows); when
        the profile holds more chunks than the window covered, the block says so
        instead of silently dropping older dead pins (window_truncated
        precedent)."""
        svc, stores, _config = service
        monkeypatch.setattr("mnemoseed_local.daemon.memory._RESIDUE_SCAN_LIMIT", 2)
        _seed_chunk(stores, "dead", self.DEAD_PIN, dense=_dense(self.DEAD_PIN), decay_weight=0.05)
        _seed_chunk(stores, "alive", "another pin entirely", dense=_dense("another pin entirely"))
        _seed_chunk(
            stores,
            "plain",
            "a plain capture chunk",
            dense=_dense("a plain capture chunk"),
            source="capture.session",
        )

        result = svc.recall(profile_id=_PROFILE, query=self.QUERY)

        block = result["memory"]["index_residue"]
        assert block["window_truncated"] is True
        assert [row["chunk_id"] for row in block["rows"]] == ["dead"]

    def test_t2_auto_injection_never_carries_residue(self, service) -> None:
        """Auto-injection stays exactly as today: the focal scan payload has no
        residue field and none of its item texts carry the residue marker."""
        svc, stores, _config = service
        session = "sess-t2"
        _seed_chunk(stores, "dead", self.DEAD_PIN, dense=_dense(self.DEAD_PIN), decay_weight=0.05)

        svc.note_user_prompt(_PROFILE, session, self.QUERY)
        pull = svc.recall_pending(_PROFILE, session, [])

        assert "index_residue" not in pull
        assert all("[pin residue" not in item["text"] for item in pull["items"])


# ------------------------------------------------- rescue -> normal reinforcement


class TestRescueReboundPath:
    def test_rescued_pin_is_served_flagged_and_rebounds(self, service) -> None:
        """End-to-end contract: a rescue-band pin enters the pool on a strong
        cue, is served below the normal candidates, flagged rescued, and the
        hit routes through the normal reinforcement path (+0.1 rebound)."""
        svc, stores, _config = service
        _seed_chunk(
            stores,
            "aged-pin",
            "AtlasDb keeps the staging cluster warm overnight",
            dense=_dense("AtlasDb keeps the staging cluster warm overnight"),
            decay_weight=0.3,
            entities=("AtlasDb",),
        )

        result = svc.recall(profile_id=_PROFILE, query="what happened with AtlasDb")

        entries = result["memory"]["entries"]
        rescued = [entry for entry in entries if entry["id"] == "aged-pin"]
        assert len(rescued) == 1
        assert "rescued" in rescued[0]["flags"]
        stored = stores.vector.get_chunk("aged-pin")
        assert stored.decay_weight == pytest.approx(0.4)
        assert stored.last_reinforced is not None
