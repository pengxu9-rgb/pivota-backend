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

RESIDUAL EXPOSURE — what this does NOT fix
------------------------------------------
The ceiling converts "wedge the whole instance" into "deny the WebSocket
surface", and that second thing is now CHEAPER than the first was. Measured
against a real uvicorn at the shipped defaults: 8 anonymous, silent sockets
parked on `/api/ws/metrics` — the route with no idle deadline — hold the shared
budget indefinitely and every subsequent handshake on BOTH routes is refused.
Pre-fix, denying the dashboard cost 80 sockets and took all HTTP down with it,
which is loud; post-fix it costs 8 and is invisible to everything except this
module's own log line.

That trade is deliberate — losing the dashboard beats losing every HTTP request
on the instance — but it is a trade, not a clean win, and the arithmetic that
makes it cheap is the same fact underneath both: neither route requires a
credential. There is no ceiling number that fixes this, because the guard cannot
prefer a legitimate socket over an anonymous one when it cannot tell them apart.
Closing it means authenticating `/api/ws/metrics` (and deciding whether
`/api/ws/simple`, which has no consumer at all, should exist), which is a
separate decision about who may use the dashboard.

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
# RFC 6455 4000-4999 is the private-use range. Deliberately NOT 1001 "Going
# Away", which clients conventionally read as "server restarting, reconnect
# now" — applied to an idle deadline that turns one stable socket into a
# handshake every WS_IDLE_TIMEOUT_SECONDS, costing more than the reclaim saves.
# 4408 echoes HTTP 408 Request Timeout so the intent is legible without a table.
WS_CLOSE_IDLE_TIMEOUT = 4408


# A ceiling above this is treated as a typo rather than an instruction. This is
# ONLY a typo guard, not a safety property: the number that actually matters is
# the deploy script's --concurrency, which this process cannot see. It exists
# because `WS_MAX_CONNECTIONS=80000` — one stray zero — would otherwise silently
# disable the entire guarantee while looking like a deliberate setting.
_CEILING_TYPO_GUARD = 256


def _positive_int(raw, default: int, maximum: Optional[int] = None) -> int:
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
    if value <= 0:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


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
        # When the ceiling most recently went from "has room" to "full". The
        # denial line reports `_active`, which at the moment of a denial is
        # ALWAYS equal to the ceiling and therefore says nothing; how long the
        # ceiling has been continuously full is the number that separates "four
        # operators opened tabs" from "someone is parking sockets on us".
        self._saturated_since = None

    @property
    def max_connections(self) -> int:
        """Read from the environment on each use.

        Not "tunable at runtime" on the deployment target, despite the
        per-call getenv: Cloud Run env vars are immutable per revision, so
        changing this ships a new revision and therefore a fresh process
        with `_active` back at 0. The per-call read buys testability and
        costs a dict lookup; it does not buy a live dial.

        The default is sized against what an INSTANCE survives rather than
        against measured client demand, because demand here is not measured:
        neither route has an in-repo consumer, and nothing ever calls
        `simple_manager.broadcast`.

        Do NOT read the low default as harmless. An earlier version of this
        docstring claimed a refused operator "reconnects, very likely onto
        another instance"; that is not a property Cloud Run provides. It routes
        on request concurrency and cannot see this in-process counter, so an
        instance holding 8 sockets out of `--concurrency 80` looks ~90% idle to
        the scheduler and is a PREFERRED target for the next handshake. With
        MIN=2 instances (infra/gcp/deploy_backend.sh) a refused client has
        roughly a coin flip of landing back on the saturated one, and retrying
        does not improve those odds. The real reason to be wrong low rather than
        high is that the failure is confined to the WebSocket surface instead of
        taking every HTTP request on the instance with it — not that it
        self-heals. See the module docstring's RESIDUAL EXPOSURE note.

        Note also that a dashboard page opening both routes spends TWO slots, so
        this default is roughly four concurrent viewers per instance. Raise
        `WS_MAX_CONNECTIONS` deliberately once real demand is measured.
        """
        return _positive_int(
            os.getenv("WS_MAX_CONNECTIONS"),
            self._default_max_connections,
            maximum=_CEILING_TYPO_GUARD,
        )

    @property
    def active(self) -> int:
        return self._active

    async def reserve(self, websocket: WebSocket) -> bool:
        """Claim a slot, or refuse the handshake. True means the caller owns a
        slot and MUST `release()` it in a `finally`.

        The refusal is sent before `accept()`. That is the cheap path on
        purpose: under the flood this guard exists for, refusing must cost less
        than accepting, or the guard becomes the amplifier.

        The trade is worse than "the client sees a different close code", and
        the cost lands on operators, so it is spelled out. uvicorn DISCARDS the
        code on a pre-accept close and answers the handshake with a flat HTTP
        403 — verified against uvicorn 0.51.0, whose sans-io implementation
        emits FORBIDDEN unconditionally. So `WS_CLOSE_TRY_AGAIN_LATER` is only
        ever observed by Starlette's TestClient; in production a refused caller
        cannot tell capacity from authorization, and on a route whose docstring
        advertises JWT auth it will read as an auth failure and send someone to
        debug tokens. `_log_denial` therefore carries the code and the
        saturation duration, because the server log is the ONLY place this
        condition is distinguishable.

        The check and the increment are deliberately not separated by an
        `await`. The event loop cannot interleave another handler between them,
        so `max_connections` sockets is a real bound rather than one that N
        simultaneous handshakes can all read as "not full yet" and overshoot.
        """
        limit = self.max_connections
        if self._active >= limit:
            if self._saturated_since is None:
                self._saturated_since = time.monotonic()
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
        if self._active < self.max_connections:
            self._saturated_since = None

    def _log_denial(self, limit: int) -> None:
        """The only place a refusal is distinguishable from an auth failure.

        Carries the close code because uvicorn drops it on the wire (see
        `reserve`), and the saturation duration because `_active` == `limit` is
        a tautology at the point of denial.
        """
        self._denied_since_log += 1
        now = time.monotonic()
        if now - self._last_denial_log < self._denial_log_interval:
            return
        saturated_for = (
            now - self._saturated_since if self._saturated_since is not None else 0.0
        )
        logger.warning(
            "websocket handshake refused with close code %d (sent on the wire as "
            "HTTP 403): %d slot(s) in use, ceiling %d, saturated for %.1fs "
            "(%d refused since last report)",
            WS_CLOSE_TRY_AGAIN_LATER,
            self._active,
            limit,
            saturated_for,
            self._denied_since_log,
        )
        self._last_denial_log = now
        self._denied_since_log = 0


# The single process-wide ceiling. Import this, do not build another.
ws_admission = WebSocketAdmission()


def idle_timeout_seconds(default: int = 90) -> int:
    """Seconds of client silence tolerated before a socket is reclaimed."""
    return _positive_int(os.getenv("WS_IDLE_TIMEOUT_SECONDS"), default)


def keepalive_seconds() -> int:
    """The interval a client should actually send on — HALF the deadline.

    Publishing the deadline itself, which is what this used to do, is a bug that
    disconnects every client that obeys it: a ping sent `idle_timeout` seconds
    after the last one arrives at `deadline + ε`, so the socket is already being
    reclaimed. Measured against a real uvicorn with the deadline at 3s, a client
    pinging every 3s got the idle-timeout error frame instead of its first pong
    and was closed before the second.

    Half leaves a full missed interval of slack, so one dropped or delayed ping
    does not cost the connection.
    """
    return max(1, idle_timeout_seconds() // 2)


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
