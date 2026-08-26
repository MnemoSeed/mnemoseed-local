"""ConfigWriteService (PRD-07 FR-7.11 / design/07 section 9, W1.1): the
daemon-owned single config writer.

Service-level contract, unit-testing the registry -> validate -> surgical toml
patch -> versioned meta-store record -> audit -> live-apply flow without the
HTTP layer:

- the key-path registry is seeded with the keys that exist today
  (dream.auto_trigger, the A2 schedule trigger keys,
  and the single dream role's driver/model/base_url/api_key_env/max_tokens
  fields) and an unknown key is a typed error naming the key;
- every write is a surgical line-oriented TOML patch: comments, layout and
  unrelated keys survive, and an existing value line is rewritten in place
  (never duplicated);
- with a meta store the write lands a versioned record (set_config) and an
  audit entry with actor attribution; without one the service still patches the
  file (offline mode) but records nothing;
- api_key_env accepts env-var NAME lists only -- anything key-like is a
  validation failure that names the key;
- rollback is append-only (a new version record, never a delete) and restores
  both the file and the live config;
- boot reconciliation (E1-4 DB-primary Phase 0) imports the file's registry
  values into the settings DB exactly once (audited as ``config_import`` when
  the DB is empty of registry entries), then the DB WINS for registry keys on
  every later boot: a hand-edited config.toml is never rebaselined into the DB
  — the DB value is applied to the live config and the toml mirror is
  regenerated from it, logged + audited as ``config_mirror_drift``. Boot-scope
  keys (preset/storage/baseurl/auth) are never registry keys: they stay
  file-scoped and restart-required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed_local.config import LLM_ROLES, load_config
from mnemoseed_local.configwrite.service import (
    CONFIG_KEY_REGISTRY,
    ConfigWriteError,
    ConfigWriteService,
)
from mnemoseed_local.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed_local.storage.ports import AuditFilter, Page

_AUDIT_ACTIONS = ("config.set", "config.rollback", "config_import", "config_mirror_drift")


def _config_toml(tmp_path: Path) -> Path:
    """A config with comments, a [dream] table, the dream role table and a
    legacy local_track table (tolerated on load, never applied)."""
    path = tmp_path / "config.toml"
    path.write_text(
        "# MnemoSeed configuration\n"
        'preset = "embedded"\n'
        'baseurl = "http://localhost:7788"\n'
        "\n"
        "# Dream-engine section\n"
        "[dream]\n"
        "\n"
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        'base_url = "http://example.test/v1"\n'
        "\n"
        "[dream.llm.local_track]\n"
        'driver = "ollama"\n'
        'model = "llama3.1:8b"\n',
        encoding="utf-8",
    )
    return path


def _meta(tmp_path: Path) -> SqliteMetaDriver:
    return SqliteMetaDriver(path=str(tmp_path / "meta.db"))


def _service(tmp_path: Path, *, meta: SqliteMetaDriver | None = None) -> tuple[ConfigWriteService, Path]:
    path = _config_toml(tmp_path)
    return ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0), path


def _audit_entries(meta: SqliteMetaDriver, action: str) -> list[object]:
    return meta.audit_query(AuditFilter(action=action), Page(limit=100)).items


# ---------------------------------------------------------------- registry


def test_registry_seeded_with_writable_keys() -> None:
    """FR-7.11: the registry carries every key the system writes today."""
    expected = {
        "dream.auto_trigger",
        "dream.floor_pool_points",
        "dream.idle_min_sec",
        "dream.hard_deadline_sec",
    }
    for role in LLM_ROLES:
        for field in ("driver", "model", "base_url", "api_key_env", "max_tokens"):
            expected.add(f"dream.llm.{role}.{field}")
        # D4: think is the seventh writable role field (factory default pins
        # it false — thinking would starve the reflect's structured output)
        expected.add(f"dream.llm.{role}.think")
    assert expected <= set(CONFIG_KEY_REGISTRY)


def test_think_role_field_roundtrip(tmp_path) -> None:
    """D4: think is writable via the registry, applies live to the route's
    params, mirrors into the file, and rejects non-boolean values."""
    service, path = _service(tmp_path)
    assert service._config.llm["dream"].params["think"] is False  # factory default (D4)
    result = service.set("dream.llm.dream.think", True, actor="console")
    assert result["ok"] is True
    assert load_config(path).llm["dream"].params["think"] is True
    assert service._config.llm["dream"].params["think"] is True
    service.set("dream.llm.dream.think", False, actor="console")
    assert load_config(path).llm["dream"].params["think"] is False
    with pytest.raises(ConfigWriteError):
        service.set("dream.llm.dream.think", "maybe", actor="console")


def test_num_ctx_and_num_predict_are_writable_role_fields(tmp_path) -> None:
    """The doctor ctx-window check hints at these keys — they must be
    writable via config set (registry), the live route and the file."""
    service, path = _service(tmp_path)
    service.set("dream.llm.dream.num_ctx", 36864, actor="console")
    service.set("dream.llm.dream.num_predict", 4096, actor="console")
    assert load_config(path).llm["dream"].params["num_ctx"] == 36864
    assert load_config(path).llm["dream"].params["num_predict"] == 4096
    assert service._config.llm["dream"].params["num_ctx"] == 36864
    with pytest.raises(ConfigWriteError):
        service.set("dream.llm.dream.num_ctx", 0, actor="console")
    with pytest.raises(ConfigWriteError):
        service.set("dream.llm.dream.num_predict", "big", actor="console")


def test_verifier_role_keys_are_writable_and_bump_their_own_generation(tmp_path) -> None:
    """B1 T1: the dream_verifier role rides the same registry surface as the
    dream role — writes hot-apply to the live route and the file, validate
    through the shared role validators, and bump only its own generation."""
    service, path = _service(tmp_path)
    result = service.set("dream.llm.dream_verifier.model", "gemma4:e4b-alt", actor="console")
    assert result["ok"] is True
    assert load_config(path).llm["dream_verifier"].model == "gemma4:e4b-alt"
    assert service._config.llm["dream_verifier"].model == "gemma4:e4b-alt"
    assert service.generation == 1
    assert service.generation_for("dream_verifier") == 1
    assert service.generation_for("dream") == 0  # the dream route is untouched
    with pytest.raises(ConfigWriteError):
        service.set("dream.llm.dream_verifier.driver", "  ", actor="console")
    with pytest.raises(ConfigWriteError):
        service.set("dream.llm.dream_verifier.api_key_env", "sk-proj-literal", actor="console")
    with pytest.raises(ConfigWriteError):
        service.set("dream.llm.dream_verifier.think", "maybe", actor="console")


def test_token_budget_usd_is_not_a_registry_key(tmp_path) -> None:
    """AC1: the removed dream.token_budget_usd key is NOT writable — a set
    against it is a typed unknown-key error, never a silent no-op."""
    assert "dream.token_budget_usd" not in CONFIG_KEY_REGISTRY
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.token_budget_usd\].*unknown"):
        service.set("dream.token_budget_usd", 5.0, actor="console")


def test_unknown_key_is_typed_error_naming_the_key(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[scoring\.w1\]"):
        service.set("scoring.w1", 0.5, actor="cli")


# ---------------------------------------------------------------- surgical patch


def test_set_patches_dream_table_preserving_comments(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.auto_trigger", True, actor="console")
    text = path.read_text(encoding="utf-8")
    # the flag landed inside [dream], and the file's comments/layout survived
    assert "# Dream-engine section" in text
    assert "auto_trigger = true" in text
    # unrelated role tables are untouched
    assert 'driver = "ollama"' in text
    # the whole file still parses, and the change round-trips through the loader
    assert load_config(path).dream.auto_trigger is True


def test_set_rewrites_existing_line_in_place(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.auto_trigger", True, actor="console")
    service.set("dream.auto_trigger", False, actor="console")
    keys = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("auto_trigger =")
    ]
    assert keys == ["auto_trigger = false"]


def test_set_inserts_dream_table_when_missing(tmp_path) -> None:
    path = _config_toml(tmp_path)
    text = path.read_text(encoding="utf-8")
    # drop the [dream] table (keep the role tables): the flag must create a new
    # [dream] table instead of leaking into a role table
    text = text.replace("[dream]\n\n", "")
    path.write_text(text, encoding="utf-8")
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0)
    service.set("dream.auto_trigger", True, actor="console")
    assert load_config(path).dream.auto_trigger is True
    assert load_config(path).dream.floor_pool_points == 10.0  # sibling defaults intact


def test_set_patches_role_table_and_in_memory_llm(tmp_path) -> None:
    service, path = _service(tmp_path)
    result = service.set("dream.llm.dream.driver", "openai_compatible", actor="cli")
    assert result["ok"] is True
    text = path.read_text(encoding="utf-8")
    assert 'driver = "openai_compatible"' in text
    assert 'model = "stub"' in text  # sibling field untouched
    assert load_config(path).llm["dream"].driver == "openai_compatible"
    # live-apply: the running Config reflects the change immediately
    assert service._config.llm["dream"].driver == "openai_compatible"


def test_set_role_param_and_clear(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.llm.dream.base_url", "http://custom.test", actor="console")
    assert load_config(path).llm["dream"].params["base_url"] == "http://custom.test"
    service.set("dream.llm.dream.base_url", "", actor="console")
    text = path.read_text(encoding="utf-8")
    # the cleared field is gone from the dream table; the live config and its
    # raw mirror drop the explicit value too (a fresh load_config re-merges
    # only the DEFAULT base_url fallback, which is a loader default, not an
    # explicit write)
    table = text.split("[dream.llm.dream]", 1)[1].split("[", 1)[0]
    assert "base_url" not in table
    assert "base_url" not in service._config.llm["dream"].params
    assert "base_url" not in service._config.raw["dream"]["llm"]["dream"]
    assert load_config(path).dream.auto_trigger is True  # the file still parses


def test_set_api_key_env_persists_names_only(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set(
        "dream.llm.dream.api_key_env",
        "MNEMOSEED_DREAM_API_KEY,FIREWORKS_API_KEY",
        actor="console",
    )
    text = path.read_text(encoding="utf-8")
    assert 'api_key_env = "MNEMOSEED_DREAM_API_KEY,FIREWORKS_API_KEY"' in text
    assert load_config(path).llm["dream"].params["api_key_env"].startswith("MNEMOSEED_")


def test_set_role_fields_accumulate_no_blank_lines(tmp_path) -> None:
    """The surgical patch keeps a written role table tight — including the LAST
    table in the file, where the trailing-newline phantom would otherwise land.
    Sequential sets of driver/model/base_url/max_tokens/api_key_env must produce
    one key per line with NO blank lines inside the table body (raw, unfiltered
    lines — a trailing phantom drifting between keys is the exact regression),
    and the neighboring tables must stay intact with their ``[`` headers
    untouched."""
    path = tmp_path / "config.toml"
    path.write_text(
        "# MnemoSeed configuration\n"
        'preset = "embedded"\n'
        "\n"
        "[dream]\n"
        "\n"
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n',
        encoding="utf-8",
    )
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0)
    for field, value in (
        ("driver", "openai_compatible"),
        ("model", "moonshotai/Kimi-K3"),
        ("base_url", "http://custom.test/v1"),
        ("max_tokens", 4096),
        ("api_key_env", "MNEMOSEED_DREAM_API_KEY"),
    ):
        service.set(f"dream.llm.dream.{field}", value, actor="console")

    blob = path.read_text(encoding="utf-8")
    lines = blob.splitlines()
    start = lines.index("[dream.llm.dream]")
    body = lines[start + 1 :]
    # raw splitlines of the LAST table: no blank line (hence no consecutive
    # blank lines) drifted into the body — the trailing-newline phantom was
    # stripped, never carried into the patched span
    assert body, "dream table body is empty"
    assert all(line.strip() for line in body), f"blank line(s) drifted into the last table body: {body!r}"
    assert body == [
        'driver = "openai_compatible"',
        'model = "moonshotai/Kimi-K3"',
        'base_url = "http://custom.test/v1"',
        "max_tokens = 4096",
        'api_key_env = "MNEMOSEED_DREAM_API_KEY"',
    ]
    # the neighboring tables are intact: every header survives in order, no key
    # line swallowed a ``[`` header
    headers = [line for line in lines if line.startswith("[")]
    assert headers == ["[dream]", "[dream.llm.dream]"]
    assert load_config(path).llm["dream"].driver == "openai_compatible"
    assert load_config(path).llm["dream"].params["base_url"] == "http://custom.test/v1"


def test_set_creates_role_table_cleanly_when_missing(tmp_path) -> None:
    """A route's first write creates [dream.llm.<role>] after the last table
    with exactly one blank separator — the mirror is clean and parseable (the
    first write lands in a config with no role table at all)."""
    path = _config_toml(tmp_path)
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '[dream.llm.dream]\ndriver = "stub"\nmodel = "stub"\nbase_url = "http://example.test/v1"\n\n',
        "",
    )
    path.write_text(text, encoding="utf-8")
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0)
    service.set("dream.llm.dream.driver", "openai_compatible", actor="console")
    service.set("dream.llm.dream.model", "moonshotai/Kimi-K3", actor="console")
    blob = path.read_text(encoding="utf-8")
    table = blob.split("[dream.llm.dream]", 1)[1].split("[", 1)[0]
    lines = [line for line in table.splitlines() if line.strip()]
    assert lines == ['driver = "openai_compatible"', 'model = "moonshotai/Kimi-K3"']
    assert load_config(path).llm["dream"].driver == "openai_compatible"
    assert load_config(path).llm["dream"].model == "moonshotai/Kimi-K3"


# ---------------------------------------------------------------- typed validation


def test_set_auto_trigger_requires_boolean(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.auto_trigger\].*boolean"):
        service.set("dream.auto_trigger", "yes", actor="console")


def test_set_token_budget_is_unknown_key(tmp_path) -> None:
    """AC1: dream.token_budget_usd was removed; a write against it is a typed
    unknown-key error naming the key."""
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.token_budget_usd\].*unknown"):
        service.set("dream.token_budget_usd", 5.0, actor="console")


def test_set_max_tokens_requires_positive_int(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.llm\.dream\.max_tokens\]"):
        service.set("dream.llm.dream.max_tokens", 1.5, actor="console")


def test_set_driver_requires_non_empty_string(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.llm\.dream\.driver\]"):
        service.set("dream.llm.dream.driver", "  ", actor="console")


def test_set_api_key_env_rejects_key_like_values(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in ("sk-abc123", "sk-proj-deadbeef", "openai_api_key"):
        with pytest.raises(ConfigWriteError, match=r"config\[dream\.llm\.dream\.api_key_env\]"):
            service.set("dream.llm.dream.api_key_env", bad, actor="console")


def test_set_api_key_env_empty_clears(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.llm.dream.api_key_env", "MNEMOSEED_DREAM_API_KEY", actor="console")
    service.set("dream.llm.dream.api_key_env", "", actor="console")
    assert "api_key_env" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- versioned record + audit


def test_set_records_version_and_audits_actor(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    result = service.set("dream.auto_trigger", True, actor="cli")
    assert result["ok"] is True
    assert isinstance(result["version_id"], int)
    assert result["restart_required"] is False  # seeded keys are all live-apply

    entry = meta.get_config("dream.auto_trigger")
    assert entry is not None
    assert entry.value["value"] is True
    assert entry.version == 1

    audit = _audit_entries(meta, "config.set")
    assert len(audit) == 1
    assert audit[0].actor == "cli"
    assert audit[0].detail["key_path"] == "dream.auto_trigger"
    assert audit[0].detail["value"] is True


def test_set_without_meta_patches_file_but_records_nothing(tmp_path) -> None:
    service, path = _service(tmp_path)  # meta=None: offline mode
    result = service.set("dream.auto_trigger", True, actor="console")
    assert result["version_id"] is None
    assert "auto_trigger = true" in path.read_text(encoding="utf-8")


def test_versions_lists_history_without_internal_keys(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    service.set("dream.auto_trigger", True, actor="console")
    service.set("dream.llm.dream.driver", "openai_compatible", actor="console")
    versions = service.versions()
    by_key = [v for v in versions if v["key"] == "dream.auto_trigger"]
    assert len(by_key) == 1
    assert isinstance(by_key[0]["version_id"], int)
    assert by_key[0]["value"] is True
    assert all("__" not in v["key"] for v in versions)


# ---------------------------------------------------------------- rollback (append-only)


def test_rollback_restores_file_and_live_config_append_only(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, path = _service(tmp_path, meta=meta)
    first = service.set("dream.auto_trigger", True, actor="console")["version_id"]
    service.set("dream.auto_trigger", False, actor="console")

    rolled = service.rollback(first, actor="console")
    assert rolled["ok"] is True
    assert rolled["restored"] == rolled["version_id"]
    assert "auto_trigger = true" in path.read_text(encoding="utf-8")
    assert service._config.dream.auto_trigger is True
    assert load_config(path).dream.auto_trigger is True

    # append-only: the rollback is a NEW version; every record survives
    entries = meta.get_config("dream.auto_trigger")
    assert entries is not None
    assert entries.version == 3
    assert entries.value["value"] is True
    assert meta.get_config("dream.auto_trigger", 2).value["value"] is False  # the reverted state stays

    audit = _audit_entries(meta, "config.rollback")
    assert len(audit) == 1
    assert audit[0].actor == "console"
    assert audit[0].detail["key_path"] == "dream.auto_trigger"


def test_rollback_unknown_version_is_typed_error(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    with pytest.raises(ConfigWriteError, match="version"):
        service.rollback(9_999_999_999, actor="console")


def test_rollback_without_meta_is_typed_error(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match="versioned"):
        service.rollback(1, actor="console")


# ---------------------------------------------------------------- generation (F2 hot-apply)


def test_generation_starts_zero_and_bumps_per_role_and_globally(tmp_path) -> None:
    """E1-2 (F2): every successful write bumps the global generation counter;
    a role-key write also bumps that role's generation so the role router can
    rebuild exactly the changed role."""
    service, _ = _service(tmp_path)
    assert service.generation == 0
    assert service.generation_for("dream") == 0
    service.set("dream.llm.dream.model", "stub2", actor="console")
    assert service.generation == 1
    assert service.generation_for("dream") == 1


def test_generation_bumps_globally_for_non_role_keys(tmp_path) -> None:
    service, _ = _service(tmp_path)
    service.set("dream.auto_trigger", True, actor="console")
    assert service.generation == 1
    assert service.generation_for("dream") == 0  # no per-role bump


def test_rollback_bumps_generation(tmp_path) -> None:
    """A rollback restores a previous value, so it is also a hot-apply event:
    the next run must rebuild the changed role."""
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    version = service.set("dream.llm.dream.model", "stub2", actor="console")["version_id"]
    assert service.generation == 1
    service.rollback(version, actor="console")
    assert service.generation == 2
    assert service.generation_for("dream") == 2


# ---------------------------------------------------------------- boot reconciliation (E1-4 DB-primary)


def test_reconcile_first_boot_imports_registry_keys_once_and_audits(tmp_path) -> None:
    """E1-4: with an empty settings DB the file's resolved registry values are
    imported EXACTLY ONCE, audited as ``config_import`` (actor=daemon); a later
    boot with no changes is a no-op and imports nothing again."""
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    result = service.reconcile_boot()
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["reason"] == "initial"
    assert "dream.auto_trigger" in result["keys_updated"]
    entry = meta.get_config("dream.auto_trigger")
    assert entry is not None and entry.value["value"] is True
    assert meta.get_config("dream.llm.dream.model").value["value"] == "stub"
    imports = _audit_entries(meta, "config_import")
    assert len(imports) == 1
    assert imports[0].actor == "daemon"
    assert imports[0].detail["reason"] == "initial"

    # the same boot state never imports again
    assert service.reconcile_boot()["changed"] is False
    assert service.reconcile_boot()["reason"] == "noop"
    assert len(_audit_entries(meta, "config_import")) == 1


def test_reconcile_hand_edit_is_mirror_drift_never_rebaseline(tmp_path) -> None:
    """E1-4: the settings DB is primary — a hand-edited config.toml is NOT
    rebaselined into the DB. The DB value wins on the live config and the toml
    mirror is regenerated from the DB, logged + audited as
    ``config_mirror_drift``."""
    meta = _meta(tmp_path)
    service, path = _service(tmp_path, meta=meta)
    service.reconcile_boot()
    # a user hand-edits the file while the daemon is down
    text = path.read_text(encoding="utf-8").replace('model = "stub"\n', 'model = "hand-edited"\n')
    path.write_text(text, encoding="utf-8")

    service = ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0)
    result = service.reconcile_boot()
    assert result["changed"] is True
    assert result["reason"] == "hand_edit"
    assert result["mirror_rewritten"] == ["dream.llm.dream.model"]
    # DB wins on the live config and the mirror line is regenerated from it
    assert service._config.llm["dream"].model == "stub"
    assert 'model = "hand-edited"' not in path.read_text(encoding="utf-8")
    assert 'model = "stub"' in path.read_text(encoding="utf-8")
    # the DB was never rebaselined from the file
    assert meta.get_config("dream.llm.dream.model").value["value"] == "stub"
    assert len(_audit_entries(meta, "config_import")) == 1  # still exactly one import
    drift = _audit_entries(meta, "config_mirror_drift")
    assert len(drift) == 1
    assert drift[0].actor == "daemon"
    assert drift[0].detail["drifted"] is True
    assert drift[0].detail["keys_rewritten"] == ["dream.llm.dream.model"]


def test_reconcile_db_value_wins_over_file_value_at_boot(tmp_path) -> None:
    """E1-4: a DB value that diverged from the file (restore/other-writer) wins
    at boot: the live config and the toml mirror both converge on the DB, and
    no hand-edit drift fires because the file itself never changed."""
    meta = _meta(tmp_path)
    service, path = _service(tmp_path, meta=meta)
    service.reconcile_boot()
    # the settings DB holds a newer value the file does not know about
    meta.set_config("dream.llm.dream.model", {"value": "db-model"})

    service = ConfigWriteService(load_config(path), meta)
    result = service.reconcile_boot()
    assert result["changed"] is True
    assert result["mirror_rewritten"] == ["dream.llm.dream.model"]
    assert service._config.llm["dream"].model == "db-model"
    # the mirror file was regenerated from the DB
    assert 'model = "db-model"' in path.read_text(encoding="utf-8")
    # no hand-edit drift: the file fingerprint never moved
    drift = _audit_entries(meta, "config_mirror_drift")
    assert len(drift) == 1
    assert drift[0].detail["drifted"] is False


def test_reconcile_boot_scope_keys_stay_file_scoped_and_restart_required(tmp_path) -> None:
    """E1-4: preset/storage/baseurl are boot-scope keys — never registry keys.
    A hand edit to one of them is a legitimate file-scoped change: it survives
    the next boot untouched, no mirror drift fires, and the DB holds no entry
    for it."""
    meta = _meta(tmp_path)
    service, path = _service(tmp_path, meta=meta)
    service.reconcile_boot()
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'baseurl = "http://localhost:7788"', 'baseurl = "http://localhost:9999"'
        ),
        encoding="utf-8",
    )

    service = ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0)
    result = service.reconcile_boot()
    assert result["changed"] is False
    assert result["reason"] == "noop"
    assert "baseurl" not in result["keys_updated"]
    assert "baseurl" not in result["mirror_rewritten"]
    # the file-scoped change survived (applies at next restart, as designed)
    assert 'baseurl = "http://localhost:9999"' in path.read_text(encoding="utf-8")
    assert len(_audit_entries(meta, "config_mirror_drift")) == 0
    assert meta.get_config("baseurl") is None  # never a registry key


def test_reconcile_completes_partial_import_without_overwriting(tmp_path) -> None:
    """E1-4: a DB holding SOME registry entries (aborted import / older writer)
    completes the one-shot import for the missing keys only — existing entries
    are never rebaselined from the file."""
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    service.reconcile_boot()
    # simulate a DB with one registry entry missing (aborted import / older writer)
    meta._conn.execute("DELETE FROM config WHERE key = 'dream.auto_trigger'")

    service = ConfigWriteService(load_config(_config_toml(tmp_path)), meta)
    result = service.reconcile_boot()
    assert result["changed"] is True
    assert "dream.auto_trigger" in result["keys_updated"]
    assert meta.get_config("dream.auto_trigger").value["value"] is True  # file value imported once
    assert meta.get_config("dream.llm.dream.model").value["value"] == "stub"  # untouched
    assert len(_audit_entries(meta, "config_import")) == 2  # completed import is audited too


def test_reconcile_without_meta_is_noop(tmp_path) -> None:
    service, _ = _service(tmp_path)
    assert service.reconcile_boot()["ok"] is False


# ---------------------------------------------------------------- resolved read (redacted)


def test_get_resolves_config_with_env_names_only(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    body = service.get()
    config = body["config"]
    assert config["preset"] == "embedded"
    assert "token_budget_usd" not in config["dream"]  # AC1: the removed key never surfaces
    assert config["dream"]["floor_pool_points"] == 10.0
    assert config["dream"]["idle_min_sec"] == 900.0
    assert config["dream"]["hard_deadline_sec"] == 86400.0
    deep = config["dream"]["llm"]["dream"]
    assert deep["driver"] == "stub"
    assert deep["model"] == "stub"
    assert deep["base_url"] == "http://example.test/v1"
    assert body["restart_required"] == {}


def test_get_redacts_literal_key_slipped_in_by_hand_edit(tmp_path) -> None:
    meta = _meta(tmp_path)
    path = _config_toml(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[dream.llm.dream]\n",
            '[dream.llm.dream]\napi_key_env = "sk-proj-literal-value"\n',
        ),
        encoding="utf-8",
    )
    service = ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0)
    blob = repr(service.get())
    assert "sk-proj-literal-value" not in blob
    assert "api_key_env" in blob  # the NAMES field still surfaces


def test_get_redacts_versions_too(tmp_path) -> None:
    meta = _meta(tmp_path)
    path = _config_toml(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[dream.llm.dream]\n",
            '[dream.llm.dream]\napi_key_env = "sk-proj-literal-value"\n',
        ),
        encoding="utf-8",
    )
    service = ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0)
    service.reconcile_boot()
    assert "sk-proj-literal-value" not in repr(service.versions())


# ------------------------------------------------- profiles.agent_bindings (#109)


def test_registry_carries_the_agent_bindings_key() -> None:
    """#109: the agent->profile binding map is a registry key, so binding
    writes ride the full single-writer pipeline (patch -> version -> audit ->
    live-apply, DB-wins at boot)."""
    assert "profiles.agent_bindings" in CONFIG_KEY_REGISTRY


def test_agent_bindings_write_patches_records_and_hot_applies(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, path = _service(tmp_path, meta=meta)
    result = service.set("profiles.agent_bindings", {"planner": "research"}, actor="cli")
    assert result["ok"] is True
    # the file mirror round-trips through the loader
    assert load_config(path).profiles.agent_bindings == {"planner": "research"}
    # hot-apply: the live config the daemon write path reads is updated
    assert service._config.profiles.agent_bindings == {"planner": "research"}
    assert service.generation == 1
    # versioned record + audit attribution
    listed = [entry for entry in service.versions() if entry["key"] == "profiles.agent_bindings"]
    assert len(listed) == 1
    assert listed[0]["value"] == {"planner": "research"}
    actions = [entry.action for entry in _audit_entries(meta, "config.set")]
    assert "config.set" in actions


def test_agent_bindings_reject_malformed_maps(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in ({"planner": ""}, {"": "research"}, {"planner": 7}, "planner=research", {"p": "  "}):
        with pytest.raises(ConfigWriteError, match=r"config\[profiles\.agent_bindings\]"):
            service.set("profiles.agent_bindings", bad, actor="console")


def test_agent_bindings_rollback_restores_file_and_live_config(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, path = _service(tmp_path, meta=meta)
    first = service.set("profiles.agent_bindings", {"planner": "research"}, actor="console")
    service.set("profiles.agent_bindings", {"planner": "archived-x"}, actor="console")
    rolled = service.rollback(first["version_id"], actor="console")
    assert rolled["ok"] is True
    assert load_config(path).profiles.agent_bindings == {"planner": "research"}
    assert service._config.profiles.agent_bindings == {"planner": "research"}


def test_agent_bindings_surface_on_the_read_face(tmp_path) -> None:
    service, _ = _service(tmp_path)
    service.set("profiles.agent_bindings", {"planner": "research"}, actor="console")
    body = service.get()
    assert body["config"]["profiles"]["agent_bindings"] == {"planner": "research"}
