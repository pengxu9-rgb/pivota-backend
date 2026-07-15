import logging
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db.database import database
from services.shopify_integration_verify import verify_shopify_integration
from utils.auth import can_access_merchant, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/merchant/v1", tags=["merchant:onboarding-shopify"])


class MerchantVerifyRequest(BaseModel):
    callback_base_url: str = Field(..., min_length=4)
    api_version: Optional[str] = None


def _decode_scopes_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
            return {"raw": parsed}
        except Exception:
            return {"raw": value}
    return {}


def _redact_ops_webhook_report(webhooks: Dict[str, Any]) -> Dict[str, Any]:
    failed = []
    for item in (webhooks or {}).get("failed") or []:
        if not isinstance(item, dict):
            continue
        failed.append({"topic": item.get("topic"), "status_code": item.get("status_code")})
    return {
        "attempted": bool((webhooks or {}).get("attempted")),
        "created_topics": [x.get("topic") for x in (webhooks or {}).get("created") or [] if isinstance(x, dict)],
        "already_exists": (webhooks or {}).get("already_exists") or [],
        "failed": failed,
    }


def _redact_policy_report(policies: Dict[str, Any]) -> Dict[str, Any]:
    latest_hashes = []
    for row in (policies or {}).get("latest_hashes") or []:
        if not isinstance(row, dict):
            continue
        latest_hashes.append(
            {
                "policy_type": row.get("policy_type"),
                "url": row.get("url"),
                "updated_at": row.get("updated_at"),
                "hash_sha256": row.get("hash_sha256"),
            }
        )
    return {
        "latest_hashes": latest_hashes,
        "fetched_types": (policies or {}).get("fetched_types") or [],
        "policies_count": (((policies or {}).get("policies_rest_diag") or {}).get("summary") or {}).get("items_count"),
    }


@router.post("/merchants/{merchant_id}/onboarding/shopify/verify")
async def merchant_onboarding_verify_shopify(
    merchant_id: str,
    request: MerchantVerifyRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Merchant-facing onboarding facade for Shopify integration verify.
    Delegates to the canonical verify service but returns a redacted report (no raw bodies/error payloads).
    """
    if current_user.get("role") not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    if not can_access_merchant(current_user, merchant_id):
        raise HTTPException(status_code=403, detail="Can only verify your own merchant")

    report = await verify_shopify_integration(
        merchant_id=merchant_id,
        callback_base_url=request.callback_base_url,
        api_version=request.api_version or "2025-10",
    )

    redacted = {
        "run_id": report.get("run_id"),
        "checked_at": report.get("checked_at"),
        "shop": report.get("shop"),
        "scopes": {
            "missing_required": ((report.get("scopes") or {}).get("missing_required")) or [],
            "missing_optional": ((report.get("scopes") or {}).get("missing_optional")) or [],
        },
        "webhooks": _redact_ops_webhook_report(report.get("webhooks") or {}),
        "policies": _redact_policy_report(report.get("policies") or {}),
        "capabilities": report.get("capabilities") or {},
    }
    return {"status": "success", "report": redacted}


@router.get("/merchants/{merchant_id}/onboarding/shopify/capability-report/latest")
async def merchant_get_latest_shopify_capability_report(
    merchant_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Merchant-facing read-only: return the latest stored capability snapshot (redacted).
    """
    if current_user.get("role") not in ["merchant", "employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    if not can_access_merchant(current_user, merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for merchant")

    row = await database.fetch_one(
        """
        SELECT scopes_json, has_shopify_payments, has_returns_api, last_checked_at
        FROM pcs_merchant_capabilities
        WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="No capability report found for merchant")

    scopes_json = _decode_scopes_json(dict(row).get("scopes_json"))
    return {
        "status": "success",
        "report": {
            "run_id": scopes_json.get("run_id"),
            "checked_at": scopes_json.get("checked_at"),
            "missing_required_scopes": scopes_json.get("missing_required_scopes") or [],
            "missing_optional_scopes": scopes_json.get("missing_optional_scopes") or [],
            "has_shopify_payments": dict(row).get("has_shopify_payments"),
            "has_returns_api": dict(row).get("has_returns_api"),
            "last_checked_at": dict(row).get("last_checked_at").isoformat() if dict(row).get("last_checked_at") else None,
        },
    }
