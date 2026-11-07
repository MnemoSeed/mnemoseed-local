"""Project-context recovery (#321): the /session/recent recovery contract.

``caller_cwd`` is the caller's workspace context as a SEPARATE input from the
raw prompt: the daemon verifies it against the project binding registry
(#311) BEFORE any history is selected. A bound workspace recovers the
project's intended root conversation without a literal repository name;
child sessions and newer unrelated projects never leak; an ambiguous shared
workspace (one profile deliberately bound to several roots) returns an
explicit unresolved result; an explicit repo name in the raw prompt wins
over the workspace hint. An unbound workspace keeps the documented legacy
anchor path and never falls back to a broad profile-global replay.

Isolation: temp app home (binding registry lives there), temp storage, no
reserved ports, the installed runtime untouched.
"""

from __future__ import annotations

from pathlib import Path

import auth_support
import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.hosts.common import project_binding as pb
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

# test_registry.py clears the driver registries wholesale; any daemon-booting
# module ordered after it must defensively re-register (test_preset_embedded
# precedent).
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


@pytest.fixture
def home(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The app home the daemon reads its binding registry from."""
    home = config_path.parent
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    return home


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True)
    return path


def _ingest(
    client: TestClient,
    session_id: str,
    text: str,
    *,
    profile: str,
    session_parent_id: str | None = None,
) -> None:
    body: dict = {
        "host": HostId.OPENCODE.value,
        "event": "user_prompt",
        "session_id": session_id,
        "profile_id": profile,
        "ts": 1.0,
        "content": {"text": text},
    }
    if session_parent_id is not None:
        body["session_parent_id"] = session_parent_id
    response = client.post("/ingest", json=body)
    assert response.status_code == 202, response.text


def _end(client: TestClient, session_id: str, profile: str) -> None:
    response = client.post("/session/end", json={"session_id": session_id, "profile_id": profile})
    assert response.status_code == 200, response.text


def _recover(
    client: TestClient, profile: str, caller_cwd: str | None, resume_query: str | None = None
) -> dict:
    body: dict = {"profile_id": profile, "caller_cwd": caller_cwd}
    if resume_query is not None:
        body["resume_query"] = resume_query
    response = client.post("/session/recent", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _session_ids(payload: dict) -> list[str]:
    return [session["session_id"] for session in payload["sessions"]]


def _verbatim(payload: dict) -> str:
    return "\n".join(chunk["text"] for session in payload["sessions"] for chunk in session["chunks"])


# ------------------------------------------------- bound recovery, unnamed


def test_bound_workspace_recovers_intended_root_conversation_without_a_name(
    config_path: Path, home: Path, tmp_path: Path
) -> None:
    """Continuation with no literal repository name: the trusted binding
    selects the project, and the newest confirmed ROOT session is recovered —
    child sessions and newer unrelated projects stay out."""
    root_alpha = _mkdir(tmp_path / "projects" / "alpha-repo")
    root_beta = _mkdir(tmp_path / "projects" / "beta-repo")
    pb.bind(home, root_alpha, "alpha")
    pb.bind(home, root_beta, "beta")
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "root-alpha-old", "alpha sentinel first conversation", profile="alpha")
        _end(client, "root-alpha-old", "alpha")
        _ingest(client, "root-alpha-new", "alpha sentinel latest conversation", profile="alpha")
        _end(client, "root-alpha-new", "alpha")
        # the newest session overall is a child of the intended root
        _ingest(
            client,
            "child-alpha",
            "alpha delegated slice",
            profile="alpha",
            session_parent_id="root-alpha-new",
        )
        _end(client, "child-alpha", "alpha")
        # newer unrelated projects: one bound to another profile, one unbound
        _ingest(client, "root-beta", "beta sentinel newest unrelated project", profile="beta")
        _end(client, "root-beta", "beta")
        _ingest(client, "root-unbound", "unbound sentinel unrelated default text", profile="default")
        _end(client, "root-unbound", "default")

        payload = _recover(client, "alpha", str(root_alpha / "sub"))
        assert payload["selection"] == "matched"
        assert payload["selection_reason"] == "bound project recovery"
        assert _session_ids(payload) == ["root-alpha-new"]
        blob = _verbatim(payload)
        assert "alpha sentinel latest conversation" in blob
        assert "delegated slice" not in blob
        assert "beta sentinel" not in blob
        assert "unbound sentinel" not in blob


def test_child_session_with_matching_prompt_stays_invisible_to_recovery(
    config_path: Path, home: Path, tmp_path: Path
) -> None:
    """A child session whose USER text matches the anchors never replaces the
    project's root conversation in the recovered context."""
    root = _mkdir(tmp_path / "projects" / "alpha-repo")
    pb.bind(home, root, "alpha")
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "root-alpha", "continue mnemoseed-local development", profile="alpha")
        _end(client, "root-alpha", "alpha")
        _ingest(
            client,
            "child-alpha",
            "delegated context: continue mnemoseed-local development",
            profile="alpha",
            session_parent_id="root-alpha",
        )
        _end(client, "child-alpha", "alpha")

        payload = _recover(client, "alpha", str(root), "continue mnemoseed-local development")
        assert payload["selection"] == "matched"
        assert _session_ids(payload) == ["root-alpha"]
        assert "delegated context" not in _verbatim(payload)


# ------------------------------------------------------- explicit unresolved


def test_ambiguous_shared_workspace_is_explicit_unresolved(
    config_path: Path, home: Path, tmp_path: Path
) -> None:
    """One profile deliberately bound to several roots: without a prompt that
    names exactly one of them the workspace context alone is ambiguous."""
    root_one = _mkdir(tmp_path / "projects" / "gamma-repo")
    root_two = _mkdir(tmp_path / "projects" / "delta-repo")
    pb.bind(home, root_one, "shared")
    pb.bind(home, root_two, "shared")
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "root-gamma", "gamma sentinel conversation", profile="shared")
        _end(client, "root-gamma", "shared")
        _ingest(client, "root-delta", "delta sentinel conversation", profile="shared")
        _end(client, "root-delta", "shared")

        for resume_query in (None, "continue"):
            payload = _recover(client, "shared", str(root_one), resume_query)
            assert payload["selection"] == "unresolved"
            assert "ambiguous shared workspace" in payload["selection_reason"]
            assert payload["sessions"] == []
            assert payload["guidance"]

        # a prompt naming BOTH bound roots disambiguates nothing: still an
        # explicit unresolved, never matched sessions of several projects
        both = _recover(client, "shared", str(root_one), "continue the gamma-repo and delta-repo work")
        assert both["selection"] == "unresolved"
        assert "ambiguous shared workspace" in both["selection_reason"]
        assert both["sessions"] == []


def test_user_scope_wins_over_workspace_hint_in_shared_workspace(
    config_path: Path, home: Path, tmp_path: Path
) -> None:
    """The raw prompt's literal repo name picks the project even when the
    caller stands in the OTHER bound root's workspace."""
    root_gamma = _mkdir(tmp_path / "projects" / "gamma-repo")
    root_delta = _mkdir(tmp_path / "projects" / "delta-repo")
    pb.bind(home, root_gamma, "shared")
    pb.bind(home, root_delta, "shared")
    with auth_support.boot(config_path.parent) as client:
        # gamma's session is newer and its text never mentions delta-repo
        _ingest(client, "root-gamma", "gamma sentinel newest conversation", profile="shared")
        _end(client, "root-gamma", "shared")
        _ingest(client, "root-delta", "continue the delta-repo nightly jobs", profile="shared")
        _end(client, "root-delta", "shared")

        payload = _recover(client, "shared", str(root_gamma), "continue the delta-repo nightly jobs")
        assert payload["selection"] == "matched"
        assert payload["selection_reason"] == "user scope named the project root"
        assert _session_ids(payload) == ["root-delta"]
        assert "gamma sentinel" not in _verbatim(payload)


def test_workspace_bound_to_another_profile_is_explicit_unresolved(
    config_path: Path, home: Path, tmp_path: Path
) -> None:
    """A workspace verified inside another profile's project root never
    silently serves the requested profile's history instead — in either
    direction."""
    root_alpha = _mkdir(tmp_path / "projects" / "alpha-repo")
    root_beta = _mkdir(tmp_path / "projects" / "beta-repo")
    pb.bind(home, root_alpha, "alpha")
    pb.bind(home, root_beta, "beta")
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "root-alpha", "alpha sentinel conversation", profile="alpha")
        _end(client, "root-alpha", "alpha")
        _ingest(client, "root-beta", "beta sentinel conversation", profile="beta")
        _end(client, "root-beta", "beta")

        for profile, other_root, other_profile in (
            ("alpha", root_beta, "beta"),
            ("beta", root_alpha, "alpha"),
        ):
            payload = _recover(client, profile, str(other_root))
            assert payload["selection"] == "unresolved"
            assert other_profile in payload["selection_reason"]
            assert payload["sessions"] == []
            assert other_profile not in _verbatim(payload)


def test_blank_caller_cwd_is_explicit_unresolved(config_path: Path, home: Path, tmp_path: Path) -> None:
    """A blank workspace directory cannot be verified and is never silently
    replaced by a daemon-derived location — even a bound project's registry
    stays unconsulted."""
    root = _mkdir(tmp_path / "projects" / "alpha-repo")
    pb.bind(home, root, "alpha")
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "root-alpha", "alpha sentinel conversation", profile="alpha")
        _end(client, "root-alpha", "alpha")

        payload = _recover(client, "alpha", "   ")
        assert payload["selection"] == "unresolved"
        assert payload["selection_reason"] == "caller workspace carries no directory"
        assert payload["sessions"] == []


# ------------------------------------------------------- unbound workspaces


def test_unbound_workspace_keeps_legacy_anchor_path_and_never_replays_broadly(
    config_path: Path, home: Path, tmp_path: Path
) -> None:
    """No trusted binding: with a raw prompt the documented anchor path still
    applies; without one the recovery result is an explicit unresolved — the
    profile-global replay stays out of the recovery path."""
    unbound = _mkdir(tmp_path / "elsewhere")
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "root-alpha", "continue mnemoseed-local development", profile="alpha")
        _end(client, "root-alpha", "alpha")

        matched = _recover(client, "alpha", str(unbound), "continue mnemoseed-local development")
        assert matched["selection"] == "matched"
        assert _session_ids(matched) == ["root-alpha"]

        unresolved = _recover(client, "alpha", str(unbound))
        assert unresolved["selection"] == "unresolved"
        assert "not a bound project" in unresolved["selection_reason"]
        assert unresolved["sessions"] == []

        anchorless = _recover(client, "alpha", str(unbound), "what did we do so far?")
        assert anchorless["selection"] == "unresolved"
        assert anchorless["sessions"] == []


def test_unreadable_registry_is_explicit_unresolved(config_path: Path, home: Path, tmp_path: Path) -> None:
    """A corrupt registry fails the recovery read closed with an explicit
    reason — never a 500, never a silent fallback to broad history."""
    root = _mkdir(tmp_path / "projects" / "alpha-repo")
    pb.bind(home, root, "alpha")
    (home / pb.BINDINGS_FILENAME).write_text("{not json", encoding="utf-8")
    with auth_support.boot(config_path.parent) as client:
        payload = _recover(client, "alpha", str(root), "continue")
        assert payload["selection"] == "unresolved"
        assert "unreadable" in payload["selection_reason"]
        assert payload["sessions"] == []


def test_sessionless_pins_never_enter_the_recovered_conversation(
    config_path: Path, home: Path, tmp_path: Path
) -> None:
    """Chunks without a session label (explicit pins) carry no conversation
    identity and are skipped by the recovery selection — safe handling of
    missing session metadata."""
    root = _mkdir(tmp_path / "projects" / "alpha-repo")
    pb.bind(home, root, "alpha")
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "root-alpha", "alpha sentinel conversation", profile="alpha")
        _end(client, "root-alpha", "alpha")
        pinned = client.post(
            "/memory/remember",
            json={"profile_id": "alpha", "text": "pinned sentinel standalone fact"},
        )
        assert pinned.status_code == 200, pinned.text

        payload = _recover(client, "alpha", str(root))
        assert payload["selection"] == "matched"
        assert _session_ids(payload) == ["root-alpha"]
        assert "pinned sentinel standalone fact" not in _verbatim(payload)


# ------------------------------------------------------- version compatibility


def test_callers_without_caller_cwd_keep_the_documented_behaviors(config_path: Path, home: Path) -> None:
    """Pre-recovery callers (no caller_cwd field) must see exactly the legacy
    dispatch: unscoped replay, anchor scoping, and the automatic-path
    fail-closed guard."""
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "root-x", "continue mnemoseed-local development", profile="default")
        _end(client, "root-x", "default")

        bare = client.post("/session/recent", json={"profile_id": "default"})
        assert bare.status_code == 200
        assert bare.json()["selection"] == "unscoped"

        scoped = client.post(
            "/session/recent",
            json={"profile_id": "default", "resume_query": "continue mnemoseed-local development"},
        )
        assert scoped.status_code == 200
        assert scoped.json()["selection"] == "matched"

        guarded = client.post(
            "/session/recent",
            json={
                "profile_id": "default",
                "exclude_session_id": "sess-current",
                "self_session_id": "sess-current",
            },
        )
        assert guarded.status_code == 200
        guarded_payload = guarded.json()
        assert guarded_payload["selection"] == "unresolved"
        assert guarded_payload["sessions"] == []
