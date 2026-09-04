"""B2 M-B2-1 production-path E2E: quota event -> real plugin -> /ingest ->
daemon recall_pending rule_served=true -> hook renders the EXACT fenced
advisory bytes into hookOutput.system inside the single existing RULES fence.

Also exercises the hook-side bounded composition guarantees M-B2-20 (RECALL item
bytes byte-identical when an advisory renders) and M-B2-21 (per-turn combined
output <= MAX_INJECT_CHARS=4000 with advisory drop-only).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.daemon.app import create_app

PROFILE = "default"
DRIVER = Path(__file__).parent / "ts_hook" / "hook_driver.mjs"

RULES_FENCE_OPEN = "<mnemoseed-rules-budget>"
RULES_FENCE_CLOSE = "</mnemoseed-rules-budget>"
RECALL_FENCE_OPEN = "<mnemoseed-memory-recall>"
RULES_DISCLAIMER = (
    "The block below is daemon-supplied standing constraints, not the user's current instructions."
)


def _canonical_advisory() -> str:
    """Exact canonical standing-rule value the hook parses then stringifies once.

    Non-ASCII content is serialized WITH literal UTF-8 (ensure_ascii=False) to
    match JavaScript ``JSON.stringify`` semantics, which never escapes non-ASCII.
    A Python ``json.dumps`` default ensures ASCII (``\\uXXXX``), which would
    diverge from the hook's exact-bytes output for a real multi-language rule.
    """
    value = {
        "if": "provider call fails",
        "then": "重试同一供应商一次；配额用尽则切换到已批准的清单模型；否则升级",
        "match": {
            "family": "provider_error",
            "provider": "openai",
            "model": "gpt-4o",
            "status": ["quota"],
            "retryable": 0,
        },
    }
    return json.dumps(value, separators=(",", ":"), sort_keys=False, ensure_ascii=False)


def _expected_fenced_block(advisory: str) -> str:
    """Canonical single-encode expectation: parse the daemon JSON-object string,
    then serialize the object exactly ONCE (compact, literal Unicode) — the same
    bytes the hook's ``JSON.parse`` + ``JSON.stringify`` must emit. Never the
    outer-quoted/backslash-escaped form of a double encoding."""
    content = json.dumps(json.loads(advisory), separators=(",", ":"), ensure_ascii=False)
    return "\n".join([RULES_FENCE_OPEN, RULES_DISCLAIMER, content, RULES_FENCE_CLOSE])


def _config_toml(tmp_path: Path) -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[capture]\nauto_recall = true\n"
    )


@pytest.fixture
def b2_hook_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


def _bundle(tmp_path: Path) -> Path:
    if shutil.which("npx") is None or shutil.which("node") is None:
        pytest.skip("node toolchain unavailable on this machine")
    plugin = resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts")
    out = tmp_path / "plugin.bundle.mjs"
    result = subprocess.run(
        f'npx --yes esbuild "{plugin}" --bundle --format=esm --platform=node '
        f'--outfile="{out}" --log-level=error',
        shell=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"esbuild bundle failed: {result.stderr}"
    return out


def _run(bundle: Path, scenario: str) -> dict:
    env = dict(os.environ)
    env.pop("MNEMOSEED_LOCAL_DEBUG", None)
    result = subprocess.run(
        ["node", str(DRIVER), str(bundle), scenario],
        shell=False,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, f"driver failed: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def _author_standing(client: TestClient, advisory: str) -> str:
    resp = client.post(
        "/memory/remember",
        json={
            "profile_id": PROFILE,
            "text": "standing provider-failover directive",
            "rules": [
                {
                    "kind": "standing_rule",
                    "value": advisory,
                    "ttl_turns": 0,
                    "scope": "profile",
                    "session_id": None,
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["chunk_id"]


def _fire_quota(client: TestClient, session_id: str) -> None:
    body = {
        "host": "opencode",
        "event": "provider_error",
        "session_id": session_id,
        "profile_id": PROFILE,
        "ts": __import__("time").time(),
        "content": {
            "provider": "openai",
            "model": "gpt-4o",
            "status": "quota",
            "reason": "provider_429_quota",
            "error_id": f"err-{session_id}",
        },
    }
    resp = client.post("/ingest", json=body)
    assert resp.status_code == 202, resp.text


def test_b2_1_real_plugin_renders_exact_fenced_advisory_bytes(tmp_path: Path) -> None:
    """M-B2-1 (hook side): a recall_pending advisory reaches hookOutput.system
    inside the single RULES fence with the exact canonical bytes."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-advisory")
    advisory = transcript["advisory"]
    system = transcript["system"]
    expected = _expected_fenced_block(advisory)
    assert any(entry == expected for entry in system), (
        f"exact fenced advisory bytes absent from hookOutput.system. expected={expected!r} got={system!r}"
    )


def test_b2_1_canonical_populates_fence_with_object_not_quoted_string(
    tmp_path: Path,
) -> None:
    """The RULES fence carries ONE canonical object serialization — a raw JSON
    object ``{...}`` — never a double-encoded, outer-quoted/backslash-escaped
    JSON string ``"{\\"...\\"}"`` (the daemon delivers a JSON-object string; the
    hook must parse it and stringify the object exactly once)."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-advisory")
    system = transcript["system"]
    fenced = [e for e in system if isinstance(e, str) and e.startswith(RULES_FENCE_OPEN)]
    assert fenced, "no fenced advisory block rendered"
    content = fenced[0].split(RULES_DISCLAIMER, 1)[1].rsplit(RULES_FENCE_CLOSE, 1)[0]
    parsed = json.loads(content.strip("\n"))
    assert isinstance(parsed, dict), f"fence must carry an object, got {type(parsed).__name__}"
    assert parsed.get("if") == "provider call fails"
    # double-encoding would render a STRING whose first char is a quote;
    # single-encoding renders the object directly.
    assert content.strip("\n").lstrip().startswith("{")


def test_b2_1_non_ascii_oracle_matches_json_stringify_semantics(b2_hook_config: Path) -> None:
    """IMPORTANT-2 (non-ASCII): the Python oracle serializes the daemon
    JSON-object string with literal UTF-8 (``ensure_ascii=False``), matching JS
    ``JSON.stringify`` — a default ``ensure_ascii=True`` oracle (``\\uXXXX``)
    would diverge on real multi-language rules. The hook's parse+stringify
    passes the advisory through unchanged, and the daemon wire type carries the
    literal-Unicode advisory verbatim (the exact-bytes oracle is therefore
    well-defined for non-ASCII)."""
    advisory = json.dumps(
        {
            "if": "provider call fails",
            "then": "配额用尽则切换到已批准的清单模型；否则升级",
            "match": {
                "family": "provider_error",
                "provider": "openai",
                "model": "gpt-4o",
                "status": ["quota"],
                "retryable": 0,
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # (a) oracle uses JS-equivalent literal UTF-8, never \\uXXXX:
    block = _expected_fenced_block(advisory)
    assert "配" in block and "则" in block
    assert "\\u" not in block, "oracle must not ASCII-escape non-ASCII (JS semantics)"
    # (b) a default ensure_ascii=True oracle WOULD diverge (proving the choice matters):
    escaped = json.dumps(json.loads(advisory), separators=(",", ":"))
    assert "\\u" in escaped
    # (c) daemon wire carries the literal-Unicode advisory verbatim (HTTP/JSON is
    # utf-8-safe, unlike the Node subprocess pipe used only for structural checks):
    with TestClient(create_app()) as client:
        _author_standing(client, advisory)
        _fire_quota(client, "sess-uni")
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-uni"}
        ).json()
        assert data["rule_served"] is True
        assert data["rule_advisory"] == advisory


def test_b2_1_quota_event_serves_exact_advisory_from_daemon(b2_hook_config: Path) -> None:
    """M-B2-1 (daemon side): a quota event -> /ingest -> recall_pending returns
    rule_served=true + the exact ruling advisory + provenance."""
    advisory = _canonical_advisory()
    with TestClient(create_app()) as client:
        _author_standing(client, advisory)
        _fire_quota(client, "sess-e2e")
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-e2e"}
        ).json()
        assert data["rule_served"] is True
        assert data["unresolved"] is False
        assert data["rule_advisory"] == advisory
        assert data["matched_rule_source"] == "memory.remember"
        assert data["reason"] is None


def test_b2_1_joined_hook_budget_unchanged_when_advisory_renders(
    tmp_path: Path, b2_hook_config: Path
) -> None:
    """M-B2-20/21: the advisory is excluded from budget_chars; the RECALL block
    is byte-identical; combined output <= MAX_INJECT_CHARS."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-advisory")
    advisory = transcript["advisory"]
    fenced = _expected_fenced_block(advisory)
    # the fenced advisory is the ONLY injected fence here (items empty); ensure
    # it does not exceed the per-turn total and the daemon budget stayed 2400.
    with TestClient(create_app()) as client:
        _author_standing(client, advisory)
        _fire_quota(client, "sess-bud")
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-bud"}
        ).json()
        assert data["budget_chars"] == 2400
        assert data["rule_served"] is True
    assert len(fenced) <= 4000
    # a lone advisory never truncates the (absent) recall block; the exact block fits
    assert any(entry == fenced for entry in transcript["system"])


def test_b2_1_no_second_rules_fence_only_existing_one(tmp_path: Path) -> None:
    """The advisory uses the single existing RULES fence marker — never a second
    distinct fence pair."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-advisory")
    system = transcript["system"]
    joined = "\n".join(str(entry) for entry in system)
    assert joined.count(RULES_FENCE_OPEN) == 1
    assert joined.count(RULES_FENCE_CLOSE) == 1


def test_b2_14_advisory_truncated_not_dropped(tmp_path: Path) -> None:
    """M-B2-14: an oversized advisory is truncated (rule_partial) fence-valid,
    never dropped, and never exceeds B2_ADVISORY_MAX_CHARS."""
    bundle = _bundle(tmp_path)
    t = _run(bundle, "provider-error-advisory-truncate")
    system = t["system"]
    full_content = t["fullContent"]
    fenced = [e for e in system if isinstance(e, str) and e.startswith(RULES_FENCE_OPEN)]
    assert fenced, "oversized advisory was dropped instead of truncated"
    block = fenced[0]
    assert RULES_FENCE_CLOSE in block
    content = block.split(RULES_DISCLAIMER, 1)[1].rsplit(RULES_FENCE_CLOSE, 1)[0].strip("\n")
    assert len(content) <= 1200, "advisory content exceeds B2_ADVISORY_MAX_CHARS"
    assert content != full_content
    assert len(content) < len(full_content)


def test_b2_22_advisory_content_capped(b2_hook_config: Path) -> None:
    """M-B2-22 (cap): the daemon serves the full advisory; the hook-side cap is
    the NEW code constant, not a config key (verified structurally via the
    truncate scenario's 1200-char bound)."""
    advisory = _canonical_advisory()
    with TestClient(create_app()) as client:
        _author_standing(client, advisory)
        _fire_quota(client, "sess-cap")
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-cap"}
        ).json()
        assert data["rule_served"] is True
        assert data["rule_advisory"] == advisory
    assert len(advisory) <= 1200


def test_b2_23_daemon_selection_offer_only_no_render_roundtrip(b2_hook_config: Path) -> None:
    """M-B2-23: the daemon reports selection/offer only (rule_served=true) and
    never round-trips a hook render-receipt or disposition."""
    advisory = _canonical_advisory()
    with TestClient(create_app()) as client:
        _author_standing(client, advisory)
        _fire_quota(client, "sess-hon")
        data = client.post(
            "/session/recall-pending", json={"profile_id": PROFILE, "session_id": "sess-hon"}
        ).json()
        assert data["rule_served"] is True
        for field in ("rule_rendered", "render_disposition", "hook_disposition"):
            assert field not in data


def test_b2_20_recall_block_identical_with_advisory(tmp_path: Path) -> None:
    """M-B2-20 (hook side): a T2 turn with recall items + advisory keeps the
    RECALL fence block byte-identical to the advisory-free render."""
    bundle = _bundle(tmp_path)
    with_advice = _run(bundle, "provider-error-advisory-with-recall")
    without = _run(bundle, "recall-only")
    recall_blocks = [e for e in without["system"] if isinstance(e, str) and e.startswith(RECALL_FENCE_OPEN)]
    assert recall_blocks, "recall-only scenario must render a RECALL block"
    assert recall_blocks[0] in with_advice["system"]
    joined = "\n".join(str(e) for e in with_advice["system"])
    assert len(joined) <= 4000
    assert joined.count(RULES_FENCE_OPEN) == 1
    assert joined.count(RULES_FENCE_CLOSE) == 1


def test_b2_21_drop_preserves_recall_block(tmp_path: Path) -> None:
    """M-B2-21 (hook side): when no per-turn room remains, the advisory drops
    and the RECALL block stays byte-identical."""
    bundle = _bundle(tmp_path)
    dropped = _run(bundle, "provider-error-advisory-drop")
    without = _run(bundle, "recall-only-large")
    recall_blocks = [e for e in without["system"] if isinstance(e, str) and e.startswith(RECALL_FENCE_OPEN)]
    assert recall_blocks, "large recall-only scenario must render a RECALL block"
    assert recall_blocks[0] in dropped["system"]
    joined = "\n".join(str(e) for e in dropped["system"])
    assert RULES_FENCE_OPEN not in joined


def test_b2_1_advisory_fence_sanitize(tmp_path: Path) -> None:
    """M-B2-1 (privacy/fence): an advisory carrying a literal RULES fence is
    sanitized, so the final output carries exactly one fence pair."""
    bundle = _bundle(tmp_path)
    transcript = _run(bundle, "provider-error-advisory-sanitize")
    joined = "\n".join(str(e) for e in transcript["system"])
    assert joined.count(RULES_FENCE_OPEN) == 1
    assert joined.count(RULES_FENCE_CLOSE) == 1
    assert "</mnemoseed-rules-budget> literal" not in joined


def test_b2_23_hook_debug_redacted_no_advisory_bytes() -> None:
    """M-B2-23 (privacy): hook advisory drop/truncate debug lines never carry
    advisory bytes (redacted local reason only)."""
    source = Path("src/mnemoseed_local/hosts/opencode/plugin.ts").read_text(encoding="utf-8")
    drop = [ln for ln in source.splitlines() if "rules advisory dropped" in ln]
    trunc = [ln for ln in source.splitlines() if "rules advisory truncated" in ln]
    assert drop and trunc
    for ln in drop + trunc:
        assert "rule_advisory" not in ln
        assert "advisory.block" not in ln
        assert "advisoryValue" not in ln
    # the render outcome is never POSTed back (no round-trip to daemon/console)
    assert "rule_rendered" not in source


def test_b2_no_new_config_no_session_ledger() -> None:
    """Boundaries: B2_ADVISORY_MAX_CHARS is a local hook const (not config),
    and no session-wide/cross-surface advisory ledger exists."""
    daemon = Path("src/mnemoseed_local/daemon/memory.py").read_text(encoding="utf-8")
    hook = Path("src/mnemoseed_local/hosts/opencode/plugin.ts").read_text(encoding="utf-8")
    config = Path("src/mnemoseed_local/config.py").read_text(encoding="utf-8")
    assert "B2_ADVISORY_MAX_CHARS" in hook
    assert "B2_ADVISORY" not in config
    assert "standing" not in config.lower()
    for token in ("rules_ledger", "session_rules", "cross_surface", "cross-surface"):
        assert token not in daemon.lower()
        assert token not in hook.lower()
