"""Driver-agnostic contract tests for the Embedder port (prd-08 appendix B.4).

Both contract arms share the deterministic synthetic embedder (prd-08 D7): fixed
dimension, hash-derived dense + structured sparse, no network and no model.
"""

from __future__ import annotations

import math

import pytest

from mnemoseed_local.storage.ports import Capability


def test_capabilities(stack) -> None:
    expected = frozenset(
        {Capability.EMBED_LOCAL_INFERENCE, Capability.EMBED_BATCH, Capability.EMBED_SPARSE_OUTPUT}
    )
    assert stack.embed.capabilities() == stack.embed.info.capabilities == expected
    assert stack.embed.dimension == stack.dimension


def test_embed_dense_deterministic_and_normalized(stack) -> None:
    first = stack.embed.embed("alpha beta gamma")
    second = stack.embed.embed("alpha beta gamma")
    assert len(first.dense) == stack.dimension
    assert first.dense == second.dense
    norm = math.sqrt(sum(value * value for value in first.dense))
    assert norm == pytest.approx(1.0, abs=1e-9)


def test_embed_sparse_is_structured_and_nonempty(stack) -> None:
    result = stack.embed.embed("alpha beta gamma alpha")
    sparse = result.sparse
    assert sparse is not None
    assert len(sparse.indices) == len(sparse.values)
    assert sparse.indices == tuple(sorted(sparse.indices))
    assert sparse.values == tuple(sorted(sparse.values))
    assert all(value > 0.0 for value in sparse.values)
    assert 0 < len(sparse.indices) <= 32


def test_embed_tokens_drive_sparse_similarity(stack) -> None:
    """Shared tokens produce overlapping sparse vectors; disjoint ones differ."""
    shared = stack.embed.embed("neural memory consolidation amnesia")
    disjoint = stack.embed.embed("quarterly finance spreadsheet pivot table")
    overlap = set(shared.sparse.indices) & set(disjoint.sparse.indices)
    assert len(overlap) == 0


def test_embed_batch_preserves_order_and_matches_single(stack) -> None:
    texts = ["first contract sentence", "second contract sentence", "third contract"]
    batch = stack.embed.embed_batch(texts)
    assert len(batch) == len(texts)
    for text, result in zip(texts, batch, strict=False):
        solo = stack.embed.embed(text)
        assert result.dense == solo.dense
        assert result.sparse == solo.sparse


def test_embed_batch_empty(stack) -> None:
    assert stack.embed.embed_batch([]) == []
