"""The store-level fused similarity (PRD-448 §8.2.1 gated-max): exhaustive
(dense, sparse) grid properties, pinned equality branches and the
W1-measured micro-tail cases.

Contract of ``_fused_similarity``:

- ``sparse is None`` -> the dense score verbatim (the unchanged None path);
- otherwise ``fused = 0.5*dense + 0.5*sparse``, returned as
  ``max(dense, fused)`` when the sparse leg carries any mass (``> 0.0``) and
  as the plain ``fused`` when it is exactly zero (bge-m3's XLM-R token ids
  are structurally disjoint across languages, so a cross-lingual query's
  sparse leg is exactly 0.0 and must keep today's admission arithmetic);
- per-row monotonic non-decreasing vs the plain fusion, bounded to [0, 1].
"""

from __future__ import annotations

import pytest

from mnemoseed_local.storage.drivers.lancedb_embedded import (
    _DENSE_FUSION_WEIGHT,
    _fused_similarity,
)


def _plain_fused(dense: float, sparse: float) -> float:
    return _DENSE_FUSION_WEIGHT * dense + (1.0 - _DENSE_FUSION_WEIGHT) * sparse


_GRID = [round(i / 400, 6) for i in range(401)]


def test_sparse_none_returns_dense_verbatim() -> None:
    for dense in (0.0, 0.25, 0.4449, 0.45, 0.5527, 1.0):
        assert _fused_similarity(dense, None) == dense


def test_zero_sparse_keeps_the_plain_fusion_exactly() -> None:
    for dense in _GRID:
        assert _fused_similarity(dense, 0.0) == _plain_fused(dense, 0.0)
    assert _fused_similarity(0.4893, 0.0) == pytest.approx(0.24465)


@pytest.mark.parametrize(
    ("dense", "sparse", "expected"),
    [
        # W1-measured micro-tails: the gate opens (sparse > 0) and the row is
        # admitted at its dense value (the disclosed honest-empty regression)
        (0.4833, 0.0001, 0.4833),
        (0.5830, 0.0953, 0.5830),
        (0.4429, 0.0963, 0.4429),
        (0.3970, 0.0014, 0.3970),
        (0.5324, 0.0004, 0.5324),
        # the W1 natural-probe targets: sparse-drag removed, dense stands
        (0.5527, 0.1696, 0.5527),
        (0.4615, 0.3061, 0.4615),
        # sparse above dense: the fusion itself is the max
        (0.4, 0.9, 0.65),
        (0.0, 0.5, 0.25),
        (0.5, 1.0, 0.75),
    ],
)
def test_pinned_cases(dense: float, sparse: float, expected: float) -> None:
    assert _fused_similarity(dense, sparse) == pytest.approx(expected, abs=1e-12)


def test_grid_gated_never_below_plain_fusion_and_bounded() -> None:
    for dense in _GRID:
        for sparse in _GRID:
            gated = _fused_similarity(dense, sparse)
            plain = _plain_fused(dense, sparse)
            assert gated >= plain - 1e-12, (dense, sparse)
            assert 0.0 <= gated <= 1.0, (dense, sparse)


def test_grid_branch_structure() -> None:
    for dense in _GRID:
        for sparse in _GRID:
            gated = _fused_similarity(dense, sparse)
            if sparse == 0.0:
                assert gated == _plain_fused(dense, sparse)
            elif sparse <= dense:
                assert gated == dense
            else:
                assert gated == _plain_fused(dense, sparse)


def test_grid_monotonic_non_decreasing_in_sparse() -> None:
    for dense in _GRID:
        previous = _fused_similarity(dense, 0.0)
        for sparse in _GRID[1:]:
            current = _fused_similarity(dense, sparse)
            assert current >= previous - 1e-12, (dense, sparse)
            previous = current


def test_dense_one_sparse_one_stays_at_one() -> None:
    assert _fused_similarity(1.0, 1.0) == 1.0
