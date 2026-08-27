from __future__ import annotations

import json
import time
from typing import Optional

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
from utils.auth import get_current_user


router = APIRouter(prefix="/integrations", tags=["SHOPLINE and Shoplazza Integrations"])


class ShoplineConnectRequest(BaseModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    handle: str = Field(min_length=1, max_length=128)
    access_token: str = Field(min_length=1, max_length=4096)
    api_version: str = Field(default=DEFAULT_SHOPLINE_API_VERSION, max_length=32)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    store_name: Optional[str] = Field(default=None, max_length=255)


class ShoplazzaConnectRequest(BaseModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    store_url: str = Field(min_length=1, max_length=512)
    access_token: str = Field(min_length=1, max_length=4096)
    api_version: str = Field(default=DEFAULT_SHOPLAZZA_API_VERSION, max_length=32)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    store_name: Optional[str] = Field(default=None, max_length=255)


def _authorize(current_user: dict, merchant_id: str) -> None:
    if current_user.get("role") not in {"merchant", "employee", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.get("role") == "merchant" and current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Can only connect your own store")


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
        SELECT store_id FROM merchant_stores
        WHERE merchant_id = :merchant_id AND platform = :platform AND domain = :domain
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "platform": platform, "domain": domain},
    )
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
        },
    )
    await sync_catalog_merchant_status(body.merchant_id, reason="shopline_connect")
    return {
        "status": "success",
        "platform": "shopline",
        "store_id": store_id,
        "catalog_adapter": "native_rest",
        "telemetry_mode": "universal_web_server_collectors",
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
        },
    )
    await sync_catalog_merchant_status(body.merchant_id, reason="shoplazza_connect")
    return {
        "status": "success",
        "platform": "shoplazza",
        "store_id": store_id,
        "catalog_adapter": "native_rest",
        "telemetry_mode": "universal_web_server_collectors",
    }
