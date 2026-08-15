"""PRD-03 FR-3.2: retrieval-side cue extractor — deterministic routing cues.

The extractor parses recent conversation text into the same ``Cues`` shape the
capture funnel stamps at write time (FR-1.6), plus a coarse intent signal, so
query-side cues are directly comparable to the chunk-side cues for entity
overlap and the situational-weak-cue rerank weight (FR-3.14).
"""

from __future__ import annotations

from mnemoseed_local.retrieve.cues import (
    CueConfig,
    CueExtractor,
    ExtractedCues,
    Intent,
    extract_cues,
)
from mnemoseed_local.schema.stamp import Cues

EXTRACT = CueExtractor().extract


# --------------------------------------------------------------- entities


def test_backticked_identifiers_become_entities() -> None:
    result = EXTRACT("refactor `connect_pool` and `retry_logic` before the release")
    assert result.cues.entities == ["connect_pool", "retry_logic"]


def test_capitalized_tokens_become_entities() -> None:
    result = EXTRACT("LanceDb and BgeM3 are both embedded; MnemoSeed uses them")
    entities = result.cues.entities
    assert "LanceDb" in entities
    assert "BgeM3" in entities
    assert "MnemoSeed" in entities


def test_plain_sentence_initial_words_are_not_entities() -> None:
    result = EXTRACT("The BgeM3 model is here")
    assert "The" not in result.cues.entities


def test_cjk_runs_become_entities() -> None:
    result = EXTRACT("修复了版本链的分片问题")
    entities = result.cues.entities
    assert "版本链" in entities
    assert "分片" in entities


def test_file_path_basename_becomes_entity() -> None:
    result = EXTRACT("changed src/mnemoseed/capture/scorer.py again")
    assert "scorer.py" in result.cues.entities


def test_entity_dedup_is_casefold_and_order_is_first_seen() -> None:
    result = EXTRACT("`connect_pool` then `Connect_Pool` again")
    assert result.cues.entities == ["connect_pool"]


def test_entities_cap_enforced() -> None:
    result = CueExtractor(CueConfig(entities_cap=2)).extract("FooBar BazQux CorgePlugh GraultThud")
    assert result.cues.entities == ["FooBar", "BazQux"]


# --------------------------------------------------------------- project


def test_windows_path_project_resolves_to_repo() -> None:
    result = EXTRACT("pushed to " + r"G:\Development\MnemoSeed\core" + " and rebuilt")
    assert result.cues.project == "MnemoSeed"


def test_org_slash_repo_is_project() -> None:
    result = EXTRACT("merged kumhou/Opmiere into main")
    assert result.cues.project == "Opmiere"


def test_most_recent_project_mention_wins() -> None:
    result = EXTRACT("worked on G:/Development/AlphaOne then moved to G:/Development/Opmiere")
    assert result.cues.project == "Opmiere"


def test_relative_source_path_does_not_yield_project() -> None:
    result = EXTRACT("fixed the bug in src/mnemoseed/capture/scorer.py")
    assert result.cues.project is None


def test_explicit_project_noun_after_keyword() -> None:
    result = EXTRACT("the Opmiere repo needs a v2")
    assert result.cues.project == "Opmiere"


def test_user_home_path_yields_no_project() -> None:
    result = EXTRACT(r"C:\Users\alice")
    assert result.cues.project is None


def test_user_desktop_file_yields_no_project() -> None:
    result = EXTRACT(r"edited C:\Users\alice\Desktop\notes.txt")
    assert result.cues.project is None


def test_url_org_repo_with_subpath_yields_project() -> None:
    result = EXTRACT("check github.com/kumhou/Opmiere/pull/12")
    assert result.cues.project == "Opmiere"
    assert "12" not in result.cues.entities
    assert "github.com" not in result.cues.tools_used


# --------------------------------------------------------------- tools


def test_mcp_tool_shapes() -> None:
    result = EXTRACT("called mcp__MCP_DOCKER__browser_click and mcp__n8n_mcp__get_node")
    assert "mcp__MCP_DOCKER__browser_click" in result.cues.tools_used
    assert "mcp__n8n_mcp__get_node" in result.cues.tools_used


def test_backticked_camel_kebab_snake_are_tools() -> None:
    result = EXTRACT("ran `retryQueue`, `clean-up`, and `browser_click`")
    assert "retryQueue" in result.cues.tools_used
    assert "clean-up" in result.cues.tools_used
    assert "browser_click" in result.cues.tools_used


def test_dotted_tool_name_is_tool() -> None:
    result = EXTRACT("check memory.recall when unsure")
    assert "memory.recall" in result.cues.tools_used


def test_dotted_filename_is_not_tool() -> None:
    result = EXTRACT("edited scorer.py and base.py")
    assert result.cues.tools_used == []


def test_backticked_config_filename_is_not_tool() -> None:
    result = EXTRACT("read `config.toml`")
    assert result.cues.tools_used == []
    assert "config.toml" in result.cues.entities


def test_backticked_python_filename_is_not_tool() -> None:
    result = EXTRACT("open `scorer.py`")
    assert result.cues.tools_used == []
    assert "scorer.py" in result.cues.entities


def test_hostname_dotted_token_is_not_tool() -> None:
    result = EXTRACT("check github.com/kumhou/Opmiere")
    assert result.cues.tools_used == []
    assert result.cues.project == "Opmiere"


def test_plain_lowercase_backtick_is_entity_only() -> None:
    result = EXTRACT("run `python` to verify")
    assert "python" in result.cues.entities
    assert result.cues.tools_used == []


def test_tools_cap_enforced() -> None:
    result = CueExtractor(CueConfig(tools_cap=2)).extract("`firstTool` `secondTool` `thirdTool`")
    assert result.cues.tools_used == ["firstTool", "secondTool"]


# --------------------------------------------------------------- intent


def test_intent_debug_english() -> None:
    assert EXTRACT("this bug keeps crashing — fix the traceback").intent is Intent.DEBUG


def test_intent_debug_chinese() -> None:
    assert EXTRACT("这个报错又卡死了，帮忙修一下").intent is Intent.DEBUG


def test_intent_implement_english() -> None:
    assert EXTRACT("let's implement the migration and deploy it").intent is Intent.IMPLEMENT


def test_intent_implement_chinese() -> None:
    assert EXTRACT("重构一下评分模块，然后新增测试").intent is Intent.IMPLEMENT


def test_intent_decide_english() -> None:
    assert EXTRACT("I decided to go with LanceDb instead").intent is Intent.DECIDE


def test_intent_decide_chinese() -> None:
    assert EXTRACT("决定改用 pgvector 做向量库").intent is Intent.DECIDE


def test_intent_recall_english() -> None:
    assert EXTRACT("as we discussed last time, keep it simple").intent is Intent.RECALL


def test_intent_recall_chinese() -> None:
    assert EXTRACT("我们之前讨论过这个方案").intent is Intent.RECALL


def test_intent_other() -> None:
    assert EXTRACT("the weather is pleasant today").intent is Intent.OTHER


def test_intent_reasons_report_markers() -> None:
    result = EXTRACT("这个报错要修")
    assert "报错" in result.intent_reasons


# ------------------------------------------- situational weak cues + contract


def test_host_project_time_bucket_context_passes_through() -> None:
    result = EXTRACT(
        "work on the cache loader",
        host="cursor",
        project="MnemoSeed",
        time_bucket="2026-W32",
    )
    assert result.cues.host == "cursor"
    assert result.cues.project == "MnemoSeed"
    assert result.cues.time_bucket == "2026-W32"


def test_text_project_overrides_context_project() -> None:
    result = EXTRACT("release kumhou/Opmiere today", project="MnemoSeed")
    assert result.cues.project == "Opmiere"


def test_empty_text_is_benign() -> None:
    result = EXTRACT("   ")
    assert result.cues.entities == []
    assert result.cues.project is None
    assert result.cues.tools_used == []
    assert result.intent is Intent.OTHER


def test_empty_text_with_context_still_passes_weak_cues() -> None:
    result = EXTRACT("", host="claude-code", time_bucket="2026-W32")
    assert result.cues.host == "claude-code"
    assert result.cues.time_bucket == "2026-W32"


def test_output_is_stable_across_calls() -> None:
    text = "debug `connect_pool` in G:/Development/Opmiere after the mcp__MCP_DOCKER__browser_click run"
    assert EXTRACT(text) == EXTRACT(text)


def test_result_uses_the_capture_cues_schema() -> None:
    result = EXTRACT("anything")
    assert isinstance(result.cues, Cues)


def test_module_level_convenience_matches() -> None:
    result = extract_cues("fix the `heater` bug", host="cursor")
    assert isinstance(result, ExtractedCues)
    assert result.intent is Intent.DEBUG
    assert "heater" in result.cues.entities
    assert result.cues.host == "cursor"
