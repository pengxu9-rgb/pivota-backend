from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

from db.database import database
from services.merchant_event_ingest_service import ingest_merchant_event_batch
from services.shopify_commerce_event_adapter import (
    UnsupportedShopifyCommerceEvent,
    map_shopify_webhook,
)
from utils.logger import logger


def _shop_hostname(value: Any) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").rstrip(".") or None


async def resolve_shopify_store_id(
    *, merchant_id: str, shop_domain: Optional[str]
) -> Optional[str]:
    """Resolve the already-verified Shopify shop to its canonical store scope."""
    rows = await database.fetch_all(
        """
        SELECT store_id, domain
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = 'shopify'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"merchant_id": merchant_id},
    )
    candidates = [dict(row) for row in rows]
    wanted = _shop_hostname(shop_domain)
    if wanted:
        for candidate in candidates:
            if _shop_hostname(candidate.get("domain")) == wanted:
                return str(candidate["store_id"])
    # The webhook caller has already authenticated and resolved the merchant.
    # A unique active Shopify store is therefore an unambiguous legacy fallback.
    if len(candidates) == 1:
        return str(candidates[0]["store_id"])
    return None


async def resolve_pivota_order_id(
    *, merchant_id: str, shopify_order_id: Optional[str]
) -> Optional[str]:
    """The Pivota order this Shopify order was written back from, if any.

    The fallback half of the canonical order identity: orders written back
    before the ``pivota_order_id`` note attribute existed carry no marker in
    the webhook body, but Pivota recorded the Shopify id on its own row at
    writeback time. ``orders.shopify_order_id`` is UNIQUE, so this is a single
    indexed lookup. Best-effort: a failure means "no marker", never a dropped
    event.
    """
    native_id = str(shopify_order_id or "").strip()
    if not native_id or not str(merchant_id or "").strip():
        return None
    try:
        row = await database.fetch_one(
            """
            SELECT order_id
            FROM orders
            WHERE merchant_id = :merchant_id
              AND shopify_order_id = :shopify_order_id
            LIMIT 1
            """,
            {"merchant_id": merchant_id, "shopify_order_id": native_id},
        )
    except Exception as exc:
        logger.warning(
            "Shopify canonical order_ref lookup failed "
            f"merchant={merchant_id} shopify_order_id={native_id}: {exc}"
        )
        return None
    if not row:
        return None
    return str(dict(row).get("order_id") or "").strip() or None


def _native_order_id(topic: str, payload: Dict[str, Any]) -> Optional[str]:
    """The Shopify order id this webhook is about, whatever the topic."""
    if str(topic or "").strip().lower() == "refunds/create":
        raw = payload.get("order_id")
    else:
        raw = payload.get("id") or payload.get("order_number") or payload.get("name")
    return str(raw or "").strip() or None


async def ingest_shopify_commerce_event_best_effort(
    *,
    merchant_id: str,
    shop_domain: Optional[str],
    topic: str,
    payload: Any,
    webhook_id: Optional[str],
    occurred_at: Any,
    signature_verified: bool,
) -> Dict[str, Any]:
    """Dual-write verified Shopify events without blocking the legacy webhook path."""
    if not signature_verified:
        return {"status": "skipped", "reason": "signature_not_verified"}
    if not isinstance(payload, dict):
        return {"status": "skipped", "reason": "payload_not_object"}
    try:
        store_id = await resolve_shopify_store_id(
            merchant_id=merchant_id, shop_domain=shop_domain
        )
        if not store_id:
            return {"status": "skipped", "reason": "store_not_resolved"}
        batch = map_shopify_webhook(
            payload,
            topic=topic,
            delivery_id=webhook_id,
            store_id=store_id,
            occurred_at=occurred_at,
            pivota_order_id=await resolve_pivota_order_id(
                merchant_id=merchant_id,
                shopify_order_id=_native_order_id(topic, payload),
            ),
        )
        result = await ingest_merchant_event_batch(
            merchant_id=merchant_id,
            batch=batch,
            agent_identity_confidence="platform_asserted",
            write_path="shopify_webhook",
        )
        return {
            "status": "accepted",
            "store_id": store_id,
            "accepted": result.get("accepted", len(batch.events)),
        }
    except UnsupportedShopifyCommerceEvent:
        return {"status": "ignored", "reason": "unsupported_topic"}
    except Exception as exc:
        logger.warning(
            "Shopify canonical event dual-write failed "
            f"merchant={merchant_id} topic={topic}: {exc}"
        )
        return {"status": "degraded", "reason": "canonical_ingest_failed"}
