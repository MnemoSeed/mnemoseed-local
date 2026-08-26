"""Deliberate supersede verb on the daemon memory surface: close the
superseded node's current version through the version chain and link the
successor with an explicit SUPERSEDES edge.

Red line under test: superseding a belief is a willful act triggered ONLY by
the verb — never a side effect of recall or scoring.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mnemoseed_local.config import load_config
from mnemoseed_local.daemon.memory import MemoryNotFoundError, MemoryService
from mnemoseed_local.schema.graph import GraphNode, NodeType, RelType
from mnemoseed_local.schema.stamp import Provenance
from mnemoseed_local.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed_local.storage.drivers._time import epoch_from_iso
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

_PROFILE = "default"


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


def _pref_node(node_id: str, *, profile: str = _PROFILE) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        profile_id=profile,
        node_type=NodeType.PREFERENCE,
        entities=["Editor"],
        props={
            "domain": "coding",
            "statement": f"editor stance of {node_id}",
            "valence": 0.5,
            "prior_width": 0.3,
            "trait_anchor": "a",
            "evidence_chain": [],
        },
        provenance=Provenance(asserted_by="test-model", source="x", session_id=None),
    )


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MemoryService, object]:
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
    stores.meta.upsert_profile(StoredProfile(profile_id="other"))
    svc = MemoryService(stores, config)
    yield svc, stores
    svc.close()
    asyncio.run(stores.close())


def _supersedes_edges(stores) -> list[tuple[str, str]]:
    rows = stores.graph._conn.execute(
        "SELECT src, dst FROM edges WHERE rel = ?", (RelType.SUPERSEDES.value,)
    ).fetchall()
    return [(str(row["src"]), str(row["dst"])) for row in rows]


def _fail_edge_write(stores, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the supersede operation's edge-write step raise mid-operation,
    wherever that step currently lives (driver-internal upsert when the close
    and the link share one transaction, else the standalone add_edge)."""
    driver_cls = type(stores.graph)

    def explode(self, edge):  # pragma: no cover - assertion helper
        raise RuntimeError("edge write failed")

    if hasattr(driver_cls, "_upsert_edge_locked"):
        monkeypatch.setattr(driver_cls, "_upsert_edge_locked", explode)
    else:
        monkeypatch.setattr(driver_cls, "add_edge", explode)


# ---------------------------------------------------------------- happy path


def test_supersede_closes_old_revision_and_links_successor(rig) -> None:
    svc, stores = rig
    stores.graph.upsert_node(_pref_node("old"))
    stores.graph.upsert_node(_pref_node("new"))

    result = svc.supersede(profile_id=_PROFILE, superseded_node_id="old", successor_node_id="new")

    assert result["superseded"] == "old"
    assert result["successor"] == "new"
    assert stores.graph.get_node("old") is None, "the superseded revision must leave current reads"
    chain = stores.graph.versions("old")
    assert len(chain) == 1
    assert chain[0].valid_to is not None, "the old version closes through the version chain"
    assert stores.graph.get_node("new") is not None, "the successor stays current"
    assert _supersedes_edges(stores) == [("new", "old")]


def test_supersede_writes_an_audit_row(rig) -> None:
    svc, stores = rig
    stores.graph.upsert_node(_pref_node("old"))
    stores.graph.upsert_node(_pref_node("new"))

    svc.supersede(profile_id=_PROFILE, superseded_node_id="old", successor_node_id="new")

    page = stores.meta.audit_query(AuditFilter(), Page(offset=0, limit=50))
    actions = [entry.action for entry in page.items]
    assert "supersede" in actions


def test_supersede_close_and_edge_carry_the_same_clock(rig) -> None:
    """The closed revision's valid_to and the SUPERSEDES edge's created_at come
    from one clock reading — the replacement is a single event, not two."""
    svc, stores = rig
    stores.graph.upsert_node(_pref_node("old"))
    stores.graph.upsert_node(_pref_node("new"))

    svc.supersede(profile_id=_PROFILE, superseded_node_id="old", successor_node_id="new")

    chain = stores.graph.versions("old")
    row = stores.graph._conn.execute(
        "SELECT created_at FROM edges WHERE rel = ?", (RelType.SUPERSEDES.value,)
    ).fetchone()
    assert chain[0].valid_to == epoch_from_iso(str(row["created_at"]))


# ---------------------------------------------------------------- atomicity


def test_failed_link_write_leaves_the_revision_open(rig, monkeypatch) -> None:
    """A failure between the close and the link must roll the whole operation
    back: a superseded node left closed with no edge/audit is unrecoverable
    (the retry finds no current revision)."""
    svc, stores = rig
    stores.graph.upsert_node(_pref_node("old"))
    stores.graph.upsert_node(_pref_node("new"))
    _fail_edge_write(stores, monkeypatch)

    with pytest.raises(RuntimeError):
        svc.supersede(profile_id=_PROFILE, superseded_node_id="old", successor_node_id="new")

    assert stores.graph.get_node("old") is not None, "the revision must NOT stay closed"
    chain = stores.graph.versions("old")
    assert chain[0].valid_to is None
    assert _supersedes_edges(stores) == []
    page = stores.meta.audit_query(AuditFilter(), Page(offset=0, limit=50))
    assert "supersede" not in [entry.action for entry in page.items]


def test_concurrent_close_between_check_and_link_is_reported_not_false_success(rig, monkeypatch) -> None:
    """A target closed behind the verb's back (between the existence checks and
    the write) must surface as an error, never a 200-style false success."""
    svc, stores = rig
    stores.graph.upsert_node(_pref_node("old"))
    stores.graph.upsert_node(_pref_node("new"))

    original_get_node = stores.graph.get_node

    def get_node_closing_behind_the_verbs_back(node_id: str):
        node = original_get_node(node_id)
        if node_id == "old" and node is not None:
            stores.graph.invalidate("old", valid_to=time.time())
        return node

    monkeypatch.setattr(
        type(stores.graph), "get_node", lambda self, node_id: get_node_closing_behind_the_verbs_back(node_id)
    )

    with pytest.raises(MemoryNotFoundError):
        svc.supersede(profile_id=_PROFILE, superseded_node_id="old", successor_node_id="new")

    assert _supersedes_edges(stores) == [], "no link may be written when nothing was closed"


# ---------------------------------------------------------------- guards


def test_supersede_missing_or_cross_profile_target_is_not_found(rig) -> None:
    svc, stores = rig
    stores.graph.upsert_node(_pref_node("new"))
    stores.graph.upsert_node(_pref_node("foreign", profile="other"))

    with pytest.raises(MemoryNotFoundError):
        svc.supersede(profile_id=_PROFILE, superseded_node_id="ghost", successor_node_id="new")
    with pytest.raises(MemoryNotFoundError):
        svc.supersede(profile_id=_PROFILE, superseded_node_id="new", successor_node_id="foreign")

    assert stores.graph.get_node("new") is not None, "a failed supersede must close nothing"
    assert _supersedes_edges(stores) == []


def test_supersede_rejects_self_reference(rig) -> None:
    svc, stores = rig
    stores.graph.upsert_node(_pref_node("solo"))

    with pytest.raises(ValueError):
        svc.supersede(profile_id=_PROFILE, superseded_node_id="solo", successor_node_id="solo")

    assert stores.graph.get_node("solo") is not None
    assert _supersedes_edges(stores) == []


# ---------------------------------------------------------------- red line


def test_recall_never_emits_supersedes_edges(rig) -> None:
    """The red line: recall/scoring must never supersede anything as a side
    effect — only the explicit verb writes the SUPERSEDES edge."""
    svc, stores = rig
    stores.graph.upsert_node(_pref_node("a"))
    stores.graph.upsert_node(_pref_node("b"))

    svc.recall(profile_id=_PROFILE, query="Editor preference stance")

    assert _supersedes_edges(stores) == []
