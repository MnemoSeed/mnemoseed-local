"""ConfigWriteService (PRD-07 FR-7.11 / design/07 section 9, W1.1).

The daemon's single config writer. Every settings change funnels through one
flow:

  registry  -> validate  -> surgical TOML patch  -> versioned record  ->
  audit (actor attributed) -> live-apply

- The key-path registry (:data:`CONFIG_KEY_REGISTRY`) is the closed schema of
  writable keys; an unknown key is a typed ``ConfigWriteError`` that names it.
- ``set`` applies a line-oriented TOML patch (never a full round-trip): the
  target line is rewritten in place, comments/layout and sibling keys survive,
  and a missing table is inserted after the last existing one.
- With a meta store attached, every write lands a versioned record
  (``set_config``) and an audit entry with actor attribution; without one the
  service still patches the file (offline mode) but records nothing.
- ``version_id`` is a global integer: ``slot * _VERSION_STRIDE + version``
  where slot is the key's registry index, so a version id decodes to exactly
  one (key, version) pair and rolls through an append-only
  ``rollback_config`` on restore.
- ``reconcile_boot`` (E1-4 DB-primary Phase 0) makes the settings DB the
  primary store for registry keys and config.toml its generated mirror: a
  one-shot audited import (``config_import``) seeds an empty DB from the file,
  then the DB WINS on every later boot (the file is regenerated from the DB,
  never the reverse — a hand edit is logged + audited as
  ``config_mirror_drift``). Boot-scope keys (preset/storage/baseurl/auth) are
  never registry keys: file-scoped + restart-required.
- Secrets are never written or read: ``api_key_env`` values are env-var NAMES,
  and every read surface redacts anything that is not a valid name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mnemoseed_local.config import (
    CONFIG_PATH,
    DREAM_ENSEMBLE_MODES,
    DREAM_HARDWARE_TIERS,
    LAMBDA_TARGETS,
    LLM_ROLES,
    CaptureConfig,
    Config,
    DecayConfig,
    DreamConfig,
    RoleLLMConfig,
)
from mnemoseed_local.secrets.refs import SECRETS_REF_RE, is_secrets_ref
from mnemoseed_local.storage.ports import AuditEntry, ConfigEntry

logger = logging.getLogger("mnemoseed_local.configwrite")

#: Global version-id stride: slot * stride + version.
_VERSION_STRIDE = 1_000_000

#: Private bookkeeping key (excluded from every public version listing).
_FILE_STATE_KEY = "__configwrite__file_state"

#: Env-var NAME grammar (FR-2.14): UPPER_SNAKE only. Anything else in an
#: ``api_key_env`` write is a literal key and a hard validation failure.
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")


class ConfigWriteError(ValueError):
    """Typed write failure naming the offending config key (mapped to 422)."""

    def __init__(self, key_path: str, message: str) -> None:
        self.key_path = key_path
        super().__init__(f"config[{key_path}]: {message}")


class ConfigWriteMeta(Protocol):
    """The minimal versioned-config store surface the writer records through."""

    def get_config(self, key: str, version: int | None = None) -> ConfigEntry | None: ...
    def set_config(self, key: str, value: dict[str, Any]) -> int: ...
    def rollback_config(self, key: str, version: int) -> None: ...
    def audit_append(self, entry: AuditEntry) -> None: ...


@dataclass(frozen=True)
class ConfigKey:
    """One writable config key and how to read/validate/apply it.

    ``validate`` normalizes the incoming wire value (raising ``ValueError`` on
    a typed failure); ``cross_validate`` (optional) re-checks the value against
    the CURRENT effective config (tier/ensemble linkage, floor/cap ordering)
    and may raise ``ValueError``; ``read`` resolves the current effective value
    from a ``Config``; ``apply`` live-updates the running ``Config`` after the
    patch lands (frozen ``DreamConfig`` is replaced, role params are rebuilt).
    """

    key_path: str
    value_type: str
    validate: Callable[[Any], Any]
    read: Callable[[Config], Any]
    apply: Callable[[Config, Any], None]
    live_apply: bool = True
    secret: bool = False
    cross_validate: Callable[[Config, Any], None] | None = None


# ---------------------------------------------------------------- validators


def _validate_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("must be a boolean")
    return value


def _validate_positive_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("must be a positive number")
    return float(value)


def _validate_non_negative_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("must be a non-negative number")
    return float(value)


def _validate_confidence_floor(value: Any) -> float:
    """dream.core_confidence_floor: a probability in [0, 1]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 1:
        raise ValueError("must be a number in [0, 1]")
    return float(value)


def _validate_focal_floor(value: Any) -> float:
    """capture.auto_recall_focal_floor: a decay in (0, 1] — a zero floor
    would make every decayed chunk focal, so it is rejected like an
    out-of-range value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or value > 1:
        raise ValueError("must be a number in (0, 1]")
    return float(value)


def _validate_delta_ceiling(value: Any) -> int:
    """dream.delta_budget_ceiling_tokens: an integer >= 5000 (the dynamic
    delta clamp must always have room above the minimum viable window)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 5000:
        raise ValueError("must be an integer >= 5000")
    return value


def _validate_reflect_batch_cap(value: Any) -> int:
    """dream.reflect_batch_max_tokens: a non-negative integer; 0 disables
    batched reflection and keeps the legacy single-pack reflect (#99)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("must be a non-negative integer (0 disables batching)")
    return value


def _cross_validate_batch_cap_vs_ceiling(config: Config, value: int) -> None:
    """The batch cap binds above the packer's ceiling would be silently
    clipped by pack(); refuse the misleading write instead (#99)."""
    ceiling = config.dream.delta_budget_ceiling_tokens
    if value > ceiling:
        raise ValueError(f"must be <= dream.delta_budget_ceiling_tokens ({ceiling})")


def _validate_forced_cap(value: Any) -> float:
    """dream.pool_forced_cap: a positive number (its >= floor ordering is the
    cross-validation's job)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("must be a positive number")
    return float(value)


def _validate_choice(value: Any, choices: frozenset[str]) -> str:
    """An enum-typed key: the value must be one of the shared config-level
    choices (the load-time and write-time validators share the same sets, so
    they can never drift apart)."""
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"must be one of {', '.join(sorted(choices))}")
    return value


def _cross_validate_lite_ensemble(config: Config, value: str) -> None:
    """The lite tier locks the ensemble off: writing ensemble != off while the
    tier is lite, or switching to lite while ensemble != off, is rejected —
    the linkage holds in BOTH directions."""
    if value == "off":
        return
    if config.dream.hardware_tier == "lite":
        raise ValueError("the lite hardware tier locks the ensemble off (use 'off')")


def _cross_validate_tier_locks_ensemble(config: Config, value: str) -> None:
    if value == "lite" and config.dream.ensemble != "off":
        raise ValueError("the lite hardware tier locks the ensemble off (use 'off')")


def _cross_validate_floor_vs_cap(config: Config, value: float) -> None:
    """pool_forced_cap >= core_confidence_floor must hold after the write."""
    if value < config.dream.core_confidence_floor:
        raise ValueError("must be >= dream.core_confidence_floor")


def _cross_validate_cap_vs_floor(config: Config, value: float) -> None:
    if config.dream.pool_forced_cap < value:
        raise ValueError("must be <= dream.pool_forced_cap")


def _cross_validate_floor_requires_isolated(config: Config, value: float) -> None:
    """T3b (design/01 §4.8): raising dream.core_confidence_floor above zero on
    a config without the 'isolated' graph instance is rejected — the downgrade
    target must exist (mirrors the load-time check, one rule on both sides)."""
    if value <= 0.0:
        return
    graph_spec = config.storage.get("graph")
    has_isolated = graph_spec is not None and "isolated" in graph_spec.instances
    if not has_isolated:
        raise ValueError(
            "requires the 'isolated' graph instance: add a "
            '[storage.graph.instances.isolated] table (driver = "sqlite_graph")'
        )


def _cross_validate_floor(config: Config, value: float) -> None:
    """The floor's two write-time invariants: it must stay at or below
    pool_forced_cap AND a non-zero floor requires the isolated graph instance."""
    _cross_validate_cap_vs_floor(config, value)
    _cross_validate_floor_requires_isolated(config, value)


def _validate_lambda_map(value: Any) -> dict[str, float]:
    """The writable λ map (decay.lambda_per_type): an object whose keys are
    frozen node types (or ``"chunk"``) and whose values are positive numbers.

    Replace semantics: the incoming map IS the map; omitted types resolve to
    their design default at sweep time (decay.model.lambda_for).
    """
    if not isinstance(value, dict):
        raise ValueError("must be an object mapping node type to a positive number")
    normalized: dict[str, float] = {}
    for key, rate in value.items():
        if not isinstance(key, str) or key not in LAMBDA_TARGETS:
            raise ValueError(f"unknown memory type {key!r}")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
            raise ValueError(f"the rate for {key!r} must be a positive number")
        normalized[key] = float(rate)
    return normalized


def _validate_positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("must be a positive integer")
    return value


def _validate_optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("must be a positive integer")
    return value


def _validate_nonempty_str(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-empty string")
    return value.strip()


def _validate_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("must be a string")
    return value.strip() or None


def _validate_env_name_list(value: Any) -> str | None:
    """Env-var NAME lists OR a single ``secrets:`` reference (T2-2).

    The failure message never echoes the offending token: a key value must not
    travel back over the wire in an error response either. A reference is
    validated for shape AND must name a live dream role; mixing a reference
    with env names is rejected.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("must be a comma-separated list of env-var names or a secrets: reference")
    stripped = value.strip()
    if is_secrets_ref(stripped):
        match = SECRETS_REF_RE.fullmatch(stripped)
        if match is None:
            raise ValueError("must be env-var NAMES or a 'secrets:mnemoseed/dream/<role>' reference")
        if match.group(1) not in LLM_ROLES:
            raise ValueError(
                f"a secrets: reference must name a live dream role (one of {', '.join(LLM_ROLES)})"
            )
        return stripped
    names = [token.strip() for token in value.split(",") if token.strip()]
    if not names:
        return None
    invalid = [name for name in names if _ENV_NAME_RE.fullmatch(name) is None]
    if invalid:
        raise ValueError("must be env-var NAMES only (a literal key value is never accepted or stored)")
    return ",".join(names)


# ---------------------------------------------------------------- live-apply


def _dream_apply(config: Config, field: str, value: Any) -> None:
    """Replace the frozen DreamConfig and mirror the change into config.raw.

    The rebuild is kwargs-preserving: EVERY dream field (including the T3a
    tier/threshold keys) is carried over, so a write to one key can never
    silently reset the others — the exact failure the field-by-field branch
    form was prone to as the config grew.
    """
    current = config.dream
    fields: dict[str, Any] = {
        "auto_trigger": current.auto_trigger,
        "floor_pool_points": current.floor_pool_points,
        "idle_min_sec": current.idle_min_sec,
        "hard_deadline_sec": current.hard_deadline_sec,
        "hardware_tier": current.hardware_tier,
        "ensemble": current.ensemble,
        "core_confidence_floor": current.core_confidence_floor,
        "delta_budget_ceiling_tokens": current.delta_budget_ceiling_tokens,
        "pool_forced_cap": current.pool_forced_cap,
    }
    if value is not None:
        fields[field] = value
    config.dream = DreamConfig(**fields)
    raw_dream = config.raw.setdefault("dream", {})
    if value is None:
        raw_dream.pop(field, None)
    else:
        raw_dream[field] = value


def _decay_apply(config: Config, field: str, value: Any) -> None:
    """Replace the frozen DecayConfig and mirror the change into config.raw.

    The decay module holds a live reference to this Config, so the write
    hot-applies to the NEXT sweep without a restart (F2).
    """
    current = config.decay
    if field == "enabled":
        config.decay = DecayConfig(
            enabled=bool(value),
            sweep_interval_s=current.sweep_interval_s,
            min_apply_delta=current.min_apply_delta,
            lambda_per_type=current.lambda_per_type,
        )
    elif field == "sweep_interval_s":
        config.decay = DecayConfig(
            enabled=current.enabled,
            sweep_interval_s=float(value),
            min_apply_delta=current.min_apply_delta,
            lambda_per_type=current.lambda_per_type,
        )
    elif field == "min_apply_delta":
        config.decay = DecayConfig(
            enabled=current.enabled,
            sweep_interval_s=current.sweep_interval_s,
            min_apply_delta=float(value),
            lambda_per_type=current.lambda_per_type,
        )
    else:  # lambda_per_type (replace semantics, see _validate_lambda_map)
        config.decay = DecayConfig(
            enabled=current.enabled,
            sweep_interval_s=current.sweep_interval_s,
            min_apply_delta=current.min_apply_delta,
            lambda_per_type=dict(value),
        )
    raw_decay = config.raw.setdefault("decay", {})
    if value is None:
        raw_decay.pop(field, None)
    else:
        raw_decay[field] = value


def _capture_apply(config: Config, field: str, value: Any) -> None:
    """Replace the frozen CaptureConfig and mirror the change into config.raw.

    The daemon's MemoryService holds a live reference to this Config, so a
    write hot-applies to the NEXT user-prompt scan without a restart (B2.1 T2
    D5). The rebuild is kwargs-preserving: every capture field is carried
    over, so a write to one key can never silently reset the others.
    """
    current = config.capture
    fields: dict[str, Any] = {
        "auto_recall": current.auto_recall,
        "auto_recall_focal_floor": current.auto_recall_focal_floor,
        "auto_recall_budget_chars": current.auto_recall_budget_chars,
    }
    if value is not None:
        fields[field] = value
    config.capture = CaptureConfig(**fields)
    raw_capture = config.raw.setdefault("capture", {})
    if value is None:
        raw_capture.pop(field, None)
    else:
        raw_capture[field] = value


def _role_apply(config: Config, role: str, field: str, value: Any) -> None:
    """Rebuild the role's RoleLLMConfig with the new field and mirror raw."""
    cfg = config.llm.get(role)
    if cfg is None:
        # A recognized-but-unconfigured role (B5 dream_vote) gains its route on
        # the first write rather than crashing; a write must set the route
        # identity, so driver/model default from the written field.
        cfg = RoleLLMConfig(role=role, driver="", model="")
        config.llm[role] = cfg
    params = dict(cfg.params)
    if field not in ("driver", "model"):
        if value is None:
            params.pop(field, None)
        else:
            params[field] = value
    new_driver = str(value) if field == "driver" else cfg.driver
    new_model = str(value) if field == "model" else cfg.model
    config.llm[role] = RoleLLMConfig(role=role, driver=new_driver, model=new_model, params=params)
    raw_table = config.raw.setdefault("dream", {}).setdefault("llm", {}).setdefault(role, {})
    if value is None:
        raw_table.pop(field, None)
    else:
        raw_table[field] = value


def _role_read(config: Config, role: str, field: str) -> Any:
    cfg = config.llm.get(role)
    if cfg is None:
        return None  # recognized-but-unconfigured role reads as unset
    if field in ("driver", "model"):
        return getattr(cfg, field)
    return cfg.params.get(field)


def _role_reader(role: str, field: str) -> Callable[[Config], Any]:
    return lambda config: _role_read(config, role, field)


def _role_applier(role: str, field: str) -> Callable[[Config, Any], None]:
    return lambda config, value: _role_apply(config, role, field, value)


# ---------------------------------------------------------------- registry


def _role_key_specs(role: str) -> dict[str, ConfigKey]:
    """The writable fields of one dream role (design/02 section 6; ``think``
    landed with the D4 thinking-output fix)."""
    fields: dict[str, tuple[str, Callable[[Any], Any], bool]] = {
        "driver": ("string", _validate_nonempty_str, False),
        "model": ("string", _validate_nonempty_str, False),
        "base_url": ("string", _validate_optional_str, False),
        "api_key_env": ("env-var names or secrets: reference", _validate_env_name_list, True),
        "max_tokens": ("positive integer", _validate_optional_positive_int, False),
        "provider": ("string", _validate_optional_str, False),
        "think": ("boolean", _validate_bool, False),
        # ollama context knobs; the doctor ctx-window check hints at these
        "num_ctx": ("positive integer", _validate_optional_positive_int, False),
        "num_predict": ("positive integer", _validate_optional_positive_int, False),
    }
    return {
        f"dream.llm.{role}.{field}": ConfigKey(
            key_path=f"dream.llm.{role}.{field}",
            value_type=label,
            validate=validator,
            read=_role_reader(role, field),
            apply=_role_applier(role, field),
            live_apply=True,
            secret=is_secret,
        )
        for field, (label, validator, is_secret) in fields.items()
    }


CONFIG_KEY_REGISTRY: dict[str, ConfigKey] = {
    "dream.auto_trigger": ConfigKey(
        key_path="dream.auto_trigger",
        value_type="boolean",
        validate=_validate_bool,
        read=lambda config: config.dream.auto_trigger,
        apply=lambda config, value: _dream_apply(config, "auto_trigger", value),
        live_apply=True,
    ),
    # A2 schedule trigger keys: score-pool floor + idle eligibility and the 24h
    # hard deadline, hot-applied to the daemon scheduler loop (no restart).
    "dream.floor_pool_points": ConfigKey(
        key_path="dream.floor_pool_points",
        value_type="positive number",
        validate=_validate_positive_float,
        read=lambda config: config.dream.floor_pool_points,
        apply=lambda config, value: _dream_apply(config, "floor_pool_points", value),
        live_apply=True,
    ),
    "dream.idle_min_sec": ConfigKey(
        key_path="dream.idle_min_sec",
        value_type="non-negative number",
        validate=_validate_non_negative_float,
        read=lambda config: config.dream.idle_min_sec,
        apply=lambda config, value: _dream_apply(config, "idle_min_sec", value),
        live_apply=True,
    ),
    "dream.hard_deadline_sec": ConfigKey(
        key_path="dream.hard_deadline_sec",
        value_type="non-negative number",
        validate=_validate_non_negative_float,
        read=lambda config: config.dream.hard_deadline_sec,
        apply=lambda config, value: _dream_apply(config, "hard_deadline_sec", value),
        live_apply=True,
    ),
    # T3a (A2.5, design/01 §4.7): tier / ensemble / threshold keys, all
    # hot-applied to their consumers (Merger / DeltaPacker / ScorePool) via the
    # live Config reference they hold at construction.
    "dream.hardware_tier": ConfigKey(
        key_path="dream.hardware_tier",
        value_type="standard | lite | advanced",
        validate=lambda value: _validate_choice(value, DREAM_HARDWARE_TIERS),
        read=lambda config: config.dream.hardware_tier,
        apply=lambda config, value: _dream_apply(config, "hardware_tier", value),
        live_apply=True,
        cross_validate=_cross_validate_tier_locks_ensemble,
    ),
    "dream.ensemble": ConfigKey(
        key_path="dream.ensemble",
        value_type="off | verify | vote",
        validate=lambda value: _validate_choice(value, DREAM_ENSEMBLE_MODES),
        read=lambda config: config.dream.ensemble,
        apply=lambda config, value: _dream_apply(config, "ensemble", value),
        live_apply=True,
        cross_validate=_cross_validate_lite_ensemble,
    ),
    "dream.core_confidence_floor": ConfigKey(
        key_path="dream.core_confidence_floor",
        value_type="number in [0, 1]",
        validate=_validate_confidence_floor,
        read=lambda config: config.dream.core_confidence_floor,
        apply=lambda config, value: _dream_apply(config, "core_confidence_floor", value),
        live_apply=True,
        cross_validate=_cross_validate_floor,
    ),
    "dream.delta_budget_ceiling_tokens": ConfigKey(
        key_path="dream.delta_budget_ceiling_tokens",
        value_type="integer >= 5000",
        validate=_validate_delta_ceiling,
        read=lambda config: config.dream.delta_budget_ceiling_tokens,
        apply=lambda config, value: _dream_apply(config, "delta_budget_ceiling_tokens", value),
        live_apply=True,
    ),
    "dream.pool_forced_cap": ConfigKey(
        key_path="dream.pool_forced_cap",
        value_type="positive number >= dream.core_confidence_floor",
        validate=_validate_forced_cap,
        read=lambda config: config.dream.pool_forced_cap,
        apply=lambda config, value: _dream_apply(config, "pool_forced_cap", value),
        live_apply=True,
        cross_validate=_cross_validate_floor_vs_cap,
    ),
    "dream.reflect_batch_max_tokens": ConfigKey(
        key_path="dream.reflect_batch_max_tokens",
        value_type="non-negative integer (0 disables batching)",
        validate=_validate_reflect_batch_cap,
        read=lambda config: config.dream.reflect_batch_max_tokens,
        apply=lambda config, value: _dream_apply(config, "reflect_batch_max_tokens", value),
        live_apply=False,
        cross_validate=_cross_validate_batch_cap_vs_ceiling,
    ),
    # Decay engine (PRD-04 FR-4.1 / design/01 stage ⑤): the sweep's tunables
    # are live-applied — a λ edit reaches the NEXT sweep without a restart.
    "decay.enabled": ConfigKey(
        key_path="decay.enabled",
        value_type="boolean",
        validate=_validate_bool,
        read=lambda config: config.decay.enabled,
        apply=lambda config, value: _decay_apply(config, "enabled", value),
        live_apply=True,
    ),
    "decay.sweep_interval_s": ConfigKey(
        key_path="decay.sweep_interval_s",
        value_type="positive number",
        validate=_validate_positive_float,
        read=lambda config: config.decay.sweep_interval_s,
        apply=lambda config, value: _decay_apply(config, "sweep_interval_s", value),
        live_apply=True,
    ),
    "decay.min_apply_delta": ConfigKey(
        key_path="decay.min_apply_delta",
        value_type="non-negative number",
        validate=_validate_non_negative_float,
        read=lambda config: config.decay.min_apply_delta,
        apply=lambda config, value: _decay_apply(config, "min_apply_delta", value),
        live_apply=True,
    ),
    "decay.lambda_per_type": ConfigKey(
        key_path="decay.lambda_per_type",
        value_type="node-type -> positive number map",
        validate=_validate_lambda_map,
        read=lambda config: config.decay.lambda_per_type,
        apply=lambda config, value: _decay_apply(config, "lambda_per_type", value),
        live_apply=True,
    ),
    # B2.1 T2 (design/01 §4.6): the mid-session auto-recall pipeline. All
    # three keys are live-applied — the daemon's MemoryService reads the
    # shared Config reference, so a flip reaches the NEXT user-prompt scan
    # without a restart (default OFF).
    "capture.auto_recall": ConfigKey(
        key_path="capture.auto_recall",
        value_type="boolean",
        validate=_validate_bool,
        read=lambda config: config.capture.auto_recall,
        apply=lambda config, value: _capture_apply(config, "auto_recall", value),
        live_apply=True,
    ),
    "capture.auto_recall_focal_floor": ConfigKey(
        key_path="capture.auto_recall_focal_floor",
        value_type="number in (0, 1]",
        validate=_validate_focal_floor,
        read=lambda config: config.capture.auto_recall_focal_floor,
        apply=lambda config, value: _capture_apply(config, "auto_recall_focal_floor", value),
        live_apply=True,
    ),
    "capture.auto_recall_budget_chars": ConfigKey(
        key_path="capture.auto_recall_budget_chars",
        value_type="positive integer",
        validate=_validate_positive_int,
        read=lambda config: config.capture.auto_recall_budget_chars,
        apply=lambda config, value: _capture_apply(config, "auto_recall_budget_chars", value),
        live_apply=True,
    ),
}
for _role in LLM_ROLES:
    CONFIG_KEY_REGISTRY.update(_role_key_specs(_role))

#: Sorted registry keys map to stable version-id slots.
_SLOT_KEYS: tuple[str, ...] = tuple(sorted(CONFIG_KEY_REGISTRY))
_REGISTRY_SLOTS: dict[str, int] = {key_path: index for index, key_path in enumerate(_SLOT_KEYS)}


def _version_id(key_path: str, version: int) -> int:
    return _REGISTRY_SLOTS[key_path] * _VERSION_STRIDE + version


def _resolve_version_id(version_id: int) -> tuple[str, int]:
    """Decode a global version id into its (key, version) pair."""
    slot, version = divmod(version_id, _VERSION_STRIDE)
    try:
        key_path = _SLOT_KEYS[slot]
    except IndexError:
        raise ConfigWriteError("version_id", f"unknown config version {version_id}") from None
    return key_path, version


def _coerce_version_id(raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ConfigWriteError("version_id", "must be an integer")
    if isinstance(raw, str):
        if not raw.isdigit():
            raise ConfigWriteError("version_id", "must be an integer")
        return int(raw)
    if raw < 1:
        raise ConfigWriteError("version_id", "must be a positive integer")
    return raw


# ---------------------------------------------------------------- TOML patch


def _line_key(line: str) -> str | None:
    """The TOML key of a key=value line; None for headers/comments/blanks."""
    stripped = line.strip()
    if not stripped or stripped.startswith("[") or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip().strip('"').strip("'")


def _table_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Header name -> (start, end) for every TOML table.

    ``end`` is the next header's index (exclusive), so a table body is
    ``lines[start + 1 : end]``; the last table runs to EOF.
    """
    spans: dict[str, tuple[int, int]] = {}
    current: str | None = None
    start: int = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if current is not None:
                spans[current] = (start, index)
            current = stripped[1:-1].strip()
            start = index
    if current is not None:
        spans[current] = (start, len(lines))
    return spans


def _toml_str(value: Any) -> str:
    """Encode a scalar as a TOML literal (double-quoted strings, JSON booleans)."""
    return json.dumps(value)


def _toml_value(value: Any) -> str:
    """Encode a config value as a TOML literal.

    Scalars reuse ``_toml_str``; mappings render as inline tables
    (``{ "KEY" = value }`` — TOML uses ``=``, not JSON's ``:``), which is how
    dict-valued keys like ``decay.lambda_per_type`` are patched.
    """
    if isinstance(value, dict):
        inner = ", ".join(f"{json.dumps(key)} = {_toml_str(item)}" for key, item in value.items())
        return "{" + inner + "}"
    return _toml_str(value)


def _inline_comment_index(line: str) -> int:
    """Index of the first ``#`` outside quotes (a trailing comment), else -1."""
    in_quotes = False
    quote = ""
    for index, char in enumerate(line):
        if char in "\"'":
            if not in_quotes:
                in_quotes = True
                quote = char
            elif quote == char:
                in_quotes = False
        elif char == "#" and not in_quotes:
            return index
    return -1


def _rewrite_value_line(line: str, leaf: str, value: Any) -> str:
    """Rewrite a ``leaf = value`` line in place, keeping indent + comments."""
    indent = line[: len(line) - len(line.lstrip())]
    comment_index = _inline_comment_index(line)
    suffix = line[comment_index:] if comment_index >= 0 else ""
    return f"{indent}{leaf} = {_toml_value(value)}{suffix}"


def _drop_nested_table(lines: list[str], table_path: str, leaf: str) -> list[str]:
    """Remove a ``[<table_path>.<leaf>]`` sub-table block from a table body.

    A dict-valued registry key (``decay.lambda_per_type``) is written as an
    inline TOML table; a hand-written sub-table spelling of the same key would
    then double-define it and fail the next load, so the stale block is dropped
    before the inline line lands.
    """
    nested = f"{table_path}.{leaf}"
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped[1:-1].strip() == nested:
                skipping = True
                continue
            skipping = False
        if not skipping:
            out.append(line)
    return out


def _patch_toml(path: Path, key_path: str, value: Any) -> None:
    """Surgical line-oriented TOML patch for one key.

    ``value=None`` removes the key's line (a clear); anything else writes
    ``leaf = <literal>`` inside the key's table, rewriting an existing line in
    place (never duplicated) or creating the table after the last existing one.
    A hand-written ``[<table_path>.<leaf>]`` sub-table spelling of the target
    key is dropped first, so the inline form never double-defines it.

    The file's trailing newline splits to a phantom empty element; it is
    dropped up front so a table span never carries it into the patched output
    (a series of writes into the last table would otherwise drift one blank
    line between every key).
    """
    table_path, _, leaf = key_path.rpartition(".")
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = _drop_nested_table(original.split("\n"), table_path, leaf)
    while lines and lines[-1] == "":
        lines.pop()
    spans = _table_spans(lines)

    if table_path in spans:
        start, end = spans[table_path]
        new_body: list[str] = []
        written = False
        for line in lines[start + 1 : end]:
            line_key = _line_key(line)
            if line_key == leaf:
                if value is None:
                    continue  # clear: drop the line
                new_body.append(_rewrite_value_line(line, leaf, value))
                written = True
                continue
            new_body.append(line)
        if value is not None and not written:
            new_body.append(f"{leaf} = {_toml_value(value)}")
        out = lines[: start + 1] + new_body + lines[end:]
    else:
        if value is None:
            return  # a clear with no table to edit writes nothing
        insert_at = max((finish for _, finish in spans.values()), default=len(lines))
        # One blank separator before a freshly created table keeps the mirror
        # readable; the final join strips it when the file was empty.
        block = ["", f"[{table_path}]", f"{leaf} = {_toml_value(value)}"]
        out = lines[:insert_at] + block + lines[insert_at:]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).strip("\n") + "\n", encoding="utf-8")


# ---------------------------------------------------------------- fingerprint


def _file_fingerprint(path: Path) -> tuple[float, str]:
    """(mtime, sha256) of the config file; hand edits break the hash."""
    if not path.exists():
        return 0.0, ""
    stat = path.stat()
    return stat.st_mtime, hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- service


class ConfigWriteService:
    """The single config writer: registry -> validate -> patch -> record ->
    audit -> live-apply (offline when no meta store is attached)."""

    def __init__(
        self,
        config: Config,
        meta: ConfigWriteMeta | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._meta = meta
        self._clock = clock if clock is not None else time.time
        # F2 hot-apply generation: every successful write/rollback bumps the
        # global counter; a role-key write also bumps that role's counter so
        # consumers (the role router) rebuild exactly what changed.
        self._generation = 0
        self._role_generations: dict[str, int] = {}

    # ------------------------------------------------------------ generation (F2)

    @property
    def generation(self) -> int:
        """Monotonic config-write generation: bumped on every successful set
        or rollback, so per-run consumers re-check their materialized state."""
        return self._generation

    def generation_for(self, role: str) -> int:
        """Per-role generation: bumped only when a write touched that role's
        route keys (a non-role key leaves every role generation untouched)."""
        return self._role_generations.get(role, 0)

    def _bump(self, key_path: str) -> None:
        self._generation += 1
        prefix = "dream.llm."
        if key_path.startswith(prefix):
            role, _, _ = key_path[len(prefix) :].partition(".")
            if role:
                self._role_generations[role] = self._generation

    # ------------------------------------------------------------ reads

    def get(self) -> dict[str, Any]:
        """The resolved config for the wire, secrets redacted to names only."""
        return {
            "config": {
                "preset": self._config.preset,
                "baseurl": self._config.baseurl,
                "dream": {
                    "auto_trigger": self._config.dream.auto_trigger,
                    "floor_pool_points": self._config.dream.floor_pool_points,
                    "idle_min_sec": self._config.dream.idle_min_sec,
                    "hard_deadline_sec": self._config.dream.hard_deadline_sec,
                    "hardware_tier": self._config.dream.hardware_tier,
                    "ensemble": self._config.dream.ensemble,
                    "core_confidence_floor": self._config.dream.core_confidence_floor,
                    "delta_budget_ceiling_tokens": self._config.dream.delta_budget_ceiling_tokens,
                    "pool_forced_cap": self._config.dream.pool_forced_cap,
                    "llm": {role: self._resolved_role(role) for role in LLM_ROLES},
                },
                "decay": {
                    "enabled": self._config.decay.enabled,
                    "sweep_interval_s": self._config.decay.sweep_interval_s,
                    "min_apply_delta": self._config.decay.min_apply_delta,
                    "lambda_per_type": dict(self._config.decay.lambda_per_type),
                },
                "capture": {
                    "auto_recall": self._config.capture.auto_recall,
                    "auto_recall_focal_floor": self._config.capture.auto_recall_focal_floor,
                    "auto_recall_budget_chars": self._config.capture.auto_recall_budget_chars,
                },
            },
            "restart_required": {},
        }

    def _resolved_role(self, role: str) -> dict[str, Any]:
        cfg = self._config.llm.get(role)
        if cfg is None:
            # A recognized-but-unconfigured role (B5 dream_vote has no factory
            # default): surface it as unconfigured so the settings page sees the
            # role exists without inventing a route.
            return {
                "driver": None,
                "model": None,
                "base_url": None,
                "api_key_env": None,
                "max_tokens": None,
                "provider": None,
                "think": None,
                "num_ctx": None,
                "num_predict": None,
            }
        return {
            "driver": cfg.driver,
            "model": cfg.model,
            "base_url": cfg.params.get("base_url"),
            "api_key_env": _redact_env_names(cfg.params.get("api_key_env")),
            "max_tokens": cfg.params.get("max_tokens"),
            "provider": cfg.params.get("provider"),
            "think": cfg.params.get("think"),
            "num_ctx": cfg.params.get("num_ctx"),
            "num_predict": cfg.params.get("num_predict"),
        }

    def versions(self) -> list[dict[str, Any]]:
        """The versioned history (registry keys only, never internal records)."""
        if self._meta is None:
            return []
        out: list[dict[str, Any]] = []
        for key_path in _SLOT_KEYS:
            version = 1
            while True:
                entry = self._meta.get_config(key_path, version)
                if entry is None:
                    break
                out.append(
                    {
                        "version_id": _version_id(key_path, entry.version),
                        "key": key_path,
                        "version": entry.version,
                        "value": self._redact(key_path, entry.value.get("value")),
                        "updated_at": entry.updated_at,
                    }
                )
                version += 1
        out.sort(key=lambda item: item["version_id"])
        return out

    # ------------------------------------------------------------ writes

    def set(self, key_path: str, value: Any, *, actor: str = "console") -> dict[str, Any]:
        """Validate, patch, record, audit and live-apply one config write."""
        spec = CONFIG_KEY_REGISTRY.get(key_path)
        if spec is None:
            raise ConfigWriteError(key_path, "unknown config key (not writable)")
        try:
            validated = spec.validate(value)
        except ValueError as exc:
            raise ConfigWriteError(key_path, str(exc)) from exc
        if spec.cross_validate is not None:
            try:
                spec.cross_validate(self._config, validated)
            except ValueError as exc:
                raise ConfigWriteError(key_path, str(exc)) from exc
        path = self._config_path()
        _patch_toml(path, key_path, validated)
        version_id = self._record(key_path, validated)
        spec.apply(self._config, validated)
        self._touch_fingerprint()
        self._bump(key_path)
        restart_required = not spec.live_apply
        self._audit(
            "config.set",
            {
                "key_path": key_path,
                "value": validated,
                "version_id": version_id,
                "restart_required": restart_required,
            },
            actor,
        )
        return {
            "ok": True,
            "version_id": version_id,
            "restart_required": restart_required,
            "key_path": key_path,
            "value": validated,
            "persisted_to": str(path),
            "actor": actor,
        }

    def rollback(self, version_id: Any, *, actor: str = "console") -> dict[str, Any]:
        """Restore a recorded version, append-only (a new record, never a
        delete): the file, the live config and the versioned store converge on
        the restored value."""
        if self._meta is None:
            raise ConfigWriteError("rollback", "no versioned config store available")
        resolved = _coerce_version_id(version_id)
        key_path, version = _resolve_version_id(resolved)
        spec = CONFIG_KEY_REGISTRY.get(key_path)
        if spec is None:
            raise ConfigWriteError(key_path, "unknown config key (not writable)")
        target = self._meta.get_config(key_path, version)
        if target is None:
            raise ConfigWriteError(key_path, f"no version {version} recorded for {key_path!r}")
        self._meta.rollback_config(key_path, version)
        restored = self._meta.get_config(key_path)
        if restored is None:
            raise ConfigWriteError(key_path, "rollback produced no restored record")
        restored_value = restored.value.get("value")
        path = self._config_path()
        _patch_toml(path, key_path, restored_value)
        spec.apply(self._config, restored_value)
        self._touch_fingerprint()
        self._bump(key_path)
        new_version_id = _version_id(key_path, restored.version)
        self._audit(
            "config.rollback",
            {"key_path": key_path, "restored_version": version, "version_id": new_version_id},
            actor,
        )
        return {
            "ok": True,
            "version_id": new_version_id,
            "restored": new_version_id,
            "key_path": key_path,
            "persisted_to": str(path),
            "actor": actor,
        }

    # ------------------------------------------------------------ boot reconcile (E1-4 DB-primary)

    def reconcile_boot(self) -> dict[str, Any]:
        """DB-primary boot overlay (Phase 0, D1 "settings DB primary").

        The settings DB is the primary store for registry keys; config.toml is
        its generated mirror. At boot:

        - one-shot audited import: a registry key with NO DB entry takes its
          value from the file (already resolved into the live Config) and is
          recorded — the only file->DB direction Phase 0 allows, audited as
          ``config_import`` (actor=daemon);
        - DB-wins overlay: for every key the DB already holds, the DB value is
          authoritative — it is applied to the live config and the toml mirror
          line is regenerated from it (``_patch_toml``); the DB is NEVER
          rebaselined from the file;
        - drift detection: when the regenerated mirror was the consequence of a
          changed file (mtime/hash drift), a ``config_mirror_drift`` log line
          and audit entry name the rewritten keys;
        - boot-scope keys (preset/storage/baseurl/auth) are NOT registry keys:
          they stay file-scoped and restart-required, untouched by this pass.
        """
        if self._meta is None:
            return {
                "ok": False,
                "reason": "no versioned config store available",
                "changed": False,
                "keys_updated": [],
                "mirror_rewritten": [],
            }
        path = self._config_path()
        fingerprint = _file_fingerprint(path)
        last = self._last_fingerprint()
        drifted = last != fingerprint
        imported: list[str] = []
        repaired: list[str] = []
        for key_path in _SLOT_KEYS:
            spec = CONFIG_KEY_REGISTRY[key_path]
            current = spec.read(self._config)
            entry = self._meta.get_config(key_path)
            if entry is None:
                # One-shot import: the DB has no registry entry for this key, so
                # the file (resolved into the live Config) is the only source.
                self._meta.set_config(key_path, {"value": current})
                imported.append(key_path)
                continue
            db_value = entry.value.get("value")
            if db_value != current:
                # DB wins on the live config; the toml mirror is regenerated to
                # converge on the DB. Never the reverse direction.
                spec.apply(self._config, db_value)
                _patch_toml(path, key_path, db_value)
                repaired.append(key_path)
        if imported:
            self._audit(
                "config_import",
                {
                    "reason": "initial",
                    "keys_imported": imported,
                    "hash": fingerprint[1],
                    "mtime": fingerprint[0],
                },
                "daemon",
            )
        if repaired:
            logger.warning(
                "config_mirror_drift: config.toml diverged from the settings DB; "
                "%d registry key(s) regenerated from the DB (the DB is primary, "
                "the file is a mirror)",
                len(repaired),
            )
            self._audit(
                "config_mirror_drift",
                {
                    "drifted": drifted,
                    "keys_rewritten": repaired,
                    "hash": fingerprint[1],
                    "mtime": fingerprint[0],
                },
                "daemon",
            )
        if imported or repaired or drifted:
            # The file or the DB changed: record the current file state so the
            # next boot compares against a known baseline (a boot-scope-only
            # file edit is legitimate and must not re-trigger drift).
            self._set_fingerprint(_file_fingerprint(path))
        return {
            "ok": True,
            "changed": bool(imported or repaired),
            "reason": "initial" if imported else ("hand_edit" if repaired else "noop"),
            "keys_updated": imported + repaired,
            "mirror_rewritten": repaired,
            "fingerprint": {"mtime": fingerprint[0], "hash": fingerprint[1]},
        }

    # ------------------------------------------------------------ plumbing

    def _config_path(self) -> Path:
        source = self._config.source
        return source if source is not None else CONFIG_PATH

    def _record(self, key_path: str, value: Any) -> int | None:
        if self._meta is None:
            return None
        version = self._meta.set_config(key_path, {"value": value})
        return _version_id(key_path, version)

    def _audit(self, action: str, detail: dict[str, Any], actor: str) -> bool:
        if self._meta is None:
            return False
        self._meta.audit_append(AuditEntry(actor=actor, action=action, detail=detail, at=self._clock()))
        return True

    def _touch_fingerprint(self) -> None:
        """A write moves the file, so the recorded file state follows."""
        if self._meta is None:
            return
        fingerprint = _file_fingerprint(self._config_path())
        self._set_fingerprint(fingerprint)

    def _last_fingerprint(self) -> tuple[float, str] | None:
        if self._meta is None:
            return None
        entry = self._meta.get_config(_FILE_STATE_KEY)
        if entry is None:
            return None
        return float(entry.value.get("mtime", 0.0)), str(entry.value.get("hash", ""))

    def _set_fingerprint(self, fingerprint: tuple[float, str]) -> None:
        if self._meta is None:
            return
        self._meta.set_config(_FILE_STATE_KEY, {"mtime": fingerprint[0], "hash": fingerprint[1]})

    def _redact(self, key_path: str, value: Any) -> Any:
        """Secret keys surface env-var NAMES only; other values pass through."""
        if CONFIG_KEY_REGISTRY[key_path].secret:
            return _redact_env_names(value)
        return value


def _redact_env_names(value: Any) -> str:
    """Keep env-var NAMES and ``secrets:`` references only: a literal key value
    never surfaces on a read (a reference is not a value)."""
    if not isinstance(value, str):
        return ""
    names = [token.strip() for token in value.split(",") if token.strip()]
    kept = [name for name in names if _ENV_NAME_RE.fullmatch(name) or is_secrets_ref(name)]
    return ",".join(kept)
