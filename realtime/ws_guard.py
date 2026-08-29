"""Admission and liveness bounds for WebSocket endpoints.

WHY THIS EXISTS
---------------
A WebSocket handler occupies a Cloud Run concurrency slot for the whole life of
the connection, not for the length of a request. `infra/gcp/deploy_backend.sh`
deploys with `--concurrency 80 --timeout 300`, so one accepted socket holds
1/concurrency of an instance for up to five minutes, and nothing in the stack
reclaimed it earlier:

* Neither WebSocket route requires credentials. `/api/ws/simple` says so in its
  own docstring; `/api/ws/metrics` takes a `token` query param but
  `ConnectionManager.connect` falls through to an anonymous session when the
  token is missing OR invalid, so it is unauthenticated in practice too.
* `RateLimitMiddleware` cannot see either of them. It is a
  `BaseHTTPMiddleware`, which Starlette runs only for `scope["type"] == "http"`;
  the WebSocket handshake never enters it.
* Neither handler had an idle cap, so a socket that says nothing after the
  handshake was indistinguishable from one doing work, and kept its slot until
  the platform timeout expired.

So filling an instance cost an attacker `concurrency` idle sockets and no
credentials. Lowering `--concurrency` does not create that exposure but does
make it cheaper to reach — at 20, twenty idle sockets are the whole instance
where eighty were needed before.

WHAT THIS BUYS, HONESTLY
------------------------
Two layers, and only one of them is an adversarial control. This mirrors the
reasoning already written down in `middleware/rate_limiter._reject_anonymous`.

1. `WebSocketAdmission` — a ceiling on concurrently held sockets, shared by
   EVERY WebSocket route in the process. This is the actual guarantee. It keys
   on nothing: there is no identity to rotate, no header to forge, and no
   per-route budget to hop between, so the arithmetic that wedges an instance
   ("open `concurrency` sockets") stops being available regardless of who is
   asking. A per-IP ceiling is deliberately NOT offered here for the reason
   `_anonymous_identity` documents at length: behind the platform edge
   X-Forwarded-For is client-supplied, so a per-IP bucket is rotated away by an
   adversary while still collapsing legitimate callers that share an egress IP.

2. `idle_receive_text` — a deadline on client silence. This bounds a socket
   that holds a slot while doing nothing, turning "free until the platform
   timeout" into "free for `idle_timeout` unless you keep sending". It is NOT
   adversarial: an attacker who sends one keepalive per window keeps its slot,
   which is precisely why layer 1 carries the guarantee and this one does not.
   It is applied only where server-initiated traffic does not exist, because
   an idle deadline keyed on client messages would disconnect a legitimately
   passive listener — see the note in `routes/dashboard_routes.py`.

The ceiling is NOT published to callers. `middleware/rate_limiter` withholds its
thresholds for the same reason: a caller told the exact ceiling is told exactly
how many sockets to open. The idle timeout IS published, because a client cannot
comply with a keepalive interval it has not been given, and an attacker gains
nothing from it — it can keep alive at any interval it likes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("ws_guard")

# RFC 6455 1013 "Try Again Later" — the capacity refusal, distinct from an
# authorization refusal. See WebSocketAdmission.reserve for what a caller
# actually observes.
WS_CLOSE_TRY_AGAIN_LATER = 1013
# RFC 6455 1001 "Going Away" — the server, not the client, is ending a healthy
# connection.
WS_CLOSE_GOING_AWAY = 1001


def _positive_int(raw, default: int) -> int:
    """Parse a positive int from the environment, falling back on anything else.

    A misconfigured ceiling must not disable the ceiling. `0`, a negative value
    and junk all fall back to the default rather than being honoured, because
    every one of those spellings would mean "hold no sockets at all" or "hold
    unlimited sockets" depending on how the comparison happened to be written,
    and neither is a thing an operator can have meant by typo.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return value if value > 0 else default


class WebSocketIdleTimeout(Exception):
    """Raised by `idle_receive_text` when the client went quiet for too long."""


class WebSocketAdmission:
    """A process-wide ceiling on concurrently held WebSocket slots.

    ONE instance is shared by every WebSocket route (`ws_admission` below). A
    per-route ceiling would not close the hole: capping `/api/ws/simple` while
    `/api/ws/metrics` stays uncapped just moves the attack one path over, and
    two routes each capped at N still let an attacker hold 2N slots.
    """

    def __init__(self, *, default_max_connections: int = 8) -> None:
        self._default_max_connections = default_max_connections
        self._active = 0
        # Denial logging is throttled because the flood this class exists to
        # absorb would otherwise turn into a log line per rejected handshake —
        # trading a wedged instance for a log bill and a swamped log pipeline.
        # The suppressed count is carried into the next line so the volume is
        # never silently lost.
        self._denied_since_log = 0
        self._last_denial_log = 0.0
        self._denial_log_interval = 10.0

    @property
    def max_connections(self) -> int:
        """Read from the environment on each use so it is tunable at runtime.

        The default is deliberately sized against what an INSTANCE survives
        rather than against measured client demand, because demand here is not
        measured: `/api/ws/simple` has no in-repo consumer and nothing ever
        calls `simple_manager.broadcast`, and `/api/ws/metrics` is an internal
        dashboard. Being wrong low is visible and recoverable — an operator's
        tab is refused and reconnects, very likely onto another instance, since
        this is per-process state. Being wrong high is the bug being fixed. Raise
        `WS_MAX_CONNECTIONS` deliberately once real demand is measured.
        """
        return _positive_int(
            os.getenv("WS_MAX_CONNECTIONS"), self._default_max_connections
        )

    @property
    def active(self) -> int:
        return self._active

    async def reserve(self, websocket: WebSocket) -> bool:
        """Claim a slot, or refuse the handshake. True means the caller owns a
        slot and MUST `release()` it in a `finally`.

        The refusal is sent before `accept()`, which Starlette turns into a
        rejected handshake (HTTP 403) rather than an accepted-then-closed
        session. That is the cheap path on purpose: under the flood this guard
        exists for, refusing must cost less than accepting, or the guard becomes
        the amplifier. The trade is that a well-behaved client sees a handshake
        rejection rather than close code 1013, so the code is logged here
        instead.

        The check and the increment are deliberately not separated by an
        `await`. The event loop cannot interleave another handler between them,
        so `max_connections` sockets is a real bound rather than one that N
        simultaneous handshakes can all read as "not full yet" and overshoot.
        """
        limit = self.max_connections
        if self._active >= limit:
            self._log_denial(limit)
            await websocket.close(code=WS_CLOSE_TRY_AGAIN_LATER)
            return False
        self._active += 1
        return True

    def release(self) -> None:
        """Give a slot back. Safe to call more often than reserve() succeeded.

        Callers must invoke this from a `finally`, not from exception handlers.
        `asyncio.CancelledError` is a BaseException, so a handler shaped
        `except Exception` — which is what both routes had — does not run when
        the platform tears a connection down at the request timeout or during a
        shutdown drain. A counter that leaks on that path would ratchet toward
        the ceiling and eventually refuse everyone, which is the failure this
        class is supposed to prevent rather than cause.
        """
        if self._active > 0:
            self._active -= 1

    def _log_denial(self, limit: int) -> None:
        self._denied_since_log += 1
        now = time.monotonic()
        if now - self._last_denial_log < self._denial_log_interval:
            return
        logger.warning(
            "websocket handshake refused: %d slot(s) in use, ceiling %d "
            "(%d refused since last report)",
            self._active,
            limit,
            self._denied_since_log,
        )
        self._last_denial_log = now
        self._denied_since_log = 0


# The single process-wide ceiling. Import this, do not build another.
ws_admission = WebSocketAdmission()


def idle_timeout_seconds(default: int = 90) -> int:
    """Seconds of client silence tolerated before a socket is reclaimed."""
    return _positive_int(os.getenv("WS_IDLE_TIMEOUT_SECONDS"), default)


async def idle_receive_text(
    websocket: WebSocket, timeout: Optional[float] = None
) -> str:
    """`websocket.receive_text()` with a deadline.

    Raises `WebSocketIdleTimeout` instead of waiting forever, so a socket that
    holds a concurrency slot while sending nothing is reclaimed by us in
    `timeout` seconds rather than by the platform in `--timeout 300`.
    """
    limit = idle_timeout_seconds() if timeout is None else timeout
    try:
        return await asyncio.wait_for(websocket.receive_text(), timeout=limit)
    except asyncio.TimeoutError as exc:
        raise WebSocketIdleTimeout(
            f"no client message within {limit}s"
        ) from exc
