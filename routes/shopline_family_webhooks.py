from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request, status

from db.database import database
from services.merchant_event_ingest_service import ingest_merchant_event_batch
from services.shopline_family_event_adapter import (
    UnsupportedShoplineFamilyEvent,
    map_shopline_webhook,
    map_shoplazza_webhook,
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
    try:
        mapper = map_shopline_webhook if platform == "shopline" else map_shoplazza_webhook
        batch = mapper(
            payload,
            topic=str(topic or ""),
            delivery_id=delivery_id,
            store_id=store_id,
        )
    except UnsupportedShoplineFamilyEvent as exc:
        return {"status": "ignored", "platform": platform, "reason": str(exc)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = await ingest_merchant_event_batch(
        merchant_id=str(store["merchant_id"]),
        batch=batch,
        agent_identity_confidence="platform_asserted",
    )
    return {"status": "recorded", "platform": platform, **result}


@router.post("/shopline/{store_id}")
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
