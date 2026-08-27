from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import select

from db.commerce_interactions import commerce_interaction_events, commerce_interactions
from db.database import database
from services.traffic_taxonomy_service import attach_traffic_taxonomy, build_traffic_taxonomy


logger = logging.getLogger("commerce_interaction_service")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _make_json_safe(payload: Any) -> Any:
    return json.loads(json.dumps(payload, default=_json_default, ensure_ascii=False))


def _stable_id(prefix: str, *parts: Any) -> str:
    normalized = "|".join(str(part or "").strip().lower() for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _coerce_refs(payload: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Optional[str]]:
    raw = dict(payload or {})
    raw.update(kwargs)
    refs = {
        "interaction_id": _normalize_text(raw.get("interaction_id")),
        "merchant_id": _normalize_text(raw.get("merchant_id")),
        "platform": _normalize_text(raw.get("platform")),
        "store_id": _normalize_text(raw.get("store_id")),
        "surface": _normalize_text(raw.get("surface")),
        "prompt_id": _normalize_text(raw.get("prompt_id")),
        "result_id": _normalize_text(raw.get("result_id")),
        "click_id": _normalize_text(raw.get("click_id")),
        "cart_id": _normalize_text(raw.get("cart_id")),
        "quote_id": _normalize_text(raw.get("quote_id")),
        "checkout_id": _normalize_text(raw.get("checkout_id")),
        "payment_id": _normalize_text(raw.get("payment_id")),
        "order_id": _normalize_text(raw.get("order_id")),
        "refund_id": _normalize_text(raw.get("refund_id")),
        "return_id": _normalize_text(raw.get("return_id")),
        "canonical_product_id": _normalize_text(raw.get("canonical_product_id")),
        "canonical_variant_id": _normalize_text(raw.get("canonical_variant_id")),
        "trace_id": _normalize_text(raw.get("trace_id")),
        "brief_id": _normalize_text(raw.get("brief_id")),
        "session_id": _normalize_text(raw.get("session_id")),
        "visitor_id": _normalize_text(raw.get("visitor_id")),
        "buyer_id": _normalize_text(raw.get("buyer_id")),
    }
    return refs


def _fallback_interaction_id(refs: Dict[str, Optional[str]]) -> str:
    for key in (
        "click_id",
        "cart_id",
        "order_id",
        "checkout_id",
        "payment_id",
        "quote_id",
        "refund_id",
        "return_id",
        "prompt_id",
        "result_id",
        "trace_id",
        "session_id",
        "canonical_variant_id",
        "canonical_product_id",
    ):
        value = refs.get(key)
        if value:
            scope = [refs.get("merchant_id")]
            if refs.get("store_id"):
                scope.append(refs["store_id"])
            return _stable_id("int", *scope, key, value)
    return f"int_{uuid.uuid4().hex[:24]}"


async def _lookup_existing_interaction(refs: Dict[str, Optional[str]]) -> Optional[Dict[str, Any]]:
    keys = (
        "interaction_id",
        "click_id",
        "order_id",
        "checkout_id",
        "payment_id",
        "cart_id",
        "quote_id",
        "refund_id",
        "return_id",
        "session_id",
    )
    merchant_id = refs.get("merchant_id")
    for key in keys:
        value = refs.get(key)
        if not value:
            continue
        conditions = [
            commerce_interactions.c.merchant_id == merchant_id,
            getattr(commerce_interactions.c, key) == value,
        ]
        if key in {"session_id", "cart_id", "payment_id"} and refs.get("store_id"):
            conditions.append(commerce_interactions.c.store_id == refs["store_id"])
        row = await database.fetch_one(select(commerce_interactions).where(*conditions))
        if row:
            return dict(row)
    return None


def _merge_metadata(existing: Optional[Any], patch: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    base = dict(existing or {}) if isinstance(existing, dict) else {}
    extra = _make_json_safe(patch or {}) if patch else {}
    if extra:
        base.update(extra)
    return base or None


def _status_from_event(event_type: str, current: Optional[str]) -> Optional[str]:
    mapping = {
        "agent.requested": "observed",
        "search.performed": "discovery",
        "product.viewed": "pdp_viewed",
        "cart.created": "cart_active",
        "cart.item_added": "cart_active",
        "cart.item_removed": "cart_active",
        "cart.updated": "cart_active",
        "checkout.started": "checkout_started",
        "checkout.submitted": "checkout_submitted",
        "payment.attempted": "payment_pending",
        "payment.authorized": "payment_authorized",
        "payment.declined": "payment_failed",
        "payment.succeeded": "paid",
        "payment.failed": "payment_failed",
        "listing.exported": "indexed",
        "listing.blocked": "blocked",
        "surface.impression": "surfaced",
        "surface.click": "clicked",
        "checkout.created": "checkout_created",
        "payment.intent.created": "awaiting_payment",
        "payment.intent.viewed": "awaiting_payment",
        "order.created": "ordered",
        "order.paid": "paid",
        "order.cancelled": "cancelled",
        "refund.created": "refund_pending",
        "refund.requested": "refund_pending",
        "refund.succeeded": "refunded",
        "return.created": "return_pending",
        "return.completed": "return_synced",
        "return.sync.completed": "return_synced",
    }
    return mapping.get(event_type, current)


async def ensure_interaction(
    *,
    metadata: Optional[Dict[str, Any]] = None,
    first_occurred_at: Optional[datetime] = None,
    last_occurred_at: Optional[datetime] = None,
    latest_event_type: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    taxonomy = build_traffic_taxonomy(
        metadata,
        metadata=kwargs,
        authenticated_agent_id=_normalize_text((metadata or {}).get("agent_id")) if isinstance(metadata, dict) else None,
        caller_id=_normalize_text(kwargs.get("caller_id")),
        default_source_channel=_normalize_text(kwargs.get("source_channel")),
        default_query_source=_normalize_text(kwargs.get("query_source")),
        default_protocol_name=_normalize_text(kwargs.get("protocol_name")),
        default_commerce_surface=(
            _normalize_text((metadata or {}).get("surface")) if isinstance(metadata, dict) else None
        ) or _normalize_text(kwargs.get("surface")),
    )
    metadata_with_taxonomy = attach_traffic_taxonomy(metadata, taxonomy)
    refs = _coerce_refs(metadata_with_taxonomy, **kwargs)
    merchant_id = refs.get("merchant_id")
    if not merchant_id:
        raise ValueError("merchant_id is required for commerce interaction tracking")

    existing = await _lookup_existing_interaction(refs)
    interaction_id = (
        (existing or {}).get("interaction_id")
        or refs.get("interaction_id")
        or _fallback_interaction_id(refs)
    )
    now = _now()
    first_seen = first_occurred_at or now
    last_seen = last_occurred_at or first_seen
    status = _status_from_event(latest_event_type or "", (existing or {}).get("status"))
    values: Dict[str, Any] = {
        "interaction_id": interaction_id,
        "merchant_id": merchant_id,
        "platform": refs.get("platform") or (existing or {}).get("platform"),
        "store_id": refs.get("store_id") or (existing or {}).get("store_id"),
        "surface": refs.get("surface") or (existing or {}).get("surface"),
        "commerce_surface": taxonomy.get("commerce_surface") or (existing or {}).get("commerce_surface"),
        "prompt_id": refs.get("prompt_id") or (existing or {}).get("prompt_id"),
        "result_id": refs.get("result_id") or (existing or {}).get("result_id"),
        "click_id": refs.get("click_id") or (existing or {}).get("click_id"),
        "cart_id": refs.get("cart_id") or (existing or {}).get("cart_id"),
        "quote_id": refs.get("quote_id") or (existing or {}).get("quote_id"),
        "checkout_id": refs.get("checkout_id") or (existing or {}).get("checkout_id"),
        "payment_id": refs.get("payment_id") or (existing or {}).get("payment_id"),
        "order_id": refs.get("order_id") or (existing or {}).get("order_id"),
        "refund_id": refs.get("refund_id") or (existing or {}).get("refund_id"),
        "return_id": refs.get("return_id") or (existing or {}).get("return_id"),
        "canonical_product_id": refs.get("canonical_product_id") or (existing or {}).get("canonical_product_id"),
        "canonical_variant_id": refs.get("canonical_variant_id") or (existing or {}).get("canonical_variant_id"),
        "trace_id": refs.get("trace_id") or (existing or {}).get("trace_id"),
        "brief_id": refs.get("brief_id") or (existing or {}).get("brief_id"),
        "session_id": refs.get("session_id") or (existing or {}).get("session_id"),
        "visitor_id": refs.get("visitor_id") or (existing or {}).get("visitor_id"),
        "buyer_id": refs.get("buyer_id") or (existing or {}).get("buyer_id"),
        "source_channel": taxonomy.get("source_channel") or (existing or {}).get("source_channel"),
        "source_family": taxonomy.get("source_family") or (existing or {}).get("source_family"),
        "query_source": taxonomy.get("query_source") or (existing or {}).get("query_source"),
        "agent_id": taxonomy.get("agent_id") or (existing or {}).get("agent_id"),
        "protocol_name": taxonomy.get("protocol_name") or (existing or {}).get("protocol_name"),
        "llm_provider": taxonomy.get("llm_provider") or (existing or {}).get("llm_provider"),
        "llm_model": taxonomy.get("llm_model") or (existing or {}).get("llm_model"),
        "caller_id": taxonomy.get("caller_id") or (existing or {}).get("caller_id"),
        "latest_event_type": latest_event_type or (existing or {}).get("latest_event_type"),
        "status": status,
        "metadata": _merge_metadata((existing or {}).get("metadata"), metadata_with_taxonomy),
        "first_occurred_at": min(
            [dt for dt in [first_seen, (existing or {}).get("first_occurred_at")] if dt is not None]
        ),
        "last_occurred_at": max(
            [dt for dt in [last_seen, (existing or {}).get("last_occurred_at")] if dt is not None]
        ),
        "updated_at": now,
    }

    if existing:
        await database.execute(
            commerce_interactions.update()
            .where(commerce_interactions.c.interaction_id == interaction_id)
            .values(**values)
        )
    else:
        await database.execute(
            commerce_interactions.insert().values(
                created_at=now,
                **values,
            )
        )

    row = await database.fetch_one(
        select(commerce_interactions).where(commerce_interactions.c.interaction_id == interaction_id)
    )
    return dict(row) if row else {"interaction_id": interaction_id, **values}


async def record_commerce_event(
    *,
    event_type: str,
    metadata: Optional[Dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
    source: Optional[str] = None,
    upstream_idempotency_key: Optional[str] = None,
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    taxonomy = build_traffic_taxonomy(
        metadata,
        metadata=kwargs,
        authenticated_agent_id=_normalize_text((metadata or {}).get("agent_id")) if isinstance(metadata, dict) else None,
        caller_id=_normalize_text(kwargs.get("caller_id")),
        default_source_channel=_normalize_text(kwargs.get("source_channel")),
        default_query_source=_normalize_text(kwargs.get("query_source")),
        default_protocol_name=_normalize_text(kwargs.get("protocol_name")),
        default_commerce_surface=(
            _normalize_text((metadata or {}).get("surface")) if isinstance(metadata, dict) else None
        ) or _normalize_text(kwargs.get("surface")),
    )
    metadata_with_taxonomy = attach_traffic_taxonomy(metadata, taxonomy)
    refs = _coerce_refs(metadata_with_taxonomy, **kwargs)
    merchant_id = refs.get("merchant_id")
    if not merchant_id:
        raise ValueError("merchant_id is required for commerce interaction tracking")

    occurred = occurred_at or _now()
    interaction = await ensure_interaction(
        metadata=metadata_with_taxonomy,
        first_occurred_at=occurred,
        last_occurred_at=occurred,
        latest_event_type=event_type,
        **refs,
    )

    duplicate_event = None
    if upstream_idempotency_key:
        duplicate_event = await database.fetch_one(
            select(commerce_interaction_events).where(
                commerce_interaction_events.c.merchant_id == merchant_id,
                commerce_interaction_events.c.event_type == event_type,
                commerce_interaction_events.c.upstream_idempotency_key == upstream_idempotency_key,
            )
        )
    if duplicate_event:
        return {
            "interaction_id": interaction["interaction_id"],
            "event_id": dict(duplicate_event).get("event_id"),
            "duplicate": True,
        }

    event_id = (
        _stable_id("evt", merchant_id, event_type, upstream_idempotency_key)
        if upstream_idempotency_key
        else f"evt_{uuid.uuid4().hex[:24]}"
    )
    payload = _make_json_safe(
        {
            **(metadata_with_taxonomy or {}),
            **{key: value for key, value in refs.items() if value},
        }
    )
    await database.execute(
        commerce_interaction_events.insert().values(
            event_id=event_id,
            interaction_id=interaction["interaction_id"],
            merchant_id=merchant_id,
            platform=refs.get("platform") or interaction.get("platform"),
            store_id=refs.get("store_id") or interaction.get("store_id"),
            surface=refs.get("surface") or interaction.get("surface"),
            event_type=event_type,
            occurred_at=occurred,
            canonical_product_id=refs.get("canonical_product_id") or interaction.get("canonical_product_id"),
            canonical_variant_id=refs.get("canonical_variant_id") or interaction.get("canonical_variant_id"),
            trace_id=refs.get("trace_id") or interaction.get("trace_id"),
            brief_id=refs.get("brief_id") or interaction.get("brief_id"),
            session_id=refs.get("session_id") or interaction.get("session_id"),
            visitor_id=refs.get("visitor_id") or interaction.get("visitor_id"),
            cart_id=refs.get("cart_id") or interaction.get("cart_id"),
            payment_id=refs.get("payment_id") or interaction.get("payment_id"),
            source=source,
            upstream_idempotency_key=upstream_idempotency_key,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload,
        )
    )

    await database.execute(
        commerce_interactions.update()
        .where(commerce_interactions.c.interaction_id == interaction["interaction_id"])
        .values(
            latest_event_type=event_type,
            status=_status_from_event(event_type, interaction.get("status")),
            last_occurred_at=occurred,
            updated_at=_now(),
        )
    )
    return {
        "interaction_id": interaction["interaction_id"],
        "event_id": event_id,
        "duplicate": False,
    }


async def record_commerce_event_best_effort(
    *,
    event_type: str,
    metadata: Optional[Dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
    source: Optional[str] = None,
    upstream_idempotency_key: Optional[str] = None,
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    refs = _coerce_refs(metadata, **kwargs)
    interaction_id = refs.get("interaction_id") or _fallback_interaction_id(refs)
    fallback_event_id = (
        _stable_id("evt", refs.get("merchant_id"), event_type, upstream_idempotency_key)
        if upstream_idempotency_key
        else f"evt_{uuid.uuid4().hex[:24]}"
    )
    try:
        return await record_commerce_event(
            event_type=event_type,
            metadata=metadata,
            occurred_at=occurred_at,
            source=source,
            upstream_idempotency_key=upstream_idempotency_key,
            actor_type=actor_type,
            actor_id=actor_id,
            **kwargs,
        )
    except Exception as exc:
        logger.warning(
            "Best-effort commerce ledger write skipped for event_type=%s merchant_id=%s: %s",
            event_type,
            refs.get("merchant_id"),
            exc,
        )
        return {
            "interaction_id": interaction_id,
            "event_id": fallback_event_id,
            "duplicate": False,
            "degraded": True,
        }


async def find_interaction_by_checkout_id(checkout_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        select(commerce_interactions).where(commerce_interactions.c.checkout_id == str(checkout_id).strip())
    )
    return dict(row) if row else None


async def find_interaction_by_order_id(order_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        select(commerce_interactions).where(commerce_interactions.c.order_id == str(order_id).strip())
    )
    return dict(row) if row else None


async def trace_interaction(interaction_id: str) -> Dict[str, Any]:
    interaction_row = await database.fetch_one(
        select(commerce_interactions).where(commerce_interactions.c.interaction_id == str(interaction_id).strip())
    )
    event_rows = await database.fetch_all(
        select(commerce_interaction_events)
        .where(commerce_interaction_events.c.interaction_id == str(interaction_id).strip())
        .order_by(commerce_interaction_events.c.occurred_at.asc(), commerce_interaction_events.c.created_at.asc())
    )
    return {
        "interaction": dict(interaction_row) if interaction_row else None,
        "events": [dict(row) for row in event_rows],
    }
