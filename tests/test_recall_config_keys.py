"""The [recall] config table: rescue-band thresholds (design/09 §3.5).

Both values live in [0, 1]; the defaults carry the rescue-band calibration
sweep's ACCEPTED verdict (see the constant comments in config.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.config import (
    DEFAULT_RECALL_RESCUE_CUE_MIN,
    DEFAULT_RECALL_RESCUE_FLOOR,
    ConfigError,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n[storage.graph.instances.isolated]\ndriver = "sqlite_graph"\n[dream]\n'
        "[recall]\n" + body,
        encoding="utf-8",
    )
    return path


def test_defaults_carry_the_calibration_verdict(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")

    assert cfg.recall.rescue_floor == DEFAULT_RECALL_RESCUE_FLOOR
    assert cfg.recall.rescue_cue_min == DEFAULT_RECALL_RESCUE_CUE_MIN
    # the band must sit below the main candidate floor to be meaningful
    assert 0.0 < cfg.recall.rescue_floor < 0.4
    assert 0.0 < cfg.recall.rescue_cue_min <= 1.0


def test_load_parses_the_rescue_band(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(_write(tmp_path, "rescue_floor = 0.2\nrescue_cue_min = 0.35\n"))

    assert cfg.recall.rescue_floor == pytest.approx(0.2)
    assert cfg.recall.rescue_cue_min == pytest.approx(0.35)


def test_load_rejects_out_of_range_values_naming_the_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    for body, key in (
        ("rescue_floor = -0.1\n", "recall.rescue_floor"),
        ("rescue_floor = 1.5\n", "recall.rescue_floor"),
        ('rescue_floor = "low"\n', "recall.rescue_floor"),
        ("rescue_cue_min = -1\n", "recall.rescue_cue_min"),
        ("rescue_cue_min = 2\n", "recall.rescue_cue_min"),
    ):
        with pytest.raises(ConfigError, match=rf"config\[{key}\]"):
            load_config(_write(tmp_path, body))
