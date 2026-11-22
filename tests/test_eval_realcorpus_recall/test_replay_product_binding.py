"""Replay must observe the real product recall output, never a self-built score."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.eval.realcorpus_recall.replay import ProductRecallReplay, RecallObservation


class _FakeService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def recall(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.payload


def _payload() -> dict[str, Any]:
    return {
        "memory": {
            "entries": [
                {"id": "chunk-a", "kind": "chunk", "text": "first", "score": 0.9123, "tokens": 111},
                {"id": "chunk-b", "kind": "chunk", "text": "second", "score": 0.4411, "tokens": 222},
            ],
            "dropped_count": 7,
            "tokens_used": 333,
            "budget_tokens": 800,
            "coverage": {"vector_hits": 9, "graph_hits": 0, "pool_size": 12},
        }
    }


def test_replay_passes_recall_request_shape() -> None:
    service = _FakeService(_payload())
    replay = ProductRecallReplay(service)
    replay.replay("自然措辞", qid="Q-001", profile_id="default")
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call == {
        "profile_id": "default",
        "query": "自然措辞",
        "top_k": 8,
        "budget": 800,
        "session_id": None,
    }


def test_replay_observes_only_product_output() -> None:
    replay = ProductRecallReplay(_FakeService(_payload()))
    observation = replay.replay("query", qid="Q-002", profile_id="default")
    assert isinstance(observation, RecallObservation)
    assert observation.served_ids == ("chunk-a", "chunk-b")
    assert [entry.rank for entry in observation.entries] == [1, 2]
    assert [entry.score for entry in observation.entries] == [0.9123, 0.4411]
    assert [entry.tokens for entry in observation.entries] == [111, 222]
    assert observation.dropped_count == 7
    assert observation.tokens_used == 333
    assert observation.coverage == {"vector_hits": 9, "graph_hits": 0, "pool_size": 12}


def test_binding_killer_product_scores_are_verbatim() -> None:
    payload = _payload()
    payload["memory"]["entries"][0]["score"] = 0.7770001
    observation = ProductRecallReplay(_FakeService(payload)).replay("q", qid="Q-003", profile_id="p")
    assert observation.entries[0].score == 0.7770001
    assert observation.entries[0].id == "chunk-a"


def test_honest_empty_observation_has_no_served_entries() -> None:
    payload = {"memory": {"entries": [], "dropped_count": 0, "tokens_used": 0, "coverage": {}}}
    observation = ProductRecallReplay(_FakeService(payload)).replay("q", qid="Q-004", profile_id="p")
    assert observation.served_ids == ()
    assert observation.tokens_used == 0


def test_no_synthetic_rig_imports() -> None:
    package = Path(__file__).resolve().parents[1] / "src/mnemoseed_local/eval/realcorpus_recall"
    forbidden = ("synthetic_embedder", "stub_dream", "recall_matrix", "TARGET_BARS")
    for path in package.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, (path.name, token)


def test_replay_rejects_missing_product_payload() -> None:
    with pytest.raises(ValueError, match="memory"):
        ProductRecallReplay(_FakeService({})).replay("q", qid="Q-005", profile_id="p")


def test_http_recall_route_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from mnemoseed_local.eval.realcorpus_recall.replay import HttpRecallService

    captured: dict[str, str] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"memory": {"entries": [], "dropped_count": 0, "tokens_used": 0}}

    def _post(url: str, **kwargs: Any) -> _Response:
        captured["url"] = url
        return _Response()

    monkeypatch.setattr(httpx, "post", _post)
    HttpRecallService("http://127.0.0.1:45001").recall(query="q")
    assert captured["url"] == "http://127.0.0.1:45001/memory/recall"
