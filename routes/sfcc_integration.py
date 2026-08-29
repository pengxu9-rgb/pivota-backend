from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Dict, Optional

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


class SalesforceCommerceCloudTelemetryProvisionRequest(BaseModel):
    rotate: bool = False


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


def _authorize(current_user: dict, merchant_id: str) -> None:
    if current_user.get("role") not in {"merchant", "employee", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.get("role") == "merchant" and current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Can only connect your own store")


async def _cas_update_store_credentials(
    *,
    store_id: str,
    merchant_id: str,
    expected_api_key: Any,
    credentials: Dict[str, Any],
    name: Optional[str] = None,
) -> bool:
    updates = "api_key = :api_key"
    values = {
        "api_key": json.dumps(credentials, separators=(",", ":")),
        "expected_api_key": expected_api_key,
        "store_id": store_id,
        "merchant_id": merchant_id,
    }
    if name is not None:
        updates += ", name = :name, status = 'active', connected_at = CURRENT_TIMESTAMP"
        values["name"] = name
    updated = await database.fetch_one(
        f"""
        UPDATE merchant_stores
        SET {updates}
        WHERE store_id = :store_id
          AND merchant_id = :merchant_id
          AND platform = 'salesforce_commerce_cloud'
          AND api_key = :expected_api_key
        RETURNING store_id
        """,
        values,
    )
    return bool(updated)


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
    # The SCAPI origin groups sites in the same realm. Exact store identity is
    # the organization/site pair retained in the credential metadata.
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
    candidates = await database.fetch_all(
        """
        SELECT store_id, api_key FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = 'salesforce_commerce_cloud'
          AND domain = :domain
        """,
        {"merchant_id": body.merchant_id, "domain": domain},
    )
    existing = None
    for candidate in candidates:
        candidate_credentials = _credentials(candidate["api_key"])
        if (
            str(candidate_credentials.get("organization_id") or "").strip()
            == adapter.organization_id
            and str(candidate_credentials.get("site_id") or "").strip()
            == adapter.site_id
        ):
            existing = candidate
            break
    telemetry_signing_secret = ""
    if existing:
        store_id = str(existing["store_id"])
        expected_api_key = existing["api_key"]
        for _ in range(3):
            current_credentials = _credentials(expected_api_key)
            telemetry_signing_secret = str(
                current_credentials.get("telemetry_signing_secret") or ""
            ).strip()
            merged_credentials = dict(credentials)
            if telemetry_signing_secret:
                merged_credentials["telemetry_signing_secret"] = telemetry_signing_secret
            if await _cas_update_store_credentials(
                store_id=store_id,
                merchant_id=body.merchant_id,
                expected_api_key=expected_api_key,
                credentials=merged_credentials,
                name=body.store_name or adapter.site_id,
            ):
                break
            latest = await database.fetch_one(
                """
                SELECT api_key FROM merchant_stores
                WHERE store_id = :store_id
                  AND merchant_id = :merchant_id
                  AND platform = 'salesforce_commerce_cloud'
                """,
                {"store_id": store_id, "merchant_id": body.merchant_id},
            )
            if not latest:
                raise HTTPException(status_code=409, detail="SFCC store changed during reconnect")
            expected_api_key = latest["api_key"]
        else:
            raise HTTPException(status_code=409, detail="SFCC store changed during reconnect")
    else:
        merchant_key = hashlib.sha256(body.merchant_id.encode("utf-8")).hexdigest()[:8]
        site_key = hashlib.sha256(
            f"{adapter.organization_id}:{adapter.site_id}".encode("utf-8")
        ).hexdigest()[:10]
        store_id = f"store_{merchant_key}_sfcc_{site_key}_{int(time.time())}"
        blob = json.dumps(credentials, separators=(",", ":"))
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
        "telemetry_mode": (
            "native_sfcc_cartridge_outbox"
            if telemetry_signing_secret
            else "native_sfcc_cartridge_outbox_available"
        ),
        "telemetry_configured": bool(telemetry_signing_secret),
        "universal_collectors_supported": True,
    }


@router.post("/{store_id}/telemetry/provision")
async def provision_salesforce_commerce_cloud_telemetry(
    store_id: str,
    body: SalesforceCommerceCloudTelemetryProvisionRequest,
    current_user: dict = Depends(get_current_user),
):
    store = await database.fetch_one(
        """
        SELECT store_id, merchant_id, api_key
        FROM merchant_stores
        WHERE store_id = :store_id
          AND platform = 'salesforce_commerce_cloud'
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Connected SFCC store not found")
    store = dict(store)
    _authorize(current_user, str(store.get("merchant_id") or ""))
    credentials = _credentials(store.get("api_key"))
    existing_secret = str(credentials.get("telemetry_signing_secret") or "").strip()
    if existing_secret and not body.rotate:
        return {
            "status": "already_configured",
            "platform": "salesforce_commerce_cloud",
            "store_id": store_id,
            "callback_path": f"/webhooks/salesforce-commerce-cloud/{store_id}",
            "site_id": str(credentials.get("site_id") or ""),
        }
    signing_secret = secrets.token_urlsafe(32)
    credentials["telemetry_signing_secret"] = signing_secret
    updated = await _cas_update_store_credentials(
        store_id=store_id,
        merchant_id=str(store["merchant_id"]),
        expected_api_key=store.get("api_key"),
        credentials=credentials,
    )
    if not updated:
        latest = await database.fetch_one(
            """
            SELECT store_id, merchant_id, api_key
            FROM merchant_stores
            WHERE store_id = :store_id
              AND merchant_id = :merchant_id
              AND platform = 'salesforce_commerce_cloud'
            """,
            {"store_id": store_id, "merchant_id": str(store["merchant_id"])},
        )
        latest_credentials = _credentials(dict(latest).get("api_key") if latest else None)
        if not body.rotate and str(
            latest_credentials.get("telemetry_signing_secret") or ""
        ).strip():
            return {
                "status": "already_configured",
                "platform": "salesforce_commerce_cloud",
                "store_id": store_id,
                "callback_path": f"/webhooks/salesforce-commerce-cloud/{store_id}",
                "site_id": str(latest_credentials.get("site_id") or ""),
            }
        raise HTTPException(
            status_code=409,
            detail="SFCC telemetry credentials changed concurrently; retry",
        )
    return {
        "status": "rotated" if existing_secret else "provisioned",
        "platform": "salesforce_commerce_cloud",
        "store_id": store_id,
        "callback_path": f"/webhooks/salesforce-commerce-cloud/{store_id}",
        "site_id": str(credentials.get("site_id") or ""),
        # Returned only when first generated or explicitly rotated. Pivota stores
        # the secret for verification but cannot recover a lost merchant copy
        # without rotation.
        "signing_secret": signing_secret,
    }
