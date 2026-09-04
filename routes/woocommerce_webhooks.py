from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request, status

from adapters.woocommerce_adapter import normalize_woocommerce_store_url
from db.database import database
from services.merchant_event_ingest_service import ingest_merchant_event_batch
from services.woocommerce_event_adapter import (
    UnsupportedWooCommerceEvent,
    map_woocommerce_webhook,
)


router = APIRouter(prefix="/webhooks/woocommerce", tags=["WooCommerce Webhooks"])
MAX_WOOCOMMERCE_WEBHOOK_BYTES = 1_000_000


def _credentials(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    value = str(raw or "").strip()
    if not value:
        return {}
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if ":" in value:
        consumer_key, consumer_secret = value.split(":", 1)
        return {"consumer_key": consumer_key, "consumer_secret": consumer_secret}
    return {"consumer_key": value}


def _source_host(value: Optional[str]) -> str:
    normalized = normalize_woocommerce_store_url(value)
    return (urlparse(normalized).hostname or "").lower() if normalized else ""


@router.post("/{store_id}")
async def receive_woocommerce_webhook(
    store_id: str,
    request: Request,
    x_wc_webhook_signature: Optional[str] = Header(default=None, alias="X-WC-Webhook-Signature"),
    x_wc_webhook_topic: Optional[str] = Header(default=None, alias="X-WC-Webhook-Topic"),
    x_wc_webhook_delivery_id: Optional[str] = Header(default=None, alias="X-WC-Webhook-Delivery-ID"),
    x_wc_webhook_source: Optional[str] = Header(default=None, alias="X-WC-Webhook-Source"),
):
    raw = await request.body()
    if len(raw) > MAX_WOOCOMMERCE_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="WooCommerce webhook exceeds 1 MB")
    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'woocommerce'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    credentials = _credentials((dict(store).get("api_key") if store else None))
    webhook_secret = str(
        credentials.get("webhook_secret") or credentials.get("consumer_secret") or ""
    ).strip()
    if not store or not webhook_secret or not x_wc_webhook_signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid WooCommerce webhook credentials",
        )
    expected = base64.b64encode(
        hmac.new(webhook_secret.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("ascii")
    if not hmac.compare_digest(expected, x_wc_webhook_signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid WooCommerce webhook credentials",
        )
    store = dict(store)
    supplied_host = _source_host(x_wc_webhook_source)
    expected_host = _source_host(store.get("domain"))
    if supplied_host and expected_host and supplied_host != expected_host:
        raise HTTPException(status_code=401, detail="Invalid WooCommerce webhook source")
    try:
        payload = json.loads(raw or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid WooCommerce webhook JSON") from exc
    try:
        batch = map_woocommerce_webhook(
            payload,
            topic=str(x_wc_webhook_topic or ""),
            delivery_id=x_wc_webhook_delivery_id,
            store_id=store_id,
        )
    except UnsupportedWooCommerceEvent as exc:
        return {"status": "ignored", "platform": "woocommerce", "reason": str(exc)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = await ingest_merchant_event_batch(
        merchant_id=str(store["merchant_id"]),
        batch=batch,
        agent_identity_confidence="platform_asserted",
    )
    return {"status": "recorded", "platform": "woocommerce", **result}
