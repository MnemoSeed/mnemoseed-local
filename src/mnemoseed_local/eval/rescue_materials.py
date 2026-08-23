"""Rescue-band calibration materials (design/09 §3.5, open question 3).

Each point stages ONE topic on the real daemon surface with four actors:

- a flashbulb pin aged into the candidate bands (decay < 0.4), carrying either
  a FULL cue overlap (β_entity = 0.6, one-of-one query entity stored) or a
  PARTIAL overlap (β_entity = 0.3, one-of-two) — the axis the cue minimum
  must discriminate;
- a plain non-pin decoy in the same band with the same perfect cue match —
  the invariant it must never cross;
- a decay-healthy chunk — the rank baseline;
- a dead-zone pin below every swept floor — the residue-only population.

The rank-flip family tightens this further: its pin's fused score BEATS the
healthy chunk's (query-mirroring pin text against a modest-decay baseline), so
the ``rank_discipline`` gate bar measures enforced ordering rather than
fused-score coincidence.

Deterministic under the factory seed; no LLM anywhere (synthetic embedder).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Pinned factory seed (material identity is part of the bar).
RESCUE_MATERIALS_SEED = 20260824

#: β-entity component values implied by the query/stored entity geometry
#: (hybrid.py's _BETA_ENTITY_WEIGHT = 0.6; ratio = matched/query-side count).
CUE_OVERLAP_FULL: float = 0.6
CUE_OVERLAP_PARTIAL: float = 0.3

#: Decay layering: the band pin walks the rescue band; the decoy sits mid-band
#: with a perfect cue match; the dead-zone pin stays below every swept floor.
DEAD_PIN_DECAY: float = 0.05
HEALTHY_DECAY: float = 0.9


@dataclass(frozen=True)
class RescueMaterial:
    """One calibration point: four actors on one topic plus its query."""

    point_id: str
    entity: str
    other_query_entity: str  # second query entity for partial-overlap points
    query: str
    pin_id: str
    pin_text: str
    pin_decay: float
    cue_class: str  # "full" | "partial"
    decoy_id: str
    decoy_text: str
    decoy_decay: float
    healthy_id: str
    healthy_text: str
    dead_pin_id: str
    dead_pin_text: str
    dead_pin_decay: float = DEAD_PIN_DECAY
    healthy_decay: float = HEALTHY_DECAY

    @property
    def cue_overlap(self) -> float:
        """The β overlap the pin scores against this point's query."""
        return CUE_OVERLAP_FULL if self.cue_class == "full" else CUE_OVERLAP_PARTIAL


_ENTITIES: tuple[str, ...] = (
    "AtlasDb",
    "NimbusDb",
    "KestrelDb",
    "HeliosDb",
    "OrionDb",
    "VegaDb",
    "LyraDb",
    "DracoDb",
)

#: (point suffix, pin decay, cue class)
_POINT_PLAN: tuple[tuple[str, float, str], ...] = (
    ("near-band", 0.38, "full"),
    ("upper-band", 0.35, "full"),
    ("mid-band", 0.30, "full"),
    ("band-low", 0.22, "full"),
    ("partial-upper", 0.35, "partial"),
    ("partial-mid", 0.30, "partial"),
    ("partial-low", 0.22, "partial"),
    ("deep", 0.12, "full"),
)

#: Rank-flip point: the pin lexically mirrors the query while the healthy
#: baseline sits just above the main floor, so the pin's fused score wins
#: (frozen margin ≈ +0.20 under the synthetic embedder) and rank discipline
#: is the only thing that can still order the healthy chunk first.
_RANK_FLIP_POINTS: tuple[RescueMaterial, ...] = (
    RescueMaterial(
        point_id="rescue-MeridianDb-rank-flip",
        entity="MeridianDb",
        other_query_entity="VelaDb",
        query="What is the current status of MeridianDb?",
        pin_id="MeridianDb-pin",
        pin_text="Current status of MeridianDb: export window moved to 02:00 UTC",
        pin_decay=0.38,
        cue_class="full",
        decoy_id="MeridianDb-decoy",
        decoy_text="MeridianDb also archives the weekly digest bundle",
        decoy_decay=0.32,
        healthy_id="MeridianDb-healthy",
        healthy_text="Staging cluster migrated to new hardware last week",
        healthy_decay=0.45,
        dead_pin_id="MeridianDb-dead",
        dead_pin_text="MeridianDb ships the audit bundle every Friday",
    ),
)


def rescue_materials(seed: int = RESCUE_MATERIALS_SEED) -> tuple[RescueMaterial, ...]:
    """The structured material points (deterministic under ``seed``)."""
    del seed  # the plan is static today; the seed keeps the factory signature stable
    materials: list[RescueMaterial] = []
    for index, (suffix, pin_decay, cue_class) in enumerate(_POINT_PLAN):
        entity = _ENTITIES[index]
        other = _ENTITIES[(index + 3) % len(_ENTITIES)]
        query = (
            f"What is the current status of {entity}?"
            if cue_class == "full"
            else f"What is the current status of {entity} and {other}?"
        )
        materials.append(
            RescueMaterial(
                point_id=f"rescue-{entity}-{suffix}",
                entity=entity,
                other_query_entity=other,
                query=query,
                pin_id=f"{entity}-pin",
                pin_text=f"{entity} keeps the nightly export window at 02:00 UTC",
                pin_decay=pin_decay,
                cue_class=cue_class,
                decoy_id=f"{entity}-decoy",
                decoy_text=f"{entity} also archives the weekly digest bundle",
                decoy_decay=0.32,
                healthy_id=f"{entity}-healthy",
                healthy_text=f"{entity} runs the staging cluster on the new hardware",
                dead_pin_id=f"{entity}-dead",
                dead_pin_text=f"{entity} ships the audit bundle every Friday",
            )
        )
    materials.extend(_RANK_FLIP_POINTS)
    return tuple(materials)
