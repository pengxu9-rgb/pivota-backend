"""One authenticated identity for the metrics dashboard surface, REST and WebSocket.

WHY THIS EXISTS
---------------
`realtime.metrics_store.snapshot()` defaults to `role="admin"`, and every caller
took that default, so an anonymous request received per-PSP, per-agent and
per-merchant volumes and latencies for the whole platform. Four separate paths
served it, and none of them required a credential:

* `GET /api/snapshot` authenticated ONLY if a token happened to be supplied.
  With none, `role` came straight off the query string with `"admin"` as its
  default — so the caller chose its own authority.
* `GET /api/recent-events`, `GET /api/connection-stats` and `GET /api/ws/status`
  had no authentication of any kind.
* `POST /api/reset-metrics` had none either, and it MUTATES: any anonymous
  caller could wipe the metrics store.
* Both WebSocket routes pushed the same unfiltered snapshot to anonymous
  sockets.

The WebSocket "authentication" was worse than absent, in a way worth recording
because it looked present. `ConnectionManager` verified tokens against a
hardcoded `"your-secret-key"` while every token this system issues is signed
with `utils.auth.JWT_SECRET` (`settings.jwt_secret_key`). The two never matched,
so a REAL token always failed to decode and was silently downgraded to
anonymous — the token parameter had never once authenticated anybody — while a
token forged with the literal in the source decoded fine. That is why this
module is the only place a dashboard identity is resolved: a second copy of the
rule is how the first one drifted.

WHAT IT ENFORCES
----------------
Fail CLOSED. No credential is not "anonymous with viewer rights", it is 401 (or
a closed socket). A token with no `role` claim is rejected rather than defaulted,
because "missing means admin" is the precise shape of the bug being fixed.

There is deliberately NO kill switch. `middleware/rate_limiter` ships one for its
limits and that is the right call there, but the same reasoning does not carry:
on Cloud Run an environment variable is immutable per revision, so flipping a
switch costs exactly one deploy — the same as rolling the revision back, which
is safer, self-documenting and does not leave a fail-open flag in the tree for
someone to find later and leave on.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Query, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials

from config.platform import is_deployed, is_production
from realtime.metrics_store import PLATFORM_WIDE_ROLES
from utils import auth as _auth
from utils.auth import decode_token, is_admin as is_admin_role, optional_security

logger = logging.getLogger("dashboard_auth")

# The value config/settings.py falls back to when JWT_SECRET_KEY is unset. It is
# a literal in this repository, so a token signed with it is forgeable by anyone
# who can read the source.
_DEV_DEFAULT_JWT_SECRET = "your-super-secret-key"
# RFC 7518 3.2: an HMAC-SHA256 key must be at least as long as the hash output.
_MIN_JWT_SECRET_BYTES = 32


def _jwt_secret_is_trustworthy() -> bool:
    """False when tokens signed with this secret are forgeable by a reader.

    `config/production.py` already declares `jwt_secret_key` as required with
    `min_length=32` — but `ProductionSettings` is never instantiated anywhere in
    the tree, so that guard has never run. `utils.auth` binds
    `config.settings.settings.jwt_secret_key`, which falls back to a literal.
    """
    # Read through the module rather than a `from ... import JWT_SECRET` snapshot:
    # `decode_token` resolves utils.auth.JWT_SECRET at call time, so a by-value
    # copy here could disagree with the secret actually verifying tokens — and a
    # test patching only one side would be asserting this guard's opinion of a
    # secret the system is not using.
    secret = (getattr(_auth, "JWT_SECRET", "") or "").strip()
    if not secret or secret == _DEV_DEFAULT_JWT_SECRET:
        return False
    return len(secret.encode()) >= _MIN_JWT_SECRET_BYTES


def _refuse_forgeable_tokens() -> bool:
    """Whether to reject token credentials outright on this host.

    BOTH predicates, OR-ed — they are independent, not nested, so either alone
    leaves a gap: `is_production()` alone permits every staging revision, and
    `is_deployed()` alone permits PIVOTA_ENV=production on an unmanaged host.
    This is a "must not happen on a real server" guard, so it fires on either.
    Same shape as `utils/runtime_safety.py:16-24`.

    It refuses rather than crashes. Raising at import would take the whole
    service down if a deployment really is running on the fallback secret, which
    trades a data-exposure bug for an outage; failing this surface closed leaves
    every other route working and leaves the X-ADMIN-KEY path — a credential
    that does not derive from the JWT secret — available to whoever has to go
    fix it.
    """
    return (is_deployed() or is_production()) and not _jwt_secret_is_trustworthy()

# RFC 6455 1008 "Policy Violation" — the WebSocket refusal for an unacceptable
# credential, kept distinct from ws_guard's 1013 capacity refusal so the two
# reasons stay separable in the log even though uvicorn collapses both to an
# HTTP 403 on the wire.
WS_CLOSE_POLICY_VIOLATION = 1008

# PLATFORM_WIDE_ROLES is imported, not redeclared. A second copy of an
# authorization list is how the two JWT secrets drifted apart in the first place.

# Admin-ness is decided by utils.auth.is_admin, not by a fourth copy of
# ["admin", "super_admin"] — the same reasoning that makes PLATFORM_WIDE_ROLES an
# import rather than a redeclaration.

# Roles that describe ONE tenant and are meaningless without naming it.
SCOPED_ROLES = frozenset({"agent", "merchant"})

# The claim each issuer actually emits for the tenant id, per role. There is no
# `entity_id` claim anywhere in this system despite the name being used
# internally: utils.auth.create_jwt_token writes `merchant_id`/`agent_id`
# (utils/auth.py:583-589) and routes/auth_routes.py writes `merchant_id` at
# login (:304). Reading `entity_id` — which is what this module did first —
# resolved every scoped token to None, and under the store's old fall-through
# that meant such a caller saw EVERYTHING.
_TENANT_CLAIMS = {
    "merchant": ("merchant_id", "entity_id"),
    "agent": ("agent_id", "entity_id"),
}
# CAVEAT on the "agent" row: nothing in this repo currently ISSUES a token
# carrying `agent_id`. utils.auth.create_jwt_token writes it, but its only
# agent-scoped caller (routes/dashboard_api.py:97) passes an `expires_delta=`
# kwarg that function does not accept, so it raises; and routes/auth_routes.py
# writes only `merchant_id` despite a comment at :302 saying "merchant_id or
# agent_id". So every role="agent" token in circulation names no tenant and is
# refused here. That direction is safe — refusal, not exposure — but it means
# agent dashboards do not work until an issuer emits the claim, and this map is
# what they will need to satisfy.


@dataclass(frozen=True)
class DashboardPrincipal:
    """Who is asking. `role` ALWAYS comes from a verified credential."""

    sub: str
    role: str
    entity_id: Optional[str] = None

    @property
    def sees_everything(self) -> bool:
        return self.role in PLATFORM_WIDE_ROLES

    @property
    def is_admin(self) -> bool:
        return is_admin_role(self.role)


class DashboardAuthError(Exception):
    """No usable credential. Carries a reason safe to log, never the credential."""


def _admin_key_matches(supplied: str) -> bool:
    """Constant-time compare against the same env keys `require_admin_or_key` uses.

    An unset key must never match an unset header — otherwise the entire surface
    opens up the moment a deployment forgets to configure one.
    """
    # Compared as BYTES. `hmac.compare_digest` on `str` raises TypeError for any
    # non-ASCII character, and Starlette decodes header bytes as latin-1, so a
    # header of `X-ADMIN-KEY: \xc3\xa9` produced an uncaught TypeError — a 500 on
    # every route of this surface, from an unauthenticated caller, and only once
    # ADMIN_API_KEY was configured (the `if expected` guard), i.e. only in
    # production. The predecessor this mirrors, utils.auth.require_admin_or_key,
    # uses `in` and cannot throw; the parity its docstring claims has to include
    # not being crashable.
    supplied_bytes = supplied.encode("utf-8", "surrogateescape")
    for name in ("ADMIN_API_KEY", "PROMOTIONS_ADMIN_KEY"):
        expected = (os.getenv(name) or "").strip()
        if expected and hmac.compare_digest(supplied_bytes, expected.encode()):
            return True
    return False


def _principal_from_token(token: str) -> DashboardPrincipal:
    try:
        payload = decode_token(token)
    except HTTPException as exc:  # decode_token speaks HTTP; we may be a socket
        raise DashboardAuthError(str(exc.detail)) from exc

    role = str(payload.get("role") or "").strip()
    if not role:
        # The bug this module exists to fix, in one line: a credential that does
        # not say what it is must not inherit the most privileged default.
        raise DashboardAuthError("token carries no role claim")

    subject = str(payload.get("sub") or "").strip()
    if not subject:
        raise DashboardAuthError("token carries no subject")

    entity_id = None
    for claim in _TENANT_CLAIMS.get(role, ("entity_id",)):
        value = payload.get(claim)
        if value:
            entity_id = str(value)
            break

    if role in SCOPED_ROLES and not entity_id:
        # A scoped role that does not say WHICH tenant cannot be authorized.
        # Refused here rather than allowed through as a principal that happens
        # to see nothing: the store's filter also returns nothing for this case,
        # but the two layers must not depend on each other to be safe.
        raise DashboardAuthError(f"{role} token names no tenant")

    return DashboardPrincipal(sub=subject, role=role, entity_id=entity_id)


def resolve_principal(
    *,
    token: Optional[str] = None,
    bearer: Optional[str] = None,
    admin_key: Optional[str] = None,
) -> DashboardPrincipal:
    """Resolve exactly one identity, or raise. Never returns an anonymous one.

    Three credential shapes, because the surface has three kinds of caller:
    the admin key for internal polling, a bearer token for ordinary API clients,
    and the `?token=` query parameter for browsers — whose WebSocket API cannot
    set headers, which is the only reason a credential in a query string is
    tolerated here.
    """
    if admin_key and _admin_key_matches(admin_key):
        return DashboardPrincipal(sub="admin_key", role="admin")

    for candidate in (bearer, token):
        if candidate:
            if _refuse_forgeable_tokens():
                logger.error(
                    "refusing JWT auth: JWT_SECRET_KEY is unset, the repo's "
                    "dev-default, or under %d bytes, so any token is forgeable "
                    "from the source. Set a strong JWT_SECRET_KEY; the "
                    "X-ADMIN-KEY path still works meanwhile.",
                    _MIN_JWT_SECRET_BYTES,
                )
                raise DashboardAuthError("token authentication is disabled on this host")
            return _principal_from_token(candidate)

    raise DashboardAuthError("no credential supplied")


async def require_dashboard_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
    x_admin_key: Optional[str] = Header(None, alias="X-ADMIN-KEY"),
) -> DashboardPrincipal:
    """FastAPI dependency. 401 when no credential resolves.

    Header credentials only. `GET /api/snapshot` used to take `?token=`, and a
    query string is not a safe place for a 24-hour bearer token: uvicorn's access
    log writes the request line with its query string, and Cloud Run records
    `httpRequest.requestUrl` regardless of what the app does — so every such
    request parked a live credential in the logs. (The app's own
    `middleware/structured_logging` redacts query keys containing "token", but it
    is a BaseHTTPMiddleware and neither the platform log nor a WebSocket scope
    passes through it.) An HTTP client can always send a header. A BROWSER
    WebSocket cannot, which is the sole reason `authenticate_websocket` still
    accepts one — unavoidable there, avoidable here.
    """
    try:
        return resolve_principal(
            bearer=credentials.credentials if credentials else None,
            admin_key=x_admin_key,
        )
    except DashboardAuthError as exc:
        logger.warning("dashboard request refused: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def require_dashboard_admin(
    principal: DashboardPrincipal = Depends(require_dashboard_principal),
) -> DashboardPrincipal:
    """For the endpoints that mutate or expose connection internals."""
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return principal


async def authenticate_websocket(
    websocket: WebSocket, token: Optional[str] = None
) -> Optional[DashboardPrincipal]:
    """Authenticate a socket BEFORE it is accepted, or close it.

    Returning None means the socket is already closed and the handler must
    return immediately.

    Called before any slot is reserved from `ws_guard`, and the ordering is the
    point rather than an accident: an unauthenticated flood is refused without
    ever touching the connection ceiling, so the budget can only be spent by
    callers holding a credential. That is what closes the residual noted in
    ws_guard's module docstring, where eight anonymous sockets could hold the
    shared ceiling and deny the dashboard to everyone.
    """
    header = websocket.headers.get("authorization") or ""
    bearer = header[7:].strip() if header[:7].lower() == "bearer " else None
    try:
        return resolve_principal(
            token=token,
            bearer=bearer,
            admin_key=websocket.headers.get("x-admin-key"),
        )
    except DashboardAuthError as exc:
        logger.warning("websocket refused: %s", exc)
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return None
