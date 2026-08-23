"""Config loading: ~/.mnemoseed-local/config.toml is the single source of truth.

A preset (embedded/custom) maps each storage layer to a default driver; layers
can be overridden individually and a layer may declare named instances
(e.g. graph.main / graph.isolated, D6). STORAGE_MODE is kept as a preset
shortcut environment variable. Parse and resolution errors always name the
offending config key.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mnemoseed_local.schema.graph import NodeType
from mnemoseed_local.secrets.refs import SECRETS_REF_RE, is_secrets_ref

logger = logging.getLogger("mnemoseed_local.config")

CONFIG_DIR = Path(os.environ.get("MNEMOSEED_LOCAL_HOME", Path.home() / ".mnemoseed-local"))
CONFIG_PATH = CONFIG_DIR / "config.toml"

LAYER_TYPES: tuple[str, ...] = ("vector", "graph", "meta", "embed")

PRESETS: dict[str, dict[str, str]] = {
    # layer -> default driver (M0 names from prd-08 FR-8.3 / FR-8.4)
    "embedded": {
        "vector": "lancedb_embedded",
        "graph": "sqlite_graph",
        "meta": "sqlite_meta",
        "embed": "bge_m3_onnx",
    },
    "custom": {},  # everything explicit; a missing layer is an error
}

VALID_PRESETS: tuple[str, ...] = tuple(PRESETS)
DEFAULT_PRESET = "embedded"
DEFAULT_INSTANCE = "main"


class ConfigError(ValueError):
    """Config parse or resolution error with the offending key named."""

    def __init__(self, key: str, message: str) -> None:
        self.key = key
        super().__init__(f"config[{key}]: {message}")


@dataclass(frozen=True)
class InstanceConfig:
    """A resolved driver instance for a layer."""

    name: str
    driver: str
    params: dict[str, Any]


@dataclass(frozen=True)
class _InstanceOverride:
    """An explicit named instance from the config file (driver optional)."""

    driver: str | None
    params: dict[str, Any]


@dataclass
class LayerSpec:
    """Per-layer explicit configuration from the config file."""

    layer: str
    driver: str | None = None  # optional per-layer override; falls back to the preset
    params: dict[str, Any] = field(default_factory=dict)
    instances: dict[str, _InstanceOverride] = field(default_factory=dict)


#: Dream schedule trigger defaults (score-pool based, design/01 + PRD-02): a
#: dream becomes eligible when the profile's score pool holds at least
#: ``floor_pool_points`` (S points from scored capture turns, the design's
#: 10-point scale) AND the profile has been idle for ``idle_min_sec``; the hard
#: deadline forces a dream once the oldest pending verbatim chunk has waited
#: ``hard_deadline_sec`` (24h).
DEFAULT_DREAM_FLOOR_POOL_POINTS: float = 10.0
DEFAULT_DREAM_IDLE_MIN_SEC: float = 900.0
DEFAULT_DREAM_HARD_DEADLINE_SEC: float = 86400.0

#: T3a (A2.5) tier / ensemble / threshold defaults (design/01 §4.7):
#: ``hardware_tier`` is the three-way anchor (standard | lite | advanced);
#: ``ensemble`` is the optional dual-reflect verification layer (off | verify |
#: vote), default off — the lite tier locks it off (configwrite linkage);
#: ``core_confidence_floor`` is the merge-boundary downgrade threshold in [0, 1]
#: (core triples below it are deterministically routed to the isolated graph);
#: ``delta_budget_ceiling_tokens`` is the dynamic delta clamp's ceiling, whose
#: module constant in dream/delta.py is the value source (mirror + pin test);
#: ``pool_forced_cap`` is the ScorePool forced-consolidation cap, >= the floor.
DEFAULT_DREAM_HARDWARE_TIER: str = "standard"
DEFAULT_DREAM_ENSEMBLE: str = "off"
DEFAULT_DREAM_CORE_CONFIDENCE_FLOOR: float = 0.0
DEFAULT_DREAM_DELTA_BUDGET_CEILING_TOKENS: int = 32000
DEFAULT_DREAM_POOL_FORCED_CAP: float = 50.0

#: B2.1 T2 mid-session auto-recall (PRD-B2.1): the focal decay floor and the
#: pending-recall selection budget (design/01 §4.6). Values landed by the T4b
#: live calibration (ACCEPTED: all gate bars pass at 0.5/2400; the floor axis
#: discriminates at 0.5 — interference decayed to 0.45 gates out — and the
#: budget headroom keeps long-band facts un-sliced).
DEFAULT_AUTO_RECALL_FOCAL_FLOOR: float = 0.5
DEFAULT_AUTO_RECALL_BUDGET_CHARS: int = 2400

#: Cue-driven rescue band (design/09 §3.5): below the main candidate floor a
#: flashbulb pin enters the recall pool only when its cue overlap clears
#: ``rescue_cue_min``, down to ``rescue_floor``. Values landed by the
#: rescue-band calibration sweep (ACCEPTED: 20-point (floor × cue_min) grid ×
#: 8 materials on the MCP-recall rig; every gate bar green on every group —
#: noise 0 / rebound 1 / dead-zone leak 0 / rank discipline 1 — and the
#: recovery scalar picks the deepest band that holds the bars: floor 0.15
#: keeps 0.22-weight pins reachable, cue_min 0.2 still rescues half-overlap
#: entity cues while gating everything weaker).
DEFAULT_RECALL_RESCUE_FLOOR: float = 0.15
DEFAULT_RECALL_RESCUE_CUE_MIN: float = 0.2

#: The T3a enum sets, shared by the config loader and the configwrite registry
#: (a drift between the two is a validation split — one source, both consumers).
DREAM_HARDWARE_TIERS: frozenset[str] = frozenset({"standard", "lite", "advanced"})
DREAM_ENSEMBLE_MODES: frozenset[str] = frozenset({"off", "verify", "vote"})


@dataclass(frozen=True)
class DreamConfig:
    """Dream-engine runtime flags (PRD-02 FR-2.8 manual-first discipline).

    ``auto_trigger`` decides whether the schedule triggers drive dreams directly
    (True, the shipped default) or are held as pending manual runs for
    ``mnemoseed dream --once`` (False; one config switch turns automation off).
    ``floor_pool_points`` / ``idle_min_sec`` / ``hard_deadline_sec`` are the
    A2 schedule trigger rules (hot-applied through the configwrite registry):
    score-pool floor+idle eligibility and the 24h hard deadline from the first
    verbatim chunk in the pending window. Every durable capture turn credits
    its S importance into the profile's score pool; a dream fires once the
    balance reaches ``floor_pool_points`` AND the profile has been idle for
    ``idle_min_sec``, and the pool drains on the fire (the same points never
    trigger twice).

    T3a tier/threshold flags (design/01 §4.7): ``hardware_tier`` anchors the
    three-way tier (standard | lite | advanced); ``ensemble`` selects the
    optional dual-reflect verification layer (off | verify | vote, default
    off — the lite tier locks it off); ``core_confidence_floor`` is the merge
    boundary at which a core triple is deterministically downgraded to the
    isolated graph; ``delta_budget_ceiling_tokens`` is the dynamic delta
    clamp's ceiling (module constant is the value source); ``pool_forced_cap``
    is the ScorePool forced-consolidation cap, always >= the floor.
    """

    auto_trigger: bool = True
    floor_pool_points: float = DEFAULT_DREAM_FLOOR_POOL_POINTS
    idle_min_sec: float = DEFAULT_DREAM_IDLE_MIN_SEC
    hard_deadline_sec: float = DEFAULT_DREAM_HARD_DEADLINE_SEC
    hardware_tier: str = DEFAULT_DREAM_HARDWARE_TIER
    ensemble: str = DEFAULT_DREAM_ENSEMBLE
    core_confidence_floor: float = DEFAULT_DREAM_CORE_CONFIDENCE_FLOOR
    delta_budget_ceiling_tokens: int = DEFAULT_DREAM_DELTA_BUDGET_CEILING_TOKENS
    pool_forced_cap: float = DEFAULT_DREAM_POOL_FORCED_CAP


#: Decay sweep cadence (NFR-4.1: the batch runs once daily).
DEFAULT_SWEEP_INTERVAL_S: float = 86400.0

#: Weight-change floor under which a sweep write is skipped ("dumb write").
DEFAULT_MIN_APPLY_DELTA: float = 0.01

#: Per-type exponential decay rates (PRD-04 FR-4.1, design/01 §5):
#: fact 0.01 (half-life ≈ 69 days), preference 0.005 (≈ 139 days),
#: episode 0.03 (≈ 23 days). The ``"chunk"`` pseudo-type covers the verbatim
#: vector channel, which carries no node_type.
DEFAULT_LAMBDA_PER_TYPE: dict[str, float] = {
    # fact-class (λ_fact = 0.01, half-life ≈ 69 days)
    "USER": 0.01,
    "HABIT": 0.01,
    "DECISION": 0.01,
    "PROJECT": 0.01,
    "TOOL": 0.01,
    "SKILL_SEQUENCE": 0.01,
    "CONSTRAINT": 0.01,
    # preference-class (λ_preference = 0.005, half-life ≈ 139 days)
    "PREFERENCE": 0.005,
    "ANIMA": 0.005,
    # episode-class (λ_episode = 0.03, half-life ≈ 23 days)
    "EPISODE": 0.03,
    "INTENTION": 0.03,
    # the verbatim channel has no node_type; chunks decay like episodes
    "chunk": 0.03,
    # the flashbulb class (design/09 §3.1): explicit-pin chunks decay at
    # preference pace (≈139-day half-life) instead of the chunk rate
    "pin": 0.005,
}

#: The writable λ-map keys: every frozen node type plus the verbatim-channel
#: pseudo-types ("chunk" ordinary, "pin" the explicit-pin class).
LAMBDA_TARGETS: frozenset[str] = frozenset(NodeType.frozen_set()) | {"chunk", "pin"}


@dataclass(frozen=True)
class DecayConfig:
    """Decay-engine runtime flags (PRD-04 FR-4.1 / FR-4.4, design/01 stage ⑤).

    ``enabled`` gates the daemon's sweep task at boot. ``sweep_interval_s`` is
    the sweep cadence (NFR-4.1: once daily by default); ``min_apply_delta`` is
    the write floor that skips sub-threshold drops. ``lambda_per_type`` maps a
    node type (or the ``"chunk"`` pseudo-type for the verbatim channel) to its
    exponential rate; the map is carried verbatim from the file — entries the
    user omitted resolve to the per-type design default at sweep time
    (``decay.model.lambda_for``), keeping the file and the settings DB always
    in agreement.
    """

    enabled: bool = True
    sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S
    min_apply_delta: float = DEFAULT_MIN_APPLY_DELTA
    lambda_per_type: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_LAMBDA_PER_TYPE))


@dataclass(frozen=True)
class CaptureConfig:
    """B2.1 T2 mid-session auto-recall flags (PRD-B2.1, design/01 §4.6).

    ``auto_recall`` gates the whole pipeline (default ON — one config switch,
    ``auto_recall = false``, turns it off). ``auto_recall_focal_floor`` is the
    focal decay floor of the embedding-free entity-anchored scan (positive, at
    most 1 — a zero floor would make every decayed chunk focal).
    ``auto_recall_budget_chars`` caps the pending-recall selection served to
    the hook (T1 budget semantics: greedy admission, boundary tail-slice).
    """

    auto_recall: bool = True
    auto_recall_focal_floor: float = DEFAULT_AUTO_RECALL_FOCAL_FLOOR
    auto_recall_budget_chars: int = DEFAULT_AUTO_RECALL_BUDGET_CHARS


@dataclass(frozen=True)
class RecallConfig:
    """MCP explicit-recall rescue band (design/09 §3.5, open question 3).

    ``rescue_floor`` is the lower bound of the two-band admission (the upper
    bound is the hybrid retriever's main candidate floor); ``rescue_cue_min``
    is the cue-overlap minimum a below-main-floor flashbulb pin must clear to
    enter the pool. Both are calibration items owned by the eval harness.
    """

    rescue_floor: float = DEFAULT_RECALL_RESCUE_FLOOR
    rescue_cue_min: float = DEFAULT_RECALL_RESCUE_CUE_MIN


# A2 MVP + B1 + B5: the dream LLM roles. The cloud deep_reflection /
# short_increment split is trimmed for the local single-user daemon; roles
# remain a pipeline-internal parameter so a future split can re-open them
# without a schema change. "dream" generates (model A); "dream_verifier" is the
# ensemble verify judging seat (model B, design/01 decision 1) — dormant unless
# dream.ensemble=verify; "dream_vote" is the B5 vote seat B's independent
# generator (a distinct model from A) — dormant unless dream.ensemble=vote.
# "dream_vote" has NO default route: vote falls back to the dream_verifier
# judging route (still a distinct model from A) when the dedicated route is
# unconfigured. Any legacy [dream.llm.deep_reflection] / [dream.llm.short_increment]
# tables are tolerated on load and ignored with a warning, never applied.
LLM_ROLES: tuple[str, ...] = ("dream", "dream_verifier", "dream_vote")

#: Removed role names (A2 trim): recognized for deprecation tolerance.
LEGACY_ROLES: frozenset[str] = frozenset({"deep_reflection", "short_increment", "local_track"})

#: Wording shared by the loader warning, the admin surface, and the wire: a
#: legacy table or a write targeting a removed role answers the same message.
LOCAL_TRACK_DEPRECATION = (
    "[dream.llm.<legacy-role>] was deprecated and removed; the local MVP routes dreams through the "
    "'dream' role (plus 'dream_verifier' when ensemble verify is enabled)"
)


@dataclass(frozen=True)
class RoleLLMConfig:
    """One role's resolved LLM route: driver + model + params.

    Keys are config attestations, never values: an API key is referenced by
    env-var NAME or a ``secrets:mnemoseed/dream/<role>`` reference in
    ``params["api_key_env"]`` and resolved at materialization time by the
    RoleRouter (mnemoseed_local.llm.routing) — a literal key in config is an error.
    """

    role: str
    driver: str
    model: str
    params: dict[str, Any] = field(default_factory=dict)


# A2 MVP: ONE dream LLM role named "dream" (no deep_reflection /
# short_increment split; the role stays a pipeline-internal param). Dream
# inference runs against a LOCAL model — ollama by default; the
# openai-compatible driver stays available as the fallback route. API keys are
# never written here: a cloud fallback references its key by env-var NAME at
# resolution time.
DEFAULT_LLM_ROUTES: dict[str, RoleLLMConfig] = {
    "dream": RoleLLMConfig(
        role="dream",
        driver="ollama",
        model="qwen3.5:9b",
        params={
            "base_url": "http://localhost:11434",
            # Reflect is structured extraction — thinking models would burn the
            # whole generation budget on internal thinking and return EMPTY
            # content ("Expecting value: line 1 column 1", D4 live finding on
            # qwen3.5:9b). The factory default pins thinking OFF for the dream
            # route; a user-deliberate `think = true` still wins at the file.
            "think": False,
            # ollama's lazy default num_ctx is 4096 — silently smaller than any
            # packed delta (floor 5000), so a dream on real session volume
            # always starved to empty output (D4 compounding). Ship a working
            # 16k window by default; the ctx-window doctor check then reports
            # honestly whether the delta ceiling fits (tier-coherent overrides
            # live on dream.delta_budget_ceiling_tokens + this key).
            "num_ctx": 16384,
        },
    ),
    # B1 ensemble verify (design/01 decision 1): the judging seat (model B).
    # Verification is a cheaper task than generation, so the factory defaults
    # to the small judge model on the same local track; it stays dormant until
    # dream.ensemble is flipped to "verify" (off by default, lite tier locked).
    "dream_verifier": RoleLLMConfig(
        role="dream_verifier",
        driver="ollama",
        model="gemma4:e4b",
        params={
            "base_url": "http://localhost:11434",
            # Judging is structured output too: thinking burns the whole
            # generation budget on inner thinking and returns EMPTY content
            # (same D4 failure shape as the dream route).
            "think": False,
            "num_ctx": 16384,
        },
    ),
}


@dataclass
class Config:
    """Resolved configuration. layer_instances() materializes per-layer drivers."""

    preset: str = DEFAULT_PRESET
    baseurl: str = "http://localhost:7788"
    storage: dict[str, LayerSpec] = field(default_factory=dict)
    dream: DreamConfig = field(default_factory=DreamConfig)
    decay: DecayConfig = field(default_factory=DecayConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)
    llm: dict[str, RoleLLMConfig] = field(default_factory=lambda: dict(DEFAULT_LLM_ROUTES))
    source: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def layer_instances(self, kind: str) -> dict[str, InstanceConfig]:
        """Resolve the instance set for one layer (always non-empty).

        order of precedence (weakest to strongest):
        preset default driver < explicit layer driver < named-instance driver.
        The default instance name is D6's "main".
        """
        if kind not in LAYER_TYPES:
            raise ConfigError(
                f"storage.{kind}", f"unknown storage layer (expected one of {', '.join(LAYER_TYPES)})"
            )
        if self.preset not in PRESETS:
            raise ConfigError("preset", f"unknown preset {self.preset!r}")

        spec = self.storage.get(kind)
        if spec is not None and spec.driver is not None:
            base_driver = spec.driver
            base_params = spec.params
        else:
            preset_driver = PRESETS[self.preset].get(kind)
            if preset_driver is None:
                raise ConfigError(
                    f"storage.{kind}.driver",
                    f"preset {self.preset!r} defines no default for layer {kind!r}; "
                    "an explicit driver is required under the custom preset",
                )
            base_driver = preset_driver
            base_params = spec.params if spec is not None else {}

        resolved: dict[str, InstanceConfig] = {}
        if spec is not None:
            for name, override in spec.instances.items():
                driver = override.driver if override.driver is not None else base_driver
                resolved[name] = InstanceConfig(name=name, driver=driver, params=override.params)
        if DEFAULT_INSTANCE not in resolved:
            resolved[DEFAULT_INSTANCE] = InstanceConfig(
                name=DEFAULT_INSTANCE, driver=base_driver, params=base_params
            )
        return resolved


def _require_table(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(key, "must be a table")
    return value


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _is_non_negative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _validate_api_key_ref(role: str, value: Any) -> None:
    """Shape-check an ``api_key_env`` param (T2-2).

    A ``secrets:`` reference must be well-formed and name a live dream role;
    a malformed reference can never resolve, so it is a load error naming the
    key. Anything else (env-var NAME lists, and hand-edited literal keys)
    passes through unchanged — literal keys stay the pre-existing
    tolerated-then-redacted contract.
    """
    if not isinstance(value, str) or not is_secrets_ref(value):
        return
    key = f"dream.llm.{role}.api_key_env"
    match = SECRETS_REF_RE.fullmatch(value)
    if match is None:
        raise ConfigError(
            key,
            "a secrets: reference must look like 'secrets:mnemoseed/dream/<role>'",
        )
    if match.group(1) not in LLM_ROLES:
        raise ConfigError(
            key,
            f"a secrets: reference must name a live dream role (one of {', '.join(LLM_ROLES)})",
        )


def _optional_driver(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(key, "must be a non-empty string")
    return value


def load_config(path: Path | None = None) -> Config:
    """Load and validate config from the TOML file (STORAGE_MODE overrides preset)."""
    path = path or CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        raw = _require_table(tomllib.loads(path.read_text(encoding="utf-8")), "<config>")

    env_preset = os.environ.get("STORAGE_MODE")
    preset_raw: Any = env_preset if env_preset is not None else raw.get("preset", DEFAULT_PRESET)
    if not isinstance(preset_raw, str) or preset_raw not in PRESETS:
        key = "STORAGE_MODE" if env_preset is not None else "preset"
        raise ConfigError(key, f"unknown preset {preset_raw!r} (choose from: {', '.join(VALID_PRESETS)})")

    baseurl_raw: Any = raw.get("baseurl", "http://localhost:7788")
    if not isinstance(baseurl_raw, str):
        raise ConfigError("baseurl", "must be a string")

    storage: dict[str, LayerSpec] = {}
    storage_raw = raw.get("storage")
    if storage_raw is not None:
        storage_table = _require_table(storage_raw, "storage")
        for layer_key, layer_value in storage_table.items():
            layer_key = str(layer_key)
            layer_path = f"storage.{layer_key}"
            if layer_key not in LAYER_TYPES:
                raise ConfigError(
                    layer_path, f"unknown storage layer (expected one of {', '.join(LAYER_TYPES)})"
                )
            layer_table = _require_table(layer_value, layer_path)

            driver = _optional_driver(layer_table.get("driver"), f"{layer_path}.driver")
            params = {k: v for k, v in layer_table.items() if k not in ("driver", "instances")}

            overrides: dict[str, _InstanceOverride] = {}
            instances_raw = layer_table.get("instances")
            if instances_raw is not None:
                instances_table = _require_table(instances_raw, f"{layer_path}.instances")
                for name, entry in instances_table.items():
                    name = str(name)
                    entry_table = _require_table(entry, f"{layer_path}.instances.{name}")
                    entry_driver = _optional_driver(
                        entry_table.get("driver"), f"{layer_path}.instances.{name}.driver"
                    )
                    overrides[name] = _InstanceOverride(
                        driver=entry_driver,
                        params={k: v for k, v in entry_table.items() if k != "driver"},
                    )

            storage[layer_key] = LayerSpec(
                layer=layer_key,
                driver=driver,
                params=params,
                instances=overrides,
            )

    dream = DreamConfig()
    llm_routes = {role: cfg for role, cfg in DEFAULT_LLM_ROUTES.items()}
    dream_raw = raw.get("dream")
    if dream_raw is not None:
        dream_table = _require_table(dream_raw, "dream")
        if "token_budget_usd" in dream_table:
            # T3b (design/01 §4.1): the FR-2.5b USD budget concept is removed;
            # a stale key is a hard deprecation error, never a silent ignore.
            raise ConfigError(
                "dream.token_budget_usd",
                "was deprecated and removed; the ledger records tokens only, "
                "never a USD budget (delete the key)",
            )
        auto_raw = dream_table.get("auto_trigger", True)
        if not isinstance(auto_raw, bool):
            raise ConfigError("dream.auto_trigger", "must be a boolean")
        floor_raw = dream_table.get("floor_pool_points", DEFAULT_DREAM_FLOOR_POOL_POINTS)
        if not _is_positive_number(floor_raw):
            raise ConfigError("dream.floor_pool_points", "must be a positive number")
        idle_raw = dream_table.get("idle_min_sec", DEFAULT_DREAM_IDLE_MIN_SEC)
        if not _is_non_negative_number(idle_raw):
            raise ConfigError("dream.idle_min_sec", "must be a non-negative number")
        deadline_raw = dream_table.get("hard_deadline_sec", DEFAULT_DREAM_HARD_DEADLINE_SEC)
        if not _is_non_negative_number(deadline_raw):
            raise ConfigError("dream.hard_deadline_sec", "must be a non-negative number")

        # T3a (A2.5, design/01 §4.7): tier / ensemble / threshold keys. The
        # enum sets live on this module and are shared with the configwrite
        # registry so load-time and write-time validation cannot drift.
        tier_raw = dream_table.get("hardware_tier", DEFAULT_DREAM_HARDWARE_TIER)
        if not isinstance(tier_raw, str) or tier_raw not in DREAM_HARDWARE_TIERS:
            raise ConfigError(
                "dream.hardware_tier",
                f"must be one of {', '.join(sorted(DREAM_HARDWARE_TIERS))}",
            )
        ensemble_raw = dream_table.get("ensemble", DEFAULT_DREAM_ENSEMBLE)
        if not isinstance(ensemble_raw, str) or ensemble_raw not in DREAM_ENSEMBLE_MODES:
            raise ConfigError("dream.ensemble", f"must be one of {', '.join(sorted(DREAM_ENSEMBLE_MODES))}")
        if tier_raw == "lite" and ensemble_raw != "off":
            raise ConfigError(
                "dream.ensemble",
                "the lite hardware tier locks the ensemble off (lit; use off)",
            )
        confidence_floor_raw = dream_table.get("core_confidence_floor", DEFAULT_DREAM_CORE_CONFIDENCE_FLOOR)
        if not _is_non_negative_number(confidence_floor_raw) or float(confidence_floor_raw) > 1.0:
            raise ConfigError("dream.core_confidence_floor", "must be a number in [0, 1]")
        ceiling_raw = dream_table.get(
            "delta_budget_ceiling_tokens", DEFAULT_DREAM_DELTA_BUDGET_CEILING_TOKENS
        )
        if not isinstance(ceiling_raw, int) or isinstance(ceiling_raw, bool) or ceiling_raw < 5000:
            raise ConfigError("dream.delta_budget_ceiling_tokens", "must be an integer >= 5000")
        forced_raw = dream_table.get("pool_forced_cap", DEFAULT_DREAM_POOL_FORCED_CAP)
        if not _is_positive_number(forced_raw):
            raise ConfigError("dream.pool_forced_cap", "must be a positive number")
        if float(forced_raw) < float(confidence_floor_raw):
            raise ConfigError(
                "dream.pool_forced_cap",
                "must be >= dream.core_confidence_floor",
            )
        # T3b (design/01 §4.8): the isolated graph instance is mandatory for a
        # non-zero floor — the downgrade target must exist or a merge would fail
        # (the Merger refuses a downgrade with no isolated instance). Rejected
        # at load with a fix hint, never deferred to a silent runtime degrade.
        if float(confidence_floor_raw) > 0.0:
            graph_spec = storage.get("graph")
            has_isolated = graph_spec is not None and "isolated" in graph_spec.instances
            if not has_isolated:
                raise ConfigError(
                    "dream.core_confidence_floor",
                    "requires the 'isolated' graph instance: add a "
                    "[storage.graph.instances.isolated] table "
                    '(driver = "sqlite_graph")',
                )
        dream = DreamConfig(
            auto_trigger=auto_raw,
            floor_pool_points=float(floor_raw),
            idle_min_sec=float(idle_raw),
            hard_deadline_sec=float(deadline_raw),
            hardware_tier=tier_raw,
            ensemble=ensemble_raw,
            core_confidence_floor=float(confidence_floor_raw),
            delta_budget_ceiling_tokens=int(ceiling_raw),
            pool_forced_cap=float(forced_raw),
        )

        # T6 (FR-2.14): [dream.llm.<role>] overrides per role. Only structural
        # validation happens here (table-ity, known role, non-empty driver/model
        # strings); semantic failures (unknown driver, bad params) defer to the
        # RoleRouter when that role is actually resolved — a misconfigured unused
        # role never breaks boot.
        llm_raw = dream_table.get("llm")
        if llm_raw is not None:
            llm_table = _require_table(llm_raw, "dream.llm")
            for role in LLM_ROLES:
                entry = llm_table.get(role)
                if entry is None:
                    continue  # unconfigured role keeps its default route
                role_path = f"dream.llm.{role}"
                entry_table = _require_table(entry, role_path)
                driver = _optional_driver(entry_table.get("driver"), f"{role_path}.driver")
                model = _optional_driver(entry_table.get("model"), f"{role_path}.model")
                # A role with no factory default (B5 dream_vote) requires an
                # explicit driver+model when it appears in the file; a role with
                # a default falls back to it for any field left unset.
                base = DEFAULT_LLM_ROUTES.get(role)
                if base is None and (driver is None or model is None):
                    raise ConfigError(
                        role_path,
                        "this role has no default route and must set both 'driver' and 'model'",
                    )
                params = {k: v for k, v in entry_table.items() if k not in ("driver", "model")}
                if "api_key_env" in params:
                    _validate_api_key_ref(role, params["api_key_env"])
                llm_routes[role] = RoleLLMConfig(
                    role=role,
                    driver=driver if driver is not None else base.driver,  # type: ignore[union-attr]
                    model=model if model is not None else base.model,  # type: ignore[union-attr]
                    params={**(base.params if base is not None else {}), **params},
                )
            unknown = [
                str(role)
                for role in llm_table
                if str(role) not in LLM_ROLES and str(role) not in LEGACY_ROLES
            ]
            if unknown:
                raise ConfigError(
                    f"dream.llm.{unknown[0]}",
                    f"unknown llm role (expected one of {', '.join(LLM_ROLES)})",
                )
            legacy = [str(role) for role in llm_table if str(role) in LEGACY_ROLES]
            if legacy:
                logger.warning(LOCAL_TRACK_DEPRECATION)

    # [decay] table (PRD-04): sweep cadence, write floor, enabled flag and the
    # per-type λ map. The λ map is carried verbatim (replace semantics: the
    # map IS what the file says) — omitted types resolve to their design
    # default at sweep time via decay.model.lambda_for, so the live config
    # always equals the file (and the DB-primary settings mirror never sees a
    # phantom drift).
    decay = DecayConfig()
    decay_raw = raw.get("decay")
    if decay_raw is not None:
        decay_table = _require_table(decay_raw, "decay")
        enabled_raw = decay_table.get("enabled", True)
        if not isinstance(enabled_raw, bool):
            raise ConfigError("decay.enabled", "must be a boolean")
        interval_raw = decay_table.get("sweep_interval_s", DEFAULT_SWEEP_INTERVAL_S)
        if not _is_positive_number(interval_raw):
            raise ConfigError("decay.sweep_interval_s", "must be a positive number")
        delta_raw = decay_table.get("min_apply_delta", DEFAULT_MIN_APPLY_DELTA)
        if not _is_non_negative_number(delta_raw):
            raise ConfigError("decay.min_apply_delta", "must be a non-negative number")
        lambda_map = dict(DEFAULT_LAMBDA_PER_TYPE)
        lambda_raw = decay_table.get("lambda_per_type")
        if lambda_raw is not None:
            lambda_table = _require_table(lambda_raw, "decay.lambda_per_type")
            parsed: dict[str, float] = {}
            for key, rate in lambda_table.items():
                key = str(key)
                if key not in LAMBDA_TARGETS:
                    raise ConfigError(f"decay.lambda_per_type.{key}", "unknown memory type")
                if not _is_positive_number(rate):
                    raise ConfigError(f"decay.lambda_per_type.{key}", "must be a positive number")
                parsed[key] = float(rate)
            lambda_map = parsed
        decay = DecayConfig(
            enabled=enabled_raw,
            sweep_interval_s=float(interval_raw),
            min_apply_delta=float(delta_raw),
            lambda_per_type=lambda_map,
        )

    # [capture] table (B2.1 T2, design/01 §4.6): the mid-session auto-recall
    # pipeline — the focal scan runs inside the ingest of every user prompt.
    # ON by default; one config switch (auto_recall = false) turns the whole
    # pipeline off. The focal floor is positive (a zero floor would make every
    # decayed chunk focal) and at most 1; the selection budget is a positive
    # int.
    capture = CaptureConfig()
    capture_raw = raw.get("capture")
    if capture_raw is not None:
        capture_table = _require_table(capture_raw, "capture")
        auto_raw = capture_table.get("auto_recall", True)
        if not isinstance(auto_raw, bool):
            raise ConfigError("capture.auto_recall", "must be a boolean")
        floor_raw = capture_table.get("auto_recall_focal_floor", DEFAULT_AUTO_RECALL_FOCAL_FLOOR)
        if not _is_positive_number(floor_raw) or float(floor_raw) > 1.0:
            raise ConfigError("capture.auto_recall_focal_floor", "must be a number in (0, 1]")
        budget_raw = capture_table.get("auto_recall_budget_chars", DEFAULT_AUTO_RECALL_BUDGET_CHARS)
        if not isinstance(budget_raw, int) or isinstance(budget_raw, bool) or budget_raw <= 0:
            raise ConfigError("capture.auto_recall_budget_chars", "must be a positive integer")
        capture = CaptureConfig(
            auto_recall=auto_raw,
            auto_recall_focal_floor=float(floor_raw),
            auto_recall_budget_chars=int(budget_raw),
        )

    # [recall] table (design/09 §3.5): the MCP explicit-recall rescue band.
    # Both thresholds are unit-range numbers owned by the eval calibration.
    recall = RecallConfig()
    recall_raw = raw.get("recall")
    if recall_raw is not None:
        recall_table = _require_table(recall_raw, "recall")
        floor_raw = recall_table.get("rescue_floor", DEFAULT_RECALL_RESCUE_FLOOR)
        if not _is_positive_number(floor_raw) or float(floor_raw) > 1.0:
            raise ConfigError("recall.rescue_floor", "must be a number in (0, 1]")
        cue_raw = recall_table.get("rescue_cue_min", DEFAULT_RECALL_RESCUE_CUE_MIN)
        if not _is_positive_number(cue_raw) or float(cue_raw) > 1.0:
            raise ConfigError("recall.rescue_cue_min", "must be a number in (0, 1]")
        recall = RecallConfig(rescue_floor=float(floor_raw), rescue_cue_min=float(cue_raw))

    return Config(
        preset=preset_raw,
        baseurl=baseurl_raw,
        storage=storage,
        dream=dream,
        decay=decay,
        capture=capture,
        recall=recall,
        llm=llm_routes,
        source=path,
        raw=raw,
    )


def default_config_toml() -> str:
    """Default config written by init."""
    return """\
# MnemoSeed Local configuration — single source of truth
preset = "embedded"          # embedded | custom
baseurl = "http://localhost:7788"

# Dream engine (PRD-02 FR-2.8): automatic consolidation is ON by default —
# dreams fire on their own under the A2 schedule triggers below, and one
# config switch (auto_trigger = false) holds every trigger as a pending
# manual run for `mnemoseed-local dream --once`. The A2
# schedule triggers (hot-applied via `mnemoseed-local config set`) are
# score-pool based (design/01 + PRD-02): every capture turn credits its S
# importance (arousal / novelty / causal components, 0..10 scale) into the
# profile's score pool (every turn is also stored verbatim — scoring never
# drops text), and a dream becomes eligible once the pool reaches
#   floor_pool_points  — the pool-points floor (design's 10-point scale); the
#                        capture pool's self-fire threshold (dream_threshold)
#   idle_min_sec       — the profile must have been idle at least this long
#                        (900s default); also the capture pool's self-fire
#                        idle window — the pool never fires on a fixed 5s
#   hard_deadline_sec  — force a dream once the oldest pending chunk waited
#                        this long (24h default); skipped when nothing pending
#   hardware_tier      — standard | lite | advanced (default standard): the
#                        tier anchor; the lite tier locks the ensemble off
#   ensemble           — off | verify | vote (default off): the optional
#                        dual-reflect verification layer; lite locks it off
#   core_confidence_floor — [0, 1] (default 0.0): core triples below it are
#                        downgraded to the isolated graph at merge time
#   delta_budget_ceiling_tokens — >= 5000 (default 32000): the dynamic delta
#                        clamp's ceiling, read by the doctor's ctx-window check
#   pool_forced_cap     — >= core_confidence_floor (default 50.0): the capture
#                        pool's forced-consolidation cap
# [dream]
# auto_trigger = true
# floor_pool_points = 10.0
# idle_min_sec = 900.0
# hard_deadline_sec = 86400.0
# hardware_tier = "standard"
# ensemble = "off"
# core_confidence_floor = 0.0
# delta_budget_ceiling_tokens = 32000
# pool_forced_cap = 50.0

# Per-layer overrides (required under the custom preset):
# [storage.vector]
# driver = "lancedb_embedded"
#
# [storage.graph]
# driver = "sqlite_graph"
#
# Named multi-instance (D6): the isolated graph instance is MANDATORY
# (design/01 §4.8) — tier-3 output and floor-downgraded core triples route
# here, never to the main graph. A fresh init writes this table.
[storage.graph.instances.isolated]
driver = "sqlite_graph"
path = "~/.mnemoseed-local/isolated.db"

# Dream LLM role routing: role "dream" generates (model A); role
# "dream_verifier" is the ensemble verify judging seat (model B, dormant unless
# dream.ensemble = "verify"). The local default is ollama (dream inference
# stays on the machine); the openai-compatible driver is the fallback for a
# cloud/remote endpoint. API keys are referenced by ENV-VAR NAME or a
# secrets:mnemoseed/dream/<role> reference — never a literal key here; the
# router resolves the value from the process environment / the local secret
# store at materialization time.
# Other params (base_url, max_tokens, ...) override the role's defaults.
# [dream.llm.dream]
# driver = "ollama"
# model = "qwen3.5:9b"
# base_url = "http://localhost:11434"
#
# [dream.llm.dream_verifier]
# driver = "ollama"
# model = "gemma4:e4b"
# base_url = "http://localhost:11434"

# The deep_reflection / short_increment / local_track roles were trimmed in the
# A2 local MVP: any legacy [dream.llm.<legacy-role>] table is tolerated on load
# and ignored with a warning.

# Decay engine (PRD-04 FR-4.1 / design/01 stage ⑤): unreinforced memories fade
# through w = confidence × exp(-λ × days). λ is layered per node type
# (fact 0.01 / preference 0.005 / episode 0.03) plus the "chunk" pseudo-type
# for the verbatim channel; the sweep runs once daily (NFR-4.1) and skips
# writes whose weight change is below min_apply_delta. The map is replace
# semantics: keys you omit fall back to their per-type default.
# [decay]
# enabled = true
# sweep_interval_s = 86400.0
# min_apply_delta = 0.01
# lambda_per_type = {"PREFERENCE": 0.005, "EPISODE": 0.03, "chunk": 0.03}

# B2.1 T2 mid-session auto-recall (PRD-B2.1, design/01 §4.6): the focal scan
# runs inside the ingest of every user prompt and parks a budgeted selection
# that the hook pulls on the next transform (ack-implies-ready). ON by
# default; one config switch (auto_recall = false) turns the whole pipeline
# off:
#   auto_recall              — enable the whole pipeline
#   auto_recall_focal_floor  — focal decay floor (0, 1]: below it a chunk is
#                              never focal (a zero floor would make everything
#                              focal, so it is rejected). 0.5 = T4b calibrated
#                              value (ACCEPTED)
#   auto_recall_budget_chars — the selection budget; the greedy admission and
#                              boundary tail-slice follow the T1 semantics.
#                              2400 = T4b calibrated value (ACCEPTED)
# [capture]
# auto_recall = true
# auto_recall_focal_floor = 0.5
# auto_recall_budget_chars = 2400
"""
