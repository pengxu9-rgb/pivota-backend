"""Synthetic calls: our own eval traffic on the production MCP door, which must write nothing partner-facing.

The gateway decides who is synthetic (PIVOTA-Agent src/guardrails/syntheticCallers.js; runbook
docs/runbooks/synthetic_callers.md there) and signs that decision into one header on every backend hop:

    X-Pivota-Synthetic-Assertion: v1.<b64url(JSON payload)>.<b64url(HMAC-SHA256(secret, "v1." + part1))>

    payload = {"c": "<iss>|<client_id>", "r": <gateway request id>, "iat": <unix s>,
               "jti": <random>, "kfp": <first 32 hex of sha256(the X-API-Key this hop sends)>}

A request is synthetic only when ALL of these hold, and anything else is an ordinary call:
  1. the MAC verifies against SYNTHETIC_ASSERTION_SECRET or SYNTHETIC_ASSERTION_SECRET_PREVIOUS;
  2. `iat` is within MAX_SKEW_SECONDS of this service's clock;
  3. `kfp` is the fingerprint of the key THIS request authenticated with, so a captured header cannot
     ride another key or an anonymous request;
  4. that key is one of Pivota's own services (routes.agent_auth._is_service_caller): a partner's key
     can never mark its own traffic synthetic;
  5. the request is one the five read tools make (SYNTHETIC_READ_ROUTES, SYNTHETIC_READ_INVOKE_OPS). The
     assertion signs no operation, so the scope is enforced here: a valid header on a money or order path
     is refused (op_not_allowed) and that request writes as a real one;
  6. the caller is not an OAuth client registered to a partner agent (agent_oauth_clients). Listing one by
     mistake must not cost that partner its rows and attribution.

A synthetic request skips writes; it never gains anything. In particular it skips agent_usage_logs, which
quota reads: that is why the assertion is bound to the gateway's key, whose quota is not enforced anyway.

The verdict lives in a contextvar for the request, so every writer (and every task it creates) can ask
is_synthetic_call(). Set by middleware.synthetic_call.SyntheticCallMiddleware.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

SYNTHETIC_ASSERTION_HEADER = "X-Pivota-Synthetic-Assertion"
SYNTHETIC_ASSERTION_SECRET_ENV = "SYNTHETIC_ASSERTION_SECRET"
SYNTHETIC_ASSERTION_SECRET_PREVIOUS_ENV = "SYNTHETIC_ASSERTION_SECRET_PREVIOUS"
_VERSION = "v1"

#: The gateway signs a fresh `iat` per attempt, so a hop's header is seconds old when it arrives.
MAX_SKEW_SECONDS = 60

#: (method, path pattern) of every backend request the gateway's five read tools make (search_catalog,
#: get_product, get_offers, get_alternatives, get_intel), and no other. POST /agent/shop/v1/invoke is in
#: scope only for the operations below.
SYNTHETIC_READ_ROUTES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("POST", r"^/agent/shop/v1/invoke$"),
        ("POST", r"^/agent/v2/products/search$"),
        ("GET", r"^/agent/v1/products/search$"),
        ("POST", r"^/agent/internal/products/search$"),
        ("POST", r"^/agent/shop/v1/attribution/external-seed-links$"),
        ("GET", r"^/agent/v1/products/merchants/[^/]+/variant/[^/]+$"),
        ("GET", r"^/agent/v1/product-groups/resolve$"),
        ("GET", r"^/agent/v1/product-groups/resolve-by-product-id$"),
        ("GET", r"^/agent/v1/product-groups/[^/]+$"),
    )
)
INVOKE_PATH = "/agent/shop/v1/invoke"
SYNTHETIC_READ_INVOKE_OPS = frozenset({"find_products", "find_products_multi", "get_product_detail", "offers.resolve"})


def synthetic_scope_allows(method: str, path: str, operation: Optional[str] = None) -> bool:
    """Whether a verified synthetic assertion is honoured on this request: only on the read tools' own."""
    verb = str(method or "").upper()
    if not any(verb == m and pattern.match(str(path or "")) for m, pattern in SYNTHETIC_READ_ROUTES):
        return False
    if path == INVOKE_PATH:
        return isinstance(operation, str) and operation in SYNTHETIC_READ_INVOKE_OPS
    return True


_PARTNER_CLIENT_SQL = """
SELECT agent_id FROM agent_oauth_clients
WHERE issuer = :issuer AND client_id = :client_id AND disabled_at IS NULL
LIMIT 1
"""
_PARTNER_CLIENT_TTL_SECONDS = 300.0
_partner_client_cache: dict = {}


async def _is_partner_client(caller: str) -> bool:
    """True when `iss|client_id` is an OAuth client registered to an agent, or when that cannot be read
    (a partner must never lose its rows to a lookup failure). Cached per process for a few minutes."""
    now = time.time()
    hit = _partner_client_cache.get(caller)
    if hit is not None and now - hit[1] < _PARTNER_CLIENT_TTL_SECONDS:
        return hit[0]
    # The gateway writes "<iss>|<client_id>"; anything that does not split into exactly two is not ours.
    if caller.count("|") != 1:
        return True
    issuer, _, client_id = caller.partition("|")
    if not issuer or not client_id:
        return True
    try:
        from db.database import database

        row = await database.fetch_one(_PARTNER_CLIENT_SQL, {"issuer": issuer, "client_id": client_id})
    except Exception:  # noqa: BLE001 -- unreadable: not cached, treated as a partner
        return True
    partner = row is not None
    _partner_client_cache[caller] = (partner, now)
    return partner


_MAX_CALLER = 1100  # "<iss>|<client_id>", each at most 512 (agent_oauth_clients columns)
_MAX_REQUEST_ID = 128
_MAX_HEADER = 4096
_B64URL_PART = re.compile(r"^[A-Za-z0-9_-]+$")
_KFP = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class SyntheticCall:
    caller: str
    gw_request_id: str


_SYNTHETIC_CALL: "contextvars.ContextVar[Optional[SyntheticCall]]" = contextvars.ContextVar(
    "pivota_synthetic_call", default=None
)


def current_synthetic_call() -> Optional[SyntheticCall]:
    return _SYNTHETIC_CALL.get()


def is_synthetic_call() -> bool:
    """True inside a request whose synthetic assertion verified. Writers skip partner-facing rows on True."""
    return _SYNTHETIC_CALL.get() is not None


def set_synthetic_call(call: Optional[SyntheticCall]) -> contextvars.Token:
    return _SYNTHETIC_CALL.set(call)


def reset_synthetic_call(token: contextvars.Token) -> None:
    _SYNTHETIC_CALL.reset(token)


def key_fingerprint(api_key: Optional[str]) -> Optional[str]:
    """The gateway's keyFingerprint: first 32 hex chars of sha256 of the trimmed key."""
    key = str(api_key or "").strip()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] if key else None


def assertion_secrets() -> Tuple[str, ...]:
    out = []
    for name in (SYNTHETIC_ASSERTION_SECRET_ENV, SYNTHETIC_ASSERTION_SECRET_PREVIOUS_ENV):
        value = (os.getenv(name) or "").strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and len(text) <= limit else None


def sign_synthetic_assertion(payload: dict, secret: str) -> str:
    """The gateway's signer, mirrored for tests. Compact JSON in the key order given, as JSON.stringify."""
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signing_input = f"{_VERSION}.{body}"
    mac = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(mac)}"


def verify_synthetic_assertion(
    token: Optional[str],
    *,
    api_key: Optional[str],
    secrets: Optional[Tuple[str, ...]] = None,
    now: Optional[float] = None,
) -> Tuple[Optional[SyntheticCall], str]:
    """(call, "accepted") for a well-formed, correctly signed, fresh assertion bound to `api_key`;
    (None, <reason>) otherwise. Never raises. Does not check that the key is a service key: see
    resolve_synthetic_call."""
    try:
        keys = assertion_secrets() if secrets is None else tuple(s for s in secrets if s)
        if not keys:
            return None, "secret_unset"
        if not isinstance(token, str) or len(token) > _MAX_HEADER:
            return None, "malformed"
        parts = token.strip().split(".")
        if len(parts) != 3 or parts[0] != _VERSION:
            return None, "malformed"
        if not _B64URL_PART.match(parts[1]) or not _B64URL_PART.match(parts[2]):
            return None, "malformed"
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        mac = _b64url_decode(parts[2])
        if not any(
            hmac.compare_digest(hmac.new(k.encode("utf-8"), signing_input, hashlib.sha256).digest(), mac) for k in keys
        ):
            return None, "bad_signature"
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        if not isinstance(payload, dict):
            return None, "malformed"
        iat = payload.get("iat")
        if isinstance(iat, bool) or not isinstance(iat, int):
            return None, "malformed"
        clock = time.time() if now is None else now
        if abs(clock - iat) > MAX_SKEW_SECONDS:
            return None, "expired"
        caller = _text(payload.get("c"), _MAX_CALLER)
        request_id = _text(payload.get("r"), _MAX_REQUEST_ID)
        if not caller or not request_id:
            return None, "malformed"
        kfp = payload.get("kfp")
        own = key_fingerprint(api_key)
        if not isinstance(kfp, str) or not _KFP.match(kfp) or own is None or not hmac.compare_digest(kfp, own):
            return None, "key_mismatch"
        return SyntheticCall(caller=caller, gw_request_id=request_id), "accepted"
    except Exception:  # noqa: BLE001 -- a malformed header is an ordinary call, never a failed request
        return None, "malformed"


async def resolve_synthetic_call(
    token: Optional[str],
    *,
    api_key: Optional[str],
    method: str,
    path: str,
    operation: Optional[str] = None,
    now: Optional[float] = None,
) -> Tuple[Optional[SyntheticCall], str]:
    """verify_synthetic_assertion, then: the key is one of Pivota's own services, the request is in the
    read tools' scope, and the caller is not a partner's OAuth client. Never raises."""
    call, reason = verify_synthetic_assertion(token, api_key=api_key, now=now)
    if call is None:
        return None, reason
    if not synthetic_scope_allows(method, path, operation):
        return None, "op_not_allowed"
    try:
        from routes.agent_auth import _is_service_caller

        if not await _is_service_caller(api_key):
            return None, "not_service_caller"
    except Exception:  # noqa: BLE001 -- an unreadable caller is not synthetic
        return None, "not_service_caller"
    if await _is_partner_client(call.caller):
        return None, "partner_client"
    return call, reason
