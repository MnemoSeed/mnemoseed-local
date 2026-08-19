"""Audit redaction (FR/G-AC2): observability surfaces never echo secret
material. Names (env var names / secrets: refs) render; literals are masked.

Two paths guarded here:
1. the presentation helper `redact_key_ref_for_display` (secrets/refs grammar);
2. the daemon-side audit written when a role is materialized/configured —
   a literal pasted into `[dream.llm.<role>].api_key_env` (tolerated by the
   loader) must surface as `<redacted>`, never verbatim.
"""

from __future__ import annotations

from mnemoseed_local.secrets.refs import redact_key_ref_for_display


def test_helper_keeps_env_name_chains_and_secrets_refs() -> None:
    assert redact_key_ref_for_display("MNEMOSEED_TEST, FIREWORKS_API_KEY") == (
        "MNEMOSEED_TEST, FIREWORKS_API_KEY"
    )
    assert redact_key_ref_for_display("secrets:mnemoseed/dream/deep_reflection") == (
        "secrets:mnemoseed/dream/deep_reflection"
    )
    assert redact_key_ref_for_display("") == ""


def test_helper_redacts_literal_key_values() -> None:
    for value in ("sk-abc123", "wk-abc.def", "AKIA1234567890", "whsec_abc", "bearer tok"):
        assert redact_key_ref_for_display(value) == "<redacted>", value


def test_helper_allows_multi_part_upper_snake_names() -> None:
    assert redact_key_ref_for_display("FIREWORKS_API_KEY") == "FIREWORKS_API_KEY"
    assert redact_key_ref_for_display("MNEMOSEED_DEEP_REFLECTION_API_KEY,FIREWORKS_API_KEY") == (
        "MNEMOSEED_DEEP_REFLECTION_API_KEY,FIREWORKS_API_KEY"
    )
    # single-token uppercase chains without a separator are NOT treated as names
    assert redact_key_ref_for_display("AKIA1234567890") == "<redacted>"


def test_role_configured_audit_does_not_echo_a_literal_key(tmp_path, monkeypatch) -> None:
    """Boot the real app with a literal pasted into [dream.llm.dream].api_key_env
    (tolerated by the loader today); the audit row must redact it."""
    import json

    from fastapi.testclient import TestClient

    from mnemoseed_local.daemon.app import create_app
    from mnemoseed_local.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
    from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
    from mnemoseed_local.storage.registry import (
        EMBED_DRIVERS,
        GRAPH_DRIVERS,
        META_DRIVERS,
        VECTOR_DRIVERS,
        register,
    )

    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)

    canary = "sk-audit-redact-canary-77"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        '[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        f'api_key_env = "{canary}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)

    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/audit")
        assert resp.status_code == 200
        text = json.dumps(resp.json(), ensure_ascii=False)
        assert canary not in text
        assert "<redacted>" in text
