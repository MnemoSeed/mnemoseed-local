"""Warm-needle material class (design/10 §5.2, Gate 2): the within-session
re-query replay shape.

Each point measures ONE needle fact in ONE session under successive
/memory/recall calls: a first query recalls it, then a changed-wording
re-query moments later probes whether it re-surfaces. Unlike the degenerate
singleton pool, every point now carries a competing SAME-BAND decoy (same
entity, different fact) so the needle can genuinely keep or lose its top-k
slot — the instrument's metric must be able to move. A negative-control probe
(decoy-aligned: a decoy's cue should win) proves the instrument can also
measure the needle NOT surfacing. The factory is deterministic under the
pinned seed — material identity is part of any future calibration bar.
"""

from __future__ import annotations

from mnemoseed_local.eval.warm_materials import (
    WARM_MATERIALS_SEED,
    WINDOW_DELAYED,
    WINDOW_IMMEDIATE,
    WINDOW_NEGATIVE_CONTROL,
    warm_materials,
)


def test_warm_materials_are_deterministic_and_structured() -> None:
    """Same seed -> byte-identical batch; every point carries ONE needle fact,
    one same-band decoy, a first query, and per-window probes (immediate +
    delayed + negative-control) with declared delays."""
    first = warm_materials()
    second = warm_materials()

    assert first == second
    assert len(first) >= 4
    assert WARM_MATERIALS_SEED == 20260829
    point_ids = [m.point_id for m in first]
    assert len(set(point_ids)) == len(point_ids)  # unique point identifiers
    windows: set[str] = set()
    for material in first:
        # the same-session shape: one stored needle + a same-band decoy set,
        # one session context
        assert isinstance(material.fact_text, str) and material.fact_text
        assert len(material.decoys) >= 4
        decoy_ids = [decoy_id for decoy_id, _ in material.decoys]
        assert material.fact_id not in decoy_ids
        for _decoy_id, decoy_text in material.decoys:
            assert isinstance(decoy_text, str) and decoy_text
        assert isinstance(material.session_id, str) and material.session_id
        assert isinstance(material.first_query, str) and material.first_query
        for probe in material.probes:
            windows.add(probe.window)
            assert probe.delay_s >= 0.0
            assert isinstance(probe.re_query, str) and probe.re_query
    assert {WINDOW_IMMEDIATE, WINDOW_DELAYED, WINDOW_NEGATIVE_CONTROL} <= windows


def test_each_point_carries_competing_same_band_decoys() -> None:
    """A decoy shares the entity/domain but asserts a DIFFERENT fact — the
    needle must be able to genuinely keep or lose its top-k slot (even drop out
    of top-k), never a vacuous rank-1 in an empty pool."""
    for material in warm_materials():
        needle_lower = material.fact_text.casefold()
        for _decoy_id, decoy_text in material.decoys:
            assert decoy_text.casefold() != needle_lower


def test_re_query_changes_the_wording_of_the_first_query() -> None:
    """A warm-needle re-query is a re-wording (换措辞), never a byte-for-byte
    repeat of the first query — otherwise the instrument would measure query
    identity, not content re-surfacing."""
    for material in warm_materials():
        for probe in material.probes:
            assert material.first_query.casefold() != probe.re_query.casefold()


def test_each_probe_window_carries_an_explicit_delay() -> None:
    """The warm window is keyed by an explicit delay: immediate (0s) vs a
    later delayed window vs the negative control (0s, decoy-aligned) — the
    axis a future activation's decay is judged on."""
    for material in warm_materials():
        by_window = {probe.window: probe.delay_s for probe in material.probes}
        assert by_window[WINDOW_IMMEDIATE] == 0.0
        assert by_window[WINDOW_DELAYED] > 0.0
        assert by_window[WINDOW_NEGATIVE_CONTROL] == 0.0


def test_immediate_and_delayed_probe_queries_genuinely_differ() -> None:
    """The delayed window must not be a verbatim clone of the immediate one —
    otherwise the instrument cannot report the two windows as differing. The
    delayed window's cue-weakened wording lets a competing decoy claim the
    slot, which is exactly the decay the window exists to probe."""
    for material in warm_materials():
        by_window = {probe.window: probe for probe in material.probes}
        assert by_window[WINDOW_IMMEDIATE].re_query != by_window[WINDOW_DELAYED].re_query
