from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

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
    adobe_io_client_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    adobe_io_provider_id: Optional[str] = Field(default=None, min_length=1, max_length=128)

    @field_validator("adobe_io_client_id", "adobe_io_provider_id", mode="before")
    @classmethod
    def strip_adobe_io_identifiers(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("Adobe I/O identifiers cannot be blank")
        return normalized

    @field_validator("adobe_io_provider_id")
    @classmethod
    def validate_adobe_io_provider_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        raw = value[9:] if value.lower().startswith("urn:uuid:") else value
        try:
            return str(uuid.UUID(raw))
        except ValueError as exc:
            raise ValueError("adobe_io_provider_id must be a UUID or urn:uuid UUID") from exc

    @model_validator(mode="after")
    def require_complete_adobe_io_binding(self) -> "MagentoConnectRequest":
        if bool(self.adobe_io_client_id) != bool(self.adobe_io_provider_id):
            raise ValueError(
                "adobe_io_client_id and adobe_io_provider_id must be provided together"
            )
        return self


def _authorize_merchant(current_user: dict, merchant_id: str) -> None:
    if current_user.get("role") not in {"merchant", "employee", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.get("role") == "merchant" and current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Can only connect your own store")


def _parse_credentials(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _credential_blob(
    body: MagentoConnectRequest,
    test_result: dict,
    *,
    existing_credentials: Optional[Dict[str, Any]] = None,
) -> str:
    adobe_io_client_id = body.adobe_io_client_id
    if adobe_io_client_id is None:
        adobe_io_client_id = str(
            (existing_credentials or {}).get("adobe_io_client_id") or ""
        ).strip() or None
    provider_source = (
        f"urn:uuid:{body.adobe_io_provider_id}" if body.adobe_io_provider_id else None
    )
    if provider_source is None:
        provider_source = str(
            (existing_credentials or {}).get("adobe_io_provider_source") or ""
        ).strip().lower() or None
    return json.dumps(
        {
            "access_token": body.access_token,
            "store_view_code": body.store_view_code,
            "currency": str(test_result.get("currency") or body.currency).upper(),
            "product_url_suffix": test_result.get("product_url_suffix"),
            "deployment_type": "paas_or_open_source",
            **({"adobe_io_client_id": adobe_io_client_id.strip()} if adobe_io_client_id else {}),
            **({"adobe_io_provider_source": provider_source} if provider_source else {}),
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
        SELECT store_id, api_key
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND platform = 'magento'
          AND domain = :domain
        LIMIT 1
        """,
        {"merchant_id": body.merchant_id, "domain": adapter.store_url},
    )
    existing_credentials = _parse_credentials(dict(existing).get("api_key")) if existing else {}
    credential_blob = _credential_blob(
        body,
        test_result,
        existing_credentials=existing_credentials,
    )
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
    persisted_credentials = _parse_credentials(credential_blob)
    adobe_io_configured = bool(
        persisted_credentials.get("adobe_io_client_id")
        and persisted_credentials.get("adobe_io_provider_source")
    )
    await sync_catalog_merchant_status(body.merchant_id, reason="magento_connect")
    return {
        "status": "success",
        "platform": "magento",
        "store_id": store_id,
        "product_count": int(test_result.get("product_count") or 0),
        "catalog_sync_path": "/products/sync-universal/",
        "telemetry_mode": (
            "adobe_io_events_plus_universal_collectors"
            if adobe_io_configured
            else "universal_web_server_collectors"
        ),
        "native_eventing": (
            "adobe_io_events_configured"
            if adobe_io_configured
            else "not_configured"
        ),
        "adobe_io_webhook_path": f"/webhooks/adobe-commerce/{store_id}",
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
    adobe_io_configured = bool(
        str(credentials.get("adobe_io_client_id") or "").strip()
        and str(credentials.get("adobe_io_provider_source") or "").strip()
    )
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
        "telemetry_mode": (
            "adobe_io_events_plus_universal_collectors"
            if adobe_io_configured
            else "universal_web_server_collectors"
        ),
        "canonical_event_path": "/merchant-events/v1/batch",
        "native_eventing": (
            "adobe_io_events_configured" if adobe_io_configured else "not_configured"
        ),
        "adobe_io_webhook_path": f"/webhooks/adobe-commerce/{store_id}",
        "adobe_io_client_id_configured": adobe_io_configured,
        "adobe_io_provider_configured": adobe_io_configured,
    }
