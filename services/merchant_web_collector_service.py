from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import jwt
from pydantic import ValidationError

from config.settings import settings
from services.merchant_event_ingest_service import MerchantEventBatch


WEB_COLLECTOR_ISSUER = "pivota-merchant-events"
WEB_COLLECTOR_AUDIENCE = "pivota-web-collector"
WEB_COLLECTOR_TOKEN_TYPE = "merchant_web_collector"
WEB_COLLECTOR_TOKEN_VERSION = 1

# A public browser write token may observe a funnel, but it must never be able
# to manufacture authoritative money movement. Payment settlement, orders,
# refunds, and returns remain signed server/webhook facts.
WEB_COLLECTOR_EVENT_TYPES = frozenset(
    {
        "agent.requested",
        "search.performed",
        "product.viewed",
        "cart.created",
        "cart.item_added",
        "cart.item_removed",
        "cart.updated",
        "checkout.started",
        "checkout.submitted",
        "payment.attempted",
    }
)

MAX_WEB_EVENT_AGE = timedelta(days=7)
MAX_WEB_EVENT_FUTURE_SKEW = timedelta(minutes=5)
MAX_ALLOWED_ORIGINS = 10
MAX_TOKEN_TTL_DAYS = 400
FORBIDDEN_WEB_EVENT_FIELDS = frozenset(
    {
        "buyer_id",
        "order_id",
        "refund_id",
        "return_id",
    }
)


@dataclass(frozen=True)
class WebCollectorError(Exception):
    status_code: int
    detail: str


def _collector_signing_key() -> bytes:
    """Derive a domain-separated key so collector tokens cannot be auth JWTs."""
    raw = str(
        os.getenv("MERCHANT_WEB_COLLECTOR_SIGNING_SECRET")
        or settings.jwt_secret_key
        or ""
    ).strip()
    if len(raw) < 32 or raw == "your-super-secret-key":
        raise WebCollectorError(503, "Web collector token signing is not configured")
    return hmac.new(
        raw.encode("utf-8"),
        b"pivota:merchant-web-collector:v1",
        hashlib.sha256,
    ).digest()


def normalize_collector_origin(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("origin is required")
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or parsed.username or parsed.password:
        raise ValueError("origin must contain only a valid host")
    local = host in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (scheme == "http" and local):
        raise ValueError("origin must use HTTPS (HTTP is allowed only for localhost)")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("origin must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin contains an invalid port") from exc
    default_port = 443 if scheme == "https" else 80
    port_suffix = f":{port}" if port and port != default_port else ""
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{rendered_host}{port_suffix}"


def collector_request_origin(*, origin: Optional[str], referer: Optional[str]) -> str:
    candidate = str(origin or "").strip()
    if not candidate and referer:
        parsed = urlparse(str(referer).strip())
        if parsed.scheme and parsed.netloc:
            candidate = f"{parsed.scheme}://{parsed.netloc}"
    if not candidate:
        raise WebCollectorError(403, "Web collector requests require an allowed Origin or Referer")
    try:
        return normalize_collector_origin(candidate)
    except ValueError as exc:
        raise WebCollectorError(403, "Web collector request origin is invalid") from exc


def default_origin_from_store_domain(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return normalize_collector_origin(raw)
    except ValueError:
        return None


def normalize_allowed_origins(values: Iterable[Any]) -> List[str]:
    normalized: List[str] = []
    for value in values:
        origin = normalize_collector_origin(value)
        if origin not in normalized:
            normalized.append(origin)
    if not normalized:
        raise ValueError("at least one allowed origin is required")
    if len(normalized) > MAX_ALLOWED_ORIGINS:
        raise ValueError(f"at most {MAX_ALLOWED_ORIGINS} allowed origins are supported")
    return normalized


def issue_web_collector_token(
    *,
    merchant_id: str,
    store_id: str,
    platform: str,
    allowed_origins: Iterable[Any],
    ttl_days: int = 90,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    issued_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ttl = max(1, min(int(ttl_days), MAX_TOKEN_TTL_DAYS))
    origins = normalize_allowed_origins(allowed_origins)
    expires_at = issued_at + timedelta(days=ttl)
    claims = {
        "iss": WEB_COLLECTOR_ISSUER,
        "aud": WEB_COLLECTOR_AUDIENCE,
        "typ": WEB_COLLECTOR_TOKEN_TYPE,
        "v": WEB_COLLECTOR_TOKEN_VERSION,
        "merchant_id": str(merchant_id).strip(),
        "store_id": str(store_id).strip(),
        "platform": str(platform).strip().lower(),
        "allowed_origins": origins,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if not claims["merchant_id"] or not claims["store_id"] or not claims["platform"]:
        raise ValueError("merchant_id, store_id, and platform are required")
    token = jwt.encode(claims, _collector_signing_key(), algorithm="HS256")
    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "allowed_origins": origins,
    }


def verify_web_collector_token(
    token: Any,
    *,
    request_origin: str,
) -> Dict[str, Any]:
    raw = str(token or "").strip()
    if not raw:
        raise WebCollectorError(401, "Missing web collector token")
    try:
        claims = jwt.decode(
            raw,
            _collector_signing_key(),
            algorithms=["HS256"],
            audience=WEB_COLLECTOR_AUDIENCE,
            issuer=WEB_COLLECTOR_ISSUER,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except WebCollectorError:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise WebCollectorError(401, "Web collector token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise WebCollectorError(401, "Invalid web collector token") from exc

    if (
        claims.get("typ") != WEB_COLLECTOR_TOKEN_TYPE
        or claims.get("v") != WEB_COLLECTOR_TOKEN_VERSION
    ):
        raise WebCollectorError(401, "Invalid web collector token")
    merchant_id = str(claims.get("merchant_id") or "").strip()
    store_id = str(claims.get("store_id") or "").strip()
    platform = str(claims.get("platform") or "").strip().lower()
    if not merchant_id or not store_id or not platform:
        raise WebCollectorError(401, "Invalid web collector token")
    try:
        allowed = normalize_allowed_origins(claims.get("allowed_origins") or [])
        normalized_request_origin = normalize_collector_origin(request_origin)
    except ValueError as exc:
        raise WebCollectorError(401, "Invalid web collector token") from exc
    if normalized_request_origin not in allowed:
        raise WebCollectorError(403, "Origin is not allowed for this web collector token")
    return {
        **claims,
        "merchant_id": merchant_id,
        "store_id": store_id,
        "platform": platform,
        "allowed_origins": allowed,
        "request_origin": normalized_request_origin,
    }


def build_web_collector_batch(
    payload: Dict[str, Any],
    *,
    claims: Dict[str, Any],
    now: Optional[datetime] = None,
) -> MerchantEventBatch:
    unknown_batch_keys = sorted(set(payload) - {"collector_token", "events"})
    if unknown_batch_keys:
        raise WebCollectorError(
            422,
            "Web collector body contains unsupported keys: " + ", ".join(unknown_batch_keys[:10]),
        )
    raw_events = payload.get("events")
    if not isinstance(raw_events, list) or not raw_events or len(raw_events) > 100:
        raise WebCollectorError(422, "Web collector batch must contain 1 to 100 events")

    platform = str(claims["platform"])
    store_id = str(claims["store_id"])
    normalized_events: List[Dict[str, Any]] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            raise WebCollectorError(422, "Each web collector event must be an object")
        event = dict(raw_event)
        event_type = str(event.get("event_type") or "").strip().lower()
        if event_type not in WEB_COLLECTOR_EVENT_TYPES:
            raise WebCollectorError(
                422,
                f"event_type is not allowed from a public web collector: {event_type or 'missing'}",
            )
        if event.get("platform") not in (None, "", platform):
            raise WebCollectorError(422, "Web collector event platform does not match its token")
        if event.get("store_id") not in (None, "", store_id):
            raise WebCollectorError(422, "Web collector event store_id does not match its token")
        forbidden_fields = sorted(
            field
            for field in FORBIDDEN_WEB_EVENT_FIELDS
            if event.get(field) not in (None, "")
        )
        if forbidden_fields:
            raise WebCollectorError(
                422,
                "Public web collector events cannot report: " + ", ".join(forbidden_fields),
            )
        if event.get("amount_cents") is not None or event.get("currency") not in (None, ""):
            raise WebCollectorError(422, "Public web collector events cannot report money amounts")
        event["event_type"] = event_type
        event["platform"] = platform
        event["store_id"] = store_id
        event["source"] = "universal_web_collector"
        event["amount_cents"] = None
        event["currency"] = None
        normalized_events.append(event)

    try:
        batch = MerchantEventBatch.model_validate({"events": normalized_events})
    except ValidationError as exc:
        raise WebCollectorError(422, "Invalid web collector event batch") from exc

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for event in batch.events:
        if event.occurred_at < current - MAX_WEB_EVENT_AGE:
            raise WebCollectorError(422, "Web collector event is older than seven days")
        if event.occurred_at > current + MAX_WEB_EVENT_FUTURE_SKEW:
            raise WebCollectorError(422, "Web collector event occurred_at is in the future")
    return batch
