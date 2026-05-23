from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.pcs_hash import sha256_json
from utils.logger import logger


MONETIZATION_BEARING_FACT_TYPES = frozenset(
    {
        "order_completed",
        "refund_issued",
        "chargeback",
        "payment_attempted",
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _topic_to_fact_type(topic: str) -> str:
    raw = (topic or "").strip().lower()
    if not raw:
        return "shopify.unknown"
    return "shopify." + raw.replace("/", ".")


def build_shopify_fact_dedupe_key(*, idempotency_key: str) -> str:
    return f"shopify:{idempotency_key}"


def build_internal_fact_dedupe_key(*, fact_type: str, order_id: Optional[str], idempotency_key: Optional[str]) -> str:
    # Prefer caller-provided idempotency key; fall back to order_id; final fallback is a hash.
    if idempotency_key:
        return f"internal:{fact_type}:{idempotency_key}"
    if order_id:
        return f"internal:{fact_type}:{order_id}"
    return f"internal:{fact_type}:{sha256_json({'fact_type': fact_type, 'at': _utc_now().isoformat()})}"


def _extract_order_id_from_shopify_payload(topic: str, payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    t = (topic or "").lower()
    # orders/*
    if t.startswith("orders/"):
        oid = payload.get("id")
        return str(oid) if oid is not None else None
    # fulfillments/*
    if t.startswith("fulfillments/"):
        oid = payload.get("order_id")
        return str(oid) if oid is not None else None
    # refunds/create
    if t == "refunds/create":
        oid = payload.get("order_id")
        return str(oid) if oid is not None else None
    # disputes/*
    if t.startswith("disputes/"):
        oid = payload.get("order_id") or payload.get("shopify_order_id")
        return str(oid) if oid is not None else None
    # tender_transactions/create
    if t == "tender_transactions/create":
        oid = payload.get("order_id")
        return str(oid) if oid is not None else None
    return None


@dataclass(frozen=True)
class BackfillResult:
    facts_scanned: int
    facts_inserted: int
    facts_duplicated: int
    orders_touched: int
    last_received_at: Optional[datetime]


async def backfill_shopify_webhook_events_to_facts(
    *,
    merchant_id: str,
    since_received_at: Optional[datetime] = None,
    limit: int = 5000,
    db=None,
) -> BackfillResult:
    """
    Best-effort backfill: convert pcs_shopify_webhook_events -> pcs_order_facts.

    Dedupe:
    - Uses pcs_shopify_webhook_events.idempotency_key (already stable and id-based where possible).
    - pcs_order_facts enforces UNIQUE (merchant_id, dedupe_key).
    """
    if db is None:
        from db.database import database as db  # local import to avoid import-time DATABASE_URL issues in some contexts

    since = since_received_at
    values: Dict[str, Any] = {"merchant_id": merchant_id, "limit": max(1, min(int(limit), 50000))}

    where = "merchant_id = :merchant_id"
    if since is not None:
        where += " AND received_at >= :since"
        values["since"] = since

    rows = await db.fetch_all(
        f"""
        SELECT
          id,
          merchant_id,
          shop_domain,
          topic,
          webhook_id,
          idempotency_key,
          signature_verified,
          received_at,
          occurred_at,
          payload_json,
          payload_sha256,
          chain_hash
        FROM pcs_shopify_webhook_events
        WHERE {where}
        ORDER BY received_at ASC, id ASC
        LIMIT :limit
        """,
        values,
    )

    scanned = 0
    inserted = 0
    duplicated = 0
    orders: set[str] = set()
    last_received: Optional[datetime] = None

    for row in rows or []:
        scanned += 1
        r = dict(row)
        topic = str(r.get("topic") or "")
        payload = r.get("payload_json") or {}
        order_id = _extract_order_id_from_shopify_payload(topic, payload)
        if order_id:
            orders.add(order_id)

        dedupe_key = build_shopify_fact_dedupe_key(idempotency_key=str(r.get("idempotency_key") or ""))
        fact_type = _topic_to_fact_type(topic)

        payload_json = payload if isinstance(payload, dict) else {"raw": payload}
        payload_json_str = json.dumps(payload_json, ensure_ascii=False)

        try:
            created = await db.fetch_one(
                """
                INSERT INTO pcs_order_facts
                  (merchant_id, stream_id, order_id, fact_id, fact_type, occurred_at, received_at,
                   source, topic, source_event_id, dedupe_key, payload_json, payload_sha256, chain_hash)
                VALUES
                  (:merchant_id, :stream_id, :order_id, gen_random_uuid(), :fact_type, :occurred_at, :received_at,
                   :source, :topic, :source_event_id, :dedupe_key, CAST(:payload_json AS jsonb), :payload_sha256, :chain_hash)
                ON CONFLICT (merchant_id, dedupe_key) DO NOTHING
                RETURNING fact_id
                """,
                {
                    "merchant_id": merchant_id,
                    "stream_id": "orders",
                    "order_id": order_id,
                    "fact_type": fact_type,
                    "occurred_at": r.get("occurred_at"),
                    "received_at": r.get("received_at"),
                    "source": "shopify_webhook",
                    "topic": topic,
                    "source_event_id": r.get("webhook_id") or r.get("id"),
                    "dedupe_key": dedupe_key,
                    "payload_json": payload_json_str,
                    "payload_sha256": r.get("payload_sha256"),
                    "chain_hash": r.get("chain_hash"),
                },
            )
            if created:
                inserted += 1
            else:
                duplicated += 1
        except Exception as e:
            logger.warning({"merchant_id": merchant_id, "error": str(e)}, "Fact backfill insert failed")

        last_received = r.get("received_at") or last_received

    return BackfillResult(
        facts_scanned=scanned,
        facts_inserted=inserted,
        facts_duplicated=duplicated,
        orders_touched=len(orders),
        last_received_at=last_received,
    )


async def append_internal_fact_best_effort(
    *,
    merchant_id: str,
    order_id: Optional[str],
    fact_type: str,
    payload: Dict[str, Any],
    occurred_at: Optional[datetime] = None,
    idempotency_key: Optional[str] = None,
    db=None,
) -> None:
    """
    Internal fact emission (no PII).
    - Raises on failure for monetization-bearing fact types. Non-monetization facts remain best-effort.
    - Strict dedupe at DB layer via pcs_order_facts(merchant_id, dedupe_key).
    """
    fact_type_str = str(fact_type)
    never_raises = os.getenv("PCS_FACT_NEVER_RAISES", "").strip().lower() == "true"
    raise_on_failure = fact_type_str in MONETIZATION_BEARING_FACT_TYPES and not never_raises

    if db is None:
        try:
            from db.database import database as db
        except Exception as e:
            if raise_on_failure:
                raise
            if not never_raises:
                logger.warning(
                    {
                        "event": "pcs_internal_fact_emit_failed",
                        "fact_type": fact_type_str,
                        "order_id": order_id,
                        "error": str(e),
                    }
                )
            return

    try:
        dedupe_key = build_internal_fact_dedupe_key(
            fact_type=fact_type_str, order_id=order_id, idempotency_key=idempotency_key
        )
        payload_json_str = json.dumps(payload or {}, ensure_ascii=False)
        payload_sha = sha256_json(payload or {})
        await db.execute(
            """
            INSERT INTO pcs_order_facts
              (merchant_id, stream_id, order_id, fact_id, fact_type, occurred_at, received_at,
               source, topic, source_event_id, dedupe_key, payload_json, payload_sha256, chain_hash)
            VALUES
              (:merchant_id, :stream_id, :order_id, gen_random_uuid(), :fact_type, :occurred_at, NOW(),
               :source, :topic, :source_event_id, :dedupe_key, CAST(:payload_json AS jsonb), :payload_sha256, NULL)
            ON CONFLICT (merchant_id, dedupe_key) DO NOTHING
            """,
            {
                "merchant_id": merchant_id,
                "stream_id": "orders",
                "order_id": order_id,
                "fact_type": fact_type_str,
                "occurred_at": occurred_at or _utc_now(),
                "source": "internal",
                "topic": None,
                "source_event_id": None,
                "dedupe_key": dedupe_key,
                "payload_json": payload_json_str,
                "payload_sha256": payload_sha,
            },
        )
    except Exception as e:
        if raise_on_failure:
            raise
        if not never_raises:
            logger.warning(
                {
                    "event": "pcs_internal_fact_emit_failed",
                    "fact_type": fact_type_str,
                    "order_id": order_id,
                    "error": str(e),
                }
            )
        return
