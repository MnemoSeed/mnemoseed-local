"""Product-bound recall replay over the real MemoryService output."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from mnemoseed_local.credential import DaemonCredentialError, read_credential_at
from mnemoseed_local.rest_client import credential_headers

PayloadSource = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class EntryObservation:
    id: str
    kind: str
    rank: int
    score: float
    tokens: int
    text: str


@dataclass(frozen=True)
class RecallObservation:
    qid: str
    query: str
    profile_id: str
    session_id: str | None
    entries: tuple[EntryObservation, ...]
    dropped_count: int
    tokens_used: int
    budget_tokens: int
    coverage: Mapping[str, Any]

    @property
    def served_ids(self) -> tuple[str, ...]:
        return tuple(entry.id for entry in self.entries)


def product_payload_source(service: Any, **kwargs: Any) -> dict[str, Any]:
    payload: dict[str, Any] = service.recall(**kwargs)
    return payload


class HttpRecallService:
    def __init__(self, base_url: str, *, home: Path | None = None, timeout: float = 300.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers: dict[str, str] = {}
        if home is not None:
            try:
                self._headers = credential_headers(read_credential_at(home))
            except DaemonCredentialError as exc:
                raise ValueError("evaluation home has no daemon credential") from exc

    def recall(self, **kwargs: Any) -> dict[str, Any]:
        response = httpx.post(
            f"{self._base_url}/memory/recall",
            json=kwargs,
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload


class ProductRecallReplay:
    def __init__(
        self,
        service: Any,
        *,
        payload_source: PayloadSource = product_payload_source,
        top_k: int = 8,
        budget: int = 800,
    ) -> None:
        self._service = service
        self._payload_source = payload_source
        self._top_k = top_k
        self._budget = budget

    def replay(
        self,
        query: str,
        *,
        qid: str,
        profile_id: str,
        session_id: str | None = None,
    ) -> RecallObservation:
        result = self._payload_source(
            self._service,
            profile_id=profile_id,
            query=query,
            top_k=self._top_k,
            budget=self._budget,
            session_id=session_id,
        )
        payload = result.get("memory")
        if not isinstance(payload, dict):
            raise ValueError("product recall response has no memory payload")
        raw_entries = payload.get("entries", [])
        entries = tuple(
            EntryObservation(
                id=str(entry["id"]),
                kind=str(entry.get("kind", "")),
                rank=rank,
                score=float(entry["score"]),
                tokens=int(entry["tokens"]),
                text=str(entry.get("text", "")),
            )
            for rank, entry in enumerate(raw_entries, start=1)
        )
        raw_coverage = payload.get("coverage", {})
        return RecallObservation(
            qid=qid,
            query=query,
            profile_id=profile_id,
            session_id=session_id,
            entries=entries,
            dropped_count=int(payload["dropped_count"]),
            tokens_used=int(payload["tokens_used"]),
            budget_tokens=int(payload.get("budget_tokens", self._budget)),
            coverage=dict(raw_coverage) if isinstance(raw_coverage, dict) else {},
        )
