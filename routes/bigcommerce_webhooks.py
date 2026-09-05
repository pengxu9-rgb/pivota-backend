"""Signed BigCommerce order lifecycle events -> the canonical commerce ledger.

Two facts make this receiver differ from the Shopify/WooCommerce/SHOPLINE ones
(both verified against https://docs.bigcommerce.com/docs/integrations/webhooks
on 2026-09-04):

1. **BigCommerce does not sign deliveries.** There is no HMAC header to verify.
   Authentication is a custom header registered with the hook itself
   (``headers`` on `POST /v3/hooks`), so Pivota mints a per-store random secret
   at subscription time, stores it in the store's credential JSON as
   ``webhook_secret``, registers it as ``X-Pivota-Webhook-Secret``, and
   compares it here with ``hmac.compare_digest``.

   A shared secret in a header is weaker than a body signature: it cannot bind
   the delivery to the BODY. The payload's ``producer`` (``stores/<hash>``) is
   therefore checked against the store's own ``store_hash`` as well, and the
   order is then read back from that same store's API — a forged body can only
   make us re-read an order the merchant already owns.

2. **The delivery carries no order fields.** It names an order id; the order
   and its refunds are fetched (services/bigcommerce_order_fetch.py) before
   mapping. A fetch failure is answered 503, never 200: BigCommerce retries a
   failed delivery over ~48 hours, and a 200 would drop the event for good.
"""

from __future__ import annotations

import hmac
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status

from db.database import database
from services.bigcommerce_event_adapter import (
    UnsupportedBigCommerceEvent,
    is_supported_bigcommerce_scope,
    map_bigcommerce_order,
)
from services.bigcommerce_order_fetch import (
    BigCommerceOrderFetchError,
    fetch_bigcommerce_order_context,
)
from services.merchant_event_ingest_service import ingest_merchant_event_batch
from services.telemetry_ingress import current_ingress, telemetry_ingress_route


router = APIRouter(prefix="/webhooks/bigcommerce", tags=["BigCommerce Webhooks"])

MAX_BIGCOMMERCE_WEBHOOK_BYTES = 1_000_000
BIGCOMMERCE_WEBHOOK_SECRET_HEADER = "X-Pivota-Webhook-Secret"
_PRODUCER_PREFIX = "stores/"
_UNAUTHORIZED = "Invalid BigCommerce webhook credentials"


def _credentials(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    value = str(raw or "").strip()
    if not value or not value.startswith("{"):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _producer_store_hash(value: Any) -> str:
    """``stores/abcde`` -> ``abcde``; anything else -> ``""``."""
    raw = str(value or "").strip().lower()
    if not raw.startswith(_PRODUCER_PREFIX):
        return ""
    return raw[len(_PRODUCER_PREFIX) :].strip().strip("/")


@router.post("/{store_id}")
@telemetry_ingress_route("bigcommerce_webhook")
async def receive_bigcommerce_webhook(
    store_id: str,
    request: Request,
    webhook_secret: Optional[str] = Header(
        default=None, alias=BIGCOMMERCE_WEBHOOK_SECRET_HEADER
    ),
):
    raw = await request.body()
    if len(raw) > MAX_BIGCOMMERCE_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="BigCommerce webhook exceeds 1 MB")

    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'bigcommerce'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    credentials = _credentials(dict(store).get("api_key") if store else None)
    expected_secret = str(credentials.get("webhook_secret") or "").strip()
    supplied_secret = str(webhook_secret or "").strip()
    # Unknown store, inactive store, un-provisioned secret, and a wrong secret
    # all answer the same 401: the caller learns nothing about which it was.
    if (
        not store
        or not expected_secret
        or not supplied_secret
        or not hmac.compare_digest(expected_secret, supplied_secret)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_UNAUTHORIZED)

    store = dict(store)
    ingress = current_ingress(request)
    ingress.identify(merchant_id=store["merchant_id"], store_id=store_id)
    await ingress.enforce_rate_limit("platform", store_id)

    try:
        payload = json.loads(raw or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid BigCommerce webhook JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail="BigCommerce webhook body must be an object"
        )

    store_hash = str(credentials.get("store_hash") or "").strip().lower()
    # The secret is a bearer credential in a header; it cannot bind a delivery
    # to a body. `producer` must name this store's own hash.
    if not store_hash or _producer_store_hash(payload.get("producer")) != store_hash:
        raise HTTPException(status_code=401, detail="Invalid BigCommerce webhook source")

    scope = str(payload.get("scope") or "").strip()
    # Ignored BEFORE the fetch: an unmapped scope must not cost a BigCommerce
    # API call, and must not be able to drive one.
    if not is_supported_bigcommerce_scope(scope):
        return {
            "status": "ignored",
            "platform": "bigcommerce",
            "reason": f"unsupported BigCommerce webhook scope: {scope or 'missing'}",
        }

    data = payload.get("data")
    order_id = ""
    if isinstance(data, dict):
        order_id = str(data.get("order_id") or data.get("id") or "").strip()
    if not order_id:
        raise HTTPException(
            status_code=422, detail="BigCommerce webhook is missing an order id"
        )

    try:
        context = await fetch_bigcommerce_order_context(
            store_hash=store_hash,
            access_token=str(credentials.get("access_token") or ""),
            client_id=str(credentials.get("client_id") or "") or None,
            order_id=order_id,
            scope=scope,
        )
    except BigCommerceOrderFetchError as exc:
        # Retryable: BigCommerce redelivers a non-2xx with backoff.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        batch = map_bigcommerce_order(
            context.order,
            context.refunds,
            scope=scope,
            delivery_hash=str(payload.get("hash") or "").strip() or None,
            store_id=store_id,
        )
    except UnsupportedBigCommerceEvent as exc:
        return {"status": "ignored", "platform": "bigcommerce", "reason": str(exc)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = await ingest_merchant_event_batch(
        merchant_id=str(store["merchant_id"]),
        batch=batch,
        agent_identity_confidence="platform_asserted",
        write_path="bigcommerce_webhook",
    )
    return {"status": "recorded", "platform": "bigcommerce", **result}
