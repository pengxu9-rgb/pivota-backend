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

AND TWO THINGS THAT ARE NOT ABOUT FORGERY AT ALL, because this route is on a public service and
an anonymous stranger's request must not be able to cost us anything:

  * **no outbound request is made for a token that cannot possibly verify** — google-auth fetches
    Google's certificates on EVERY verification, unconditionally, and does it BEFORE parsing the
    token, so a cache, a header pre-check and a fetch budget stand in front of it (see the block
    comment above `_BoundedCertsRequest`);
  * **the verification never runs on the event loop** — it is synchronous and does blocking I/O,
    so `require_admin_or_gateway_identity` hands it to a threadpool under a hard timeout.

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

import asyncio
import base64
import binascii
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Set, Tuple
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from starlette.concurrency import run_in_threadpool

from utils.auth import ADMIN_ROLES, get_current_user, security

logger = logging.getLogger("utils.gateway_oidc_auth")

#: The `aud` the gateway's identity token must carry. Recommended value: this backend's canonical
#: https ORIGIN, `https://api.pivota.cc`.
#:
#: ⚠️ THE VALUE IS NORMALISED, AND IT IS NORMALISED THE WAY THE GATEWAY NORMALISES IT. Review of
#: the first cut found the two sides disagreeing: the gateway's `cloudRunAudience()` lower-cases
#: the host, drops a default `:443` and folds one trailing slash to the origin, while this side
#: only `.strip()`ed — so an operator who pasted the SAME STRING `https://api.pivota.cc/` into
#: both envs got a 401 on every read. Because the gateway fails OPEN that is SILENT: the gate
#: disarms and every dial still says "on". This side now applies the identical rule (see
#: `normalize_audience`), so "paste the same string into both" is true rather than nearly true.
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

#: Bound on ONE Google certs fetch, in seconds. `google.auth.transport.requests.Request` defaults
#: to 120 s, which on a route with a 2 s client budget is an outage shaped like a hang.
#:
#: 1.5 s, not 3 s: `requests` applies this PER SOCKET OPERATION, so a 3 s setting is a multiple of
#: 3 s in the worst case, and the gateway's whole call budget for this route is 2 s. Anything we
#: spend past that is spent on a client that has already given up.
CERTS_TIMEOUT_SECONDS = 1.5

#: Hard ceiling on the WHOLE verification — certs fetch, RSA verify, claim checks — enforced by
#: the async dependency around the worker thread. A caller that waits longer than the gateway's
#: own budget is waiting for nothing.
VERIFY_TIMEOUT_SECONDS = 2.0

#: How long a fetched certs document is reused. google-auth does NOT cache: `_fetch_certs` issues
#: an unconditional GET on every `verify_oauth2_token`, and a `requests.Session` is a connection
#: pool, not a cache. Measured on the first cut: five valid reads made five requests to
#: googleapis, and — because the fetch happens BEFORE the token is parsed — three junk tokens from
#: an anonymous caller made three more. This route is on a PUBLIC service, so that is an
#: unauthenticated stranger steering our outbound traffic. Google rotates these keys on the order
#: of days; an hour is conservative.
CERTS_TTL_SECONDS_ENV = "OPS_GATEWAY_OIDC_CERTS_TTL_SECONDS"
DEFAULT_CERTS_TTL_SECONDS = 3600.0
MIN_CERTS_TTL_SECONDS = 60.0
MAX_CERTS_TTL_SECONDS = 86400.0

#: A `kid` we have never seen is the ONE legitimate reason to refetch inside the TTL (Google
#: rotated early). It is also the cheapest way for a stranger to make us fetch, so it is allowed
#: at most once per this interval, process-wide. A `kid` still missing after a fresh document is
#: a 401, never another fetch.
CERTS_KID_REFRESH_MIN_INTERVAL_SECONDS = 60.0

#: A process-wide ceiling on certs fetches, independent of every reason for making one. The last
#: line: whatever bug or novel input gets past the two mechanisms above, the backend cannot be
#: made to issue more than this many outbound requests per window.
CERTS_FETCH_MAX_PER_WINDOW = 10
CERTS_FETCH_WINDOW_SECONDS = 60.0

#: Google's OAuth2 x509 certs document — the one `verify_oauth2_token` uses. Named here because
#: the cache is keyed on it.
GOOGLE_OAUTH2_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"

#: The only JWS algorithm a Google ID token is ever signed with, and the only one we will spend a
#: certs fetch on. `google.auth.jwt.decode` refuses everything else anyway — but it refuses it
#: AFTER the fetch, which is the whole problem.
REQUIRED_TOKEN_ALG = "RS256"

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

def normalize_audience(value: Any) -> Optional[str]:
    """A bare https ORIGIN, or `None`.

    THE RULE, AND IT IS SHARED WITH THE GATEWAY (`src/services/cloudRunIdentityToken.js`
    `cloudRunAudience`): https only; no userinfo; no path beyond `/`; no query; no fragment; the
    host is lower-cased; a default `:443` is dropped; one trailing slash folds to the origin.

    Anything else is `None`, which DISABLES the path — it does not "pass through". An audience
    that is not an origin cannot be the audience the gateway asked the metadata server for, so
    accepting it would arm a door that can never open, and arming a door that can never open is
    how the gate disarms silently.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.path not in ("", "/"):
        return None
    if parsed.query or parsed.fragment:
        return None
    if port not in (None, 443):
        return None
    host = (parsed.hostname or "").strip()
    if not host:
        return None
    return f"https://{host}"


def configured_audience() -> str:
    """The normalised audience, or `""` when the env is unset OR unusable.

    A value that was SET and refused is logged once per interval with a reason code — otherwise
    a typo in this env is indistinguishable from not having set it, and the symptom of both is
    "the gate quietly does nothing".
    """
    raw = (os.getenv(AUDIENCE_ENV) or "").strip()
    if not raw:
        return ""
    normalized = normalize_audience(raw)
    if normalized is None:
        # The env's VALUE is operator-supplied configuration, not caller input, so naming it is
        # not a leak — and not naming it makes this line useless.
        _warn_rate_limited(f"audience_env_invalid:{raw[:120]}")
        return ""
    return normalized


def configured_service_accounts() -> Set[str]:
    raw = os.getenv(SERVICE_ACCOUNTS_ENV) or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def gateway_identity_enabled() -> bool:
    """Both envs, or nothing. Either one alone is a half-configured gate, and a half-configured
    gate that accepted tokens would be either audience-less or allowlist-less — both are open
    doors, so both read as DISABLED."""
    return bool(configured_audience()) and bool(configured_service_accounts())


# ---------------------------------------------------------------------------
# Certs: a bounded transport, a cache with a TTL, and a fetch rate limit
#
# ⚠️ GOOGLE-AUTH DOES NOT CACHE. `google.oauth2.id_token._fetch_certs` issues an unconditional
# `GET https://www.googleapis.com/oauth2/v1/certs` on EVERY `verify_oauth2_token`, and it does so
# BEFORE `jwt.decode` looks at the token. A `requests.Session` is a connection pool, not a cache.
# On a route served by a `--allow-unauthenticated` service that composes into: any stranger can
# make this backend issue an outbound request to googleapis, once per request they send, by
# posting `aaaa.bbbb.cccc`. Three measurements from the first cut: 5 valid reads -> 5 GETs;
# 3 anonymous junk tokens -> 3 GETs.
#
# Three mechanisms, in the order a request meets them, each one sufficient on its own to cap the
# damage the others let through:
#
#   1. `unverified_token_header` — the token must be a well-formed compact JWS of at most 8 KiB
#      with `alg: RS256` and a `kid`, BEFORE any fetch. Junk costs zero network.
#   2. `_CertsCache` — one document per certs URL, reused for a TTL, with a `kid`-miss refresh
#      that is itself rate-limited. A `kid` still missing after a fresh document is a 401, not
#      another fetch.
#   3. `_FetchBudget` — a process-wide ceiling on fetches per window, whatever the reason.
#
# And `verify_oauth2_token` is then handed a transport that CANNOT reach the network at all: it
# replays the cached bytes. That is what makes "0 fetches" a property rather than a hope.
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


class _ReplayedCertsResponse:
    """The two attributes `google.oauth2.id_token._fetch_certs` reads off a response."""

    __slots__ = ("status", "data")

    def __init__(self, data: bytes) -> None:
        self.status = 200
        self.data = data


class _ReplayCertsRequest:
    """A transport that serves ONE in-memory certs document and can reach nothing.

    This is what `verify_oauth2_token` is given, so the library's own unconditional fetch becomes
    a dictionary lookup. A URL other than the one we cached is a hard error rather than a
    fallthrough to the network — if the library ever starts asking for a second document we want
    to find out from a test, not from an egress bill.
    """

    __slots__ = ("_url", "_data")

    def __init__(self, url: str, certs: Dict[str, Any]) -> None:
        self._url = url
        self._data = json.dumps(certs).encode("utf-8")

    def __call__(self, url: str, method: str = "GET", **kwargs: Any) -> _ReplayedCertsResponse:
        if url != self._url:
            raise RuntimeError("the certs verifier asked for an uncached URL")
        return _ReplayedCertsResponse(self._data)


_certs_request_lock = threading.Lock()
_certs_request: Optional[_BoundedCertsRequest] = None


def certs_request() -> Any:
    """The one OUTBOUND transport for the process, with our timeout forced onto it."""
    global _certs_request
    with _certs_request_lock:
        if _certs_request is None:
            import google.auth.transport.requests as google_requests

            _certs_request = _BoundedCertsRequest(google_requests.Request())
        return _certs_request


def certs_ttl_seconds() -> float:
    """Env-tunable, and CLAMPED. A TTL of 0 would restore the unbounded-fetch defect exactly, and
    a TTL of a week would outlive a real Google rotation, so neither is reachable from an env."""
    raw = (os.getenv(CERTS_TTL_SECONDS_ENV) or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_CERTS_TTL_SECONDS
    except ValueError:
        value = DEFAULT_CERTS_TTL_SECONDS
    return max(MIN_CERTS_TTL_SECONDS, min(MAX_CERTS_TTL_SECONDS, value))


def _http_fetch_certs(url: str) -> Dict[str, Any]:
    """THE ONE PLACE THIS MODULE TOUCHES THE NETWORK. Patched wholesale by the tests, which is
    how "N requests made exactly M fetches" is measured rather than asserted."""
    from google.oauth2 import id_token as google_id_token

    certs = google_id_token._fetch_certs(certs_request(), url)
    if not isinstance(certs, dict) or not certs:
        raise _Refused("certs_unusable")
    return certs


class _FetchBudget:
    """A process-wide ceiling on outbound certs fetches per window."""

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._lock = threading.Lock()
        self._stamps: list = []

    def take(self, now: float) -> bool:
        with self._lock:
            self._stamps = [t for t in self._stamps if (now - t) < self._window]
            if len(self._stamps) >= self._limit:
                return False
            self._stamps.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._stamps = []


class _CertsCache:
    """One certs document per URL, with a TTL and a rate-limited `kid`-miss refresh."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._certs: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._last_kid_refresh: Dict[str, float] = {}
        self._budget = _FetchBudget(CERTS_FETCH_MAX_PER_WINDOW, CERTS_FETCH_WINDOW_SECONDS)

    def reset(self) -> None:
        with self._lock:
            self._certs.clear()
            self._last_kid_refresh.clear()
            self._budget.reset()

    def _fetch(self, url: str, now: float) -> Dict[str, Any]:
        if not self._budget.take(now):
            raise _Refused("certs_fetch_rate_limited")
        try:
            certs = _http_fetch_certs(url)
        except _Refused:
            raise
        except Exception:
            raise _Refused("certs_unavailable") from None
        with self._lock:
            self._certs[url] = (certs, now)
        return certs

    def certs_for_kid(self, kid: str, url: str = GOOGLE_OAUTH2_CERTS_URL) -> Dict[str, Any]:
        """The certs document that contains `kid`, or `_Refused`. At most one fetch, and only
        when there is a reason for one."""
        now = time.monotonic()
        ttl = certs_ttl_seconds()
        with self._lock:
            entry = self._certs.get(url)
            fresh = entry is not None and (now - entry[1]) < ttl
            cached = entry[0] if entry is not None else None

        if fresh and kid in cached:
            return cached

        if not fresh:
            # Nothing usable in hand: one fetch, and whatever comes back must contain the kid.
            certs = self._fetch(url, now)
            if kid not in certs:
                raise _Refused("unknown_kid")
            return certs

        # FRESH, BUT THE KID IS NOT IN IT. The legitimate cause is an early Google rotation. It
        # is also the cheapest lever a stranger has on our egress, so it is allowed at most once
        # per interval process-wide — and a kid still missing after a refresh is a 401, never a
        # second fetch.
        with self._lock:
            last = self._last_kid_refresh.get(url)
            if last is not None and (now - last) < CERTS_KID_REFRESH_MIN_INTERVAL_SECONDS:
                raise _Refused("unknown_kid")
            self._last_kid_refresh[url] = now

        certs = self._fetch(url, now)
        if kid not in certs:
            raise _Refused("unknown_kid")
        return certs


_certs_cache = _CertsCache()


def _reset_certs_cache_for_test() -> None:
    _certs_cache.reset()


def _b64url_json(segment: str) -> Dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value


def unverified_token_header(token: str) -> Dict[str, Any]:
    """Parse the JWS header WITHOUT verifying anything, so a token that cannot possibly verify is
    refused BEFORE it can cost a network request.

    This is not a security check — everything it looks at is attacker-controlled, and every one of
    these fields is checked again, for real, inside `verify_oauth2_token`. It is a COST check, and
    the cost it controls is outbound requests made on behalf of anonymous callers.
    """
    candidate = token or ""
    if not candidate.strip():
        raise _Refused("empty_token")
    if len(candidate.encode("utf-8", "ignore")) > MAX_TOKEN_BYTES:
        raise _Refused("token_too_large")
    segments = candidate.split(".")
    if len(segments) != 3 or not all(segments):
        raise _Refused("malformed_token")
    try:
        header = _b64url_json(segments[0])
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise _Refused("malformed_token") from None
    if header.get("alg") != REQUIRED_TOKEN_ALG:
        raise _Refused("unsupported_alg")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid.strip():
        raise _Refused("no_kid")
    return header


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
    # SHAPE FIRST, NETWORK SECOND. Size, compact form, `alg` and `kid` are all settled before a
    # single byte leaves this process — see the block comment above `_BoundedCertsRequest`.
    header = unverified_token_header(candidate)

    # Cached certs, or at most one fetch. Never a fetch driven by an unparseable token.
    certs = _certs_cache.certs_for_kid(str(header["kid"]))

    from google.oauth2 import id_token as google_id_token

    try:
        claims = google_id_token.verify_oauth2_token(
            candidate,
            # A transport that replays the cached document. The library's own unconditional
            # fetch is thereby a dict lookup, and "0 outbound requests" is structural.
            _ReplayCertsRequest(GOOGLE_OAUTH2_CERTS_URL, certs),
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
        # ⚠️ NOT INLINE. `verify_gateway_identity` is SYNCHRONOUS and does blocking I/O (the certs
        # fetch). Called directly from this `async def` it runs ON THE EVENT LOOP, and every other
        # request this worker is serving stops for its duration — measured on the first cut, a
        # 0.6 s certs fetch let 0 of 12 50 ms heartbeats run. On a PUBLIC route that is a remotely
        # triggerable stall of all of `web`'s serving traffic, which is a far worse outcome than
        # any auth failure this function can have. The threadpool hop is the fix; the `wait_for`
        # is the ceiling, because a caller waiting longer than the gateway's own 2 s budget is
        # waiting for nothing.
        identity = await asyncio.wait_for(
            run_in_threadpool(verify_gateway_identity, credentials.credentials),
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _warn_rate_limited("verify_timeout")
        raise admin_error from None
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
