from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from adapters.magento_adapter import MagentoAdapter, normalize_magento_store_url
from db.database import database
from services.store_lifecycle_service import sync_catalog_merchant_status
from utils.auth import get_current_user


router = APIRouter(prefix="/integrations/magento", tags=["Magento Integration"])


class MagentoConnectRequest(BaseModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    store_url: str = Field(min_length=1, max_length=512)
    access_token: str = Field(min_length=1, max_length=2048)
    store_view_code: str = Field(default="default", min_length=1, max_length=64)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    store_name: Optional[str] = Field(default=None, max_length=255)


def _authorize_merchant(current_user: dict, merchant_id: str) -> None:
    if current_user.get("role") not in {"merchant", "employee", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.get("role") == "merchant" and current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Can only connect your own store")


def _credential_blob(body: MagentoConnectRequest, test_result: dict) -> str:
    return json.dumps(
        {
            "access_token": body.access_token,
            "store_view_code": body.store_view_code,
            "currency": str(test_result.get("currency") or body.currency).upper(),
            "product_url_suffix": test_result.get("product_url_suffix"),
            "deployment_type": "paas_or_open_source",
        },
        separators=(",", ":"),
    )


@router.post("/connect")
async def connect_magento(
    body: MagentoConnectRequest,
    current_user: dict = Depends(get_current_user),
):
    """Connect Adobe Commerce PaaS/on-prem or Magento Open Source.

    Adobe Commerce as a Cloud Service uses IMS server-to-server credentials and
    is intentionally not accepted by this Integration Token flow.
    """
    _authorize_merchant(current_user, body.merchant_id)
    adapter = MagentoAdapter(
        {
            "store_url": body.store_url,
            "access_token": body.access_token,
            "store_view_code": body.store_view_code,
        }
    )
    valid, error = adapter.validate_config()
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    test_result = await adapter.test_connection()
    if not test_result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=f"Magento connection failed: {test_result.get('error')}",
        )

    existing = await database.fetch_one(
        """
        SELECT store_id
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = 'magento'
          AND domain = :domain
        LIMIT 1
        """,
        {"merchant_id": body.merchant_id, "domain": adapter.store_url},
    )
    credential_blob = _credential_blob(body, test_result)
    if existing:
        store_id = str(existing["store_id"])
        await database.execute(
            """
            UPDATE merchant_stores
            SET api_key = :api_key,
                name = :name,
                status = 'active',
                connected_at = CURRENT_TIMESTAMP
            WHERE store_id = :store_id
            """,
            {
                "store_id": store_id,
                "api_key": credential_blob,
                "name": body.store_name or test_result.get("store_name") or adapter.store_url,
            },
        )
    else:
        store_id = f"store_{body.merchant_id[:8]}_magento_{int(time.time())}"
        await database.execute(
            """
            INSERT INTO merchant_stores
                (store_id, merchant_id, platform, domain, name, api_key, status, connected_at)
            VALUES
                (:store_id, :merchant_id, 'magento', :domain, :name, :api_key, 'active', CURRENT_TIMESTAMP)
            """,
            {
                "store_id": store_id,
                "merchant_id": body.merchant_id,
                "domain": adapter.store_url,
                "name": body.store_name or test_result.get("store_name") or adapter.store_url,
                "api_key": credential_blob,
            },
        )
    await sync_catalog_merchant_status(body.merchant_id, reason="magento_connect")
    return {
        "status": "success",
        "platform": "magento",
        "store_id": store_id,
        "product_count": int(test_result.get("product_count") or 0),
        "catalog_sync_path": "/products/sync-universal/",
        "telemetry_mode": "universal_web_server_collectors",
        "native_eventing": "adobe_io_events_or_webhooks_follow_up",
    }


@router.get("/{store_id}/status")
async def magento_status(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, domain, status, api_key
        FROM merchant_stores
        WHERE store_id = :store_id AND platform = 'magento'
        """,
        {"store_id": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Magento store not found")
    store = dict(store)
    _authorize_merchant(current_user, str(store["merchant_id"]))
    try:
        credentials = json.loads(store.get("api_key") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        credentials = {}
    return {
        "status": store.get("status"),
        "platform": "magento",
        "store_id": store_id,
        "store_url": normalize_magento_store_url(store.get("domain")),
        "store_view_code": credentials.get("store_view_code") or "default",
        "currency": credentials.get("currency") or "USD",
        "product_url_suffix": credentials.get("product_url_suffix"),
        "deployment_type": credentials.get("deployment_type") or "paas_or_open_source",
        "catalog_adapter": "native_rest",
        "telemetry_mode": "universal_web_server_collectors",
        "canonical_event_path": "/merchant-events/v1/batch",
        "native_eventing": "adobe_io_events_or_webhooks_follow_up",
    }
