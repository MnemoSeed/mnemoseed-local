"""Secrets reference grammar (T2-2): ``api_key_env`` accepts env-var NAME
lists (unchanged) OR a single ``secrets:mnemoseed/dream/<role>`` reference.

Behavior pinned here:

- the configwrite registry validator accepts env-var NAME lists and a single
  well-formed ``secrets:`` reference; a literal key-like string is still a
  hard failure (G-AC2) whose message never echoes the offending value;
- a ``secrets:`` reference must name a LIVE dream role — ``local_track`` and
  unknown roles are rejected on the write surface and on config load;
- config load validates the reference SHAPE: a malformed ``secrets:`` value is
  a typed ConfigError naming the key (a hand-edited literal key stays
  tolerated + redacted, the pre-existing contract);
- read surfaces keep the reference (never the secret value) and redact
  anything that is not a valid env-var name or reference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.config import ConfigError, load_config
from mnemoseed_local.configwrite.service import ConfigWriteError, ConfigWriteService
from mnemoseed_local.secrets.refs import is_secrets_ref, secret_name_from_ref

_VALID_REF = "secrets:mnemoseed/dream/dream"


def _config_toml(tmp_path: Path, *, role_table: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        'preset = "embedded"\n[dream]\n[dream.llm.dream]\ndriver = "stub"\nmodel = "stub"\n' + role_table,
        encoding="utf-8",
    )
    return path


def _service(tmp_path: Path) -> tuple[ConfigWriteService, Path]:
    path = _config_toml(tmp_path)
    return ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0), path


# ---------------------------------------------------------------- ref helpers


def test_is_secrets_ref_detects_the_prefix() -> None:
    assert is_secrets_ref("secrets:mnemoseed/dream/dream") is True
    assert is_secrets_ref("FIREWORKS_API_KEY") is False
    assert is_secrets_ref("sk-proj-literal") is False


def test_secret_name_from_ref_extracts_the_store_name() -> None:
    assert secret_name_from_ref(_VALID_REF) == "mnemoseed/dream/dream"
    assert secret_name_from_ref("FIREWORKS_API_KEY") is None
    assert secret_name_from_ref("secrets:") is None


# ---------------------------------------------------------------- write validation


def test_write_accepts_env_name_list_unchanged(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set(
        "dream.llm.dream.api_key_env",
        "MNEMOSEED_DREAM_API_KEY,FIREWORKS_API_KEY",
        actor="console",
    )
    assert 'api_key_env = "MNEMOSEED_DREAM_API_KEY,FIREWORKS_API_KEY"' in path.read_text(encoding="utf-8")


def test_write_accepts_a_valid_secrets_reference(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.llm.dream.api_key_env", _VALID_REF, actor="console")
    assert f'api_key_env = "{_VALID_REF}"' in path.read_text(encoding="utf-8")
    assert load_config(path).llm["dream"].params["api_key_env"] == _VALID_REF


def test_write_rejects_literal_key_like_values_without_echoing(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in ("sk-abc123", "sk-proj-deadbeef", "openai_api_key"):
        with pytest.raises(ConfigWriteError, match=r"config\[dream\.llm\.dream\.api_key_env\]"):
            service.set("dream.llm.dream.api_key_env", bad, actor="console")


def test_write_rejects_malformed_references(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in (
        "secrets:",
        "secrets:mnemoseed",
        "secrets:mnemoseed/dream",
        "secrets:other/dream/dream",
        "secrets:mnemoseed/dream/dream,EXTRA",
    ):
        with pytest.raises(ConfigWriteError, match=r"config\[dream\.llm\.dream\.api_key_env\]"):
            service.set("dream.llm.dream.api_key_env", bad, actor="console")


def test_write_rejects_reference_to_the_deprecated_or_unknown_role(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in (
        "secrets:mnemoseed/dream/local_track",
        "secrets:mnemoseed/dream/not_a_role",
    ):
        with pytest.raises(ConfigWriteError, match=r"config\[dream\.llm\.dream\.api_key_env\]"):
            service.set("dream.llm.dream.api_key_env", bad, actor="console")


# ---------------------------------------------------------------- config load validation


def test_load_accepts_a_valid_reference(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = _config_toml(
        tmp_path,
        role_table='api_key_env = "secrets:mnemoseed/dream/dream"\n',
    )
    assert load_config(path).llm["dream"].params["api_key_env"] == _VALID_REF


def test_load_rejects_a_malformed_reference_naming_the_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = _config_toml(tmp_path, role_table='api_key_env = "secrets:mnemoseed/boom"\n')
    with pytest.raises(ConfigError, match=r"config\[dream\.llm\.dream\.api_key_env\]"):
        load_config(path)


def test_load_rejects_a_reference_to_the_deprecated_role(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = _config_toml(tmp_path, role_table='api_key_env = "secrets:mnemoseed/dream/local_track"\n')
    with pytest.raises(ConfigError, match=r"config\[dream\.llm\.dream\.api_key_env\]"):
        load_config(path)


def test_load_tolerates_a_hand_edited_literal_key(tmp_path, monkeypatch) -> None:
    """G-AC2 contract: a hand-edited literal key never breaks boot — it is
    tolerated on load and redacted on every read surface."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    path = _config_toml(tmp_path, role_table='api_key_env = "sk-proj-literal-value"\n')
    cfg = load_config(path)
    assert cfg.llm["dream"].params["api_key_env"] == "sk-proj-literal-value"


# ---------------------------------------------------------------- read redaction


def test_get_surfaces_the_reference_never_the_value(tmp_path) -> None:
    service, _ = _service(tmp_path)
    service.set("dream.llm.dream.api_key_env", _VALID_REF, actor="console")
    blob = repr(service.get())
    assert _VALID_REF in blob
    assert "sk-" not in blob


def test_read_redacts_a_literal_key_but_keeps_references(tmp_path) -> None:
    from mnemoseed_local.configwrite.service import _redact_env_names

    assert _redact_env_names("FIREWORKS_API_KEY") == "FIREWORKS_API_KEY"
    assert _redact_env_names(_VALID_REF) == _VALID_REF
    assert _redact_env_names("sk-proj-literal") == ""
    assert _redact_env_names(None) == ""
