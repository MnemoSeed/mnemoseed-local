"""Secrets reference grammar (T2-2).

The ``api_key_env`` field of a dream role accepts either a comma-separated
env-var NAME list (unchanged) OR a single ``secrets:mnemoseed/dream/<role>``
reference. A reference names the secret-store key — never a value — so the
grammar here is the one shared by the config loader, the configwrite registry
validator, and the role router's resolve-time precedence:

    reference  ->  secrets:mnemoseed/dream/<role>
    store name ->  mnemoseed/dream/<role>

The embedded role is shape-checked against the live dream roles by the
callers (LLM_ROLES lives in mnemoseed_local.config; importing it here would create
a cycle, since config validation consumes this grammar).
"""

from __future__ import annotations

import re

#: The reference prefix that distinguishes a store reference from an env name.
SECRETS_REF_PREFIX = "secrets:"

#: Shape of a reference; the role is validated against the live roles by the
#: callers (LLM_ROLES lives in mnemoseed_local.config).
SECRETS_REF_RE = re.compile(r"secrets:mnemoseed/dream/([a-z][a-z0-9_]*)")


def is_secrets_ref(value: str) -> bool:
    """True when the value is a secrets-store reference (not an env name)."""
    return value.startswith(SECRETS_REF_PREFIX)


def secret_name_from_ref(ref: str) -> str | None:
    """The store key a reference addresses (``mnemoseed/dream/<role>``), or
    None when the value is not a reference."""
    if not is_secrets_ref(ref):
        return None
    name = ref[len(SECRETS_REF_PREFIX) :]
    return name or None


ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]+")


def redact_key_ref_for_display(value: object) -> str:
    """Presentation-safe form of an api_key_env config value.

    Env-var NAME lists and ``secrets:`` references are names, never values, so
    they are shown verbatim. ANYTHING else (e.g. a user-pasted literal key
    tolerated by an older config) becomes ``<redacted>`` — audit, /api/v1/config,
    and every other observability surface must never echo secret material.

    Env-name grammar: UPPER_SNAKE segments joined by at least one underscore
    (prevents plain long-uppercase key shapes like ``AKIA…`` from sneaking
    through as "names").
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if is_secrets_ref(text):
        return text
    names = [p.strip() for p in text.split(",") if p.strip()]
    if names and all(ENV_NAME_RE.fullmatch(name) for name in names) and all("_" in name for name in names):
        return text
    return "<redacted>"
