"""GatewayClient: a thin retry + honest-error wrapper over the daemon client.

PRD-B2.3 D5/D6: the gateway's ``serve()`` wraps its daemon client in a
:class:`GatewayClient` that duck-types the inner client (``.post(path, body)``
and ``.profile_id``) and adds exactly one behavior — a fast retry that fires
ONLY when the first ``DaemonUnavailableError`` is caused by an
``httpx.ConnectError`` (a loopback refusal is the daemon-restart window). A
retry re-issues the SAME request body to a bounded-timeout twin of the client
(a real ``DaemonClient`` is a frozen dataclass, so ``dataclasses.replace``
yields one; a duck-typed stub without a ``timeout`` field falls back to the
same client — honestly, a loopback refusal already fails in milliseconds, so
the 1.5s budget only documents intent there).

Rest errors, timeout causes and cause-less unavailability never retry; the
refused/timeout failure shapes become honest user-facing hints. Retry state
is strictly per-call local (no instance-level consumed-retry state).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import httpx

from mnemoseed_local.rest_client import DaemonUnavailableError

#: Budget for the single fast retry leg (the first attempt keeps the client's
#: own 30s timeout; this bounds the retry where the inner client allows a
#: per-call override).
RETRY_TIMEOUT_SECONDS = 1.5

#: Honest shape for a loopback refusal that survived the single retry.
DAEMON_DOWN_HINT = "cannot reach {base_url}: daemon is not running (start it with 'mnemoseed-local up')"

#: Honest shape for a timeout-caused unavailability.
DAEMON_TIMEOUT_HINT = "cannot reach {base_url}: daemon timed out after 30s (busy or hung; try again shortly)"


class GatewayClient:
    """Duck-typed wrapper that retries a ConnectError-caused refusal once."""

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def wrap(cls, client: Any) -> GatewayClient:
        """Idempotent: return a wrapped client, or the client itself if it is
        already a :class:`GatewayClient`."""
        if isinstance(client, cls):
            return client
        return cls(client)

    @property
    def profile_id(self) -> str:
        return cast(str, self._client.profile_id)

    @property
    def base_url(self) -> str:
        return cast(str, getattr(self._client, "base_url", ""))

    def _retry_client(self) -> Any:
        """A bounded-timeout twin for the retry leg.

        ``DaemonClient`` is a frozen dataclass, so ``dataclasses.replace``
        yields a fresh client whose per-call timeout bounds the fast retry.
        A duck-typed stub with no ``timeout`` field can't be replaced — fall
        back to the same client (the refusal case fails in milliseconds
        anyway, so the 1.5s budget only documents intent there).
        """
        try:
            return replace(self._client, timeout=RETRY_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            return self._client

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return cast(dict[str, Any], self._client.post(path, body))
        except DaemonUnavailableError as exc:
            cause = exc.__cause__
            if isinstance(cause, httpx.ConnectError):
                # Loopback refused = the daemon-restart window: exactly one
                # fast retry, then report down honestly.
                try:
                    return cast(dict[str, Any], self._retry_client().post(path, body))
                except DaemonUnavailableError:
                    raise DaemonUnavailableError(DAEMON_DOWN_HINT.format(base_url=self.base_url)) from None
            if isinstance(cause, httpx.TimeoutException):
                raise DaemonUnavailableError(DAEMON_TIMEOUT_HINT.format(base_url=self.base_url)) from None
            raise
