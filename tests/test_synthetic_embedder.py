"""SyntheticEmbedder determinism (prd-08 D7): identical text yields identical
dense and sparse vectors across calls and across fresh instances, with no
network or model involved.
"""

import math

import pytest

from mnemoseed_local.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed_local.storage.ports import Capability, EmbeddingResult
from mnemoseed_local.storage.registry import EMBED_DRIVERS, register

_DIM = 64


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not EMBED_DRIVERS.contains("synthetic"):
        register(EMBED_DRIVERS)(SyntheticEmbedder)
    yield


def test_registered_in_shared_registry():
    assert EMBED_DRIVERS.contains("synthetic")


def test_capabilities_declared():
    caps = SyntheticEmbedder.info.capabilities
    assert Capability.EMBED_LOCAL_INFERENCE in caps
    assert Capability.EMBED_BATCH in caps
    assert Capability.EMBED_SPARSE_OUTPUT in caps


def test_embed_deterministic_same_instance():
    embedder = SyntheticEmbedder(dimension=_DIM)
    first = embedder.embed("the quick brown fox jumps")
    second = embedder.embed("the quick brown fox jumps")
    assert first.dense == second.dense
    assert first.sparse is not None and second.sparse is not None
    assert first.sparse.indices == second.sparse.indices
    assert first.sparse.values == second.sparse.values


def test_embed_deterministic_across_instances():
    first = SyntheticEmbedder(dimension=_DIM).embed("all work and no play")
    second = SyntheticEmbedder(dimension=_DIM).embed("all work and no play")
    assert first.dense == second.dense
    assert first.sparse == second.sparse


def test_embed_differs_across_texts():
    embedder = SyntheticEmbedder(dimension=_DIM)
    a = embedder.embed("alpha bravo charlie")
    b = embedder.embed("delta echo foxtrot")
    assert a.dense != b.dense
    assert a.sparse != b.sparse


def test_dense_dimension_and_unit_length():
    embedder = SyntheticEmbedder(dimension=_DIM)
    result = embedder.embed("measure of a unit vector")
    assert len(result.dense) == _DIM
    norm = math.sqrt(sum(v * v for v in result.dense))
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_sparse_parallel_and_positive():
    result = SyntheticEmbedder(dimension=_DIM).embed("sparse weights stay positive")
    assert result.sparse is not None
    assert len(result.sparse.indices) == len(result.sparse.values)
    assert all(index >= 0 for index in result.sparse.indices)
    assert all(value > 0.0 for value in result.sparse.values)


def test_sparse_shares_indices_across_common_tokens():
    embedder = SyntheticEmbedder(dimension=_DIM)
    shared = embedder.embed("alpha gamma")
    other = embedder.embed("alpha gamma extra words here")
    shared_indices = set(shared.sparse.indices if shared.sparse else ())
    other_indices = set(other.sparse.indices if other.sparse else ())
    # shared tokens hash to the same indices in both texts (token identity)
    assert shared_indices & other_indices
    assert shared_indices <= other_indices  # adding tokens never drops shared ones


def test_sparse_disjoint_lexicons():
    embedder = SyntheticEmbedder(dimension=_DIM)
    left = embedder.embed("mars venus earth")
    right = embedder.embed("quantum chess lexicon")
    left_indices = set(left.sparse.indices if left.sparse else ())
    right_indices = set(right.sparse.indices if right.sparse else ())
    assert not left_indices & right_indices


def test_embed_batch_matches_individual():
    embedder = SyntheticEmbedder(dimension=_DIM)
    texts = ["first sentence here", "second one there", "third away"]
    results = embedder.embed_batch(texts)
    assert isinstance(results, list)
    assert all(isinstance(result, EmbeddingResult) for result in results)
    for text, result in zip(texts, results, strict=True):
        solo = embedder.embed(text)
        assert result.dense == solo.dense
        assert result.sparse == solo.sparse
