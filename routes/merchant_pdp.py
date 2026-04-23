from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db.database import database
from services.pdp_governance_service import (
    DEFAULT_MARKET,
    create_merchant_contribution,
    ensure_pdp_governance_tables,
    get_pdp_projection,
    parse_product_key,
)
from utils.auth import get_current_user


router = APIRouter(prefix="/merchant/pdps", tags=["merchant-pdp-governance"])


class MerchantContributionRequest(BaseModel):
    module_key: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None
    market: str = DEFAULT_MARKET


def _merchant_id(current_user: Dict[str, Any]) -> str:
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=403, detail="MERCHANT_REQUIRED")
    return str(merchant_id)


def _map_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if message in {"PDP_NOT_FOUND", "PDP_MODULE_VERSION_NOT_FOUND"}:
        return HTTPException(status_code=404, detail=message)
    if message in {"INVALID_PRODUCT_KEY", "INVALID_PDP_MODULE", "PDP_RESOLUTION_REQUIRES_PRODUCT_KEY_OR_SEED"}:
        return HTTPException(status_code=400, detail=message)
    if message == "MERCHANT_PRODUCT_FORBIDDEN":
        return HTTPException(status_code=403, detail=message)
    return HTTPException(status_code=500, detail=message[:300])


@router.get("/product/{platform}/{platform_product_id}")
async def get_product_pdp_status(
    platform: str,
    platform_product_id: str,
    market: str = Query(default=DEFAULT_MARKET),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    merchant_id = _merchant_id(current_user)
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    try:
        projection = await get_pdp_projection(product_key=product_key, market=market)
        await ensure_pdp_governance_tables()
        rows = await database.fetch_all(
            """
            SELECT id, pdp_id, product_key, merchant_id, module_key, status,
                   reviewed_by_actor_type, reviewed_by_actor_id, review_decision,
                   review_notes, notes, created_at, updated_at
            FROM merchant_pdp_contributions
            WHERE merchant_id = :merchant_id
              AND product_key = :product_key
            ORDER BY created_at DESC
            LIMIT 50
            """,
            {"merchant_id": merchant_id, "product_key": product_key},
        )
        return {
            "status": "success",
            "product_key": product_key,
            "pdp": projection["pdp"],
            "modules": projection["modules"],
            "published_payload": projection["published_payload"],
            "contributions": [
                {
                    "id": row["id"],
                    "pdp_id": row["pdp_id"],
                    "product_key": row["product_key"],
                    "module_key": row["module_key"],
                    "status": row["status"],
                    "reviewed_by_actor_type": row["reviewed_by_actor_type"],
                    "reviewed_by_actor_id": row["reviewed_by_actor_id"],
                    "review_decision": row["review_decision"],
                    "review_notes": row["review_notes"],
                    "notes": row["notes"],
                    "created_at": str(row["created_at"]) if row["created_at"] else None,
                    "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
                }
                for row in rows
            ],
        }
    except Exception as exc:
        raise _map_error(exc)


@router.post("/product/{platform}/{platform_product_id}/contributions")
async def submit_product_pdp_contribution(
    platform: str,
    platform_product_id: str,
    body: MerchantContributionRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    merchant_id = _merchant_id(current_user)
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    try:
        parse_product_key(product_key)
        return await create_merchant_contribution(
            product_key=product_key,
            merchant_id=merchant_id,
            module_key=body.module_key,
            payload=body.payload,
            notes=body.notes,
            market=body.market,
        )
    except Exception as exc:
        raise _map_error(exc)
