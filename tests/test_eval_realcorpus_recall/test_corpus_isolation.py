"""Corpus copy, provenance pins, spare-port isolation, pristine restore."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from mnemoseed_local.eval.realcorpus_recall.corpus import (
    copy_corpus,
    directory_digest,
    restore_writable_state,
    snapshot_pristine,
)
from mnemoseed_local.eval.realcorpus_recall.isolation import (
    RESERVED_PORTS,
    allocate_spare_port,
    assert_port_is_spare,
    daemon_command,
    daemon_env,
    pin_home_in_config,
)
from mnemoseed_local.eval.realcorpus_recall.manifest import (
    InvalidRun,
    compute_corpus_manifest,
    verify_manifest,
    write_manifest,
)


def test_embedder_pin_uses_preset_not_first_driver(tmp_path: Path) -> None:
    source = tmp_path / "home"
    _file(source, "config.toml", b'preset = "embedded"\n[storage.graph]\ndriver = "sqlite_graph"\n')
    _file(source, "models/bge-m3/tokenizer.json", b"tokenizer")
    manifest = compute_corpus_manifest(source, code_revision="r", embedder_version=None)
    assert manifest.embedder_model_version.startswith("bge_m3_onnx@")


def _file(root: Path, rel: str, data: bytes) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _tree(root: Path) -> None:
    _file(root, "config.toml", b'preset = "embedded"\n')
    _file(root, "meta.db", b"meta-v1")
    _file(root, "meta.db-wal", b"wal-v1")
    _file(root, "meta.db-shm", b"shm-v1")
    _file(root, "chunks.lance/parts/a.bin", b"alpha" * 20)
    _file(root, "chunks.lance/parts/b.bin", b"beta" * 30)


def test_copy_is_measured_and_covers_every_file(tmp_path: Path) -> None:
    source = tmp_path / "live"
    _tree(source)
    before = directory_digest(source)
    copy = copy_corpus(source, temp_root=tmp_path / "temp", code_revision="abc123")
    assert copy.file_count == 6
    assert copy.total_bytes == sum(p.stat().st_size for p in source.rglob("*") if p.is_file())
    assert copy.elapsed_seconds >= 0.0
    assert copy.home.is_relative_to(tmp_path / "temp")
    assert (copy.home / "chunks.lance/parts/a.bin").read_bytes() == b"alpha" * 20
    assert directory_digest(source) == before
    assert copy.manifest.code_revision == "abc123"


def test_manifest_pins_four_elements(tmp_path: Path) -> None:
    source = tmp_path / "live"
    _tree(source)
    manifest = compute_corpus_manifest(source, code_revision="deadbeef", embedder_version="bge-m3@v1")
    as_dict = manifest.to_dict()
    assert set(as_dict) == {
        "corpus_manifest_sha256",
        "config_sha256",
        "embedder_model_version",
        "code_revision",
    }
    assert as_dict["corpus_manifest_sha256"]
    assert as_dict["config_sha256"] == hashlib.sha256(b'preset = "embedded"\n').hexdigest()
    assert as_dict["embedder_model_version"] == "bge-m3@v1"
    assert as_dict["code_revision"] == "deadbeef"


def test_manifest_file_is_written_and_verified(tmp_path: Path) -> None:
    source = tmp_path / "live"
    _tree(source)
    manifest = compute_corpus_manifest(source, code_revision="r1", embedder_version="e1")
    path = tmp_path / "corpus_manifest.sha256"
    write_manifest(manifest, path)
    assert "corpus_manifest_sha256" in path.read_text(encoding="utf-8")
    verify_manifest(manifest, manifest)
    drifted = manifest.__class__(**{**manifest.to_dict(), "code_revision": "r2"})
    with pytest.raises(InvalidRun):
        verify_manifest(manifest, drifted)


def test_corpus_drift_becomes_invalid_run(tmp_path: Path) -> None:
    source = tmp_path / "live"
    _tree(source)
    pinned = compute_corpus_manifest(source, code_revision="r1", embedder_version="e1")
    _file(source, "chunks.lance/parts/a.bin", b"mutated")
    actual = compute_corpus_manifest(source, code_revision="r1", embedder_version="e1")
    with pytest.raises(InvalidRun):
        verify_manifest(pinned, actual)


def test_spare_port_is_ephemeral_and_never_reserved() -> None:
    port = allocate_spare_port()
    assert 1024 < port < 65536
    assert port not in RESERVED_PORTS
    for _ in range(8):
        assert_port_is_spare(allocate_spare_port())


@pytest.mark.parametrize("port", list(RESERVED_PORTS))
def test_reserved_ports_are_rejected(port: int) -> None:
    with pytest.raises(InvalidRun):
        assert_port_is_spare(port)


def test_daemon_launch_targets_temp_home_and_spare_port(tmp_path: Path) -> None:
    home = tmp_path / "corpus"
    port = allocate_spare_port()
    command = daemon_command(port)
    assert "-m" in command and "uvicorn" in command
    assert str(port) in command
    env = daemon_env(home)
    assert env["MNEMOSEED_LOCAL_HOME"] == str(home)
    assert env["MNEMOSEED_LOCAL_HOME"] != str(Path.home() / ".mnemoseed-local")
    assert sys.executable in command


def test_daemon_env_inherits_but_overrides_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOSEED_LOCAL_BASEURL", "http://127.0.0.1:12345")
    monkeypatch.setenv("MNEMOSEED_LOCAL_HOME", str(Path.home() / ".mnemoseed-local"))
    env = daemon_env(tmp_path / "isolated")
    assert env["MNEMOSEED_LOCAL_HOME"] == str(tmp_path / "isolated")
    assert env["MNEMOSEED_LOCAL_BASEURL"] == "http://127.0.0.1:12345"
    assert os.environ["MNEMOSEED_LOCAL_HOME"] == str(Path.home() / ".mnemoseed-local")


def test_config_home_rebinding_never_touches_live_home(tmp_path: Path) -> None:
    live = tmp_path / "live"
    _file(
        live, "config.toml", b'[storage.graph.instances.isolated]\npath = "~/.mnemoseed-local/isolated.db"\n'
    )
    _file(live, "isolated.db", b"live-data")
    work = tmp_path / "work"
    _tree(work)
    _file(work, "config.toml", (live / "config.toml").read_bytes())
    pin_home_in_config(work, live_home=live)
    pinned = (work / "config.toml").read_text(encoding="utf-8")
    assert str(work).replace("\\", "/") in pinned
    assert str(live) not in pinned
    assert (live / "isolated.db").read_bytes() == b"live-data"


def test_pristine_snapshot_restores_byte_identical(tmp_path: Path) -> None:
    source = tmp_path / "live"
    _tree(source)
    pristine = snapshot_pristine(source, tmp_path / "pristine")
    baseline = directory_digest(pristine)
    work = tmp_path / "work"
    restore_writable_state(pristine, work)
    assert directory_digest(work) == baseline
    _file(work, "meta.db", b"polluted")
    _file(work, "chunks.lance/parts/extra.bin", b"extra")
    assert directory_digest(work) != baseline
    restore_writable_state(pristine, work)
    assert directory_digest(work) == baseline


def test_pristine_baseline_is_never_mutated_by_a_run(tmp_path: Path) -> None:
    source = tmp_path / "live"
    _tree(source)
    pristine = snapshot_pristine(source, tmp_path / "pristine")
    baseline = directory_digest(pristine)
    for index in range(3):
        work = tmp_path / f"run-{index}"
        restore_writable_state(pristine, work)
        _file(work, "meta.db", f"run-{index}".encode())
    assert directory_digest(pristine) == baseline
