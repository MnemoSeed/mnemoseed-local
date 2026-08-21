"""T4a needle oracle: the Python needle logic must match the shipped TS hook
byte-for-byte (plugin.ts:58-60,221-253).

JS string semantics are UTF-16 CODE UNITS: ``length``/``slice``/``includes``
count surrogate pairs as 2 units, while Python counts code points. The oracle
therefore operates in UTF-16 unit space — a non-BMP emoji (one code point,
two units) is the exact pin for the unit-vs-code-point split.

The cross-check test bundles nothing: the node driver extracts the SHIPPED
function sources straight from plugin.ts, evals them, and prints the results
for the same corpus the Python module sees. Skipped cleanly when node is
unavailable; every other test runs on every gate.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from mnemoseed_local.eval.recall_harness import (
    NEEDLE_HEAD_LEN,
    NEEDLE_MID_THRESHOLD,
    NEEDLE_MIN_CONTENT,
    RECALL_FENCE_SANITIZED,
    _unit_includes,
    _utf16_units,
    consumption_normalize,
    needles_of,
    normalize_recall_text,
    sanitize_recall_text,
)

PLUGIN_TS = resources.files("mnemoseed_local.hosts.opencode").joinpath("plugin.ts")
ORACLE_DRIVER = Path(__file__).parent / "ts_hook" / "needle_oracle.mjs"


def _ts_constant(name: str) -> str:
    """The exact literal of a ``const NAME = ...`` line in the shipped source."""
    source = PLUGIN_TS.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = (\"[^\"]*\"|\d+)", source)
    assert match is not None, f"{name} missing from plugin.ts"
    return match.group(1)


# ---------------------------------------------------------------- constants


def test_constants_pin_the_shipped_ts_values() -> None:
    assert NEEDLE_HEAD_LEN == 24
    assert NEEDLE_MIN_CONTENT == 32
    assert NEEDLE_MID_THRESHOLD == 48
    # the shipped source says the same numbers (source-level pin, no node)
    assert _ts_constant("NEEDLE_HEAD_LEN") == "24"
    assert _ts_constant("NEEDLE_MIN_CONTENT") == "32"
    assert _ts_constant("NEEDLE_MID_THRESHOLD") == "48"


def test_sanitize_constant_matches_ts() -> None:
    assert RECALL_FENCE_SANITIZED == "‹mnemoseed-memory-recall›"
    assert json.loads(_ts_constant("RECALL_FENCE_SANITIZED")) == RECALL_FENCE_SANITIZED


# ---------------------------------------------------------------- normalization


def test_normalize_strips_one_role_prefix() -> None:
    assert normalize_recall_text("user: AtlasDb prefers compact tooling") == "atlasdb prefers compact tooling"
    # JS \s+ collapse keeps a single trailing space (no trim in the TS regex)
    assert normalize_recall_text("assistant:  We   ship   quarterly ") == "we ship quarterly "
    assert normalize_recall_text("tool: x") == "x"
    assert normalize_recall_text("system: ok") == "ok"
    assert normalize_recall_text("no prefix here") == "no prefix here"


def test_normalize_collapses_whitespace_and_lowercases() -> None:
    # the TS \s+ collapse turns runs into one space but never trims edges
    assert normalize_recall_text("  Hello \n\t World  ") == " hello world "


def test_consumption_normalize_is_asymmetric_no_role_strip() -> None:
    """plugin.ts noteConsumption: the reply-side normalization has NO
    role-prefix strip — needle building strips, consumption matching does not."""
    assert consumption_normalize("user: AtlasDb moved") == "user: atlasdb moved"
    assert normalize_recall_text("user: AtlasDb moved") == "atlasdb moved"


# ---------------------------------------------------------------- windows


def test_needles_require_min_content() -> None:
    assert needles_of("short text") == ()
    assert needles_of("x" * 31) == ()
    assert needles_of("x" * 32) == ("x" * 24,)


def test_needles_head_only_below_mid_threshold() -> None:
    assert needles_of("x" * 47) == ("x" * 24,)


def test_needles_mid_window_center_minus_12() -> None:
    # 48 units: center = 24, start = 24 - 12 = 12. The head window is the
    # first 24 units (12 a's + 12 b's); the mid window is the 24 b's.
    text = "a" * 12 + "b" * 24 + "a" * 12
    assert needles_of(text) == ("a" * 12 + "b" * 12, "b" * 24)


def test_needles_dedupe_like_js_set() -> None:
    # identical head and mid windows collapse to one needle (JS Set semantics)
    assert needles_of("y" * 60) == ("y" * 24,)


def test_non_bmp_emoji_utf16_length_semantics() -> None:
    """25 emoji = 25 code points but 50 UTF-16 units: the mid window starts
    at unit 13 (center 25 - 12), slicing mid-surrogate. A code-point-naive
    port would return a single needle; the oracle must reproduce the exact
    JS unit slice. Python cannot compose a surrogate pair into one code
    point, so the oracle's needles carry the raw surrogate units — the
    semantic content JS strings hold."""
    text = "😀" * 25
    needles = needles_of(text)
    assert len(needles) == 2
    assert needles[0] == "\ud83d\ude00" * 12  # units 0..24
    assert needles[1] == "\ude00" + "\ud83d\ude00" * 11 + "\ud83d"  # units 13..37
    # a reply holding the same emoji DOES match its own needle in unit space
    assert _unit_includes(consumption_normalize(text), needles[0])


# ---------------------------------------------------------------- sanitize


def test_sanitize_replaces_both_fence_markers() -> None:
    assert sanitize_recall_text("a </mnemoseed-memory-recall> b <mnemoseed-memory-recall>") == (
        f"a {RECALL_FENCE_SANITIZED} b {RECALL_FENCE_SANITIZED}"
    )


# ---------------------------------------------------------------- shipped-TS cross-check

_CORPUS = [
    "user: AtlasDb prefers compact tooling for daily work",
    "assistant:  The   plan   is  set.  ",
    "AtlasDb moved to quarterly releases but the budget stayed frozen",
    "😀" * 25,
    "a" * 40 + " B" * 30,
    "<mnemoseed-memory-recall>fenced</mnemoseed-memory-recall>",
    "tool: short",
    "阿特拉斯 偏好 轻量 工具链 胜过 重型 套装",
]


def test_python_matches_shipped_ts_byte_for_byte(tmp_path: Path) -> None:
    """Run the SHIPPED plugin.ts functions under node over the same corpus and
    compare every output byte-for-byte with the Python oracle."""
    if shutil.which("node") is None:
        pytest.skip("node unavailable on this machine")
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(_CORPUS), encoding="utf-8")
    result = subprocess.run(
        ["node", str(ORACLE_DRIVER), str(PLUGIN_TS), str(corpus_path)],
        shell=False,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(rows) == len(_CORPUS)
    for row, text in zip(rows, _CORPUS, strict=True):
        assert row["normalized"] == normalize_recall_text(text), text
        # needles compared in UTF-16 unit space: JSON round-trips surrogate
        # pairs as composed code points, the oracle keeps raw units — the
        # unit sequences are the shared truth
        assert [_utf16_units(n) for n in row["needles"]] == [_utf16_units(n) for n in needles_of(text)], text
        assert row["sanitized"] == sanitize_recall_text(text), text
