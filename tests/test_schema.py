"""Schema base tests: stamp filter view roundtrip, graph node version chain."""

import time

from mnemoseed_local.schema.graph import GraphNode, NodeType
from mnemoseed_local.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance


def make_stamp(**over) -> ChunkStamp:
    base = dict(
        profile_id="p1",
        text="user prefers dark mode",
        cognitive_tier=CognitiveTier.TIER_1,
        model_id="test-model",
        cues=Cues(project="demo", entities=["ui", "theme"]),
        provenance=Provenance(asserted_by="test-model", source="session://s1"),
    )
    base.update(over)
    return ChunkStamp(**base)


def test_metadata_filter_view_roundtrip():
    stamp = make_stamp()
    view = stamp.metadata_filter_view()
    rebuilt = ChunkStamp.from_filter_view(stamp.chunk_id, stamp.text, view)
    assert rebuilt.chunk_id == stamp.chunk_id
    assert rebuilt.profile_id == "p1"
    assert rebuilt.cognitive_tier == CognitiveTier.TIER_1
    assert rebuilt.cues.entities == ["ui", "theme"]
    assert rebuilt.consolidated is False
    assert rebuilt.ingested_at == stamp.ingested_at
    assert rebuilt.turn_start is None  # no turn bounds by default
    assert rebuilt.turn_end is None


def test_metadata_filter_view_roundtrips_turn_bounds():
    stamp = make_stamp(turn_start=3, turn_end=5)
    view = stamp.metadata_filter_view()
    assert view["turn_start"] == 3
    assert view["turn_end"] == 5
    rebuilt = ChunkStamp.from_filter_view(stamp.chunk_id, stamp.text, view)
    assert rebuilt.turn_start == 3
    assert rebuilt.turn_end == 5


def test_emotion_never_in_confidence():
    # red line: emotion cues exist but confidence stays a provenance-only field
    stamp = make_stamp()
    assert 0.0 <= stamp.provenance.confidence <= 1.0
    assert stamp.cues.emotion is None


def make_node(**over) -> GraphNode:
    base = dict(
        profile_id="p1",
        node_type=NodeType.PREFERENCE,
        entities=["ui"],
        props={"key": "theme", "value": "dark"},
        provenance=Provenance(asserted_by="test", source="test"),
        valid_from=time.time(),
    )
    base.update(over)
    return GraphNode(**base)


def test_node_current_flag():
    node = make_node()
    assert node.is_current
    node.valid_to = time.time()
    assert not node.is_current
