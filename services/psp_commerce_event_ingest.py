from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from services.merchant_event_ingest_service import ingest_merchant_event_batch
from services.merchant_store_service import get_merchant_active_stores
from services.stripe_commerce_event_adapter import (
    UnsupportedStripeCommerceEvent,
    map_stripe_webhook_event,
)
from utils.logger import logger


async def resolve_order_store_scope(order: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    merchant_id = str(order.get("merchant_id") or "").strip()
    if not merchant_id:
        return None
    stores = await get_merchant_active_stores(merchant_id)
    if not stores:
        return None
    requested_store_id = str(order.get("store_id") or "").strip()
    if requested_store_id:
        for store in stores:
            if str(store.get("store_id") or "").strip() == requested_store_id:
                platform = str(store.get("platform") or "").strip().lower()
                if platform:
                    return requested_store_id, platform
    if len(stores) == 1:
        store_id = str(stores[0].get("store_id") or "").strip()
        platform = str(stores[0].get("platform") or "").strip().lower()
        if store_id and platform:
            return store_id, platform
    primary = next((store for store in stores if store.get("is_primary")), None)
    if primary:
        store_id = str(primary.get("store_id") or "").strip()
        platform = str(primary.get("platform") or "").strip().lower()
        if store_id and platform:
            return store_id, platform
    return None


async def ingest_stripe_commerce_event_best_effort(
    *,
    event_type: str,
    stripe_event_id: str,
    event_created: Any,
    data: Any,
    order: Any,
    signature_verified: bool,
) -> Dict[str, Any]:
    """Write a validated Stripe fact without changing payment webhook outcomes."""
    if not signature_verified:
        return {"status": "skipped", "reason": "signature_not_verified"}
    if not isinstance(data, dict) or not isinstance(order, dict):
        return {"status": "skipped", "reason": "invalid_input"}
    merchant_id = str(order.get("merchant_id") or "").strip()
    if not merchant_id:
        return {"status": "skipped", "reason": "merchant_not_resolved"}
    try:
        scope = await resolve_order_store_scope(order)
        if not scope:
            return {"status": "skipped", "reason": "store_not_resolved"}
        store_id, platform = scope
        batch = map_stripe_webhook_event(
            data,
            event_type=event_type,
            stripe_event_id=stripe_event_id,
            event_created=event_created,
            order=order,
            store_id=store_id,
            platform=platform,
        )
        result = await ingest_merchant_event_batch(
            merchant_id=merchant_id,
            batch=batch,
            agent_identity_confidence="platform_asserted",
            write_path="stripe_webhook",
        )
        return {
            "status": "accepted",
            "store_id": store_id,
            "platform": platform,
            "accepted": result.get("accepted", len(batch.events)),
        }
    except UnsupportedStripeCommerceEvent:
        return {"status": "ignored", "reason": "unsupported_or_nonterminal_event"}
    except Exception as exc:
        logger.warning(
            "Stripe canonical event dual-write failed "
            f"merchant={merchant_id} event_type={event_type}: {exc}"
        )
        return {"status": "degraded", "reason": "canonical_ingest_failed"}
