"""The capture-recall setup mode: an install-time selection mapped onto the two
existing config keys (dream.auto_trigger / capture.auto_recall).

The setup writes only when no config exists, reports the local embedder's
resource state honestly (ready / needs download / failed), never rewrites an
existing config (conflicts become exact conversion steps), and refuses an
unknown mode before anything is written. The install orchestrators pass the
mode through to `mnemoseed-local init --mode <mode>`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mnemoseed_local.cli import main
from mnemoseed_local.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The bge-m3 onnx artifact basenames in the local model cache (flat layout).
EMBEDDER_ARTIFACTS = ("tokenizer.json", "model_quantized.onnx", "sparse_linear.pt")

#: A capture-recall posture the setup can confirm without writing anything.
CAPTURE_RECALL_CONFIG = (
    'preset = "embedded"\n\n[dream]\nauto_trigger = false\n\n[capture]\nauto_recall = true\n'
)

#: An explicit legacy posture that conflicts with the capture-recall request.
LEGACY_CONFIG = 'preset = "embedded"\n\n[dream]\nauto_trigger = true\n'

#: A custom-preset config whose embed driver has no local resource state to probe.
CUSTOM_EMBED_CONFIG = (
    'preset = "custom"\n'
    "[storage.vector]\n"
    'driver = "lancedb_embedded"\n'
    "[storage.graph]\n"
    'driver = "sqlite_graph"\n'
    "[storage.meta]\n"
    'driver = "sqlite_meta"\n'
    "[storage.embed]\n"
    'driver = "synthetic"\n'
    "[dream]\n"
    "auto_trigger = false\n"
    "[capture]\n"
    "auto_recall = true\n"
)


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".mnemoseed-local"
    home.mkdir(parents=True)
    # cli.py binds its own CONFIG_DIR/CONFIG_PATH at import time while
    # load_config() and the bge-m3 driver each read their own module-level
    # bindings, so every namespace must be patched to the same hermetic home.
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.cli.CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", home)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("mnemoseed_local.storage.drivers.bge_m3_onnx.CONFIG_DIR", home)
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    return home


def _embedding_line(out: str) -> str | None:
    for line in out.splitlines():
        if line.startswith("embedding readiness:"):
            return line
    return None


def _seed_embedder_artifacts(home: Path) -> None:
    model_dir = home / "models" / "bge-m3"
    model_dir.mkdir(parents=True, exist_ok=True)
    for artifact in EMBEDDER_ARTIFACTS:
        (model_dir / artifact).write_bytes(b"cached")


# (a) fresh config: the two existing keys, and no Ollama anywhere in the path


def test_capture_recall_setup_writes_two_existing_keys_and_no_ollama(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", "--mode", "capture-recall"]) == 0
    text = (cli_home / "config.toml").read_text(encoding="utf-8")
    raw = tomllib.loads(text)
    assert raw["dream"]["auto_trigger"] is False
    assert raw["capture"]["auto_recall"] is True
    # no new config keys beyond the ones the template already defines
    assert set(raw) == {"preset", "baseurl", "storage", "dream", "capture"}
    cfg = load_config(cli_home / "config.toml")
    assert cfg.dream.auto_trigger is False
    assert cfg.capture.auto_recall is True
    out = capsys.readouterr().out
    assert "capture-recall" in out
    assert "ollama" not in out.lower()
    assert _embedding_line(out) is not None


# (b) embedding readiness: ready / needs download / failed


def test_capture_recall_setup_reports_ready_when_artifacts_are_cached(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_embedder_artifacts(cli_home)
    assert main(["init", "--mode", "capture-recall"]) == 0
    line = _embedding_line(capsys.readouterr().out)
    assert line is not None
    assert line.startswith("embedding readiness: ready")


def test_capture_recall_setup_reports_needs_download_when_artifacts_are_missing(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", "--mode", "capture-recall"]) == 0
    line = _embedding_line(capsys.readouterr().out)
    assert line is not None
    assert line.startswith("embedding readiness: needs download")
    for artifact in EMBEDDER_ARTIFACTS:
        assert artifact in line
    assert str(cli_home / "models" / "bge-m3") in line


def test_capture_recall_setup_reports_failed_when_the_embedder_cannot_be_probed(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (cli_home / "config.toml").write_bytes(CUSTOM_EMBED_CONFIG.encode("utf-8"))
    assert main(["init", "--mode", "capture-recall"]) == 0
    out = capsys.readouterr().out
    line = _embedding_line(out)
    assert line is not None
    assert line.startswith("embedding readiness: failed")
    assert "synthetic" in line


# (c) existing config: preserved verbatim, conflicts become exact next steps


def test_conflicting_explicit_config_is_preserved_verbatim_with_next_steps(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = cli_home / "config.toml"
    config_path.write_bytes(LEGACY_CONFIG.encode("utf-8"))
    assert main(["init", "--mode", "capture-recall"]) == 1
    assert config_path.read_bytes() == LEGACY_CONFIG.encode("utf-8")
    out = capsys.readouterr().out
    assert "config set dream.auto_trigger false" in out
    assert "refinement" in out


def test_matching_existing_config_is_confirmed_without_rewrite(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = cli_home / "config.toml"
    config_path.write_bytes(CAPTURE_RECALL_CONFIG.encode("utf-8"))
    assert main(["init", "--mode", "capture-recall"]) == 0
    assert config_path.read_bytes() == CAPTURE_RECALL_CONFIG.encode("utf-8")
    out = capsys.readouterr().out
    assert "capture-recall" in out
    assert "config set" not in out


def test_unset_dream_auto_trigger_reports_the_conversion_step(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = 'preset = "embedded"\n'
    config_path = cli_home / "config.toml"
    config_path.write_bytes(original.encode("utf-8"))
    assert main(["init", "--mode", "capture-recall"]) == 1
    assert config_path.read_bytes() == original.encode("utf-8")
    out = capsys.readouterr().out
    assert "config set dream.auto_trigger false" in out
    assert "not set" in out


def test_mode_setup_refuses_to_combine_with_force(cli_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = cli_home / "config.toml"
    config_path.write_bytes(LEGACY_CONFIG.encode("utf-8"))
    assert main(["init", "--force", "--mode", "capture-recall"]) == 1
    assert config_path.read_bytes() == LEGACY_CONFIG.encode("utf-8")
    assert "never overwrites" in capsys.readouterr().out


# (d) refinement mode: honest unavailable status, legacy auto dream stays off


def test_refinement_mode_on_a_fresh_config_is_honest_and_keeps_auto_dream_off(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", "--mode", "refinement"]) == 0
    out = capsys.readouterr().out
    assert "refinement v2 is not yet accepted or available" in out
    assert "11-refinement-v2.md" in out
    assert "auto_trigger = true" not in out
    raw = tomllib.loads((cli_home / "config.toml").read_text(encoding="utf-8"))
    assert raw["dream"]["auto_trigger"] is False
    assert raw["capture"]["auto_recall"] is True
    cfg = load_config(cli_home / "config.toml")
    assert cfg.dream.auto_trigger is False


def test_refinement_mode_on_an_existing_config_preserves_it_and_reports(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = cli_home / "config.toml"
    config_path.write_bytes(LEGACY_CONFIG.encode("utf-8"))
    assert main(["init", "--mode", "refinement"]) == 0
    assert config_path.read_bytes() == LEGACY_CONFIG.encode("utf-8")
    out = capsys.readouterr().out
    assert "not yet accepted" in out
    assert "dream.auto_trigger = true" in out


# (e) unknown mode: refused before anything is written


def test_unknown_mode_is_refused_before_anything_is_written(
    cli_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["init", "--mode", "bogus"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert not (cli_home / "config.toml").exists()


# installer wiring: the flag exists, defaults to capture-recall, and keeps
# Ollama out of that mode's path


def test_posix_installer_wires_the_mode_flag() -> None:
    text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert "MODE='capture-recall'" in text
    assert "--mode)" in text
    assert "--mode=*" in text
    assert 'die "unknown mode' in text
    assert 'mnemoseed-local init --mode "$MODE"' in text
    assert "skipped (mode capture-recall runs capture, local embeddings and recall without Ollama)" in text
    assert "skipped (mode capture-recall keeps the legacy auto dream off; no dream model is pulled)" in text


def test_windows_installer_wires_the_mode_flag() -> None:
    text = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "[ValidateSet('CaptureRecall', 'Refinement')]" in text
    assert "[string]$Mode = 'CaptureRecall'" in text
    assert "& mnemoseed-local init --mode $CliMode" in text
    assert "skipped (mode capture-recall runs capture, local embeddings and recall without Ollama)" in text
    assert "skipped (mode capture-recall keeps the legacy auto dream off; no dream model is pulled)" in text
