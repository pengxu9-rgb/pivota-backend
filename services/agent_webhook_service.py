from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import httpx

from config.settings import resolve_public_api_base_url
from db.database import database


logger = logging.getLogger(__name__)

WEBHOOK_EVENT_CATALOG: List[Dict[str, Any]] = [
    {
        "event_type": "order.created",
        "category": "orders",
        "description": "A new order was created by the agent API.",
    },
    {
        "event_type": "order.payment_attempted",
        "category": "orders",
        "description": "A shopper reached the payment-attempt stage for an order.",
    },
    {
        "event_type": "order.payment_succeeded",
        "category": "orders",
        "description": "An order payment succeeded.",
    },
    {
        "event_type": "order.payment_failed",
        "category": "orders",
        "description": "An order payment failed.",
    },
    {
        "event_type": "order.completed",
        "category": "orders",
        "description": "An order completed successfully.",
    },
    {
        "event_type": "order.refunded",
        "category": "orders",
        "description": "An order refund was issued.",
    },
    {
        "event_type": "order.cancelled",
        "category": "orders",
        "description": "An order was cancelled.",
    },
    {
        "event_type": "api.auth_failed",
        "category": "api",
        "description": "An authenticated agent request failed authorization.",
    },
    {
        "event_type": "api.rate_limited",
        "category": "api",
        "description": "A request was rate limited or quota blocked.",
    },
    {
        "event_type": "api.request_failed",
        "category": "api",
        "description": "A request returned an application or validation error.",
    },
]

DEFAULT_WEBHOOK_EVENTS = [item["event_type"] for item in WEBHOOK_EVENT_CATALOG]
TEST_EVENT_TYPE = "webhook.test"
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
        return list(DEFAULT_WEBHOOK_EVENTS)
    normalized: List[str] = []
    allowed = set(DEFAULT_WEBHOOK_EVENTS)
    for item in events:
        event_type = str(item or "").strip()
        if event_type and event_type in allowed and event_type not in normalized:
            normalized.append(event_type)
    return normalized or list(DEFAULT_WEBHOOK_EVENTS)


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


def _resolve_managed_receiver_base_url() -> str:
    value = resolve_public_api_base_url()
    parsed = urlparse(value)
    host = (parsed.hostname or "").strip().lower()
    if host in {"127.0.0.1", "0.0.0.0", "localhost"}:
        return "https://api.pivota.cc"
    return value


def get_managed_receiver_url(agent_id: str) -> str:
    base = _resolve_managed_receiver_base_url()
    return f"{base}/agents/{agent_id}/webhooks/managed-inbox"


def _normalize_destination_url(agent_id: str, destination_url: Optional[str]) -> Optional[str]:
    raw = str(destination_url or "").strip()
    if not raw:
        return None

    managed_url = get_managed_receiver_url(agent_id)
    try:
        parsed = urlparse(raw)
        managed = urlparse(managed_url)
    except Exception:
        return raw

    if parsed.path == managed.path and parsed.path.endswith("/webhooks/managed-inbox"):
        return managed_url

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
    retry_index = attempt_count - 1
    if retry_index < 0:
        retry_index = 0
    if retry_index >= len(RETRY_DELAYS_SECONDS):
        return None
    return _utcnow() + timedelta(seconds=RETRY_DELAYS_SECONDS[retry_index])


async def ensure_agent_webhook_tables() -> None:
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_webhook_configs (
            id SERIAL PRIMARY KEY,
            agent_id VARCHAR(255) NOT NULL UNIQUE,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            destination_url TEXT,
            subscribed_events JSON NOT NULL DEFAULT '[]',
            signing_secret TEXT,
            pending_signing_secret TEXT,
            pending_secret_activates_at TIMESTAMP,
            last_test_at TIMESTAMP,
            last_test_status VARCHAR(32),
            migration_source VARCHAR(128),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_webhook_deliveries (
            id SERIAL PRIMARY KEY,
            delivery_id VARCHAR(255) NOT NULL UNIQUE,
            agent_id VARCHAR(255) NOT NULL,
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
        """
    )
    await database.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_webhook_deliveries_agent_created
        ON agent_webhook_deliveries(agent_id, created_at DESC)
        """
    )
    await database.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_webhook_deliveries_retry
        ON agent_webhook_deliveries(status, next_retry_at)
        """
    )
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_webhook_managed_inbox_events (
            id SERIAL PRIMARY KEY,
            delivery_id VARCHAR(255) NOT NULL UNIQUE,
            agent_id VARCHAR(255) NOT NULL,
            event_id VARCHAR(255),
            event_type VARCHAR(255),
            signature_valid BOOLEAN NOT NULL DEFAULT FALSE,
            received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            source_ip VARCHAR(255),
            user_agent TEXT,
            request_headers JSON NOT NULL DEFAULT '{}',
            payload JSON NOT NULL DEFAULT '{}',
            raw_body TEXT
        )
        """
    )
    await database.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_webhook_managed_inbox_agent_received
        ON agent_webhook_managed_inbox_events(agent_id, received_at DESC)
        """
    )


async def _sync_legacy_agent_webhook_url(agent_id: str, destination_url: Optional[str]) -> None:
    try:
        await database.execute(
            """
            UPDATE agents
            SET webhook_url = :destination_url
            WHERE agent_id = :agent_id
            """,
            {
                "agent_id": agent_id,
                "destination_url": destination_url.strip() if isinstance(destination_url, str) and destination_url.strip() else None,
            },
        )
    except Exception as exc:
        logger.warning("Failed to sync legacy agents.webhook_url for %s: %s", agent_id, exc)


async def _activate_pending_secret_if_due(agent_id: str) -> None:
    config = await database.fetch_one(
        """
        SELECT pending_signing_secret, pending_secret_activates_at
        FROM agent_webhook_configs
        WHERE agent_id = :agent_id
        """,
        {"agent_id": agent_id},
    )
    if not config:
        return
    activates_at = _coerce_datetime(config["pending_secret_activates_at"])
    pending_secret = str(config["pending_signing_secret"] or "").strip()
    if not pending_secret or not activates_at or activates_at > _utcnow():
        return
    await database.execute(
        """
        UPDATE agent_webhook_configs
        SET signing_secret = :signing_secret,
            pending_signing_secret = NULL,
            pending_secret_activates_at = NULL,
            updated_at = :updated_at
        WHERE agent_id = :agent_id
        """,
        {
            "agent_id": agent_id,
            "signing_secret": pending_secret,
            "updated_at": _db_now(),
        },
    )


async def _get_or_create_raw_config(agent_id: str) -> Dict[str, Any]:
    await ensure_agent_webhook_tables()
    await _activate_pending_secret_if_due(agent_id)

    row = await database.fetch_one(
        """
        SELECT *
        FROM agent_webhook_configs
        WHERE agent_id = :agent_id
        """,
        {"agent_id": agent_id},
    )
    if row:
        return dict(row)

    legacy = await database.fetch_one(
        """
        SELECT webhook_url
        FROM agents
        WHERE agent_id = :agent_id
        LIMIT 1
        """,
        {"agent_id": agent_id},
    )
    legacy_url = str(_record_to_dict(legacy).get("webhook_url") or "").strip()
    if legacy_url:
        secret = _generate_signing_secret()
        now = _db_now()
        await database.execute(
            """
            INSERT INTO agent_webhook_configs (
                agent_id,
                enabled,
                destination_url,
                subscribed_events,
                signing_secret,
                migration_source,
                created_at,
                updated_at
            )
            VALUES (
                :agent_id,
                :enabled,
                :destination_url,
                :subscribed_events,
                :signing_secret,
                :migration_source,
                :created_at,
                :updated_at
            )
            """,
            {
                "agent_id": agent_id,
                "enabled": True,
                "destination_url": legacy_url,
                "subscribed_events": json.dumps(DEFAULT_WEBHOOK_EVENTS),
                "signing_secret": secret,
                "migration_source": "agents.webhook_url",
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await database.fetch_one(
            """
            SELECT *
            FROM agent_webhook_configs
            WHERE agent_id = :agent_id
            """,
            {"agent_id": agent_id},
        )
        if row:
            return dict(row)

    return {
        "agent_id": agent_id,
        "enabled": False,
        "destination_url": None,
        "subscribed_events": list(DEFAULT_WEBHOOK_EVENTS),
        "signing_secret": None,
        "pending_signing_secret": None,
        "pending_secret_activates_at": None,
        "last_test_at": None,
        "last_test_status": None,
        "migration_source": None,
        "created_at": None,
        "updated_at": None,
    }


async def get_delivery_summary(agent_id: str) -> Dict[str, Any]:
    await ensure_agent_webhook_tables()
    since = _db_datetime(_utcnow() - timedelta(hours=24))
    row = await database.fetch_one(
        """
        SELECT
            COUNT(*)::int AS total,
            SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END)::int AS succeeded,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)::int AS failed,
            SUM(CASE WHEN status = 'retrying' THEN 1 ELSE 0 END)::int AS retrying,
            MAX(created_at) AS last_delivery_at
        FROM agent_webhook_deliveries
        WHERE agent_id = :agent_id
          AND created_at >= :since
        """,
        {"agent_id": agent_id, "since": since},
    )
    row_data = _record_to_dict(row)
    total = int(row_data.get("total") or 0)
    succeeded = int(row_data.get("succeeded") or 0)
    failed = int(row_data.get("failed") or 0)
    retrying = int(row_data.get("retrying") or 0)
    success_rate = round((succeeded / total) * 100, 2) if total > 0 else 0.0
    last_delivery_at = _coerce_datetime(row_data.get("last_delivery_at"))
    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "retrying": retrying,
        "success_rate": success_rate,
        "last_delivery_at": last_delivery_at.isoformat() if last_delivery_at else None,
    }


async def get_webhook_config(agent_id: str) -> Dict[str, Any]:
    config = await _get_or_create_raw_config(agent_id)
    summary = await get_delivery_summary(agent_id)
    managed_receiver_url = get_managed_receiver_url(agent_id)
    destination_url = _normalize_destination_url(agent_id, config.get("destination_url"))
    current_secret = str(config.get("signing_secret") or "").strip() or None
    pending_secret = str(config.get("pending_signing_secret") or "").strip() or None
    pending_activates_at = _coerce_datetime(config.get("pending_secret_activates_at"))
    return {
        "enabled": bool(config.get("enabled") and destination_url),
        "destination_url": destination_url,
        "managed_receiver_url": managed_receiver_url,
        "subscribed_events": _normalize_events(_coerce_json(config.get("subscribed_events"), [])),
        "signing_secret_last4": _last4(current_secret),
        "pending_signing_secret_last4": _last4(pending_secret),
        "pending_secret_activates_at": pending_activates_at.isoformat() if pending_activates_at else None,
        "last_test_at": (_coerce_datetime(config.get("last_test_at")) or None).isoformat() if _coerce_datetime(config.get("last_test_at")) else None,
        "last_test_status": config.get("last_test_status"),
        "delivery_summary_24h": summary,
        "migration_source": config.get("migration_source"),
    }


async def update_webhook_config(
    agent_id: str,
    *,
    enabled: bool,
    destination_url: Optional[str],
    subscribed_events: Optional[Iterable[str]],
) -> Dict[str, Any]:
    current = await _get_or_create_raw_config(agent_id)
    now = _db_now()
    next_url = _normalize_destination_url(agent_id, destination_url)
    next_enabled = bool(enabled and next_url)
    next_events = _normalize_events(subscribed_events)
    current_secret = str(current.get("signing_secret") or "").strip() or _generate_signing_secret()

    if current.get("created_at") is None:
        await database.execute(
            """
            INSERT INTO agent_webhook_configs (
                agent_id,
                enabled,
                destination_url,
                subscribed_events,
                signing_secret,
                migration_source,
                created_at,
                updated_at
            )
            VALUES (
                :agent_id,
                :enabled,
                :destination_url,
                :subscribed_events,
                :signing_secret,
                :migration_source,
                :created_at,
                :updated_at
            )
            ON CONFLICT (agent_id) DO UPDATE
            SET enabled = EXCLUDED.enabled,
                destination_url = EXCLUDED.destination_url,
                subscribed_events = EXCLUDED.subscribed_events,
                signing_secret = COALESCE(agent_webhook_configs.signing_secret, EXCLUDED.signing_secret),
                updated_at = EXCLUDED.updated_at
            """,
            {
                "agent_id": agent_id,
                "enabled": next_enabled,
                "destination_url": next_url,
                "subscribed_events": json.dumps(next_events),
                "signing_secret": current_secret,
                "migration_source": current.get("migration_source"),
                "created_at": now,
                "updated_at": now,
            },
        )
    else:
        await database.execute(
            """
            UPDATE agent_webhook_configs
            SET enabled = :enabled,
                destination_url = :destination_url,
                subscribed_events = :subscribed_events,
                signing_secret = COALESCE(signing_secret, :signing_secret),
                updated_at = :updated_at
            WHERE agent_id = :agent_id
            """,
            {
                "agent_id": agent_id,
                "enabled": next_enabled,
                "destination_url": next_url,
                "subscribed_events": json.dumps(next_events),
                "signing_secret": current_secret,
                "updated_at": now,
            },
        )

    await _sync_legacy_agent_webhook_url(agent_id, next_url)
    return await get_webhook_config(agent_id)


def list_webhook_events_catalog() -> Dict[str, Any]:
    return {
        "status": "success",
        "events": [
            *WEBHOOK_EVENT_CATALOG,
            {
                "event_type": TEST_EVENT_TYPE,
                "category": "testing",
                "description": "Manual test delivery issued from the developer portal.",
            },
        ],
    }


def _headers_to_dict(headers: Any) -> Dict[str, str]:
    lowered: Dict[str, str] = {}
    if not headers:
        return lowered
    for key, value in headers.items():
        lowered[str(key).lower()] = str(value)
    return lowered


def _parse_json_body(raw_body: str) -> Dict[str, Any]:
    if not raw_body.strip():
        return {}
    try:
        parsed = json.loads(raw_body)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except Exception:
        return {}


def _extract_signing_secrets(config: Dict[str, Any]) -> List[str]:
    secrets_to_try: List[str] = []
    current_secret = str(config.get("signing_secret") or "").strip()
    pending_secret = str(config.get("pending_signing_secret") or "").strip()
    if current_secret:
        secrets_to_try.append(current_secret)
    if pending_secret and pending_secret not in secrets_to_try:
        secrets_to_try.append(pending_secret)
    return secrets_to_try


async def receive_managed_inbox_delivery(
    agent_id: str,
    *,
    raw_body: str,
    headers: Any,
    source_ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_agent_webhook_tables()
    config = await _get_or_create_raw_config(agent_id)

    header_map = _headers_to_dict(headers)
    delivery_id = header_map.get("x-pivota-delivery") or f"managed_{uuid.uuid4().hex[:24]}"
    event_type = header_map.get("x-pivota-event") or "unknown"
    timestamp = header_map.get("x-pivota-timestamp")
    signature = header_map.get("x-pivota-signature")
    payload = _parse_json_body(raw_body)
    event_id = str(payload.get("id") or "").strip() or None

    if not timestamp or not signature:
        raise ValueError("Missing webhook signature headers.")

    valid_signature = False
    for signing_secret in _extract_signing_secrets(config):
        expected = _build_signature(signing_secret, timestamp, raw_body)
        if hmac.compare_digest(expected, signature):
            valid_signature = True
            break

    await database.execute(
        """
        INSERT INTO agent_webhook_managed_inbox_events (
            delivery_id,
            agent_id,
            event_id,
            event_type,
            signature_valid,
            received_at,
            source_ip,
            user_agent,
            request_headers,
            payload,
            raw_body
        )
        VALUES (
            :delivery_id,
            :agent_id,
            :event_id,
            :event_type,
            :signature_valid,
            :received_at,
            :source_ip,
            :user_agent,
            :request_headers,
            :payload,
            :raw_body
        )
        ON CONFLICT (delivery_id) DO UPDATE
        SET signature_valid = EXCLUDED.signature_valid,
            received_at = EXCLUDED.received_at,
            source_ip = EXCLUDED.source_ip,
            user_agent = EXCLUDED.user_agent,
            request_headers = EXCLUDED.request_headers,
            payload = EXCLUDED.payload,
            raw_body = EXCLUDED.raw_body
        """,
        {
            "delivery_id": delivery_id,
            "agent_id": agent_id,
            "event_id": event_id,
            "event_type": event_type,
            "signature_valid": valid_signature,
            "received_at": _db_now(),
            "source_ip": source_ip,
            "user_agent": user_agent,
            "request_headers": json.dumps(header_map),
            "payload": json.dumps(payload),
            "raw_body": raw_body,
        },
    )

    if not valid_signature:
        raise PermissionError("Invalid webhook signature.")

    return {
        "delivery_id": delivery_id,
        "event_id": event_id,
        "event_type": event_type,
        "signature_valid": True,
        "managed_receiver_url": get_managed_receiver_url(agent_id),
    }


async def _persist_delivery_attempt(
    *,
    delivery_id: str,
    agent_id: str,
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
        SELECT COUNT(*) FROM agent_webhook_deliveries WHERE delivery_id = :delivery_id
        """,
        {"delivery_id": delivery_id},
    )
    params = {
        "delivery_id": delivery_id,
        "agent_id": agent_id,
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
            UPDATE agent_webhook_deliveries
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
            INSERT INTO agent_webhook_deliveries (
                delivery_id,
                agent_id,
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
                :agent_id,
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
    agent_id: str,
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
    delivery_id = delivery_id or f"whd_{uuid.uuid4().hex[:24]}"
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
            status = "failed"
            last_error = f"Webhook destination returned HTTP {response.status_code}"
    except Exception as exc:
        latency_ms = int((_utcnow() - started).total_seconds() * 1000)
        last_error = str(exc)
        if attempt_count < MAX_DELIVERY_ATTEMPTS:
            status = "retrying"
            next_retry_at = _next_retry_at(attempt_count)
        else:
            status = "failed"

    await _persist_delivery_attempt(
        delivery_id=delivery_id,
        agent_id=agent_id,
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


async def emit_agent_webhook_event(
    agent_id: str,
    *,
    event_type: str,
    payload: Dict[str, Any],
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    config = await _get_or_create_raw_config(agent_id)
    destination_url = str(config.get("destination_url") or "").strip()
    enabled = bool(config.get("enabled") and destination_url)
    subscribed_events = _normalize_events(_coerce_json(config.get("subscribed_events"), []))
    signing_secret = str(config.get("signing_secret") or "").strip()
    if not signing_secret:
        signing_secret = _generate_signing_secret()
        await database.execute(
            """
            INSERT INTO agent_webhook_configs (agent_id, signing_secret, subscribed_events, created_at, updated_at)
            VALUES (:agent_id, :signing_secret, :subscribed_events, :created_at, :updated_at)
            ON CONFLICT (agent_id) DO UPDATE
            SET signing_secret = COALESCE(agent_webhook_configs.signing_secret, EXCLUDED.signing_secret),
                updated_at = EXCLUDED.updated_at
            """,
            {
                "agent_id": agent_id,
                "signing_secret": signing_secret,
                "subscribed_events": json.dumps(subscribed_events),
                "created_at": _db_now(),
                "updated_at": _db_now(),
            },
        )

    if event_type != TEST_EVENT_TYPE and event_type not in subscribed_events:
        return {
            "status": "skipped",
            "reason": "event_not_subscribed",
            "event_type": event_type,
        }

    if not enabled:
        return {
            "status": "skipped",
            "reason": "webhook_not_configured",
            "event_type": event_type,
        }

    return await _attempt_delivery(
        agent_id=agent_id,
        event_type=event_type,
        payload=payload,
        destination_url=destination_url,
        signing_secret=signing_secret,
        request_id=request_id,
    )


async def send_test_webhook(agent_id: str, *, request_id: Optional[str] = None) -> Dict[str, Any]:
    config = await _get_or_create_raw_config(agent_id)
    destination_url = str(config.get("destination_url") or "").strip()
    if not destination_url or not bool(config.get("enabled")):
        raise ValueError("Configure and enable a webhook destination before sending a test event.")

    result = await emit_agent_webhook_event(
        agent_id,
        event_type=TEST_EVENT_TYPE,
        payload={
            "message": "Pivota webhook test delivery",
            "agent_id": agent_id,
            "triggered_by": "developer_portal",
        },
        request_id=request_id,
    )
    await database.execute(
        """
        UPDATE agent_webhook_configs
        SET last_test_at = :last_test_at,
            last_test_status = :last_test_status,
            updated_at = :updated_at
        WHERE agent_id = :agent_id
        """,
        {
            "agent_id": agent_id,
            "last_test_at": _db_now(),
            "last_test_status": result.get("status"),
            "updated_at": _db_now(),
        },
    )
    return result


async def list_deliveries(
    agent_id: str,
    *,
    limit: int = 25,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_agent_webhook_tables()
    await process_due_retries(limit=10)

    clauses = ["agent_id = :agent_id"]
    params: Dict[str, Any] = {"agent_id": agent_id, "limit": limit}
    if status:
        clauses.append("status = :status")
        params["status"] = status
    rows = await database.fetch_all(
        f"""
        SELECT *
        FROM agent_webhook_deliveries
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
        "summary_24h": await get_delivery_summary(agent_id),
    }


async def retry_delivery(agent_id: str, delivery_id: str) -> Dict[str, Any]:
    await ensure_agent_webhook_tables()
    row = await database.fetch_one(
        """
        SELECT *
        FROM agent_webhook_deliveries
        WHERE agent_id = :agent_id
          AND delivery_id = :delivery_id
        LIMIT 1
        """,
        {"agent_id": agent_id, "delivery_id": delivery_id},
    )
    if not row:
        raise ValueError("Webhook delivery not found.")

    config = await _get_or_create_raw_config(agent_id)
    destination_url = str(row["destination_url"] or config.get("destination_url") or "").strip()
    signing_secret = str(config.get("signing_secret") or "").strip()
    if not destination_url or not signing_secret:
        raise ValueError("Webhook destination is not configured.")

    payload = _coerce_json(row["payload"], {})
    created_at = _coerce_datetime(row["created_at"]) or _utcnow()
    request_id = row["request_id"]
    return await _attempt_delivery(
        agent_id=agent_id,
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


async def rotate_signing_secret(agent_id: str) -> Dict[str, Any]:
    current = await _get_or_create_raw_config(agent_id)
    current_secret = str(current.get("signing_secret") or "").strip() or _generate_signing_secret()
    new_secret = _generate_signing_secret()
    activates_at = _utcnow() + timedelta(hours=24)
    now = _db_now()

    if current.get("created_at") is None:
        await database.execute(
            """
            INSERT INTO agent_webhook_configs (
                agent_id,
                enabled,
                destination_url,
                subscribed_events,
                signing_secret,
                pending_signing_secret,
                pending_secret_activates_at,
                created_at,
                updated_at
            )
            VALUES (
                :agent_id,
                :enabled,
                :destination_url,
                :subscribed_events,
                :signing_secret,
                :pending_signing_secret,
                :pending_secret_activates_at,
                :created_at,
                :updated_at
            )
            """,
            {
                "agent_id": agent_id,
                "enabled": False,
                "destination_url": None,
                "subscribed_events": json.dumps(DEFAULT_WEBHOOK_EVENTS),
                "signing_secret": current_secret,
                "pending_signing_secret": new_secret,
                "pending_secret_activates_at": _db_datetime(activates_at),
                "created_at": now,
                "updated_at": now,
            },
        )
    else:
        await database.execute(
            """
            UPDATE agent_webhook_configs
            SET signing_secret = COALESCE(signing_secret, :current_signing_secret),
                pending_signing_secret = :pending_signing_secret,
                pending_secret_activates_at = :pending_secret_activates_at,
                updated_at = :updated_at
            WHERE agent_id = :agent_id
            """,
            {
                "agent_id": agent_id,
                "current_signing_secret": current_secret,
                "pending_signing_secret": new_secret,
                "pending_secret_activates_at": _db_datetime(activates_at),
                "updated_at": now,
            },
        )

    return {
        "status": "success",
        "new_signing_secret": new_secret,
        "activates_at": activates_at.isoformat(),
        "overlap_ends_at": activates_at.isoformat(),
        "signing_secret_last4": _last4(current_secret),
        "current_signing_secret_last4": _last4(current_secret),
        "pending_signing_secret_last4": _last4(new_secret),
    }


async def process_due_retries(limit: int = 20) -> int:
    await ensure_agent_webhook_tables()
    rows = await database.fetch_all(
        """
        SELECT *
        FROM agent_webhook_deliveries
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
        try:
            await retry_delivery(str(row["agent_id"]), str(row["delivery_id"]))
            processed += 1
        except Exception as exc:
            logger.warning("Failed to process webhook retry %s: %s", row["delivery_id"], exc)
    return processed


async def _retry_worker_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            if getattr(database, "is_connected", False):
                await process_due_retries(limit=20)
        except Exception as exc:
            logger.warning("Agent webhook retry worker iteration failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RETRY_POLL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def start_agent_webhook_retry_worker() -> None:
    global _retry_worker_task, _retry_worker_stop
    if _retry_worker_task and not _retry_worker_task.done():
        return
    _retry_worker_stop = asyncio.Event()
    # Fresh context => own `databases` Connection (issue #1754: startup-spawned
    # workers otherwise share the startup context's Connection with each other
    # and, before the fix, with every scheduler job).
    from services.scheduler_job_runner import spawn_isolated
    _retry_worker_task = spawn_isolated(_retry_worker_loop(_retry_worker_stop), name="agent_webhook_retry_worker")


async def stop_agent_webhook_retry_worker() -> None:
    global _retry_worker_task, _retry_worker_stop
    if _retry_worker_stop is not None:
        _retry_worker_stop.set()
    if _retry_worker_task is not None:
        try:
            await _retry_worker_task
        except Exception:
            pass
    _retry_worker_task = None
    _retry_worker_stop = None
