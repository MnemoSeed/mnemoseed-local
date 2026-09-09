"""Foundation tests for the shared P-008 evaluation helpers.

Exercises normalize_exp and payload_key only. No daemon, config, provider,
port, or model use: the owned module is stdlib-only.
"""

import copy
import os
import subprocess
import sys

import pytest

from mnemoseed_local.eval.experience_common import normalize_exp, payload_key

# -- normalize_exp ----------------------------------------------------------


def test_casefold_and_trim():
    assert normalize_exp("  Hello World  ") == "hello world"


def test_whitespace_collapse():
    assert normalize_exp("a  b\tc\nd") == "a b c d"


def test_nfkc_fullwidth_and_ligature():
    assert normalize_exp("\uff28\uff45\uff4c\uff4c\uff4f") == "hello"
    assert normalize_exp("\ufb01sh") == "fish"


def test_single_trailing_punctuation_removed():
    assert normalize_exp("hello.") == "hello"
    assert normalize_exp("hello?") == "hello"
    assert normalize_exp("hello;") == "hello"
    assert normalize_exp("hello,") == "hello"


def test_at_most_one_trailing_punctuation_char():
    assert normalize_exp("hello?!") == "hello?"
    assert normalize_exp("hello..") == "hello."


def test_chinese_sentence_punctuation_removed_once():
    assert normalize_exp("\u4f60\u597d\u3002") == "\u4f60\u597d"
    assert normalize_exp("\u4f60\u597d\uff01") == "\u4f60\u597d"


def test_trailing_space_after_punctuation_trimmed():
    assert normalize_exp("hello. ") == "hello"


def test_internal_punctuation_preserved():
    assert normalize_exp("well, hello") == "well, hello"
    assert normalize_exp("don't stop") == "don't stop"


def test_cpp_csharp_apostrophe_stay_distinct():
    cpp = normalize_exp("C++")
    csharp = normalize_exp("C#")
    assert cpp == "c++"
    assert csharp == "c#"
    assert cpp != csharp
    assert normalize_exp("it's.") == "it's"


# -- payload_key: all kinds ---------------------------------------------------


def test_fact_key_normalizes_all_fields():
    payload = {
        "subject": " Alice ",
        "predicate": "Likes",
        "object": "Tea.",
        "polarity": "Positive",
    }
    assert payload_key("fact", payload) == ("fact", "alice", "likes", "tea", "positive")


def test_lesson_key():
    assert payload_key("lesson", {"text": " Retry  after  backoff. "}) == (
        "lesson",
        "retry after backoff",
    )


def test_intention_key_includes_frozen_status():
    payload = {"trigger_condition": "If X", "action": "Do Y.", "status": "pending"}
    assert payload_key("intention", payload) == ("intention", "if x", "do y", "pending")


def test_skill_key_normalizes_task_and_chain():
    payload = {"task_type": "Backup ", "tool_chain": ["A ", "b."], "success_rate": 0.5}
    assert payload_key("skill_sequence", payload) == (
        "skill_sequence",
        "backup",
        ("list", ("a", "b")),
        0.5,
    )


# -- payload_key: malformed inputs ----------------------------------------------


def test_missing_fact_polarity_raises():
    with pytest.raises(ValueError):
        payload_key("fact", {"subject": "a", "predicate": "b", "object": "c"})


def test_missing_fact_subject_raises():
    with pytest.raises(ValueError):
        payload_key("fact", {"predicate": "b", "object": "c", "polarity": "positive"})


def test_non_string_fact_field_raises():
    with pytest.raises(ValueError):
        payload_key(
            "fact",
            {"subject": 5, "predicate": "b", "object": "c", "polarity": "positive"},
        )


def test_invalid_intention_status_raises():
    with pytest.raises(ValueError):
        payload_key(
            "intention",
            {"trigger_condition": "t", "action": "a", "status": "archived"},
        )


def test_cased_intention_status_raises():
    with pytest.raises(ValueError):
        payload_key(
            "intention",
            {"trigger_condition": "t", "action": "a", "status": "Pending"},
        )


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        payload_key("chunk", {"text": "hi"})


def test_non_dict_payload_raises():
    for bad in (None, [], "fact", 42):
        with pytest.raises(ValueError):
            payload_key("fact", bad)


# -- payload_key: skill rate shapes ------------------------------------------------


def test_skill_none_rate_raises():
    with pytest.raises(ValueError):
        payload_key(
            "skill_sequence",
            {"task_type": "t", "tool_chain": [], "success_rate": None},
        )


def test_skill_bool_rate_raises():
    with pytest.raises(ValueError):
        payload_key(
            "skill_sequence",
            {"task_type": "t", "tool_chain": [], "success_rate": True},
        )


def test_skill_string_rate_raises():
    with pytest.raises(ValueError):
        payload_key(
            "skill_sequence",
            {"task_type": "t", "tool_chain": [], "success_rate": "0.9"},
        )


def test_skill_nonfinite_rate_raises():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            payload_key(
                "skill_sequence",
                {"task_type": "t", "tool_chain": [], "success_rate": bad},
            )


def test_skill_int_float_equivalence():
    base = {"task_type": "t", "tool_chain": ["a"], "success_rate": 1}
    same = {"task_type": "t", "tool_chain": ["a"], "success_rate": 1.0}
    assert payload_key("skill_sequence", base) == payload_key("skill_sequence", same)


# -- payload_key: structure, order, extras -------------------------------------------


def test_structured_tool_chain_order_matters():
    first = {"task_type": "t", "tool_chain": ["a", "b"], "success_rate": 1.0}
    second = {"task_type": "t", "tool_chain": ["b", "a"], "success_rate": 1.0}
    assert payload_key("skill_sequence", first) != payload_key("skill_sequence", second)


def test_structured_tool_chain_dict_order_ignored():
    first = {
        "task_type": "t",
        "tool_chain": [{"b": 1, "a": 2}],
        "success_rate": 1,
    }
    second = {
        "task_type": "t",
        "tool_chain": [{"a": 2, "b": 1}],
        "success_rate": 1.0,
    }
    assert payload_key("skill_sequence", first) == payload_key("skill_sequence", second)


def test_bool_vs_number_stays_distinct():
    with_bool = {"task_type": "t", "tool_chain": [True], "success_rate": 1.0}
    with_num = {"task_type": "t", "tool_chain": [1], "success_rate": 1.0}
    assert payload_key("skill_sequence", with_bool) != payload_key("skill_sequence", with_num)
    keyed_bool = {"task_type": "t", "tool_chain": [{True: "x"}], "success_rate": 1.0}
    keyed_num = {"task_type": "t", "tool_chain": [{1: "x"}], "success_rate": 1.0}
    assert payload_key("skill_sequence", keyed_bool) != payload_key("skill_sequence", keyed_num)


def test_extras_ignored_in_key_but_not_mutated():
    plain = {"text": "Retry soon."}
    extended = {"text": "Retry soon.", "confidence": 0.9, "weight": 2, "scope": "s1"}
    snapshot = copy.deepcopy(extended)
    assert payload_key("lesson", extended) == payload_key("lesson", plain)
    assert extended == snapshot


def test_inputs_never_mutated():
    payload = {
        "task_type": "Backup",
        "tool_chain": [{"Tool": "A ", "args": {"n": 1}}],
        "success_rate": 1,
        "confidence": 0.7,
    }
    snapshot = copy.deepcopy(payload)
    first = payload_key("skill_sequence", payload)
    second = payload_key("skill_sequence", payload)
    assert first == second
    assert payload == snapshot


def _assert_fresh_import_clean(module_name, exercise):
    """Import the owned module in a fresh interpreter and reject forbidden imports.

    The check runs in a new process, so earlier suites in the same pytest run
    cannot pollute the result through ambient sys.modules.
    """
    script = (
        "import sys; "
        f"import {module_name} as target; "
        f"{exercise}; "
        "bad = sorted(n for n in sys.modules "
        "if n.startswith(('mnemoseed_local.daemon', 'mnemoseed_local.config')) "
        "or (n.startswith('mnemoseed_local') and 'provider' in n)); "
        "sys.exit('forbidden imports: ' + ','.join(bad) if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_no_daemon_config_provider_imports():
    _assert_fresh_import_clean(
        "mnemoseed_local.eval.experience_common",
        "target.normalize_exp('Hi'); target.payload_key('lesson', {'text': 'Go.'})",
    )


# -- payload_key: freezer collision regressions ----------------------------------


def test_empty_list_vs_empty_dict_tool_chain():
    with_list = {"task_type": "t", "tool_chain": [[]], "success_rate": 1.0}
    with_dict = {"task_type": "t", "tool_chain": [{}], "success_rate": 1.0}
    assert payload_key("skill_sequence", with_list) != payload_key("skill_sequence", with_dict)


def test_empty_dict_vs_dict_mimic_list_tool_chain():
    with_dict = {"task_type": "t", "tool_chain": [{}], "success_rate": 1.0}
    mimic = {"task_type": "t", "tool_chain": [["dict", []]], "success_rate": 1.0}
    assert payload_key("skill_sequence", with_dict) != payload_key("skill_sequence", mimic)


def test_nested_dict_vs_dict_mimic_list_tool_chain():
    with_dict = {"task_type": "t", "tool_chain": [{"a": {}}], "success_rate": 1.0}
    mimic = {
        "task_type": "t",
        "tool_chain": [{"a": ["dict", []]}],
        "success_rate": 1.0,
    }
    assert payload_key("skill_sequence", with_dict) != payload_key("skill_sequence", mimic)


def test_none_vs_none_string_tool_chain():
    with_none = {"task_type": "t", "tool_chain": [None], "success_rate": 1.0}
    with_str = {"task_type": "t", "tool_chain": [["none"]], "success_rate": 1.0}
    assert payload_key("skill_sequence", with_none) != payload_key("skill_sequence", with_str)


def test_nested_primitive_tags_stay_distinct():
    bool_key = payload_key("skill_sequence", {"task_type": "t", "tool_chain": [True], "success_rate": 1.0})
    bool_mimic = payload_key(
        "skill_sequence",
        {"task_type": "t", "tool_chain": ["bool", True], "success_rate": 1.0},
    )
    assert bool_key != bool_mimic
    num_key = payload_key("skill_sequence", {"task_type": "t", "tool_chain": [1], "success_rate": 1.0})
    num_mimic = payload_key(
        "skill_sequence", {"task_type": "t", "tool_chain": ["num", 1], "success_rate": 1.0}
    )
    assert num_key != num_mimic
    none_key = payload_key("skill_sequence", {"task_type": "t", "tool_chain": [None], "success_rate": 1.0})
    none_mimic = payload_key(
        "skill_sequence",
        {"task_type": "t", "tool_chain": ["none", None], "success_rate": 1.0},
    )
    assert none_key != none_mimic


def test_adjacent_huge_integers_stay_distinct():
    low = 9007199254740992
    high = 9007199254740993
    low_chain = payload_key("skill_sequence", {"task_type": "t", "tool_chain": [low], "success_rate": 1.0})
    high_chain = payload_key("skill_sequence", {"task_type": "t", "tool_chain": [high], "success_rate": 1.0})
    assert low_chain != high_chain
    low_rate = payload_key("skill_sequence", {"task_type": "t", "tool_chain": [], "success_rate": low})
    high_rate = payload_key("skill_sequence", {"task_type": "t", "tool_chain": [], "success_rate": high})
    assert low_rate != high_rate


def test_huge_finite_integers_raise_no_exception():
    payload = {"task_type": "t", "tool_chain": [10**1000], "success_rate": 10**1000}
    first = payload_key("skill_sequence", payload)
    second = payload_key(
        "skill_sequence",
        {"task_type": "t", "tool_chain": [10**1000], "success_rate": 10**1000},
    )
    assert first == second
