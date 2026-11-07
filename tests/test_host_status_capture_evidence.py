"""Host status capture-freshness surface: the ``host status`` verb reports
capture evidence as independent observations — the hook/config structure, the
since-boot ingest acknowledgement, persisted capture recency (host-attributed,
profile-scoped), a later successful replay, and the manual trust step — none
of which alone proves another.

Honesty pins: an idle user is not a failure; unreadable evidence is unknown,
never a guess and never a crash; writing hook config never reports capture as
working; the manual trust step is reported, never performed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import auth_support
import pytest
from _ports import unused_loopback_port

from mnemoseed_local.cli import main
from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.rest_client import DaemonClient
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
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
WINDOW_S = 600.0

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
def _ensure_registered() -> Iterator[None]:
    for registry, cls in _DRIVERS:
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


@pytest.fixture(autouse=True)
def _pin_default_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNEMOSEED_LOCAL_PROFILE_ID", raising=False)


@pytest.fixture(autouse=True)
def _sandbox_host_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every host config root resolves into the sandbox: the status surface
    must never read the machine's real host installs."""
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "opencode-root"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-root"))


def _config_toml(home: Path) -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
    )


@contextmanager
def _daemon(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """The real daemon: isolated config home, spare port, live uvicorn server.

    The app reference stays reachable so a test can reach the boot state the
    status surface reads (the store, the memory service)."""
    home = tmp_path / "daemon-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(_config_toml(home), encoding="utf-8")
    with auth_support.isolated_home(home):
        credential = auth_support.seed_credential(home)
        app = create_app()
        with auth_support.live_server(app, unused_loopback_port()) as base_url:
            yield {"base_url": base_url, "credential": credential, "app": app, "home": home}


def _daemon_client(base_url: str, credential: str) -> DaemonClient:
    return DaemonClient(base_url=base_url, token=credential)


def _seed_hook_capture(
    base_url: str,
    credential: str,
    *,
    host: str,
    session_id: str,
    text: str,
) -> None:
    """One acknowledged hook ingest persisted through the settle drain.

    The client presents the hook actor so the daemon's since-boot ingest
    acknowledgement counter moves exactly as it would for a real hook."""
    client = DaemonClient(base_url=base_url, token=credential, actor="hook")
    acked = client.post(
        "/ingest",
        {
            "host": host,
            "event": "user_prompt",
            "session_id": session_id,
            "profile_id": PROFILE,
            "ts": 1.0,
            "content": {"text": text},
        },
    )
    assert acked.get("status") == "accepted", acked
    settled = client.post("/session/end", {"session_id": session_id, "profile_id": PROFILE})
    assert settled.get("status") == "settled", settled


def _seed_stale_capture(app: Any, *, host: str, age_s: float) -> float:
    """A persisted host-attributed chunk older than the freshness window."""
    import time

    stores = app.state.stores
    ingested_at = time.time() - age_s
    text = "user: a capture from long ago"
    result = stores.embed.embed(text)
    stamp = ChunkStamp(
        chunk_id="stale-evidence",
        profile_id=PROFILE,
        text=text,
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(entities=[], host=host),
        provenance=Provenance(asserted_by="user", session_id="sess-stale", source="manual"),
        ingested_at=ingested_at,
    )
    stores.vector.upsert_chunk(stamp, result.dense, result.sparse)
    return ingested_at


def _status_json(monkeypatch: pytest.MonkeyPatch, capsys: Any, host: str) -> dict[str, Any]:
    assert main(["host", "status", "--host", host, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- additive JSON fields


def test_status_json_gains_the_observation_fields_over_an_empty_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An idle deployment: daemon reachable, store empty. Every observation is
    its own honest state — nothing is reported broken for lacking events."""
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        payload = _status_json(monkeypatch, capsys, "opencode")

        assert payload["host"] == "opencode"
        assert payload["profile_id"] == PROFILE
        structure = payload["structure"]
        assert structure["state"] == "not-installed"
        assert structure["daemon_reachable"] is True
        assert structure["base_url"] == daemon["base_url"]
        assert "path" in structure

        ack = payload["ingest_ack"]
        assert ack["state"] == "not_observed"
        assert ack["count"] == 0
        assert ack["detail"]

        capture = payload["capture_evidence"]
        assert capture["state"] == "none"
        assert capture["last_persisted_at"] is None
        assert capture["window_s"] == WINDOW_S
        assert capture["detail"]

        replay = payload["replay_evidence"]
        assert replay["state"] == "not_observed"
        assert replay["count"] == 0

        trust = payload["trust"]
        assert trust["state"] == "not_required"
        assert trust["detail"]

        assert "broken" not in json.dumps(payload).lower()


def test_human_status_stays_one_line_per_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        assert main(["host", "status", "--host", "opencode"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("host: ")
        assert lines[1].startswith("path: ")
        assert lines[2].startswith("daemon: ")
        labels = [line.split(": ", 1)[0] for line in lines[3:]]
        assert labels == ["ingest ack", "capture", "replay", "trust"]
        assert "capture: none" in lines[4]


# ---------------------------------------------------------------- recency states


def test_recent_capture_requires_an_acknowledged_ingest_that_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An acknowledged hook ingest that the settle drain persisted lands the
    capture state on recent — and the same evidence is invisible to a host
    that never captured (attribution scopes the observation)."""
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        _seed_hook_capture(
            daemon["base_url"],
            daemon["credential"],
            host=HostId.CODEX_CLI.value,
            session_id="sess-fresh",
            text="user: the capture that should read as recent",
        )

        payload = _status_json(monkeypatch, capsys, "codex_cli")
        capture = payload["capture_evidence"]
        assert capture["state"] == "recent"
        assert capture["last_persisted_at"] is not None
        ack = payload["ingest_ack"]
        assert ack["state"] == "observed"
        assert isinstance(ack["count"], int) and ack["count"] >= 1

        other = _status_json(monkeypatch, capsys, "opencode")
        assert other["capture_evidence"]["state"] == "none"


def test_stale_capture_older_than_the_window_is_not_recent_and_not_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        ingested_at = _seed_stale_capture(daemon["app"], host=HostId.CODEX_CLI.value, age_s=3600.0)

        payload = _status_json(monkeypatch, capsys, "codex_cli")
        capture = payload["capture_evidence"]
        assert capture["state"] == "stale"
        assert capture["last_persisted_at"] == pytest.approx(ingested_at)
        assert "broken" not in json.dumps(payload).lower()
        # the observations are independent: no hook ingest was acknowledged
        assert payload["ingest_ack"]["state"] == "not_observed"


# ---------------------------------------------------------------- unreadable evidence


def test_unreadable_store_reports_unknown_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])

        class _UnreadableVector:
            def list_chunks(self, *_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("store unreadable")

        class _UnreadableStores:
            vector = _UnreadableVector()

        monkeypatch.setattr(daemon["app"].state.memory, "_stores", _UnreadableStores())

        payload = _status_json(monkeypatch, capsys, "codex_cli")
        capture = payload["capture_evidence"]
        assert capture["state"] == "unknown"
        assert capture["last_persisted_at"] is None
        assert capture["detail"]

        capsys.readouterr()
        assert main(["host", "status", "--host", "codex_cli"]) == 0
        assert "capture: unknown" in capsys.readouterr().out


def test_unreachable_daemon_leaves_every_remote_observation_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "daemon-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(_config_toml(home), encoding="utf-8")
    with auth_support.isolated_home(home):
        auth_support.seed_credential(home)
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", f"http://127.0.0.1:{unused_loopback_port()}")

        payload = _status_json(monkeypatch, capsys, "opencode")
        assert payload["structure"]["daemon_reachable"] is False
        assert payload["ingest_ack"]["state"] == "unknown"
        assert payload["ingest_ack"]["count"] is None
        assert payload["capture_evidence"]["state"] == "unknown"
        assert payload["replay_evidence"]["state"] == "unknown"


# ---------------------------------------------------------------- trust and reinstall


def test_codex_trust_is_its_own_sub_line_and_never_automated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        assert main(["host", "install", "--host", "codex_cli"]) == 0
        capsys.readouterr()

        payload = _status_json(monkeypatch, capsys, "codex_cli")
        trust = payload["trust"]
        assert trust["state"] == "unverified"
        assert "/hooks" in trust["detail"]
        # the structural line keeps its own vocabulary, unchanged
        assert payload["structure"]["state"].startswith("installed, trust unverified")

        capsys.readouterr()
        assert main(["host", "status", "--host", "codex_cli"]) == 0
        out = capsys.readouterr().out
        assert "trust: unverified (review and approve the hooks in Codex via /hooks)" in out


def test_reinstall_writes_config_but_never_reports_capture_working(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A complete canonical install with zero persisted capture evidence: the
    capture state is none — config alone is never a capture claim."""
    opencode_root = tmp_path / "opencode-root"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(opencode_root))
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        assert main(["host", "install", "--host", "opencode"]) == 0
        capsys.readouterr()

        payload = _status_json(monkeypatch, capsys, "opencode")
        assert payload["structure"]["state"] == "match"
        assert payload["capture_evidence"]["state"] == "none"
        assert payload["ingest_ack"]["state"] == "not_observed"
        assert "working" not in json.dumps(payload).lower()


def test_partial_codex_structure_leaves_trust_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        assert main(["host", "install", "--host", "codex_cli"]) == 0
        capsys.readouterr()
        hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
        hooks["hooks"].pop("Stop")
        (codex_home / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")

        payload = _status_json(monkeypatch, capsys, "codex_cli")
        trust = payload["trust"]
        assert trust["state"] == "unknown"
        assert "partial" in trust["detail"]


def test_uninstalled_codex_has_no_pending_trust_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        payload = _status_json(monkeypatch, capsys, "codex_cli")
        trust = payload["trust"]
        assert trust["state"] == "not_required"
        assert "no mnemoseed hooks registered" in trust["detail"]


def test_evidence_query_scopes_to_the_selected_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A capture under another profile is invisible to this profile's status:
    the evidence read is profile-scoped, never guessed across namespaces."""
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", "other-profile")
        client = _daemon_client(daemon["base_url"], daemon["credential"])
        acked = client.post(
            "/ingest",
            {
                "host": HostId.CODEX_CLI.value,
                "event": "user_prompt",
                "session_id": "sess-other-profile",
                "profile_id": "other-profile",
                "ts": 1.0,
                "content": {"text": "user: captured under another profile"},
            },
        )
        assert acked.get("status") == "accepted", acked
        client.post("/session/end", {"session_id": "sess-other-profile", "profile_id": "other-profile"})

        payload = _status_json(monkeypatch, capsys, "codex_cli")
        assert payload["profile_id"] == "other-profile"
        assert payload["capture_evidence"]["state"] == "recent"


def test_stale_scan_reads_past_newer_foreign_captures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Newer chunks from other sources must not hide older evidence for the
    selected host: the read walks recency-desc until the host attribution is
    found or the store is exhausted."""
    with _daemon(tmp_path) as daemon:
        monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", daemon["base_url"])
        import time

        stores = daemon["app"].state.stores
        fresh_text = "user: a newer capture from another host"
        fresh = stores.embed.embed(fresh_text)
        stores.vector.upsert_chunk(
            ChunkStamp(
                chunk_id="fresh-foreign",
                profile_id=PROFILE,
                text=fresh_text,
                cognitive_tier=CognitiveTier.TIER_1,
                model_id="test-model",
                cues=Cues(entities=[], host=HostId.CLAUDE_CODE.value),
                provenance=Provenance(asserted_by="user", session_id="sess-fresh", source="manual"),
                ingested_at=time.time(),
            ),
            fresh.dense,
            fresh.sparse,
        )
        ingested_at = _seed_stale_capture(daemon["app"], host=HostId.CODEX_CLI.value, age_s=3600.0)

        payload = _status_json(monkeypatch, capsys, "codex_cli")
        capture = payload["capture_evidence"]
        assert capture["state"] == "stale"
        assert capture["last_persisted_at"] == pytest.approx(ingested_at)
