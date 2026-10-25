"""conversation visibility: the daemon-side contract.

``[visibility] mode`` (off | compact | verbose, default compact) rides the
existing wire fields: /session/recent and /session/recall-pending answers carry
a ``visibility`` object; the daemon-side per-session first-capture latch is
consumed through the recall-pending pull (the claude_code host is stateless).
Field semantics are additive: an absent ``visibility`` key means off for the
hooks (old daemon x new hook stays byte-identical), and the field changes no
existing key.
"""

from __future__ import annotations

import re
from pathlib import Path

import auth_support
import pytest
from fastapi.testclient import TestClient

from mnemoseed_local.config import (
    DEFAULT_VISIBILITY_MODE,
    VISIBILITY_MODES,
    default_config_toml,
    load_config,
)
from mnemoseed_local.hosts.claude_code import events as events_module
from mnemoseed_local.schema.turn import HostId

PROFILE = "default"


def _config_toml(tmp_path: Path, visibility: str = "") -> str:
    return (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.graph.instances.isolated]\npath = "{(tmp_path / "isolated.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
        "[dream.llm.dream]\n"
        'driver = "stub"\n'
        'model = "stub"\n' + visibility
    )


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, visibility: str = "") -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_toml(tmp_path, visibility), encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed_local.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("mnemoseed_local.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Default-visibility daemon config (the compact default)."""
    return _write_config(tmp_path, monkeypatch)


@pytest.fixture
def disabled_recall_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """capture.auto_recall=false: the T2 lane answers disabled while the
    visibility field still rides (the capture-only stdout case)."""
    return _write_config(tmp_path, monkeypatch, "[capture]\nauto_recall = false\n")


def _ingest(client: TestClient, session_id: str, ts: float, text: str, event: str = "user_prompt") -> None:
    content: dict = {"text": text}
    if event == "tool_use":
        content = {"tool_name": "Bash", "input": {}, "output": text}
    response = client.post(
        "/ingest",
        json={
            "host": HostId.CLAUDE_CODE.value,
            "event": event,
            "session_id": session_id,
            "profile_id": PROFILE,
            "ts": ts,
            "content": content,
        },
    )
    assert response.status_code == 202, response.text


def _pull(client: TestClient, session_id: str) -> dict:
    body = client.post("/session/recall-pending", json={"profile_id": PROFILE, "session_id": session_id})
    assert body.status_code == 200, body.text
    return body.json()


# ---------------------------------------------------------------- [visibility] config


def test_visibility_mode_enum_and_default() -> None:
    assert VISIBILITY_MODES == frozenset({"off", "compact", "verbose"})
    assert DEFAULT_VISIBILITY_MODE == "compact"


def test_visibility_defaults_to_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, monkeypatch)
    assert load_config(tmp_path / "missing.toml").visibility.mode == "compact"


@pytest.mark.parametrize("mode", ["off", "compact", "verbose"])
def test_visibility_mode_parses_all_three_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _write_config(tmp_path, monkeypatch, f'[visibility]\nmode = "{mode}"\n')
    assert load_config(tmp_path / "config.toml").visibility.mode == mode


def test_visibility_mode_rejects_invalid_value_names_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch, '[visibility]\nmode = "loud"\n')
    with pytest.raises(Exception, match=r"config\[visibility\.mode\]"):
        load_config(tmp_path / "config.toml")


def test_default_template_documents_visibility_and_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = default_config_toml()
    assert "[visibility]" in template
    assert "compact" in template and "verbose" in template and "off" in template
    _write_config(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text(template, encoding="utf-8")
    assert load_config(tmp_path / "config.toml").visibility.mode == "compact"


# ---------------------------------------------------------------- wire fields


def test_session_recent_carries_the_visibility_mode(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        body = client.post("/session/recent", json={"profile_id": PROFILE, "sessions": 1})
        assert body.status_code == 200, body.text
        assert body.json()["visibility"] == {"mode": "compact"}


def test_recall_pending_carries_visibility_with_no_pending_capture(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        payload = _pull(client, "never-captured")
        assert payload["visibility"] == {"mode": "compact", "capture": False}


def test_recall_pending_disabled_path_still_carries_visibility(disabled_recall_path: Path) -> None:
    with auth_support.boot(disabled_recall_path.parent) as client:
        payload = _pull(client, "never-captured")
        assert payload["enabled"] is False
        assert payload["visibility"] == {"mode": "compact", "capture": False}


# ---------------------------------------------------------------- capture latch


def test_first_capture_signals_once_then_stays_silent(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "sess-cap", 1.0, "第一次捕获")
        first = _pull(client, "sess-cap")
        assert first["visibility"] == {"mode": "compact", "capture": True}
        second = _pull(client, "sess-cap")
        assert second["visibility"] == {"mode": "compact", "capture": False}
        _ingest(client, "sess-cap", 2.0, "第二次捕获")
        third = _pull(client, "sess-cap")
        assert third["visibility"] == {"mode": "compact", "capture": False}


def test_assistant_and_tool_ingests_also_count_as_first_capture(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "sess-ast", 1.0, "助手回复", event="assistant_message")
        assert _pull(client, "sess-ast")["visibility"]["capture"] is True


def test_capture_latch_waits_through_off_and_fires_after_the_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mode=off consumes nothing: the armed latch fires on the first pull after
    the mode flips (hot-applied, next event)."""
    _write_config(tmp_path, monkeypatch, '[visibility]\nmode = "off"\n')
    with auth_support.boot(tmp_path) as client:
        _ingest(client, "sess-off", 1.0, "关灯期间捕获")
        assert _pull(client, "sess-off")["visibility"] == {"mode": "off", "capture": False}
        flipped = client.post("/api/v1/config/set", json={"key_path": "visibility.mode", "value": "compact"})
        assert flipped.json()["ok"] is True
        assert _pull(client, "sess-off")["visibility"] == {"mode": "compact", "capture": True}


def test_verbose_mode_reports_capture_counts_and_resets_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch, '[visibility]\nmode = "verbose"\n')
    with auth_support.boot(tmp_path) as client:
        _ingest(client, "sess-v", 1.0, "第一条")
        _ingest(client, "sess-v", 1.5, "第二条", event="assistant_message")
        _ingest(client, "sess-v", 2.0, "第三条", event="tool_use")
        payload = _pull(client, "sess-v")
        assert payload["visibility"] == {"mode": "verbose", "capture": 3}
        assert _pull(client, "sess-v")["visibility"] == {"mode": "verbose", "capture": False}
        _ingest(client, "sess-v", 3.0, "第四条")
        assert _pull(client, "sess-v")["visibility"] == {"mode": "verbose", "capture": 1}


def test_verbose_mode_reports_zero_as_no_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, monkeypatch, '[visibility]\nmode = "verbose"\n')
    with auth_support.boot(tmp_path) as client:
        _ingest(client, "sess-v0", 1.0, "第一条")
        assert _pull(client, "sess-v0")["visibility"]["capture"] == 1
        # no capture since the last serve: no signal, not a zero count
        assert _pull(client, "sess-v0")["visibility"]["capture"] is False


def test_verbose_consumption_keeps_the_compact_first_capture_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verbose pull (zero or non-zero count) never consumes the compact
    latch: after a verbose->compact flip the once-per-session line still fires
    exactly once."""
    _write_config(tmp_path, monkeypatch, '[visibility]\nmode = "verbose"\n')
    with auth_support.boot(tmp_path) as client:
        _ingest(client, "sess-vc", 1.0, "第一条")
        assert _pull(client, "sess-vc")["visibility"] == {"mode": "verbose", "capture": 1}
        assert _pull(client, "sess-vc")["visibility"] == {"mode": "verbose", "capture": False}
        flipped = client.post("/api/v1/config/set", json={"key_path": "visibility.mode", "value": "compact"})
        assert flipped.json()["ok"] is True
        assert _pull(client, "sess-vc")["visibility"] == {"mode": "compact", "capture": True}
        assert _pull(client, "sess-vc")["visibility"] == {"mode": "compact", "capture": False}


def test_capture_latch_is_scoped_per_session(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "sess-one", 1.0, "会话一")
        assert _pull(client, "sess-other")["visibility"]["capture"] is False
        assert _pull(client, "sess-one")["visibility"]["capture"] is True


def test_session_end_clears_the_capture_latch(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "sess-end", 1.0, "待结算")
        settled = client.post("/session/end", json={"session_id": "sess-end", "profile_id": PROFILE})
        assert settled.status_code == 200, settled.text
        assert _pull(client, "sess-end")["visibility"]["capture"] is False


# ---------------------------------------------------------------- configwrite hot-apply + rollback


def test_visibility_mode_hot_applies_and_rolls_back(config_path: Path) -> None:
    """Acceptance 1: the mode hot-applies to the NEXT event through the
    configwrite surface (no restart), and a rollback to the recorded version
    hot-applies the same way."""
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "sess-hot", 1.0, "热生效轮")
        assert _pull(client, "sess-hot")["visibility"]["mode"] == "compact"
        written = client.post("/api/v1/config/set", json={"key_path": "visibility.mode", "value": "off"})
        assert written.status_code == 200, written.text
        assert _pull(client, "sess-hot")["visibility"]["mode"] == "off"
        # undo = roll back to the PRE-WRITE version (the boot-imported compact)
        versions = client.get("/api/v1/config/versions").json()["versions"]
        compact_ids = [v["version_id"] for v in versions if v["value"] == "compact"]
        assert compact_ids, f"the imported compact version must be recorded: {versions}"
        rolled = client.post("/api/v1/config/rollback", json={"version_id": compact_ids[-1]})
        assert rolled.status_code == 200, rolled.text
        assert _pull(client, "sess-hot")["visibility"]["mode"] == "compact"


def test_visibility_mode_write_rejects_invalid_values(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        response = client.post("/api/v1/config/set", json={"key_path": "visibility.mode", "value": "loud"})
        assert response.status_code == 422, response.text


def test_visibility_mode_surfaces_on_the_config_get(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        body = client.get("/api/v1/config").json()["config"]
        assert body["visibility"]["mode"] == "compact"


# ---------------------------------------------------------------- wire-compat (additive)


def test_visibility_field_is_additive_over_the_existing_recall_keys(config_path: Path) -> None:
    with auth_support.boot(config_path.parent) as client:
        _ingest(client, "sess-a", 1.0, "上一轮我们把 LanceDb 定为向量存储")
        client.post("/session/end", json={"session_id": "sess-a", "profile_id": PROFILE})
        _ingest(client, "sess-b", 2.0, "LanceDb 现在处于什么阶段")
        payload = _pull(client, "sess-b")
        for key in (
            "enabled",
            "items",
            "non_focal_above_floor",
            "budget_chars",
            "slot_consumed",
            "detector_fired",
            "rule_served",
        ):
            assert key in payload, f"existing key {key} must survive the additive field"
        recent = client.post("/session/recent", json={"profile_id": PROFILE, "sessions": 1}).json()
        for key in ("profile_id", "sessions", "self_window", "guidance", "selection"):
            assert key in recent, f"existing key {key} must survive the additive field"


# ---------------------------------------------------------------- cross-host signal oracle


PLUGIN_TS = (
    Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local" / "hosts" / "opencode" / "plugin.ts"
)
SHARED_RECALL_PY = (
    Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local" / "hosts" / "common" / "recall.py"
)
EVENTS_PY = (
    Path(__file__).resolve().parents[1] / "src" / "mnemoseed_local" / "hosts" / "claude_code" / "events.py"
)

T1_SIGNAL = "[mnemoseed] replayed context from a previous session"
T2_SIGNAL = "[mnemoseed] recalled related memories"
CAPTURE_SIGNAL = "[mnemoseed] captured this session"

#: The imperative ban list of the regex oracle (acceptance 4): a signal states
#: what mnemoseed did and never instructs the model.
IMPERATIVE_WORD_RE = re.compile(
    r"\b(must|should|shall|use|please|ensure|remember|always|never|do not|don't)\b", re.IGNORECASE
)


def _ts_signal_constants() -> list[str]:
    """The shipped TS signal literals and count-line templates."""
    source = PLUGIN_TS.read_text(encoding="utf-8")
    lines = []
    for name in ("T1_SIGNAL", "T2_SIGNAL", "CAPTURE_SIGNAL"):
        match = re.search(rf'const {name} = "([^"]+)"', source)
        assert match is not None, f"{name} missing from plugin.ts"
        lines.append(match.group(1))
    for name in ("t1CountLine", "t2CountLine", "captureCountLine"):
        pattern = rf"function {name}\([^)]*\): string \{{\n  return `([^`]+)`"
        match = re.search(pattern, source)
        assert match is not None, f"{name} missing from plugin.ts"
        lines.append(re.sub(r"\$\{[^}]+\}", "{}", match.group(1)))
    return lines


def test_signal_vocabulary_is_fixed_and_shared_across_hosts() -> None:
    """The two hosts carry the same shared signal bytes and the fixed count
    templates; any rename or new line fails the vocabulary pin."""
    ts_lines = _ts_signal_constants()
    assert ts_lines[:3] == [T1_SIGNAL, T2_SIGNAL, CAPTURE_SIGNAL]
    assert ts_lines[3] == "[mnemoseed] {} sessions, {} memories, {} chars"
    assert ts_lines[4] == "[mnemoseed] {} memories, {} chars"
    assert ts_lines[5] == "[mnemoseed] captured {} turns since the last report"
    source = SHARED_RECALL_PY.read_text(encoding="utf-8")
    for name, expected in (("T2_SIGNAL", T2_SIGNAL), ("CAPTURE_SIGNAL", CAPTURE_SIGNAL)):
        match = re.search(rf'{name} = "([^"]+)"', source)
        assert match is not None and match.group(1) == expected, name
    # the claude_code count lines render the same bytes as the TS templates
    assert events_module.recall_count_line(2, 1200) == "[mnemoseed] 2 memories, 1200 chars"
    assert events_module.capture_count_line(3) == "[mnemoseed] captured 3 turns since the last report"


def test_signal_lines_carry_no_imperative_content() -> None:
    """Acceptance 4 regex oracle: no signal line or count template may instruct
    the model (must/should/use/please/ensure/remember/always/never/do not)."""
    for line in [T1_SIGNAL, T2_SIGNAL, CAPTURE_SIGNAL] + _ts_signal_constants()[3:]:
        assert IMPERATIVE_WORD_RE.search(line) is None, line
    for rendered in (
        events_module.recall_count_line(1, 1),
        events_module.capture_count_line(1),
    ):
        assert IMPERATIVE_WORD_RE.search(rendered) is None, rendered


def test_signal_text_never_enters_the_needle_registry() -> None:
    """Acceptance 4 needle isolation, source-level: the TS host never feeds a
    signal into the needle channel, by constant name, by alias, or by inline
    literal, and the claude_code host has no needle channel at all. The
    behavioral proof lives in the driver oracle."""
    ts_source = PLUGIN_TS.read_text(encoding="utf-8")
    for name in ("T1_SIGNAL", "T2_SIGNAL", "CAPTURE_SIGNAL"):
        assert ("registerNeedles(registry, " + name) not in ts_source
        assert ("needlesOf(" + name) not in ts_source
    # alias-tight: every signal literal appears exactly once (its definition),
    # so neither a second named reference nor an inline copy can reach the
    # registry; the needle channel itself has exactly two call sites, both fed
    # from chunk text variables.
    for literal in (T1_SIGNAL, T2_SIGNAL, CAPTURE_SIGNAL):
        assert ts_source.count(literal) == 1, literal
    assert len(re.findall(r"registerNeedles\(", ts_source)) == 5  # definition + 4 call sites
    assert len(re.findall(r"registerNeedles\(registry, (?:text|slicedText), chunkId\)", ts_source)) == 4
    assert len(re.findall(r"needlesOf\(", ts_source)) == 2  # definition + the registerNeedles call
    py_source = EVENTS_PY.read_text(encoding="utf-8")
    assert "needles_of(" not in py_source
