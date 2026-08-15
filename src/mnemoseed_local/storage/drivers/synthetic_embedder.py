"""Deterministic synthetic embedder: hash-based pseudo-vectors for tests and CI.

Produces fixed-dimension dense vectors plus a sparse struct from token hashes.
Never runs a model, never touches the network, and two calls with the same text
yield identical outputs. Contract tests and CI use this driver so the embedded
preset is exercisable without downloading a 500MB model (prd-08 D7).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

from mnemoseed_local.storage.ports import (
    Capability,
    DriverInfo,
    EmbeddingResult,
    SparseVector,
)
from mnemoseed_local.storage.registry import EMBED_DRIVERS, register

_CAPABILITIES = frozenset(
    {
        Capability.EMBED_LOCAL_INFERENCE,
        Capability.EMBED_BATCH,
        Capability.EMBED_SPARSE_OUTPUT,
    }
)

_TOKEN_SPLIT = re.compile(r"[\s,.;:!?()\[\]{}<>\"'`~@#$%^&*_+=|/\\-]+")


@register(EMBED_DRIVERS)
class SyntheticEmbedder:
    """Hash-based pseudo-embedder: dense + sparse, fully deterministic."""

    info = DriverInfo(
        name="synthetic",
        capabilities=_CAPABILITIES,
        description="deterministic hash pseudo-vectors for tests and CI",
    )

    def __init__(
        self,
        dimension: int = 64,
        sparse_capacity: int = 65_536,
        **kwargs: Any,
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        if sparse_capacity <= 0:
            raise ValueError("sparse_capacity must be positive")
        self.params: dict[str, Any] = kwargs
        self.dimension = dimension
        self.sparse_capacity = sparse_capacity

    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    def embed(self, text: str) -> EmbeddingResult:
        dense = self._dense(text)
        sparse = self._sparse(text)
        return EmbeddingResult(dense=dense, sparse=sparse)

    def embed_batch(self, texts: Sequence[str]) -> list[EmbeddingResult]:
        return [self.embed(text) for text in texts]

    # ------------------------------------------------------------ internals

    def _digest(self, *parts: bytes) -> bytes:
        loader = hashlib.sha256()
        for part in parts:
            loader.update(part)
        return loader.digest()

    def _dense(self, text: str) -> list[float]:
        seed = self._digest(text.encode("utf-8"))
        values: list[float] = []
        norm = 0.0
        for i in range(self.dimension):
            digest = self._digest(seed, i.to_bytes(4, "big"))
            raw = int.from_bytes(digest[:4], "big") / 2**32
            value = raw * 2.0 - 1.0
            values.append(value)
            norm += value * value
        if norm > 0.0:
            scale = norm**0.5
            values = [v / scale for v in values]
        return values

    def _tokens(self, text: str) -> list[str]:
        lower = text.lower()
        return [token for token in _TOKEN_SPLIT.split(lower) if token]

    def _sparse(self, text: str) -> SparseVector:
        weights: dict[int, float] = {}
        for token in self._tokens(text):
            digest = self._digest(token.encode("utf-8"))
            index = int.from_bytes(digest[:4], "big") % self.sparse_capacity
            raw = int.from_bytes(digest[4:8], "big") / 2**32
            weight = max(raw * 2.0 - 1.0, 0.0)
            if weight > 0.0:
                weights[index] = max(weights.get(index, 0.0), weight)
        indices = tuple(sorted(weights))
        values = tuple(weights[index] for index in indices)
        return SparseVector(indices=indices, values=values)
