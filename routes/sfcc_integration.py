from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from adapters.sfcc_adapter import (
    SalesforceCommerceCloudAdapter,
    build_sfcc_api_origin,
    normalize_sfcc_storefront_url,
)
from db.database import database
from services.store_lifecycle_service import sync_catalog_merchant_status
from utils.auth import get_current_user


router = APIRouter(
    prefix="/integrations/salesforce-commerce-cloud",
    tags=["Salesforce Commerce Cloud Integration"],
)


class SalesforceCommerceCloudConnectRequest(BaseModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    short_code: str = Field(min_length=2, max_length=128)
    organization_id: str = Field(min_length=2, max_length=128)
    site_id: str = Field(min_length=2, max_length=128)
    client_id: str = Field(min_length=2, max_length=256)
    client_secret: str = Field(min_length=1, max_length=4096)
    storefront_url: Optional[str] = Field(default=None, max_length=512)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    locale: Optional[str] = Field(default=None, max_length=32)
    store_name: Optional[str] = Field(default=None, max_length=255)


def _authorize(current_user: dict, merchant_id: str) -> None:
    if current_user.get("role") not in {"merchant", "employee", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.get("role") == "merchant" and current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Can only connect your own store")


@router.post("/connect")
async def connect_salesforce_commerce_cloud(
    body: SalesforceCommerceCloudConnectRequest,
    current_user: dict = Depends(get_current_user),
):
    _authorize(current_user, body.merchant_id)
    adapter = SalesforceCommerceCloudAdapter(body.model_dump())
    valid, error = adapter.validate_config()
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    result = await adapter.test_connection()
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=f"Salesforce Commerce Cloud connection failed: {result.get('error')}",
        )
    api_origin = build_sfcc_api_origin(adapter.short_code)
    storefront_url = normalize_sfcc_storefront_url(body.storefront_url)
    # The SCAPI origin is the stable connection identity. Storefront vanity
    # domains can change independently and stay in the credential metadata.
    domain = api_origin
    credentials = {
        "short_code": adapter.short_code,
        "organization_id": adapter.organization_id,
        "site_id": adapter.site_id,
        "client_id": adapter.client_id,
        "client_secret": body.client_secret,
        "storefront_url": storefront_url or None,
        "currency": body.currency.upper(),
        "locale": str(body.locale or "").strip() or None,
        "auth_mode": "slas_private_client",
    }
    existing = await database.fetch_one(
        """
        SELECT store_id FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = 'salesforce_commerce_cloud'
          AND domain = :domain
        LIMIT 1
        """,
        {"merchant_id": body.merchant_id, "domain": domain},
    )
    blob = json.dumps(credentials, separators=(",", ":"))
    if existing:
        store_id = str(existing["store_id"])
        await database.execute(
            """
            UPDATE merchant_stores
            SET api_key = :api_key, name = :name, status = 'active',
                connected_at = CURRENT_TIMESTAMP
            WHERE store_id = :store_id
            """,
            {
                "store_id": store_id,
                "api_key": blob,
                "name": body.store_name or adapter.site_id,
            },
        )
    else:
        store_id = (
            f"store_{body.merchant_id[:8]}_salesforce_commerce_cloud_{int(time.time())}"
        )
        await database.execute(
            """
            INSERT INTO merchant_stores
                (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
            VALUES
                (:store_id, :merchant_id, 'salesforce_commerce_cloud', :domain,
                 :name, :api_key, 'active', CURRENT_TIMESTAMP)
            """,
            {
                "store_id": store_id,
                "merchant_id": body.merchant_id,
                "domain": domain,
                "name": body.store_name or adapter.site_id,
                "api_key": blob,
            },
        )
    await sync_catalog_merchant_status(
        body.merchant_id,
        reason="salesforce_commerce_cloud_connect",
    )
    return {
        "status": "success",
        "platform": "salesforce_commerce_cloud",
        "store_id": store_id,
        "catalog_adapter": "native_scapi_shopper_search_products",
        "telemetry_mode": "universal_web_server_collectors",
    }
