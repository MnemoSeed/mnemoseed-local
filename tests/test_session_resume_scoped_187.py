"""Issue #187 RED: scoped session resume must hard-filter cross-project
and child-session tails before verbatim reaches the model.

A prompt naming ``mnemoseed-local`` must never return or inject an
unmatched ``mnemoseed-orchestrator`` tail, even when the latter is newer,
longer, and shares the same host CWD. Child/subagent sessions must stay
invisible to a root caller. Ambiguous prompts resolve to explicit
unresolved metadata with zero injectable tails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.daemon.memory import _extract_resume_anchors
from mnemoseed_local.schema.turn import HostId
from mnemoseed_local.storage.drivers import (
    bge_m3_onnx,
    lancedb_embedded,
    sqlite_graph,
    sqlite_meta,
    synthetic_embedder,
)
from mnemoseed_local.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

PROFILE = "default"

_DRIVERS = (
    (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
    (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
    (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
    (EMBED_DRIVERS, bge_m3_onnx.BgeM3OnnxEmbedder),
    (EMBED_DRIVERS, synthetic_embedder.SyntheticEmbedder),
)


@pytest.fixture(autouse=True)
def _ensure_registered():
    for registry, cls in _DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


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


def _ingest(
    client: TestClient,
    session_id: str,
    ts: float,
    text: str,
    session_parent_id: str | None = None,
) -> None:
    body: dict = {
        "host": HostId.OPENCODE.value,
        "event": "user_prompt",
        "session_id": session_id,
        "profile_id": PROFILE,
        "ts": ts,
        "content": {"text": text},
    }
    if session_parent_id is not None:
        body["session_parent_id"] = session_parent_id
    response = client.post("/ingest", json=body)
    assert response.status_code == 202, response.text


def _end(client: TestClient, session_id: str) -> None:
    response = client.post("/session/end", json={"session_id": session_id, "profile_id": PROFILE})
    assert response.status_code == 200, response.text


def test_anchors_extract_bare_kebab_repo_name() -> None:
    assert "mnemoseed-local" in _extract_resume_anchors("continue mnemoseed-local development")


def test_anchors_empty_for_ambiguous_prompt() -> None:
    assert _extract_resume_anchors("what did we do so far?") == []


def test_scoped_query_returns_only_matching_root(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        _ingest(client, "root-local", 1.0, "continue mnemoseed-local development")
        _end(client, "root-local")
        _ingest(client, "root-orch", 2.0, "continue mnemoseed-orchestrator work")
        _ingest(client, "root-orch", 3.0, "orchestrator slice two")
        _ingest(client, "root-orch", 4.0, "orchestrator slice three")
        _end(client, "root-orch")

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "resume_query": "continue mnemoseed-local development"},
        )
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["selection"] == "matched"
        ids = [s["session_id"] for s in payload["sessions"]]
        assert ids == ["root-local"]
        blob = "\n".join(c["text"] for s in payload["sessions"] for c in s["chunks"])
        assert "mnemoseed-orchestrator" not in blob


def test_scoped_query_excludes_child_session(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        _ingest(client, "root-local", 1.0, "continue mnemoseed-local development")
        _end(client, "root-local")
        _ingest(
            client,
            "child-local",
            2.0,
            "delegated context: continue mnemoseed-local development",
            session_parent_id="root-local",
        )
        _end(client, "child-local")

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "resume_query": "continue mnemoseed-local development"},
        )
        assert body.status_code == 200, body.text
        payload = body.json()
        ids = [s["session_id"] for s in payload["sessions"]]
        assert "child-local" not in ids
        assert ids == ["root-local"]


def test_ambiguous_prompt_is_unresolved_with_no_tails(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        _ingest(client, "root-local", 1.0, "continue mnemoseed-local development")
        _end(client, "root-local")

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "resume_query": "what did we do so far?"},
        )
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["selection"] == "unresolved"
        assert payload["sessions"] == []


def test_assistant_only_mention_does_not_match(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        _ingest(client, "root-generic", 1.0, "doing generic refactoring work")
        client.post(
            "/ingest",
            json={
                "host": HostId.OPENCODE.value,
                "event": "assistant_message",
                "session_id": "root-generic",
                "profile_id": PROFILE,
                "ts": 2.0,
                "content": {"text": "I compared mnemoseed-local internals here"},
            },
        )
        _end(client, "root-generic")

        body = client.post(
            "/session/recent",
            json={"profile_id": PROFILE, "resume_query": "continue mnemoseed-local development"},
        )
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["selection"] == "unresolved"
        assert payload["sessions"] == []


def test_legacy_path_reports_unscoped_selection(config_path: Path) -> None:
    with TestClient(create_app()) as client:
        _ingest(client, "root-local", 1.0, "continue mnemoseed-local development")
        _end(client, "root-local")

        body = client.post("/session/recent", json={"profile_id": PROFILE})
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["selection"] == "unscoped"


def test_single_identity_field_without_query_stays_unscoped(config_path: Path) -> None:
    """#187: the compatibility discriminator requires BOTH automatic-path identity
    fields. Existing direct diagnostics send only ONE (either exclude_session_id or
    self_session_id); a bare single field must keep the legacy unscoped diagnostic
    behavior."""
    with TestClient(create_app()) as client:
        _ingest(client, "root-x", 1.0, "diagnostic tail")
        _end(client, "root-x")

        for body in [
            {"profile_id": PROFILE, "exclude_session_id": "sess-cur"},
            {"profile_id": PROFILE, "self_session_id": "sess-cur"},
        ]:
            response = client.post("/session/recent", json=body)
            assert response.status_code == 200, response.text
            assert response.json()["selection"] == "unscoped"


def test_dual_identity_fields_without_query_fails_closed(config_path: Path) -> None:
    """#187 mixed-version compat: a pre-#187 OpenCode hook POSTs
    /session/recent with BOTH automatic-path identity fields
    (self_session_id == exclude_session_id == current session) and no resume_query,
    then ignores the selection field and injects returned sessions. That is an
    automatic path — it must NEVER silently fall back to profile-global replay.
    Fail closed: explicit unresolved metadata, zero sessions, no cross-project
    verbatim."""
    with TestClient(create_app()) as client:
        _ingest(client, "root-other", 1.0, "other project tail that must NOT replay")
        _end(client, "root-other")

        body = client.post(
            "/session/recent",
            json={
                "profile_id": PROFILE,
                "exclude_session_id": "sess-current",
                "self_session_id": "sess-current",
            },
        )
        assert body.status_code == 200, body.text
        payload = body.json()
        assert payload["selection"] == "unresolved"
        assert payload["sessions"] == []
        blob = "\n".join(c["text"] for s in payload["sessions"] for c in s["chunks"])
        assert "must NOT replay" not in blob


def test_session_parent_id_column_migrates_and_legacy_rows_stay_null(tmp_path: Path) -> None:
    """Pre-lineage tables gain session_parent_id; legacy rows stay NULL."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent / "contract"))
    from _support import DIMENSION, make_stamp  # noqa: E402
    from lancedb import connect  # noqa: E402

    from mnemoseed_local.storage.drivers.lancedb_embedded import (  # noqa: E402
        LanceDbEmbeddedStore,
    )
    from mnemoseed_local.storage.drivers.synthetic_embedder import (  # noqa: E402
        SyntheticEmbedder,
    )

    uri = tmp_path / "old.lance"
    db = connect(str(uri))
    probe = LanceDbEmbeddedStore(uri=tmp_path / "probe.lance", dimensions=DIMENSION)
    embedder = SyntheticEmbedder(dimension=DIMENSION)
    old_schema = probe._schema().remove(probe._schema().get_field_index("session_parent_id"))
    db.create_table("chunks", schema=old_schema)
    legacy = make_stamp("legacy-1", "pre-lineage turn")
    emb = embedder.embed(legacy.text)
    row = {
        key: value
        for key, value in probe._to_row(legacy, list(emb.dense), None).items()
        if key != "session_parent_id"
    }
    db.open_table("chunks").add([row])

    store = LanceDbEmbeddedStore(uri=uri, dimensions=DIMENSION)
    assert "session_parent_id" in store._table.schema.names

    got = store.get_chunk("legacy-1")
    assert got is not None
    assert got.session_parent_id is None

    child = make_stamp("child-1", "delegated turn")
    child.session_parent_id = "root-1"
    result = embedder.embed(child.text)
    store.upsert_chunk(child, result.dense, result.sparse)
    stored = store.get_chunk("child-1")
    assert stored is not None
    assert stored.session_parent_id == "root-1"
