from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

import httpx

from db.database import database
from db.startup_ddl import execute_ddl


logger = logging.getLogger(__name__)

MERCHANT_WEBHOOK_EVENT_CATALOG: List[Dict[str, Any]] = [
    {
        "event_type": "order.created",
        "category": "orders",
        "description": "A new order was created for the merchant.",
    },
    {
        "event_type": "payment.completed",
        "category": "payments",
        "description": "A payment completed successfully.",
    },
    {
        "event_type": "payment.failed",
        "category": "payments",
        "description": "A payment attempt failed.",
    },
    {
        "event_type": "refund.processed",
        "category": "refunds",
        "description": "A refund was processed.",
    },
]

DEFAULT_MERCHANT_WEBHOOK_EVENTS = [
    item["event_type"] for item in MERCHANT_WEBHOOK_EVENT_CATALOG
]
RETRY_DELAYS_SECONDS = (60, 300, 1800, 7200, 43200)
MAX_DELIVERY_ATTEMPTS = 6
RETRY_POLL_SECONDS = 30
DELIVERY_TIMEOUT_SECONDS = 10.0

_retry_worker_task: Optional[asyncio.Task] = None
_retry_worker_stop: Optional[asyncio.Event] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _db_datetime(value: Any) -> Optional[datetime]:
    coerced = _coerce_datetime(value)
    if not coerced:
        return None
    return coerced.astimezone(timezone.utc).replace(tzinfo=None)


def _db_now() -> datetime:
    now = _db_datetime(_utcnow())
    if now is None:
        raise RuntimeError("Failed to generate a database timestamp")
    return now


def _record_to_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return dict(value)
    except Exception:
        return {}


def _coerce_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return fallback


def _normalize_events(events: Optional[Iterable[str]]) -> List[str]:
    if not events:
        return list(DEFAULT_MERCHANT_WEBHOOK_EVENTS)
    allowed = set(DEFAULT_MERCHANT_WEBHOOK_EVENTS)
    normalized: List[str] = []
    for item in events:
        event_type = str(item or "").strip()
        if event_type and event_type in allowed and event_type not in normalized:
            normalized.append(event_type)
    return normalized or list(DEFAULT_MERCHANT_WEBHOOK_EVENTS)


def _generate_signing_secret() -> str:
    return f"whsec_{secrets.token_hex(24)}"


def _last4(value: Optional[str]) -> Optional[str]:
    token = str(value or "").strip()
    if not token:
        return None
    return token[-4:]


def _build_signature(secret: str, timestamp: str, raw_body: str) -> str:
    signed = f"{timestamp}.{raw_body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _validate_destination_url(destination_url: Optional[str]) -> Optional[str]:
    raw = str(destination_url or "").strip()
    if not raw:
        return None
    if not (raw.startswith("https://") or raw.startswith("http://")):
        raise ValueError("Webhook URL must start with http:// or https://")
    return raw


def _build_event_payload(
    *,
    event_id: str,
    event_type: str,
    payload: Dict[str, Any],
    created_at: datetime,
) -> Dict[str, Any]:
    return {
        "id": event_id,
        "type": event_type,
        "created_at": created_at.isoformat(),
        "data": payload,
    }


def _is_retryable_status_code(status_code: Optional[int]) -> bool:
    if status_code is None:
        return True
    return status_code in {408, 429} or status_code >= 500


def _next_retry_at(attempt_count: int) -> Optional[datetime]:
    if attempt_count >= MAX_DELIVERY_ATTEMPTS:
        return None
    retry_index = max(0, attempt_count - 1)
    if retry_index >= len(RETRY_DELAYS_SECONDS):
        return None
    return _utcnow() + timedelta(seconds=RETRY_DELAYS_SECONDS[retry_index])


_MERCHANT_WEBHOOK_DDL_READY = False

_MERCHANT_WEBHOOK_DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS merchant_webhook_configs (
        id SERIAL PRIMARY KEY,
        merchant_id VARCHAR(255) NOT NULL UNIQUE,
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        destination_url TEXT,
        subscribed_events JSON NOT NULL DEFAULT '[]',
        signing_secret TEXT,
        last_test_at TIMESTAMP,
        last_test_status VARCHAR(32),
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS merchant_webhook_deliveries (
        id SERIAL PRIMARY KEY,
        delivery_id VARCHAR(255) NOT NULL UNIQUE,
        merchant_id VARCHAR(255) NOT NULL,
        event_id VARCHAR(255) NOT NULL,
        event_type VARCHAR(255) NOT NULL,
        status VARCHAR(32) NOT NULL,
        http_status INTEGER,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        delivered_at TIMESTAMP,
        next_retry_at TIMESTAMP,
        request_id VARCHAR(255),
        destination_url TEXT,
        payload JSON NOT NULL DEFAULT '{}',
        request_headers JSON NOT NULL DEFAULT '{}',
        response_body TEXT,
        last_error TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_merchant_webhook_deliveries_merchant_created
    ON merchant_webhook_deliveries(merchant_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_merchant_webhook_deliveries_retry
    ON merchant_webhook_deliveries(status, next_retry_at)
    """,
)


async def ensure_merchant_webhook_tables() -> None:
    """
    Idempotent, memoized per process. See ensure_agent_webhook_tables() for
    why a lost CREATE ... IF NOT EXISTS race must be treated as success.
    """
    global _MERCHANT_WEBHOOK_DDL_READY
    if _MERCHANT_WEBHOOK_DDL_READY:
        return
    for stmt in _MERCHANT_WEBHOOK_DDL_STATEMENTS:
        await execute_ddl(stmt, db=database)
    _MERCHANT_WEBHOOK_DDL_READY = True


async def _get_or_create_raw_config(merchant_id: str) -> Dict[str, Any]:
    await ensure_merchant_webhook_tables()
    row = await database.fetch_one(
        """
        SELECT *
        FROM merchant_webhook_configs
        WHERE merchant_id = :merchant_id
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    if row:
        return _record_to_dict(row)
    return {
        "merchant_id": merchant_id,
        "enabled": False,
        "destination_url": None,
        "subscribed_events": list(DEFAULT_MERCHANT_WEBHOOK_EVENTS),
        "signing_secret": None,
        "last_test_at": None,
        "last_test_status": None,
        "created_at": None,
        "updated_at": None,
    }


async def _ensure_signing_secret(merchant_id: str) -> str:
    current = await _get_or_create_raw_config(merchant_id)
    secret = str(current.get("signing_secret") or "").strip()
    if secret:
        return secret

    secret = _generate_signing_secret()
    now = _db_now()
    await database.execute(
        """
        INSERT INTO merchant_webhook_configs (
            merchant_id,
            enabled,
            destination_url,
            subscribed_events,
            signing_secret,
            created_at,
            updated_at
        )
        VALUES (
            :merchant_id,
            :enabled,
            :destination_url,
            :subscribed_events,
            :signing_secret,
            :created_at,
            :updated_at
        )
        ON CONFLICT (merchant_id) DO UPDATE
        SET signing_secret = COALESCE(merchant_webhook_configs.signing_secret, EXCLUDED.signing_secret),
            updated_at = EXCLUDED.updated_at
        """,
        {
            "merchant_id": merchant_id,
            "enabled": bool(current.get("enabled")),
            "destination_url": current.get("destination_url"),
            "subscribed_events": json.dumps(
                _normalize_events(_coerce_json(current.get("subscribed_events"), []))
            ),
            "signing_secret": secret,
            "created_at": now,
            "updated_at": now,
        },
    )
    return secret


async def get_delivery_summary(merchant_id: str) -> Dict[str, Any]:
    await ensure_merchant_webhook_tables()
    row = await database.fetch_one(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'delivered') AS succeeded,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed,
            COUNT(*) FILTER (WHERE status = 'retrying') AS retrying,
            MAX(created_at) AS last_delivery_at
        FROM merchant_webhook_deliveries
        WHERE merchant_id = :merchant_id
          AND created_at >= (NOW() - INTERVAL '24 hours')
        """,
        {"merchant_id": merchant_id},
    )
    row_data = _record_to_dict(row)
    total = int(row_data.get("total") or 0)
    succeeded = int(row_data.get("succeeded") or 0)
    failed = int(row_data.get("failed") or 0)
    retrying = int(row_data.get("retrying") or 0)
    last_delivery_at = _coerce_datetime(row_data.get("last_delivery_at"))
    success_rate = round((succeeded / total) * 100, 2) if total > 0 else 0.0
    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "retrying": retrying,
        "success_rate": success_rate,
        "last_delivery_at": last_delivery_at.isoformat() if last_delivery_at else None,
    }


async def get_webhook_config(merchant_id: str) -> Dict[str, Any]:
    config = await _get_or_create_raw_config(merchant_id)
    summary = await get_delivery_summary(merchant_id)
    signing_secret = str(config.get("signing_secret") or "").strip() or None
    return {
        "enabled": bool(config.get("enabled") and config.get("destination_url")),
        "url": config.get("destination_url"),
        "events": _normalize_events(_coerce_json(config.get("subscribed_events"), [])),
        "signing_secret_last4": _last4(signing_secret),
        "last_test_at": (_coerce_datetime(config.get("last_test_at")) or None).isoformat()
        if _coerce_datetime(config.get("last_test_at"))
        else None,
        "last_test_status": config.get("last_test_status"),
        "delivery_summary_24h": summary,
    }


async def update_webhook_config(
    merchant_id: str,
    *,
    enabled: bool,
    destination_url: Optional[str],
    subscribed_events: Optional[Iterable[str]],
) -> Dict[str, Any]:
    current = await _get_or_create_raw_config(merchant_id)
    now = _db_now()
    next_url = _validate_destination_url(destination_url)
    next_enabled = bool(enabled and next_url)
    next_events = _normalize_events(subscribed_events)
    current_secret = str(current.get("signing_secret") or "").strip() or _generate_signing_secret()

    await database.execute(
        """
        INSERT INTO merchant_webhook_configs (
            merchant_id,
            enabled,
            destination_url,
            subscribed_events,
            signing_secret,
            created_at,
            updated_at
        )
        VALUES (
            :merchant_id,
            :enabled,
            :destination_url,
            :subscribed_events,
            :signing_secret,
            :created_at,
            :updated_at
        )
        ON CONFLICT (merchant_id) DO UPDATE
        SET enabled = EXCLUDED.enabled,
            destination_url = EXCLUDED.destination_url,
            subscribed_events = EXCLUDED.subscribed_events,
            signing_secret = COALESCE(merchant_webhook_configs.signing_secret, EXCLUDED.signing_secret),
            updated_at = EXCLUDED.updated_at
        """,
        {
            "merchant_id": merchant_id,
            "enabled": next_enabled,
            "destination_url": next_url,
            "subscribed_events": json.dumps(next_events),
            "signing_secret": current_secret,
            "created_at": now,
            "updated_at": now,
        },
    )
    return await get_webhook_config(merchant_id)


async def get_signing_secret(merchant_id: str) -> Dict[str, Any]:
    signing_secret = await _ensure_signing_secret(merchant_id)
    return {
        "status": "success",
        "signing_secret": signing_secret,
        "signing_secret_last4": _last4(signing_secret),
    }


def list_webhook_events_catalog() -> Dict[str, Any]:
    return {
        "status": "success",
        "events": list(MERCHANT_WEBHOOK_EVENT_CATALOG),
    }


async def rotate_signing_secret(merchant_id: str) -> Dict[str, Any]:
    await ensure_merchant_webhook_tables()
    previous = await _ensure_signing_secret(merchant_id)
    new_secret = _generate_signing_secret()
    now = _db_now()
    await database.execute(
        """
        INSERT INTO merchant_webhook_configs (
            merchant_id,
            enabled,
            destination_url,
            subscribed_events,
            signing_secret,
            created_at,
            updated_at
        )
        VALUES (
            :merchant_id,
            false,
            NULL,
            :subscribed_events,
            :signing_secret,
            :created_at,
            :updated_at
        )
        ON CONFLICT (merchant_id) DO UPDATE
        SET signing_secret = :signing_secret,
            updated_at = :updated_at
        """,
        {
            "merchant_id": merchant_id,
            "subscribed_events": json.dumps(DEFAULT_MERCHANT_WEBHOOK_EVENTS),
            "signing_secret": new_secret,
            "created_at": now,
            "updated_at": now,
        },
    )
    return {
        "status": "success",
        "new_signing_secret": new_secret,
        "previous_signing_secret_last4": _last4(previous),
        "signing_secret_last4": _last4(new_secret),
        "rotated_at": _utcnow().isoformat(),
    }


async def _persist_delivery_attempt(
    *,
    delivery_id: str,
    merchant_id: str,
    event_id: str,
    event_type: str,
    status: str,
    http_status: Optional[int],
    attempt_count: int,
    latency_ms: Optional[int],
    created_at: datetime,
    delivered_at: Optional[datetime],
    next_retry_at: Optional[datetime],
    request_id: Optional[str],
    destination_url: Optional[str],
    payload: Dict[str, Any],
    request_headers: Dict[str, Any],
    response_body: Optional[str],
    last_error: Optional[str],
) -> None:
    existing = await database.fetch_val(
        """
        SELECT COUNT(*) FROM merchant_webhook_deliveries WHERE delivery_id = :delivery_id
        """,
        {"delivery_id": delivery_id},
    )
    params = {
        "delivery_id": delivery_id,
        "merchant_id": merchant_id,
        "event_id": event_id,
        "event_type": event_type,
        "status": status,
        "http_status": http_status,
        "attempt_count": attempt_count,
        "latency_ms": latency_ms,
        "created_at": _db_datetime(created_at),
        "delivered_at": _db_datetime(delivered_at),
        "next_retry_at": _db_datetime(next_retry_at),
        "request_id": request_id,
        "destination_url": destination_url,
        "payload": json.dumps(payload),
        "request_headers": json.dumps(request_headers),
        "response_body": response_body,
        "last_error": last_error,
    }
    if existing:
        update_params = {
            key: params[key]
            for key in (
                "delivery_id",
                "status",
                "http_status",
                "attempt_count",
                "latency_ms",
                "delivered_at",
                "next_retry_at",
                "request_id",
                "destination_url",
                "payload",
                "request_headers",
                "response_body",
                "last_error",
            )
        }
        await database.execute(
            """
            UPDATE merchant_webhook_deliveries
            SET status = :status,
                http_status = :http_status,
                attempt_count = :attempt_count,
                latency_ms = :latency_ms,
                delivered_at = :delivered_at,
                next_retry_at = :next_retry_at,
                request_id = :request_id,
                destination_url = :destination_url,
                payload = :payload,
                request_headers = :request_headers,
                response_body = :response_body,
                last_error = :last_error
            WHERE delivery_id = :delivery_id
            """,
            update_params,
        )
    else:
        await database.execute(
            """
            INSERT INTO merchant_webhook_deliveries (
                delivery_id,
                merchant_id,
                event_id,
                event_type,
                status,
                http_status,
                attempt_count,
                latency_ms,
                created_at,
                delivered_at,
                next_retry_at,
                request_id,
                destination_url,
                payload,
                request_headers,
                response_body,
                last_error
            )
            VALUES (
                :delivery_id,
                :merchant_id,
                :event_id,
                :event_type,
                :status,
                :http_status,
                :attempt_count,
                :latency_ms,
                :created_at,
                :delivered_at,
                :next_retry_at,
                :request_id,
                :destination_url,
                :payload,
                :request_headers,
                :response_body,
                :last_error
            )
            """,
            params,
        )


async def _attempt_delivery(
    *,
    merchant_id: str,
    event_type: str,
    payload: Dict[str, Any],
    destination_url: str,
    signing_secret: str,
    request_id: Optional[str],
    delivery_id: Optional[str] = None,
    event_id: Optional[str] = None,
    prior_attempts: int = 0,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    delivery_id = delivery_id or f"mwh_{uuid.uuid4().hex[:24]}"
    event_id = event_id or f"evt_{uuid.uuid4().hex[:24]}"
    created_at = created_at or _utcnow()
    timestamp = str(int(created_at.timestamp()))
    event_payload = _build_event_payload(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
        created_at=created_at,
    )
    raw_body = json.dumps(event_payload, separators=(",", ":"), sort_keys=True)
    headers = {
        "Content-Type": "application/json",
        "X-Pivota-Event": event_type,
        "X-Pivota-Delivery": delivery_id,
        "X-Pivota-Timestamp": timestamp,
        "X-Pivota-Signature": _build_signature(signing_secret, timestamp, raw_body),
    }

    attempt_count = prior_attempts + 1
    http_status: Optional[int] = None
    latency_ms: Optional[int] = None
    response_body: Optional[str] = None
    delivered_at: Optional[datetime] = None
    last_error: Optional[str] = None
    status = "failed"
    next_retry_at: Optional[datetime] = None

    started = _utcnow()
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(destination_url, content=raw_body, headers=headers)
        latency_ms = int((_utcnow() - started).total_seconds() * 1000)
        http_status = response.status_code
        response_body = response.text[:4000]
        if 200 <= response.status_code < 300:
            status = "delivered"
            delivered_at = _utcnow()
        elif _is_retryable_status_code(response.status_code) and attempt_count < MAX_DELIVERY_ATTEMPTS:
            status = "retrying"
            next_retry_at = _next_retry_at(attempt_count)
            last_error = f"Webhook destination returned HTTP {response.status_code}"
        else:
            last_error = f"Webhook destination returned HTTP {response.status_code}"
    except Exception as exc:
        latency_ms = int((_utcnow() - started).total_seconds() * 1000)
        last_error = str(exc)
        if attempt_count < MAX_DELIVERY_ATTEMPTS:
            status = "retrying"
            next_retry_at = _next_retry_at(attempt_count)

    await _persist_delivery_attempt(
        delivery_id=delivery_id,
        merchant_id=merchant_id,
        event_id=event_id,
        event_type=event_type,
        status=status,
        http_status=http_status,
        attempt_count=attempt_count,
        latency_ms=latency_ms,
        created_at=created_at,
        delivered_at=delivered_at,
        next_retry_at=next_retry_at,
        request_id=request_id,
        destination_url=destination_url,
        payload=event_payload,
        request_headers=headers,
        response_body=response_body,
        last_error=last_error,
    )
    return {
        "delivery_id": delivery_id,
        "event_id": event_id,
        "event_type": event_type,
        "status": status,
        "http_status": http_status,
        "attempt_count": attempt_count,
        "latency_ms": latency_ms,
        "created_at": created_at.isoformat(),
        "delivered_at": delivered_at.isoformat() if delivered_at else None,
        "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
        "request_id": request_id,
        "last_error": last_error,
    }


async def emit_merchant_webhook_event(
    merchant_id: str,
    *,
    event_type: str,
    payload: Dict[str, Any],
    request_id: Optional[str] = None,
    force_delivery: bool = False,
) -> Dict[str, Any]:
    if event_type not in DEFAULT_MERCHANT_WEBHOOK_EVENTS:
        raise ValueError(f"Unsupported merchant webhook event: {event_type}")

    config = await _get_or_create_raw_config(merchant_id)
    destination_url = str(config.get("destination_url") or "").strip()
    enabled = bool(config.get("enabled") and destination_url)
    subscribed_events = _normalize_events(_coerce_json(config.get("subscribed_events"), []))

    if not enabled:
        return {
            "status": "skipped",
            "reason": "webhook_not_configured",
            "event_type": event_type,
        }

    if not force_delivery and event_type not in subscribed_events:
        return {
            "status": "skipped",
            "reason": "event_not_subscribed",
            "event_type": event_type,
        }

    signing_secret = await _ensure_signing_secret(merchant_id)
    return await _attempt_delivery(
        merchant_id=merchant_id,
        event_type=event_type,
        payload=payload,
        destination_url=destination_url,
        signing_secret=signing_secret,
        request_id=request_id,
    )


async def send_test_webhook(
    merchant_id: str,
    *,
    event_type: str = "order.created",
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    if event_type not in DEFAULT_MERCHANT_WEBHOOK_EVENTS:
        raise ValueError("Unsupported webhook test event.")

    config = await _get_or_create_raw_config(merchant_id)
    destination_url = str(config.get("destination_url") or "").strip()
    if not destination_url or not bool(config.get("enabled")):
        raise ValueError("Configure and enable a webhook destination before sending a test event.")

    result = await emit_merchant_webhook_event(
        merchant_id,
        event_type=event_type,
        payload={
            "merchant_id": merchant_id,
            "test": True,
            "triggered_by": "merchant_portal",
            "message": "Pivota merchant webhook test delivery",
        },
        request_id=request_id,
        force_delivery=True,
    )
    await database.execute(
        """
        UPDATE merchant_webhook_configs
        SET last_test_at = :last_test_at,
            last_test_status = :last_test_status,
            updated_at = :updated_at
        WHERE merchant_id = :merchant_id
        """,
        {
            "merchant_id": merchant_id,
            "last_test_at": _db_now(),
            "last_test_status": result.get("status"),
            "updated_at": _db_now(),
        },
    )
    return result


async def list_deliveries(
    merchant_id: str,
    *,
    limit: int = 25,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_merchant_webhook_tables()
    await process_due_retries(limit=10)

    clauses = ["merchant_id = :merchant_id"]
    params: Dict[str, Any] = {"merchant_id": merchant_id, "limit": limit}
    if status:
        clauses.append("status = :status")
        params["status"] = status
    rows = await database.fetch_all(
        f"""
        SELECT *
        FROM merchant_webhook_deliveries
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        params,
    )
    deliveries = []
    for row in rows:
        payload = _coerce_json(row["payload"], {})
        headers = _coerce_json(row["request_headers"], {})
        created_at = _coerce_datetime(row["created_at"])
        delivered_at = _coerce_datetime(row["delivered_at"])
        next_retry_at = _coerce_datetime(row["next_retry_at"])
        deliveries.append(
            {
                "delivery_id": row["delivery_id"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "status": row["status"],
                "http_status": row["http_status"],
                "attempt_count": row["attempt_count"],
                "latency_ms": row["latency_ms"],
                "created_at": created_at.isoformat() if created_at else None,
                "delivered_at": delivered_at.isoformat() if delivered_at else None,
                "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                "request_id": row["request_id"],
                "last_error": row["last_error"],
                "payload": payload,
                "request_headers": headers,
            }
        )
    return {
        "status": "success",
        "deliveries": deliveries,
        "summary_24h": await get_delivery_summary(merchant_id),
    }


async def retry_delivery(merchant_id: str, delivery_id: str) -> Dict[str, Any]:
    await ensure_merchant_webhook_tables()
    row = await database.fetch_one(
        """
        SELECT *
        FROM merchant_webhook_deliveries
        WHERE merchant_id = :merchant_id
          AND delivery_id = :delivery_id
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "delivery_id": delivery_id},
    )
    if not row:
        raise ValueError("Webhook delivery not found.")

    config = await _get_or_create_raw_config(merchant_id)
    destination_url = str(row["destination_url"] or config.get("destination_url") or "").strip()
    signing_secret = await _ensure_signing_secret(merchant_id)
    if not destination_url or not signing_secret:
        raise ValueError("Webhook destination is not configured.")

    payload = _coerce_json(row["payload"], {})
    created_at = _coerce_datetime(row["created_at"]) or _utcnow()
    request_id = row["request_id"]
    return await _attempt_delivery(
        merchant_id=merchant_id,
        event_type=str(row["event_type"]),
        payload=payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload,
        destination_url=destination_url,
        signing_secret=signing_secret,
        request_id=request_id,
        delivery_id=str(row["delivery_id"]),
        event_id=str(row["event_id"]),
        prior_attempts=int(row["attempt_count"] or 0),
        created_at=created_at,
    )


async def process_due_retries(
    limit: int = 20,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Deliver every retry that is due, oldest first.

    `should_stop` IS CHECKED BETWEEN DELIVERIES, and that is what makes shutdown bounded.
    Without it the only stop check was at the top of `_retry_worker_loop`, so a shutdown
    arriving mid-batch still had to sit through the rest of it: up to `limit` sequential
    deliveries at DELIVERY_TIMEOUT_SECONDS each, which at the default is ~200s. That was
    survivable only because `database.disconnect()` used to run first and blow the remaining
    rows up; once the lifespan was reordered so the scheduler could drain against a live pool,
    the accidental bound was gone and the real one had to be written down. Its stop is now also
    wrapped in `asyncio.wait_for`, but cancelling a delivery mid-flight is the fallback -
    stopping cleanly between them is the intent.

    Nothing is lost by stopping early: an undelivered retry stays `retrying` with its
    `next_retry_at` unchanged, so the next instance picks it up on its next poll.
    """
    await ensure_merchant_webhook_tables()
    rows = await database.fetch_all(
        """
        SELECT *
        FROM merchant_webhook_deliveries
        WHERE status = 'retrying'
          AND next_retry_at IS NOT NULL
          AND next_retry_at <= :now
        ORDER BY next_retry_at ASC
        LIMIT :limit
        """,
        {"now": _db_now(), "limit": limit},
    )
    processed = 0
    for row in rows:
        if should_stop is not None and should_stop():
            # `info`, not `warning`: with the stop check in place this is the ORDINARY
            # shutdown path and the worker restarts 15-34 times a day. A warning per restart
            # is the kind of noise that teaches people to skim the log.
            logger.info(
                "%s webhook retry worker stopping: %d delivered, %d left due for the next "
                "instance.", "merchant", processed, len(rows) - processed,
            )
            break
        try:
            await retry_delivery(str(row["merchant_id"]), str(row["delivery_id"]))
            processed += 1
        except Exception as exc:
            logger.warning("Failed to process merchant webhook retry %s: %s", row["delivery_id"], exc)
    return processed


async def _retry_worker_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            if getattr(database, "is_connected", False):
                await process_due_retries(limit=20, should_stop=stop_event.is_set)
        except Exception as exc:
            logger.warning("Merchant webhook retry worker iteration failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RETRY_POLL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def start_merchant_webhook_retry_worker() -> None:
    global _retry_worker_task, _retry_worker_stop
    if _retry_worker_task and not _retry_worker_task.done():
        return
    _retry_worker_stop = asyncio.Event()
    # Fresh context => own `databases` Connection (issue #1754).
    from services.scheduler_job_runner import spawn_isolated
    _retry_worker_task = spawn_isolated(_retry_worker_loop(_retry_worker_stop), name="merchant_webhook_retry_worker")


# How long a shutdown may wait for the retry worker to finish its current iteration.
#
# SMALL, AND IT HAS TO BE BOUNDED AT ALL. `stop_event` is only checked at the TOP of
# `_retry_worker_loop`, so setting it does not interrupt an iteration in progress — and an
# iteration is `process_due_retries(limit=20)`, up to twenty sequential deliveries each with a
# 10.0s HTTP timeout. A bare `await` on that task is therefore an await of up to ~200s.
#
# That used to be masked by accident: `database.disconnect()` ran BEFORE this, so the next
# `database.*` call in the loop raised "DatabaseBackend is not running" and the remaining rows
# failed in microseconds. Reordering the lifespan so the scheduler drains while the pool is
# still open (which it must, or drained jobs cannot write) removed that accidental bound and
# left a ~200s await in front of the disconnect, on `web` as well as `worker`. Found in review.
#
# There is nothing to lose by cutting a retry short: it is a RETRY, it stays due, and the next
# instance picks it up. `asyncio.wait_for` cancels the task on timeout, so this is a real bound
# and not just a log line.
_STOP_TIMEOUT_SECONDS = 1.0


async def stop_merchant_webhook_retry_worker() -> None:
    global _retry_worker_task, _retry_worker_stop
    if _retry_worker_stop is not None:
        _retry_worker_stop.set()
    task = _retry_worker_task
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=_STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # wait_for has already cancelled it; say so rather than letting a shutdown that
            # cut work short look like a clean one.
            logger.warning(
                "%s: retry worker did not stop within %.1fs and was cancelled mid-iteration; "
                "any unfinished retries stay due and the next instance will pick them up.",
                __name__, _STOP_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
    _retry_worker_task = None
    _retry_worker_stop = None
