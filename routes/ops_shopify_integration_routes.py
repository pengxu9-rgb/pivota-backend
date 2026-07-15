import logging
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db.database import database
from services.merchant_store_service import get_primary_store
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.shopify_integration_verify import register_webhooks_best_effort, verify_shopify_integration
from utils.auth import get_current_employee

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ops/v1", tags=["ops:shopify-integration"])


class OpsVerifyRequest(BaseModel):
    callback_base_url: str = Field(..., min_length=4)
    api_version: Optional[str] = None


class OpsResubscribeRequest(BaseModel):
    callback_base_url: str = Field(..., min_length=4)
    api_version: Optional[str] = None
    topics: Optional[List[str]] = None


class OpsSyncReturnsRequest(BaseModel):
    api_version: Optional[str] = None
    limit: int = Field(20, ge=1, le=100)


class OpsResubscribeAllRequest(BaseModel):
    callback_base_url: str = Field(..., min_length=4)
    api_version: Optional[str] = None
    limit: int = Field(200, ge=1, le=2000)


# Mainline order/uninstall topics for the idempotent sweep. Deliberately mirrors
# _SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS in routes/merchant_store_connections.py.
# Compliance topics (customers/data_request, customers/redact, shop/redact) are
# EXCLUDED — they are toml/dashboard-managed app-config subscriptions and cannot
# be REST-registered (audit fix #5).
_SWEEP_WEBHOOK_TOPICS: List[str] = [
    "orders/create",
    "orders/updated",
    "orders/paid",
    "orders/cancelled",
    "fulfillments/create",
    "fulfillments/update",
    "orders/fulfilled",
    "app/uninstalled",
]


def _decode_scopes_json(value: Any) -> Any:
    """
    Backward-compatible: older versions stored scopes_json as a JSON string.
    Normalize to a dict when possible.
    """
    if isinstance(value, dict) or value is None:
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {"raw": value}
    return {"raw": str(value)}


@router.post("/merchants/{merchant_id}/integrations/shopify/verify")
async def ops_verify_shopify(
    merchant_id: str,
    request: OpsVerifyRequest,
    current_user: dict = Depends(get_current_employee),
):
    """
    Canonical verify endpoint for Ops usage.
    This endpoint can be safely retried and is intended for internal troubleshooting and onboarding support.
    """
    if not request.callback_base_url or not request.callback_base_url.strip():
        raise HTTPException(status_code=400, detail="callback_base_url is required")
    report = await verify_shopify_integration(
        merchant_id=merchant_id,
        callback_base_url=request.callback_base_url,
        api_version=request.api_version or "2025-10",
    )
    return {"status": "success", "report": report, "requested_by": current_user.get("sub")}


@router.get("/merchants/{merchant_id}/integrations/shopify/capability-report/latest")
async def ops_get_latest_shopify_capability_report(
    merchant_id: str,
    current_user: dict = Depends(get_current_employee),
):
    row = await database.fetch_one(
        """
        SELECT merchant_id, shopify_api_version, scopes_json, has_shopify_payments, has_returns_api, last_checked_at
        FROM pcs_merchant_capabilities
        WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="No capability report found for merchant")
    report = dict(row)
    report["scopes_json"] = _decode_scopes_json(report.get("scopes_json"))
    return {"status": "success", "report": report, "requested_by": current_user.get("sub")}


@router.post("/merchants/{merchant_id}/integrations/shopify/resubscribe")
async def ops_resubscribe_shopify_webhooks(
    merchant_id: str,
    request: OpsResubscribeRequest,
    current_user: dict = Depends(get_current_employee),
):
    """
    Ops helper: best-effort webhook (re)subscription for a merchant's primary Shopify store.
    """
    store_info = await get_primary_store(merchant_id)
    if not store_info or (store_info.get("platform") or "").lower() != "shopify":
        raise HTTPException(status_code=400, detail="Primary store is not Shopify")

    shop_domain = store_info.get("domain") or ""
    access_token, _ = await resolve_shopify_admin_access_token(
        shop_domain=shop_domain,
        api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key"),
        store_id=str(store_info.get("store_id") or "").strip() or None,
    )
    access_token = (access_token or "").strip()
    if not shop_domain or not access_token:
        raise HTTPException(status_code=400, detail="Missing Shopify credentials")

    topics = request.topics or [
        "orders/create",
        "orders/updated",
        "orders/paid",
        "orders/cancelled",
        "fulfillments/create",
        "fulfillments/update",
        "orders/fulfilled",
        "refunds/create",
        "tender_transactions/create",
        "disputes/create",
        "disputes/update",
        "returns/create",
        "returns/update",
        # NOTE: compliance topics (customers/data_request, customers/redact, shop/redact)
        # are toml/dashboard-managed app-config subscriptions and CANNOT be REST-registered.
    ]

    report: Dict[str, Any] = {"attempted": True}
    try:
        report.update(
            await register_webhooks_best_effort(
                shop_domain=shop_domain,
                access_token=access_token,
                merchant_id=merchant_id,
                callback_base_url=request.callback_base_url,
                topics=topics,
                api_version=request.api_version or "2025-10",
            )
        )
    except Exception as e:
        logger.warning("Ops resubscribe failed merchant=%s: %s", merchant_id, e)
        raise HTTPException(status_code=500, detail="Failed to resubscribe webhooks")

    return {"status": "success", "webhooks": report, "requested_by": current_user.get("sub")}


@router.post("/integrations/shopify/resubscribe-all")
async def ops_resubscribe_all_shopify_webhooks(
    request: OpsResubscribeAllRequest,
    current_user: dict = Depends(get_current_employee),
):
    """
    Idempotent re-registration sweep over ALL active/connected Shopify stores.

    This backfills webhook subscriptions for stores connected before the connect
    path registered them (audit fix #1b). It is safe to re-run: Shopify REST
    create returns 422 "address has already been taken" for existing hooks, which
    register_webhooks_best_effort reports as already_exists.

    Processes stores sequentially (rate-safety) and NEVER raises on a single
    store's failure — errors are collected per-merchant. Capped by `limit`
    (default 200).
    """
    api_version = (request.api_version or "2025-10").strip() or "2025-10"

    rows = await database.fetch_all(
        """
        SELECT store_id, merchant_id, domain, api_key
        FROM merchant_stores
        WHERE platform = 'shopify'
          AND lower(COALESCE(status, '')) IN ('active', 'connected')
        ORDER BY connected_at DESC NULLS LAST
        LIMIT :limit
        """,
        {"limit": request.limit},
    )

    results: List[Dict[str, Any]] = []
    for row in rows or []:
        store = dict(row)
        merchant_id = store.get("merchant_id")
        shop_domain = (store.get("domain") or "").strip()
        store_id = str(store.get("store_id") or "").strip() or None
        summary: Dict[str, Any] = {
            "merchant_id": merchant_id,
            "shop_domain": shop_domain,
            "created": [],
            "already_exists": [],
            "failed": [],
            "error": None,
        }
        try:
            access_token, _meta = await resolve_shopify_admin_access_token(
                shop_domain=shop_domain,
                api_key_raw=store.get("api_key"),
                store_id=store_id,
            )
            access_token = (access_token or "").strip()
            if not shop_domain or not access_token:
                summary["error"] = "missing_credentials"
                results.append(summary)
                continue

            report = await register_webhooks_best_effort(
                shop_domain=shop_domain,
                access_token=access_token,
                merchant_id=merchant_id,
                callback_base_url=request.callback_base_url,
                topics=_SWEEP_WEBHOOK_TOPICS,
                api_version=api_version,
            )
            summary["created"] = report.get("created") or []
            summary["already_exists"] = report.get("already_exists") or []
            summary["failed"] = report.get("failed") or []
        except Exception as e:  # never let one store abort the sweep
            logger.warning("Resubscribe-all failed merchant=%s shop=%s: %s", merchant_id, shop_domain, str(e)[:200])
            summary["error"] = str(e)[:300]
        results.append(summary)

    return {
        "status": "success",
        "processed": len(results),
        "results": results,
        "requested_by": current_user.get("sub"),
    }


@router.post("/merchants/{merchant_id}/integrations/shopify/returns/sync")
async def ops_sync_shopify_returns(
    merchant_id: str,
    request: OpsSyncReturnsRequest,
    current_user: dict = Depends(get_current_employee),
):
    """
    Ops helper: fetch latest Shopify returns via Admin GraphQL and upsert to return_records.
    This is a pragmatic bridge when Returns webhooks are unavailable or not yet enabled.
    """
    store_info = await get_primary_store(merchant_id)
    if not store_info or (store_info.get("platform") or "").lower() != "shopify":
        raise HTTPException(status_code=400, detail="Primary store is not Shopify")

    shop_domain = store_info.get("domain") or ""
    access_token = store_info.get("api_key") or ""
    if not shop_domain or not access_token:
        raise HTTPException(status_code=400, detail="Missing Shopify credentials")

    try:
        from services.shopify_returns_service import sync_shopify_returns_best_effort

        result = await sync_shopify_returns_best_effort(
            merchant_id=merchant_id,
            shop_domain=shop_domain,
            access_token=access_token,
            api_version=request.api_version or "2025-10",
            limit=request.limit,
        )
    except Exception as e:
        logger.warning("Ops returns sync failed merchant=%s: %s", merchant_id, e)
        raise HTTPException(status_code=500, detail="Failed to sync returns")

    return {"status": "success", "result": result, "requested_by": current_user.get("sub")}
