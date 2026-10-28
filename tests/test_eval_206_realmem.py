import json
from pathlib import Path

EVIDENCE = Path(__file__).parents[1] / "evidence-206-realmem"


def test_real_memory_observation_evidence_is_redacted_and_complete() -> None:
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    evidence = json.loads((EVIDENCE / "observations.json").read_text(encoding="utf-8"))

    assert manifest["source_sha"] == "b6dd3a5"
    assert manifest["sample_size"] == 20
    assert evidence["status"] == "observed"
    assert len(evidence["pairs"]) == 20
    assert manifest["completed_pairs"] == 20
    assert manifest["seat_calls"] == 40
    assert (EVIDENCE / "ids.json").exists()
    assert (EVIDENCE / "pairs.json").exists()
    assert all(
        set(pair) <= {"pair_id", "left_id", "right_id", "category", "seat_a", "seat_b", "result"}
        for pair in evidence["pairs"]
    )
    assert "text" not in json.dumps(evidence).lower()


def test_real_memory_rosters_contain_ids_only() -> None:
    ids = json.loads((EVIDENCE / "ids.json").read_text(encoding="utf-8"))
    pairs = json.loads((EVIDENCE / "pairs.json").read_text(encoding="utf-8"))

    assert ids["memories_collected"] >= 25
    assert len(ids["distinct_memory_ids"]) == ids["memories_collected"]
    assert len(pairs["pairs"]) == 20
    assert "text" not in json.dumps(ids).lower()
    assert "text" not in json.dumps(pairs).lower()
