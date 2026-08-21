"""Chunk stamp schema — the metadata imprint every captured shard carries.

Every shard entering the hippocampus must carry a complete stamp: verbatim
text, cognitive tier, situational cues, and full provenance.
"""

from __future__ import annotations

import time
import uuid
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field


class CognitiveTier(IntEnum):
    """Cognitive tier of the producing model; routes the write path."""

    TIER_1 = 1  # high-capability model output -> core graph
    TIER_2 = 2
    TIER_3 = 3  # low-capability model output -> isolated graph


class EmotionCue(BaseModel):
    """Emotion cue.

    Red line: arousal only feeds scoring (and is capped), valence only serves
    as a retrieval cue. Emotion never contributes to provenance.confidence —
    flashbulb memories feel certain without being accurate.
    """

    valence: float | None = Field(default=None, ge=-1.0, le=1.0)
    arousal: float | None = Field(default=None, ge=0.0, le=1.0)
    peripheral_gaps: bool = False  # attentional narrowing under high arousal


class Cues(BaseModel):
    """Situational cues (encoding specificity: retrieval works best when the
    retrieval context matches the encoding context)."""

    project: str | None = None
    host: str | None = None  # encoding context: which host wrote the chunk
    task: str | None = None  # encoding context: active task clue (FR-1.6)
    tools_used: list[str] = Field(default_factory=list)
    time_bucket: str | None = None  # e.g. "2026-W32", "weekday-evening"
    entities: list[str] = Field(default_factory=list)  # required for freshness checks
    emotion: EmotionCue | None = None


class ProvenanceEvent(BaseModel):
    """One event in the provenance history (rewrite / reinforcement / flag)."""

    at: float = Field(default_factory=time.time)
    action: str  # created | reinforced | superseded | flagged | reconciled | decayed
    actor: str  # agent_id / "dream-engine" / "user"
    detail: dict[str, Any] = Field(default_factory=dict)


class Provenance(BaseModel):
    """Provenance backbone (source monitoring: a memory without a source is a
    confabulation risk)."""

    asserted_by: str  # model_id or "user"
    agent_id: str | None = None
    session_id: str | None = None
    source: str  # session reference / file / manual input
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)  # emotion never weights this
    asserted_at: float = Field(default_factory=time.time)
    history: list[ProvenanceEvent] = Field(default_factory=list)


class ChunkStamp(BaseModel):
    """Hippocampal shard: verbatim text plus the full stamp."""

    chunk_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    profile_id: str
    text: str  # verbatim channel: the original text is never summarized away
    cognitive_tier: CognitiveTier
    model_id: str
    persona_id: str | None = None
    cues: Cues = Field(default_factory=Cues)
    provenance: Provenance
    decay_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    last_reinforced: float | None = Field(
        default=None,
        description="most recent reinforcement epoch; None falls back to ingested_at",
    )
    score: float = 0.0  # capture-time salience score
    consolidated: bool = False  # pinned after dream-engine write-back
    ingested_at: float = Field(default_factory=time.time)
    turn_start: int | None = None  # capturing turn window (safe purge scoping)
    turn_end: int | None = None  # inclusive; both ends must be set together
    # B2.7 Scheme 2-lite: standing constraints carried with the chunk metadata
    # (stored verbatim in the vector driver's ``rules_json`` column). The values
    # are RecallRule dictionaries (ports.RecallRule) so the stamp stays a plain
    # JSON carrier and the driver never depends on the port model.
    rules: list[dict[str, Any]] = Field(default_factory=list)

    def metadata_filter_view(self) -> dict[str, Any]:
        """Flat view stored in vector-DB metadata (driver-agnostic).

        Only filterable fields go here; complex objects are serialized by the
        driver if needed.
        """
        return {
            "chunk_id": self.chunk_id,
            "profile_id": self.profile_id,
            "cognitive_tier": int(self.cognitive_tier),
            "model_id": self.model_id,
            "project": self.cues.project or "",
            "entities": ",".join(self.cues.entities),
            "consolidated": self.consolidated,
            "decay_weight": self.decay_weight,
            "ingested_at": self.ingested_at,
            "turn_start": self.turn_start,
            "turn_end": self.turn_end,
        }

    @classmethod
    def from_filter_view(cls, chunk_id: str, text: str, meta: dict[str, Any]) -> ChunkStamp:
        """Rebuild a shard from the vector-DB metadata view (retrieval hot path).

        Cold fields (provenance, emotion) are not in metadata; the rebuilt
        object carries only what freshness checks and scoring need. Fetch the
        full stamp from MetaStore when needed.
        """
        entities_raw = meta.get("entities") or ""
        return cls(
            chunk_id=chunk_id,
            profile_id=str(meta.get("profile_id", "")),
            text=text,
            cognitive_tier=CognitiveTier(int(meta.get("cognitive_tier", 3))),
            model_id=str(meta.get("model_id", "")),
            cues=Cues(
                project=meta.get("project") or None,
                entities=[e for e in str(entities_raw).split(",") if e],
            ),
            provenance=Provenance(asserted_by=str(meta.get("model_id", "")), source="vector_meta"),
            decay_weight=float(meta.get("decay_weight", 1.0)),
            consolidated=bool(meta.get("consolidated", False)),
            ingested_at=float(meta.get("ingested_at", 0.0)),
            turn_start=(int(meta["turn_start"]) if meta.get("turn_start") is not None else None),
            turn_end=int(meta["turn_end"]) if meta.get("turn_end") is not None else None,
        )
