"""Project binding across the host adapters: one shared gate decides, per
hook event, whether the launcher-pinned profile may act on the event's
project root.

The profile VALUE always comes from ``MNEMOSEED_LOCAL_PROFILE_ID`` (the
controlled launch); the binding registry only verifies consistency and
refuses what would silently switch namespaces: a cwd bound to another
profile, a cwd outside every root bound to the active profile, an unreadable
registry, or a controlled event whose host cannot supply the cwd. The
opencode plugin runs the same verification at its launch root; the MCP
gateway's namespace arrives through the controlled process tree's
environment, proved on a real gateway subprocess.

Unbound/default entries keep the legacy flow: no registry consultation, the
default profile, an "unbound" debug line, and no data migration.

Isolation: temp ``MNEMOSEED_LOCAL_HOME``, spare loopback ports, the real
``_hook-event`` transformer subprocess; the reserved daemon ports and the
installed runtime are never touched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import auth_support
import pytest
from _ports import unused_loopback_port

from mnemoseed_local.daemon.app import create_app
from mnemoseed_local.hosts.claude_code import events as cc_events
from mnemoseed_local.hosts.codex_cli import events as cx_events
from mnemoseed_local.hosts.codex_cli import install as cx_install
from mnemoseed_local.hosts.common import project_binding as pb
from mnemoseed_local.hosts.common.daemon import RESTART_INSTRUCTION, binding_gate
from mnemoseed_local.hosts.zcode import events as zc_events
from mnemoseed_local.hosts.zcode import install as zc_install
from mnemoseed_local.rest_client import DaemonClient, DaemonRestError
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

WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows roots compare case-insensitively")

ALPHA = "alpha"
BETA = "beta"

CC_SESSION = "cc-bind-alpha"
CX_SESSION = "cx-bind-beta"
CC_PROMPT_A = "项目A哨兵：FlakyClockHelper 已改为注入时钟"
CC_REPLY_A = "FlakyClockHelper 用例已稳定，复跑三次全绿。"
CX_PROMPT_B = "项目B哨兵：WatermarkReplay 的水位由 daemon 去重"

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
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MNEMOSEED_LOCAL_DEBUG",
        "MNEMOSEED_LOCAL_PROFILE_ID",
        "MNEMOSEED_LOCAL_BASEURL",
        "MNEMOSEED_LOCAL_HOME",
        "MNEMOSEED_LOCAL_DATA_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


def _config_toml(home: Path, port: int) -> str:
    return (
        f'baseurl = "http://127.0.0.1:{port}"\n'
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(home / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(home / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(home / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(home / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[capture]\nauto_recall = true\n"
    )


@pytest.fixture
def daemon(tmp_path: Path) -> Iterator[tuple[str, str, Path]]:
    """The real daemon on a spare port; one isolated home is daemon home,
    binding registry home, and hook-subprocess home at once."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    port = unused_loopback_port()
    (home / "config.toml").write_text(_config_toml(home, port), encoding="utf-8")
    with auth_support.isolated_home(home):
        credential = auth_support.seed_credential(home)
        with auth_support.live_server(create_app(), port) as base_url:
            yield base_url, credential, home


# ---------------------------------------------------------------- helpers


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True)
    return path


def _client(base_url: str, credential: str) -> DaemonClient:
    return DaemonClient(base_url=base_url, token=credential)


def _export(client: DaemonClient, profile: str) -> list[dict[str, Any]]:
    reply = client.post("/memory/export", {"profile_id": profile})
    assert isinstance(reply, dict) and "chunks" in reply, reply
    return list(reply["chunks"])


def _flush(client: DaemonClient, session_id: str, profile: str) -> None:
    reply = client.post("/flush", {"session_id": session_id, "profile_id": profile})
    assert reply.get("status") == "flushed", reply


def _hook_env(
    base_url: str, home: Path, *, profile: str | None = None, debug: bool = False
) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNEMOSEED_LOCAL_")}
    env["MNEMOSEED_LOCAL_BASEURL"] = base_url
    env["MNEMOSEED_LOCAL_HOME"] = str(home)
    if profile is not None:
        env["MNEMOSEED_LOCAL_PROFILE_ID"] = profile
    if debug:
        env["MNEMOSEED_LOCAL_DEBUG"] = "1"
    return env


def _fire_hook(
    host: str,
    payload: dict[str, Any],
    base_url: str,
    home: Path,
    *,
    profile: str | None = None,
    debug: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """One hook stdin event through the real registered transformer process."""
    return subprocess.run(
        [sys.executable, "-m", "mnemoseed_local.cli", "_hook-event", "--host", host],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        timeout=120,
        env=_hook_env(base_url, home, profile=profile, debug=debug),
        shell=False,
    )


def _record_posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post(self: DaemonClient, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        posts.append((path, body or {}))
        return {}

    monkeypatch.setattr(DaemonClient, "post", fake_post)
    return posts


def _stop_payload(**fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": CC_SESSION,
        "hook_event_name": "Stop",
        "transcript_path": "",
        "stop_hook_active": False,
        "last_assistant_message": CC_REPLY_A,
    }
    payload.update(fields)
    return payload


# ---------------------------------------------------------------- shared gate matrix


@pytest.fixture
def gate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(home))
    return home


def test_gate_without_env_is_the_legacy_unbound_entry_and_never_reads_the_registry(
    gate_home: Path,
) -> None:
    pb.bindings_path(gate_home).write_bytes(b"{ broken ")
    decision = binding_gate("G:/dev/anywhere")
    assert decision.mode == "legacy"
    assert decision.profile == "default"
    assert decision.rejected is None


def test_gate_allows_the_bound_root_under_its_own_profile(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    decision = binding_gate(str(root))
    assert decision.mode == "bound"
    assert decision.profile == ALPHA
    assert decision.bound_roots == (root,)
    assert decision.rejected is None


def test_gate_rejects_a_profile_env_that_contradicts_the_binding(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", BETA)
    decision = binding_gate(str(root))
    assert decision.rejected is not None
    assert ALPHA in decision.rejected and BETA in decision.rejected
    assert "project run" in decision.rejected
    assert RESTART_INSTRUCTION in decision.rejected


def test_gate_rejects_a_cwd_outside_every_bound_root(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    elsewhere = _mkdir(tmp_path / "elsewhere")
    pb.bind(gate_home, root, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    decision = binding_gate(str(elsewhere))
    assert decision.rejected is not None
    assert str(elsewhere) in decision.rejected
    assert str(root) in decision.rejected


def test_gate_rejects_a_same_name_directory_in_a_different_parent(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound = pb.canonical_root(_mkdir(tmp_path / "one" / "proj"))
    namesake = pb.canonical_root(_mkdir(tmp_path / "two" / "proj"))
    pb.bind(gate_home, bound, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    decision = binding_gate(str(namesake))
    assert decision.rejected is not None


def test_gate_allows_a_shared_root_and_carries_the_sharing_roots(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "main"))
    worktree = pb.canonical_root(_mkdir(tmp_path / "wt"))
    pb.bind(gate_home, root, ALPHA)
    pb.bind(gate_home, worktree, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    decision = binding_gate(str(worktree))
    assert decision.mode == "bound"
    assert set(decision.bound_roots) == {root, worktree}
    assert decision.rejected is None


def test_gate_rejects_a_worktree_bound_to_another_profile(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "main"))
    worktree = pb.canonical_root(_mkdir(tmp_path / "wt"))
    pb.bind(gate_home, root, ALPHA)
    pb.bind(gate_home, worktree, BETA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    decision = binding_gate(str(worktree))
    assert decision.rejected is not None
    assert BETA in decision.rejected


def test_gate_rejects_a_moved_root_until_explicitly_rebound(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, ALPHA)
    moved = tmp_path / "moved"
    root.rename(moved)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    decision = binding_gate(str(pb.canonical_root(moved)))
    assert decision.rejected is not None


@WIN_ONLY
def test_gate_windows_case_variant_cwd_matches_the_bound_root(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    variant = str(root).upper()
    if variant == str(root):
        pytest.skip("test root has no case-variant spelling")
    decision = binding_gate(variant)
    assert decision.mode == "bound"
    assert decision.rejected is None


def test_gate_allows_a_subdirectory_of_the_bound_root(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    sub = _mkdir(root / "sub")
    pb.bind(gate_home, root, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    decision = binding_gate(str(sub))
    assert decision.mode == "bound"
    assert decision.rejected is None


def test_gate_rejects_a_controlled_event_that_carries_no_cwd(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    for cwd in (None, "", "   "):
        decision = binding_gate(cwd)
        assert decision.rejected is not None, cwd


def test_gate_allows_an_explicit_env_selection_on_an_unbound_root(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = _mkdir(tmp_path / "elsewhere")
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    decision = binding_gate(str(elsewhere))
    assert decision.mode == "manual"
    assert decision.profile == ALPHA
    assert decision.rejected is None


def test_gate_rejects_a_malformed_registry_under_a_controlled_launch(
    gate_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = pb.bindings_path(gate_home)
    path.write_bytes(b"{ broken ")
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    decision = binding_gate("G:/dev/anywhere")
    assert decision.rejected is not None
    assert "registry" in decision.rejected
    assert RESTART_INSTRUCTION in decision.rejected
    assert path.read_bytes() == b"{ broken "


# ---------------------------------------------------------------- transformer enforcement (in-process)


def test_claude_hook_rejects_a_mismatched_cwd_without_a_daemon_write(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    posts = _record_posts(monkeypatch)
    payload = _stop_payload(cwd=str(tmp_path / "elsewhere"))
    assert cc_events.transform_event(json.dumps(payload)) == 0
    assert posts == []
    err = capsys.readouterr().err
    assert "event dropped" in err
    assert "project run" in err


def test_zcode_hook_rejects_a_mismatched_cwd_without_a_daemon_write(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, ALPHA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    posts = _record_posts(monkeypatch)
    payload = _stop_payload(cwd=str(tmp_path / "elsewhere"))
    assert zc_events.transform_event(json.dumps(payload)) == 0
    assert posts == []
    assert "event dropped" in capsys.readouterr().err


def test_codex_hook_rejects_a_cwd_bound_to_another_profile_without_a_daemon_write(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = pb.canonical_root(_mkdir(tmp_path / "proj"))
    pb.bind(gate_home, root, BETA)
    monkeypatch.setenv("MNEMOSEED_LOCAL_PROFILE_ID", ALPHA)
    posts = _record_posts(monkeypatch)
    payload = {
        "session_id": CX_SESSION,
        "hook_event_name": "UserPromptSubmit",
        "transcript_path": "",
        "cwd": str(root),
        "prompt": CX_PROMPT_B,
    }
    assert cx_events.transform_event(json.dumps(payload)) == 0
    assert posts == []
    err = capsys.readouterr().err
    assert "event dropped" in err
    assert BETA in err and ALPHA in err


def test_legacy_hook_flow_is_unchanged_and_renders_unbound(
    gate_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = pb.bindings_path(gate_home)
    path.write_bytes(b"{ broken ")
    monkeypatch.setenv("MNEMOSEED_LOCAL_DEBUG", "1")
    posts = _record_posts(monkeypatch)
    payload = _stop_payload(cwd=str(tmp_path / "elsewhere"))
    assert cc_events.transform_event(json.dumps(payload)) == 0
    assert [path for path, _body in posts] == ["/ingest"]
    assert posts[0][1]["profile_id"] == "default"
    assert "unbound" in capsys.readouterr().err
    assert path.read_bytes() == b"{ broken "


# ---------------------------------------------------------------- two hosts on a real daemon


def test_two_priority_hosts_share_the_bound_profile_across_all_paths(
    daemon: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, credential, home = daemon
    client = _client(base_url, credential)
    root_a = _mkdir(tmp_path / "proj-a")
    root_b = _mkdir(tmp_path / "proj-b")
    pb.bind(home, root_a, ALPHA)
    pb.bind(home, root_b, BETA)

    first = _fire_hook(
        "claude_code",
        {
            "session_id": CC_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(root_a),
            "prompt": CC_PROMPT_A,
        },
        base_url,
        home,
        profile=ALPHA,
    )
    assert first.returncode == 0, first.stderr.decode("utf-8", "replace")
    second = _fire_hook("claude_code", _stop_payload(cwd=str(root_a)), base_url, home, profile=ALPHA)
    assert second.returncode == 0, second.stderr.decode("utf-8", "replace")
    codex = _fire_hook(
        "codex_cli",
        {
            "session_id": CX_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "transcript_path": "",
            "cwd": str(root_b),
            "prompt": CX_PROMPT_B,
        },
        base_url,
        home,
        profile=BETA,
    )
    assert codex.returncode == 0, codex.stderr.decode("utf-8", "replace")

    _flush(client, CC_SESSION, ALPHA)
    _flush(client, CX_SESSION, BETA)
    alpha_text = json.dumps(_export(client, ALPHA), ensure_ascii=False)
    beta_text = json.dumps(_export(client, BETA), ensure_ascii=False)
    assert CC_PROMPT_A in alpha_text
    assert CC_REPLY_A in alpha_text
    assert CX_PROMPT_B not in alpha_text
    assert CX_PROMPT_B in beta_text
    assert CC_PROMPT_A not in beta_text
    assert _export(client, "default") == []


def test_uncontrolled_instance_on_a_bound_root_stays_on_the_legacy_default_profile(
    daemon: tuple[str, str, Path], tmp_path: Path
) -> None:
    """A hook event on a bound root WITHOUT the launcher env is the legacy
    uncontrolled instance: it captures under the default profile — never
    silently adopts the bound namespace, never gets refused — renders the
    unbound line, and leaves the registry byte-identical."""
    base_url, credential, home = daemon
    client = _client(base_url, credential)
    root = _mkdir(tmp_path / "proj")
    pb.bind(home, root, ALPHA)
    registry_before = pb.bindings_path(home).read_bytes()
    result = _fire_hook("claude_code", _stop_payload(cwd=str(root)), base_url, home, profile=None, debug=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.decode("utf-8", "replace").strip() == ""
    err = result.stderr.decode("utf-8", "replace")
    assert "unbound" in err
    assert "event dropped" not in err
    _flush(client, CC_SESSION, "default")
    assert CC_REPLY_A in json.dumps(_export(client, "default"), ensure_ascii=False)
    assert _export(client, ALPHA) == []
    assert pb.bindings_path(home).read_bytes() == registry_before


def test_two_hosts_on_one_bound_project_post_under_the_same_profile(
    daemon: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, credential, home = daemon
    client = _client(base_url, credential)
    root = _mkdir(tmp_path / "proj")
    pb.bind(home, root, ALPHA)
    claude = _fire_hook(
        "claude_code",
        {
            "session_id": CC_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(root),
            "prompt": CC_PROMPT_A,
        },
        base_url,
        home,
        profile=ALPHA,
    )
    assert claude.returncode == 0, claude.stderr.decode("utf-8", "replace")
    codex = _fire_hook(
        "codex_cli",
        {
            "session_id": CX_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "transcript_path": "",
            "cwd": str(root),
            "prompt": CX_PROMPT_B,
        },
        base_url,
        home,
        profile=ALPHA,
    )
    assert codex.returncode == 0, codex.stderr.decode("utf-8", "replace")
    _flush(client, CC_SESSION, ALPHA)
    _flush(client, CX_SESSION, ALPHA)
    alpha_text = json.dumps(_export(client, ALPHA), ensure_ascii=False)
    assert CC_PROMPT_A in alpha_text
    assert CX_PROMPT_B in alpha_text
    assert _export(client, "default") == []


def test_projects_bound_to_different_profiles_keep_their_sentinels_isolated(
    daemon: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, credential, home = daemon
    client = _client(base_url, credential)
    root_a = _mkdir(tmp_path / "proj-a")
    root_b = _mkdir(tmp_path / "proj-b")
    pb.bind(home, root_a, ALPHA)
    pb.bind(home, root_b, BETA)
    leaked = _fire_hook(
        "claude_code",
        {
            "session_id": CC_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(root_b),
            "prompt": "项目B的内容试图进入项目A的会话",
        },
        base_url,
        home,
        profile=ALPHA,
    )
    assert leaked.returncode == 0
    assert leaked.stdout.decode("utf-8", "replace").strip() == ""
    assert "event dropped" in leaked.stderr.decode("utf-8", "replace")
    # nothing to flush: the daemon never captured the session, and nothing is stored
    with pytest.raises(DaemonRestError) as excinfo:
        _flush(client, CC_SESSION, ALPHA)
    assert excinfo.value.status == 404
    assert _export(client, ALPHA) == []


def test_hook_refusal_leaves_no_partial_write_for_the_session(
    daemon: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, credential, home = daemon
    client = _client(base_url, credential)
    root_a = _mkdir(tmp_path / "proj-a")
    pb.bind(home, root_a, ALPHA)
    rejected = _fire_hook(
        "claude_code",
        {
            "session_id": CC_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(_mkdir(tmp_path / "elsewhere")),
            "prompt": CC_PROMPT_A,
        },
        base_url,
        home,
        profile=ALPHA,
    )
    assert rejected.returncode == 0
    allowed = _fire_hook(
        "claude_code",
        {
            "session_id": CC_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(root_a),
            "prompt": CC_PROMPT_A,
        },
        base_url,
        home,
        profile=ALPHA,
    )
    assert allowed.returncode == 0
    _flush(client, CC_SESSION, ALPHA)
    chunks = _export(client, ALPHA)
    assert len(chunks) == 1, chunks
    assert CC_PROMPT_A in json.dumps(chunks, ensure_ascii=False)


def test_hook_refuses_a_malformed_registry_under_a_controlled_launch(
    daemon: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, credential, home = daemon
    client = _client(base_url, credential)
    root_a = _mkdir(tmp_path / "proj-a")
    pb.bind(home, root_a, ALPHA)
    path = pb.bindings_path(home)
    path.write_bytes(b"{ broken ")
    result = _fire_hook(
        "claude_code",
        {
            "session_id": CC_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(root_a),
            "prompt": CC_PROMPT_A,
        },
        base_url,
        home,
        profile=ALPHA,
    )
    assert result.returncode == 0
    assert result.stdout.decode("utf-8", "replace").strip() == ""
    err = result.stderr.decode("utf-8", "replace")
    assert "registry" in err
    assert "project run" in err
    assert path.read_bytes() == b"{ broken "
    assert _export(client, ALPHA) == []


def test_legacy_unbound_flow_keeps_the_default_profile_and_renders_unbound(
    daemon: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, credential, home = daemon
    client = _client(base_url, credential)
    path = pb.bindings_path(home)
    path.write_bytes(b"{ broken ")
    result = _fire_hook(
        "claude_code",
        {
            "session_id": CC_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(_mkdir(tmp_path / "anywhere")),
            "prompt": CC_PROMPT_A,
        },
        base_url,
        home,
        profile=None,
        debug=True,
    )
    assert result.returncode == 0
    assert "unbound" in result.stderr.decode("utf-8", "replace")
    _flush(client, CC_SESSION, "default")
    default_text = json.dumps(_export(client, "default"), ensure_ascii=False)
    assert CC_PROMPT_A in default_text
    assert path.read_bytes() == b"{ broken "


def test_shared_profile_is_visibly_labelled_as_sharing(daemon: tuple[str, str, Path], tmp_path: Path) -> None:
    base_url, credential, home = daemon
    client = _client(base_url, credential)
    root_a = _mkdir(tmp_path / "proj-a")
    worktree = _mkdir(tmp_path / "proj-a-wt")
    pb.bind(home, root_a, ALPHA)
    pb.bind(home, worktree, ALPHA)
    result = _fire_hook(
        "claude_code",
        {
            "session_id": CC_SESSION,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(worktree),
            "prompt": CC_PROMPT_A,
        },
        base_url,
        home,
        profile=ALPHA,
        debug=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    err = result.stderr.decode("utf-8", "replace")
    assert "shared" in err
    assert str(root_a) in err
    _flush(client, CC_SESSION, ALPHA)
    assert CC_PROMPT_A in json.dumps(_export(client, ALPHA), ensure_ascii=False)


# ---------------------------------------------------------------- MCP gateway


def _gateway_remember(home: Path, *, profile: str | None, text: str) -> None:
    """One real MCP gateway subprocess speaking JSON-RPC to the test daemon."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNEMOSEED_LOCAL_")}
    env["MNEMOSEED_LOCAL_HOME"] = str(home)
    if profile is not None:
        env["MNEMOSEED_LOCAL_PROFILE_ID"] = profile
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "remember", "arguments": {"text": text}},
        },
    ]
    result = subprocess.run(
        [sys.executable, "-m", "mnemoseed_local.cli", "mcp"],
        input="".join(json.dumps(line) + "\n" for line in lines).encode("utf-8"),
        capture_output=True,
        timeout=120,
        env=env,
        shell=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    out_lines = result.stdout.decode("utf-8", "replace").splitlines()
    replies = [json.loads(line) for line in out_lines if line.strip()]
    assert [reply.get("id") for reply in replies] == [1, 2], replies
    assert replies[1]["result"]["isError"] is False, replies[1]


def test_mcp_gateway_subprocess_inherits_the_bound_profile(
    daemon: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, credential, home = daemon
    root = _mkdir(tmp_path / "proj")
    pb.bind(home, root, ALPHA)
    _gateway_remember(home, profile=ALPHA, text="gateway-sentinel-bound")
    _gateway_remember(home, profile=None, text="gateway-sentinel-default")
    client = _client(base_url, credential)
    alpha_text = json.dumps(_export(client, ALPHA), ensure_ascii=False)
    default_text = json.dumps(_export(client, "default"), ensure_ascii=False)
    assert "gateway-sentinel-bound" in alpha_text
    assert "gateway-sentinel-default" not in alpha_text
    assert "gateway-sentinel-default" in default_text
    assert "gateway-sentinel-bound" not in default_text


def test_gateway_config_entries_pin_no_profile_value() -> None:
    assert "profile" not in json.dumps(zc_install.mcp_server_entry()).lower()
    assert "profile" not in cx_install._canonical_mcp_block().lower()
    # the plugin's remote entry derives its profile header from the env at
    # runtime (proved by the node scenarios below); the shipped source must
    # keep that derivation, never a pinned literal.
    source = (
        resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts").read_text(encoding="utf-8")
    )
    assert re.search(r'"x-mnemoseed-profile":\s*PROFILE_ID', source)


# ---------------------------------------------------------------- opencode plugin (node)


DRIVER_SOURCE = """
import { pathToFileURL } from "node:url"
const [bundle] = process.argv.slice(2)
const { default: plugin } = await import(pathToFileURL(bundle).href)
const errors = []
const realError = console.error
console.error = (...args) => {
  errors.push(args.map((a) => String(a)).join(" "))
}
try {
  const result = await plugin({}, undefined)
  const out = { keys: Object.keys(result), errors }
  if (typeof result.config === "function") {
    const cfg = {}
    await result.config(cfg)
    out.mcp = cfg.mcp
  }
  process.stdout.write(JSON.stringify(out))
} finally {
  console.error = realError
}
"""


@pytest.fixture
def plugin_bundle(tmp_path: Path) -> Path:
    from test_hook_ts_behavior import _bundle

    return _bundle(tmp_path)


def _plugin_driver(tmp_path: Path) -> Path:
    driver = tmp_path / "binding_driver.mjs"
    driver.write_text(DRIVER_SOURCE, encoding="utf-8")
    return driver


def _run_plugin(bundle: Path, driver: Path, cwd: Path, home: Path, *, profile: str | None) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNEMOSEED_LOCAL_")}
    env["MNEMOSEED_LOCAL_HOME"] = str(home)
    if profile is not None:
        env["MNEMOSEED_LOCAL_PROFILE_ID"] = profile
    result = subprocess.run(
        ["node", str(driver), str(bundle)],
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        env=env,
        shell=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_plugin_launch_root_verification_and_refusal(tmp_path: Path, plugin_bundle: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = _mkdir(tmp_path / "proj")
    other = _mkdir(tmp_path / "other")
    driver = _plugin_driver(tmp_path)

    pb.bind(home, root, ALPHA)
    ok = _run_plugin(plugin_bundle, driver, root, home, profile=ALPHA)
    assert "config" in ok["keys"]
    assert ok["errors"] == []

    outside = _run_plugin(plugin_bundle, driver, other, home, profile=ALPHA)
    assert outside["keys"] == []
    assert "project run" in " ".join(outside["errors"])
    assert "unsupported" in " ".join(outside["errors"])

    pb.bind(home, root, BETA, replace=True)
    conflict = _run_plugin(plugin_bundle, driver, root, home, profile=ALPHA)
    assert conflict["keys"] == []
    joined = " ".join(conflict["errors"])
    assert ALPHA in joined and BETA in joined

    pb.bindings_path(home).write_bytes(b"{ broken ")
    malformed = _run_plugin(plugin_bundle, driver, root, home, profile=ALPHA)
    assert malformed["keys"] == []
    assert "registry" in " ".join(malformed["errors"])

    legacy = _run_plugin(plugin_bundle, driver, root, home, profile=None)
    assert "config" in legacy["keys"]
    assert legacy["errors"] == []

    pb.bindings_path(home).unlink()
    manual = _run_plugin(plugin_bundle, driver, other, home, profile=ALPHA)
    assert "config" in manual["keys"]
    assert manual["errors"] == []


def test_plugin_mcp_entry_header_follows_the_env(tmp_path: Path, plugin_bundle: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    driver = _plugin_driver(tmp_path)
    cwd = _mkdir(tmp_path / "anywhere")
    controlled = _run_plugin(plugin_bundle, driver, cwd, home, profile=ALPHA)
    headers = controlled["mcp"]["mnemoseed"]["headers"]
    assert headers["x-mnemoseed-profile"] == ALPHA
    legacy = _run_plugin(plugin_bundle, driver, cwd, home, profile=None)
    assert legacy["mcp"]["mnemoseed"]["headers"]["x-mnemoseed-profile"] == "default"
