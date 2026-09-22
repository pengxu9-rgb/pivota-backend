"""Accept the PIVOTA-Agent gateway's **Google-signed Cloud Run identity token** on the one
read-only ops route the gateway calls, so that route stops depending on a standing admin JWT.

WHY THIS EXISTS, AND WHY IT IS LOAD-BEARING.

`GET /ops/merchant-purchasability` is the gateway's contract with this backend (see
routes/merchant_purchasability_ops.py and docs/runbooks/merchant_purchasability.md). Until now
the gateway authenticated with `PIVOTA_OPS_ADMIN_TOKEN` — a long-lived admin/super_admin JWT
pasted into the gateway's environment. That is the weakest part of the design and the failure is
SILENT: the gateway fails OPEN on any non-200, by design, so the day that JWT expires the ops
read starts 401-ing, the gateway logs `merchant_purchasability_read_failed` once per five
minutes, and the purchasability gate is **disarmed** while every dial still reads "on". A gate
that disarms itself on a calendar date is not a gate.

A Google OIDC identity token has no such date: Cloud Run's metadata server mints a fresh one per
hour for the gateway's own service account, and nothing has to be copied anywhere.

**THE APP-LEVEL CHECK IS THE ONLY GUARANTEE.** Production `web` is deployed
`--allow-unauthenticated` (`infra/gcp/deploy_backend.sh`, `PUBLIC=1`), so Cloud Run IAM does not
stand in front of this route at all: anyone on the internet may open a TCP connection to it. The
verification below is therefore not "defence in depth behind IAM" — it IS the defence. Every
branch in here fails CLOSED.

WHAT IS ACCEPTED. All of these must hold, and each is a separate conjunct so that a test can kill
a mutant that drops one:

  1. the token verifies against Google's published certificates (RS256; `alg: none` and HS256
     are rejected by `google.auth.jwt.decode`, which has an algorithm allow-list);
  2. `iss` is one of `accounts.google.com` / `https://accounts.google.com`;
  3. `aud` equals `OPS_GATEWAY_OIDC_AUDIENCE` **exactly** — and when that env is unset this whole
     path is DISABLED, not "unconstrained";
  4. `email_verified` is boolean true;
  5. `email` is in `OPS_GATEWAY_SERVICE_ACCOUNTS` (comma-separated, compared lower-cased) — and
     when THAT env is unset this whole path is DISABLED too;
  6. `exp`/`iat` are inside the library's own window with a clock skew of 10 s.

Both envs unset is the shipped default, which means: **this module changes nothing until an
operator sets both.** Until then `require_admin_or_gateway_identity` is `require_admin`.

WHAT IS NEVER DONE HERE. The token is never logged, never echoed, never put in an exception
message, and never returned in a response body. On failure the caller gets **exactly** the
HTTPException `require_admin` would have raised for the same request — same status, same body —
so a prober cannot learn from the response whether this path exists, is enabled, or which
conjunct refused it. The reason lives in a rate-limited `warning` as a short CODE, with no token
material and no attacker-controlled text.

THIS IS FOR ONE READ-ONLY ROUTE. It is deliberately NOT a general widening of `require_admin`,
and it is deliberately NOT `require_admin_or_key`: `X-ADMIN-KEY` is not accepted here and no
behaviour of any other route changes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Set

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from utils.auth import ADMIN_ROLES, get_current_user, security

logger = logging.getLogger("utils.gateway_oidc_auth")

#: Exact `aud` the gateway's identity token must carry. Recommended value: this backend's
#: canonical https origin (e.g. `https://api.pivota.cc`). MUST match the gateway's
#: `PIVOTA_OPS_OIDC_AUDIENCE` byte for byte — an audience is a string compare, not a URL compare,
#: so a trailing slash or an `http://` is a different audience and the token is refused.
AUDIENCE_ENV = "OPS_GATEWAY_OIDC_AUDIENCE"

#: Comma-separated service-account emails allowed to use this path. Prod gateway:
#: `sa-gateway@pivota-prod.iam.gserviceaccount.com` (infra/gcp/deploy_gateway.sh `--service-account`).
SERVICE_ACCOUNTS_ENV = "OPS_GATEWAY_SERVICE_ACCOUNTS"

#: The two spellings Google uses. Checked here as well as inside `verify_oauth2_token`, because a
#: conjunct that lives only in a dependency is a conjunct this repo cannot test for removal.
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

#: A Google ID token is ~1 KB. 8 KiB is a ceiling on work done before any parsing, so a caller
#: cannot spend our CPU on base64 of their choosing.
MAX_TOKEN_BYTES = 8 * 1024

#: `exp`/`iat` leeway. Cloud Run clocks are NTP-disciplined; 10 s is the brief's ceiling and there
#: is no reason to want more.
CLOCK_SKEW_SECONDS = 10

#: Bound on the Google certs fetch. `google.auth.transport.requests.Request` defaults to 120 s,
#: which on a route with a 2 s client budget is an outage shaped like a hang.
CERTS_TIMEOUT_SECONDS = 3.0

#: The `role` on the synthesised principal. NOT `admin`: nothing downstream should be able to
#: mistake a machine identity for a human administrator by reading `role`.
GATEWAY_IDENTITY_ROLE = "gateway_identity"

#: One warning per reason code per interval. Unbounded warn-per-request is how a refused prober
#: turns an auth check into a log-spend DoS.
WARN_INTERVAL_SECONDS = 60.0


class _Refused(Exception):
    """Internal: a short machine reason code. NEVER reaches a response body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Configuration (read per request, never at import — so an env flip needs no redeploy of
# anything that merely imports this module, and so tests can monkeypatch it).
# ---------------------------------------------------------------------------

def configured_audience() -> str:
    return (os.getenv(AUDIENCE_ENV) or "").strip()


def configured_service_accounts() -> Set[str]:
    raw = os.getenv(SERVICE_ACCOUNTS_ENV) or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def gateway_identity_enabled() -> bool:
    """Both envs, or nothing. Either one alone is a half-configured gate, and a half-configured
    gate that accepted tokens would be either audience-less or allowlist-less — both are open
    doors, so both read as DISABLED."""
    return bool(configured_audience()) and bool(configured_service_accounts())


# ---------------------------------------------------------------------------
# Certs transport with a bounded timeout
# ---------------------------------------------------------------------------

class _BoundedCertsRequest:
    """Wrap `google.auth.transport.requests.Request` so the certs fetch cannot hang.

    The library calls `request(certs_url, method="GET")` with no timeout, which lands on the
    transport's 120 s default. This forces ours regardless of what the caller passes.
    """

    def __init__(self, inner: Any, timeout: float = CERTS_TIMEOUT_SECONDS) -> None:
        self._inner = inner
        self._timeout = timeout

    def __call__(self, url: str, method: str = "GET", body: Any = None,
                 headers: Any = None, timeout: Any = None, **kwargs: Any) -> Any:
        return self._inner(url, method=method, body=body, headers=headers,
                           timeout=self._timeout, **kwargs)


_certs_request_lock = threading.Lock()
_certs_request: Optional[_BoundedCertsRequest] = None


def certs_request() -> _BoundedCertsRequest:
    """One transport for the process. The google-auth library caches the fetched certs against
    the session this wraps, so re-using it is what keeps the common path off the network."""
    global _certs_request
    with _certs_request_lock:
        if _certs_request is None:
            import google.auth.transport.requests as google_requests

            _certs_request = _BoundedCertsRequest(google_requests.Request())
        return _certs_request


# ---------------------------------------------------------------------------
# Rate-limited refusal logging — reason CODE only
# ---------------------------------------------------------------------------

_warn_lock = threading.Lock()
_warn_last: Dict[str, float] = {}


def _warn_rate_limited(code: str) -> None:
    now = time.monotonic()
    with _warn_lock:
        last = _warn_last.get(code)
        if last is not None and (now - last) < WARN_INTERVAL_SECONDS:
            return
        _warn_last[code] = now
        # Bounded: the code space is this module's own constants, but a dict keyed on anything
        # deserves a ceiling anyway.
        if len(_warn_last) > 64:
            _warn_last.clear()
            _warn_last[code] = now
    logger.warning(
        "ops gateway identity refused (reason=%s); falling back to the admin JWT refusal", code,
    )


def _reset_warn_state_for_test() -> None:
    with _warn_lock:
        _warn_last.clear()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_gateway_identity(token: str) -> Dict[str, Any]:
    """Return the synthesised principal, or raise `_Refused` with a reason code.

    Raises only `_Refused`. Anything the library throws — including a network failure reaching
    Google — is folded into `verify_failed`, because the CALLER must not be able to tell a
    forged signature from a certs outage.
    """
    audience = configured_audience()
    if not audience:
        raise _Refused("audience_env_unset")
    allowed = configured_service_accounts()
    if not allowed:
        raise _Refused("allowlist_env_unset")

    candidate = token or ""
    if not candidate.strip():
        raise _Refused("empty_token")
    # Measured in BYTES, before anything parses it.
    if len(candidate.encode("utf-8", "ignore")) > MAX_TOKEN_BYTES:
        raise _Refused("token_too_large")

    from google.oauth2 import id_token as google_id_token

    try:
        claims = google_id_token.verify_oauth2_token(
            candidate,
            certs_request(),
            audience=audience,
            clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
        )
    except Exception:
        # Deliberately not `exc_info`: a JWT library's exception text can quote token segments.
        raise _Refused("verify_failed") from None

    if not isinstance(claims, dict):
        raise _Refused("claims_not_mapping")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise _Refused("bad_iss")
    if claims.get("aud") != audience:
        raise _Refused("bad_aud")
    # Boolean true, not truthy: `"false"`, `1` and `"yes"` are all truthy and none of them is
    # what Google mints (a JSON boolean).
    if claims.get("email_verified") is not True:
        raise _Refused("email_not_verified")

    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise _Refused("no_email")
    normalized_email = email.strip().lower()
    if normalized_email not in allowed:
        raise _Refused("email_not_allowlisted")

    # No `sub`, no user id: this is a MACHINE, and anything that later wants to attribute an
    # action to a person must fail to find one rather than find a plausible-looking one.
    return {
        "role": GATEWAY_IDENTITY_ROLE,
        "email": normalized_email,
        "auth_method": "gateway_oidc",
        "audience": audience,
    }


# ---------------------------------------------------------------------------
# The dependency
# ---------------------------------------------------------------------------

async def require_admin_or_gateway_identity(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    """`require_admin`, OR the gateway's Google identity token.

    **The admin path runs first and is unchanged**, including the `test-token` bypass and the
    503-on-unusable-secret behaviour, because it is `get_current_user` itself that runs — not a
    copy of it that could drift.

    `security` is `HTTPBearer(auto_error=True)`, the SAME dependency `require_admin` reaches
    through, so a missing header, a non-Bearer scheme and an empty credential all produce
    byte-identical refusals to today's.

    On any failure of BOTH paths the admin path's exception is what propagates. That is the
    no-fingerprinting rule: an OIDC token that fails for any reason gets the 401 a garbage admin
    JWT gets, and every other ops route — which still depends on `require_admin` — answers
    identically to the same request.
    """
    admin_error: HTTPException
    try:
        current_user = await get_current_user(credentials)
    except HTTPException as exc:
        admin_error = exc
    else:
        if current_user.get("role") in ADMIN_ROLES:
            return current_user
        # Same status and body as `require_admin`'s role refusal.
        admin_error = HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    try:
        identity = verify_gateway_identity(credentials.credentials)
    except _Refused as refusal:
        _warn_rate_limited(refusal.code)
        raise admin_error from None
    except Exception:
        # FAIL CLOSED on anything unforeseen. A bug in here must refuse, never admit.
        _warn_rate_limited("unexpected_error")
        raise admin_error from None

    # DEBUG, and only the email: at info this would publish the calling identity on every
    # gateway read, which is a per-request log line on a serving path.
    logger.debug("ops gateway identity accepted: %s", identity["email"])
    return identity
