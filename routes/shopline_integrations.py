from __future__ import annotations

import json
import os
import time
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from adapters.shopline_adapter import (
    DEFAULT_SHOPLINE_API_VERSION,
    ShoplineAdapter,
    build_shopline_domain,
)
from adapters.shoplazza_adapter import (
    DEFAULT_SHOPLAZZA_API_VERSION,
    ShoplazzaAdapter,
)
from db.database import database
from services.store_lifecycle_service import sync_catalog_merchant_status
from services.shopline_family_event_adapter import (
    SUPPORTED_SHOPLAZZA_TOPICS,
    SUPPORTED_SHOPLINE_TOPICS,
)
from services.shopline_family_webhook_auth import resolve_webhook_secret
from services.shopline_family_webhook_subscriptions import (
    WebhookSubscriptionError,
    ensure_shoplazza_subscriptions,
    ensure_shopline_subscriptions,
)
from utils.auth import get_current_user


router = APIRouter(prefix="/integrations", tags=["SHOPLINE and Shoplazza Integrations"])


class ShoplineConnectRequest(BaseModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    handle: str = Field(min_length=1, max_length=128)
    access_token: str = Field(min_length=1, max_length=4096)
    app_secret: Optional[str] = Field(default=None, max_length=4096)
    api_version: str = Field(default=DEFAULT_SHOPLINE_API_VERSION, max_length=32)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    store_name: Optional[str] = Field(default=None, max_length=255)


class ShoplazzaConnectRequest(BaseModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    store_url: str = Field(min_length=1, max_length=512)
    access_token: str = Field(min_length=1, max_length=4096)
    app_secret: Optional[str] = Field(default=None, max_length=4096)
    api_version: str = Field(default=DEFAULT_SHOPLAZZA_API_VERSION, max_length=32)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    store_name: Optional[str] = Field(default=None, max_length=255)


def _authorize(current_user: dict, merchant_id: str) -> None:
    if current_user.get("role") not in {"merchant", "employee", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.get("role") == "merchant" and current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Can only connect your own store")


def _credentials(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _callback_url(platform: str, store_id: str) -> str:
    platform_env = "SHOPLINE_WEBHOOK_BASE_URL"
    if platform == "shoplazza":
        platform_env = "SHOPLAZZA_WEBHOOK_BASE_URL"
    base = str(
        os.getenv(platform_env)
        or os.getenv("PUBLIC_BASE_URL")
        or os.getenv("PIVOTA_BACKEND_BASE_URL")
        or ""
    ).strip().rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=503,
            detail=f"Configure {platform_env} or PUBLIC_BASE_URL as an HTTPS origin",
        )
    callback_url = f"{base}/webhooks/{platform}/{quote(store_id, safe='')}"
    if len(callback_url) > 255:
        raise HTTPException(status_code=503, detail="Webhook callback URL exceeds 255 characters")
    return callback_url


async def _upsert_store(
    *,
    merchant_id: str,
    platform: str,
    domain: str,
    name: str,
    credentials: dict,
) -> str:
    existing = await database.fetch_one(
        """
        SELECT store_id, api_key FROM merchant_stores
        WHERE merchant_id = :merchant_id AND platform = :platform AND domain = :domain
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "platform": platform, "domain": domain},
    )
    if existing:
        raw_existing = dict(existing).get("api_key")
        try:
            parsed_existing = json.loads(str(raw_existing or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_existing = {}
        if isinstance(parsed_existing, dict):
            # A catalog-token rotation must not silently disable an already
            # configured webhook when the merchant omits the app secret.
            credentials = {**parsed_existing, **credentials}
    blob = json.dumps(credentials, separators=(",", ":"))
    if existing:
        store_id = str(existing["store_id"])
        await database.execute(
            """
            UPDATE merchant_stores
            SET api_key = :api_key, name = :name, status = 'active', connected_at = CURRENT_TIMESTAMP
            WHERE store_id = :store_id
            """,
            {"store_id": store_id, "api_key": blob, "name": name},
        )
        return store_id
    store_id = f"store_{merchant_id[:8]}_{platform}_{int(time.time())}"
    await database.execute(
        """
        INSERT INTO merchant_stores
            (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
        VALUES
            (:store_id, :merchant_id, :platform, :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)
        """,
        {
            "store_id": store_id,
            "merchant_id": merchant_id,
            "platform": platform,
            "domain": domain,
            "name": name,
            "api_key": blob,
        },
    )
    return store_id


async def _connected_store(store_id: str, platform: str) -> dict:
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
    if not row:
        raise HTTPException(status_code=404, detail=f"Connected {platform} store not found")
    return dict(row)


async def _ensure_webhooks(store_id: str, platform: str, current_user: dict):
    store = await _connected_store(store_id, platform)
    _authorize(current_user, str(store["merchant_id"]))
    credentials = _credentials(store.get("api_key"))
    if not resolve_webhook_secret(platform, credentials):
        raise HTTPException(
            status_code=409,
            detail=f"{platform} app secret is required before enabling signed webhooks",
        )
    callback_url = _callback_url(platform, store_id)
    access_token = str(credentials.get("access_token") or "").strip()
    api_version = str(credentials.get("api_version") or "").strip()
    if not access_token or not api_version:
        raise HTTPException(status_code=409, detail=f"{platform} API credentials are incomplete")
    try:
        if platform == "shopline":
            result = await ensure_shopline_subscriptions(
                handle=str(credentials.get("handle") or store.get("domain") or ""),
                access_token=access_token,
                api_version=api_version,
                callback_url=callback_url,
                topics=sorted(SUPPORTED_SHOPLINE_TOPICS),
            )
        else:
            result = await ensure_shoplazza_subscriptions(
                store_url=str(store.get("domain") or ""),
                access_token=access_token,
                api_version=api_version,
                callback_url=callback_url,
                topics=sorted(SUPPORTED_SHOPLAZZA_TOPICS),
            )
    except WebhookSubscriptionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "success", "store_id": store_id, **result}


@router.post("/shopline/connect")
async def connect_shopline(
    body: ShoplineConnectRequest,
    current_user: dict = Depends(get_current_user),
):
    _authorize(current_user, body.merchant_id)
    adapter = ShoplineAdapter(body.model_dump())
    valid, error = adapter.validate_config()
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    result = await adapter.test_connection()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=f"SHOPLINE connection failed: {result.get('error')}")
    domain = build_shopline_domain(adapter.handle)
    store_id = await _upsert_store(
        merchant_id=body.merchant_id,
        platform="shopline",
        domain=domain,
        name=body.store_name or result.get("store_name") or domain,
        credentials={
            "access_token": body.access_token,
            "handle": adapter.handle,
            "api_version": adapter.api_version,
            "currency": body.currency.upper(),
            "token_type": "merchant_supplied",
            **({"app_secret": body.app_secret.strip()} if body.app_secret and body.app_secret.strip() else {}),
        },
    )
    await sync_catalog_merchant_status(body.merchant_id, reason="shopline_connect")
    return {
        "status": "success",
        "platform": "shopline",
        "store_id": store_id,
        "catalog_adapter": "native_rest",
        "telemetry_mode": "native_order_webhooks_plus_universal_collectors",
        "webhook_path": f"/webhooks/shopline/{store_id}",
        "webhook_subscription_path": f"/integrations/shopline/{store_id}/webhooks/ensure",
        "required_webhook_topics": [
            "orders/create",
            "orders/paid",
            "orders/cancelled",
            "refunds/create",
        ],
    }


@router.post("/shoplazza/connect")
async def connect_shoplazza(
    body: ShoplazzaConnectRequest,
    current_user: dict = Depends(get_current_user),
):
    _authorize(current_user, body.merchant_id)
    adapter = ShoplazzaAdapter(body.model_dump())
    valid, error = adapter.validate_config()
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    result = await adapter.test_connection()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=f"Shoplazza connection failed: {result.get('error')}")
    store_id = await _upsert_store(
        merchant_id=body.merchant_id,
        platform="shoplazza",
        domain=adapter.store_url,
        name=body.store_name or result.get("store_name") or adapter.store_url,
        credentials={
            "access_token": body.access_token,
            "api_version": adapter.api_version,
            "currency": body.currency.upper(),
            **({"app_secret": body.app_secret.strip()} if body.app_secret and body.app_secret.strip() else {}),
        },
    )
    await sync_catalog_merchant_status(body.merchant_id, reason="shoplazza_connect")
    return {
        "status": "success",
        "platform": "shoplazza",
        "store_id": store_id,
        "catalog_adapter": "native_rest",
        "telemetry_mode": "native_order_webhooks_plus_universal_collectors",
        "webhook_path": f"/webhooks/shoplazza/{store_id}",
        "webhook_subscription_path": f"/integrations/shoplazza/{store_id}/webhooks/ensure",
        "required_webhook_topics": [
            "orders/create",
            "orders/paid",
            "orders/partially_refunded",
            "orders/refunded",
            "orders/cancelled",
        ],
    }


@router.post("/shopline/{store_id}/webhooks/ensure")
async def ensure_shopline_webhooks(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    return await _ensure_webhooks(store_id, "shopline", current_user)


@router.post("/shoplazza/{store_id}/webhooks/ensure")
async def ensure_shoplazza_webhooks(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    return await _ensure_webhooks(store_id, "shoplazza", current_user)
