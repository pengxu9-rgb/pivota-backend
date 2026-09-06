from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request, status

from db.database import database
from services.commerce_interaction_service import (
    order_money_read_modify_write_lock,
    recorded_refund_amount_cents,
)
from services.merchant_event_ingest_service import ingest_merchant_event_batch
from services.telemetry_ingress import current_ingress, telemetry_ingress_route
from services.shopline_family_event_adapter import (
    SHOPLAZZA_REFUND_TOPICS,
    UnsupportedShoplineFamilyEvent,
    map_shopline_webhook,
    map_shoplazza_webhook,
    shoplazza_order_ref,
)
from services.shopline_family_webhook_auth import resolve_webhook_secret


router = APIRouter(prefix="/webhooks", tags=["SHOPLINE and Shoplazza Webhooks"])
MAX_SHOPLINE_FAMILY_WEBHOOK_BYTES = 1_000_000


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


def _host(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    return (urlparse(raw).hostname or "").lower()


def _valid_signature(raw: bytes, signature: Optional[str], secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


async def _store(store_id: str, platform: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = :platform
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id, "platform": platform},
    )
    return dict(row) if row else None


async def _receive(
    *,
    platform: str,
    store_id: str,
    request: Request,
    signature: Optional[str],
    topic: Optional[str],
    delivery_id: Optional[str],
    source_domain: Optional[str],
):
    raw = await request.body()
    if len(raw) > MAX_SHOPLINE_FAMILY_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail=f"{platform} webhook exceeds 1 MB")
    store = await _store(store_id, platform)
    credentials = _credentials((store or {}).get("api_key"))
    app_secret = resolve_webhook_secret(platform, credentials)
    if not store or not _valid_signature(raw, signature, app_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid {platform} webhook credentials",
        )
    ingress = current_ingress(request)
    ingress.identify(merchant_id=store["merchant_id"], store_id=store_id)
    await ingress.enforce_rate_limit("platform", store_id)
    supplied_host = _host(source_domain)
    expected_host = _host(store.get("domain"))
    # The app secret can be shared by every installation of a public app. The
    # signed body alone therefore cannot bind a delivery to this path's store;
    # the platform's required source-domain header must match as well.
    if not supplied_host or not expected_host or supplied_host != expected_host:
        raise HTTPException(status_code=401, detail=f"Invalid {platform} webhook source")
    try:
        payload = json.loads(raw or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {platform} webhook JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"{platform} webhook body must be an object")
    merchant_id = str(store["merchant_id"])
    # Shoplazza's refund deliveries carry only a CUMULATIVE `total_refund_price`
    # and no per-refund identity, so the new money in one delivery is that total
    # minus what this write path has already recorded for the order. The read
    # belongs here, not in the mapper: the mapper stays pure, and the read and
    # the write it feeds are held under one lock.
    refund_order_ref = (
        shoplazza_order_ref(payload)
        if platform == "shoplazza"
        and str(topic or "").strip().lower() in SHOPLAZZA_REFUND_TOPICS
        else None
    )
    if refund_order_ref is None:
        return await _map_and_record(
            platform=platform,
            merchant_id=merchant_id,
            store_id=store_id,
            payload=payload,
            topic=topic,
            delivery_id=delivery_id,
        )
    async with order_money_read_modify_write_lock(
        merchant_id=merchant_id,
        store_id=store_id,
        order_ref=refund_order_ref,
        scope="shoplazza_refund",
    ):
        previously_recorded = await recorded_refund_amount_cents(
            merchant_id=merchant_id,
            store_id=store_id,
            order_ref=refund_order_ref,
            write_path="shoplazza_webhook",
        )
        return await _map_and_record(
            platform=platform,
            merchant_id=merchant_id,
            store_id=store_id,
            payload=payload,
            topic=topic,
            delivery_id=delivery_id,
            previously_recorded_refund_cents=previously_recorded,
        )


async def _map_and_record(
    *,
    platform: str,
    merchant_id: str,
    store_id: str,
    payload: Dict[str, Any],
    topic: Optional[str],
    delivery_id: Optional[str],
    **mapper_kwargs: Any,
):
    try:
        if platform == "shopline":
            batch = map_shopline_webhook(
                payload,
                topic=str(topic or ""),
                delivery_id=delivery_id,
                store_id=store_id,
            )
        else:
            batch = map_shoplazza_webhook(
                payload,
                topic=str(topic or ""),
                delivery_id=delivery_id,
                store_id=store_id,
                **mapper_kwargs,
            )
    except UnsupportedShoplineFamilyEvent as exc:
        return {"status": "ignored", "platform": platform, "reason": str(exc)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = await ingest_merchant_event_batch(
        merchant_id=merchant_id,
        batch=batch,
        agent_identity_confidence="platform_asserted",
        write_path="shopline_webhook" if platform == "shopline" else "shoplazza_webhook",
    )
    return {"status": "recorded", "platform": platform, **result}


@router.post("/shopline/{store_id}")
@telemetry_ingress_route("shopline_webhook")
async def receive_shopline_webhook(
    store_id: str,
    request: Request,
    signature: Optional[str] = Header(default=None, alias="X-Shopline-Hmac-Sha256"),
    topic: Optional[str] = Header(default=None, alias="X-Shopline-Topic"),
    delivery_id: Optional[str] = Header(default=None, alias="X-Shopline-Webhook-Id"),
    source_domain: Optional[str] = Header(default=None, alias="X-Shopline-Shop-Domain"),
):
    return await _receive(
        platform="shopline",
        store_id=store_id,
        request=request,
        signature=signature,
        topic=topic,
        delivery_id=delivery_id,
        source_domain=source_domain,
    )


@router.post("/shoplazza/{store_id}")
@telemetry_ingress_route("shoplazza_webhook")
async def receive_shoplazza_webhook(
    store_id: str,
    request: Request,
    signature: Optional[str] = Header(default=None, alias="X-Shoplazza-Hmac-Sha256"),
    topic: Optional[str] = Header(default=None, alias="X-Shoplazza-Topic"),
    delivery_id: Optional[str] = Header(default=None, alias="X-Shoplazza-Deduplication-ID"),
    source_domain: Optional[str] = Header(default=None, alias="X-Shoplazza-Shop-Domain"),
):
    return await _receive(
        platform="shoplazza",
        store_id=store_id,
        request=request,
        signature=signature,
        topic=topic,
        delivery_id=delivery_id,
        source_domain=source_domain,
    )
