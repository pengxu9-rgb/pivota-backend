"""
Internal risk APIs for Ops/admin usage.

Scope:
- Disputes/chargebacks: Stripe disputes + Shopify disputes (signals).
- Returns: Shopify returns (signals).

Auth:
- Admin-key protected (X-ADMIN-KEY) using ADMIN_API_KEY (or PROMOTIONS_ADMIN_KEY for compatibility).
"""

import os
import secrets
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from db.database import database
from services.merchant_store_service import get_primary_store
from utils.logger import logger

router = APIRouter(prefix="/agent/internal", tags=["risk"])

def _count_from_row(row: Any) -> int:
    if row is None:
        return 0
    try:
        return int(row["n"])
    except Exception:
        pass
    try:
        return int(dict(row).get("n") or 0)
    except Exception:
        return 0


def _db_error_details(err: Exception) -> Dict[str, Any]:
    msg = str(err or "")
    sqlstate = None
    for obj in (err, getattr(err, "orig", None), getattr(err, "__cause__", None)):
        if not obj:
            continue
        s = getattr(obj, "sqlstate", None)
        if isinstance(s, str) and s:
            sqlstate = s
            break
    return {
        "error_type": err.__class__.__name__,
        "sqlstate": sqlstate,
        "db_error": msg[:800],
    }


def _looks_like_missing_relation(err: Exception, relation: str) -> bool:
    details = _db_error_details(err)
    msg = (details.get("db_error") or "").lower()
    rel = relation.lower()
    if details.get("sqlstate") in {"42P01", "42703"}:
        return True
    if "undefinedtable" in err.__class__.__name__.lower():
        return True
    if "relation" in msg and rel in msg and "does not exist" in msg:
        return True
    if "no such table" in msg and rel in msg:
        return True
    return False


async def require_admin_key(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


@router.get("/disputes", response_model=Dict[str, Any])
async def list_disputes(
    merchantId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    where = []
    params: Dict[str, Any] = {"limit": limit, "offset": offset}

    if merchantId:
        where.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchantId
    if status:
        where.append("status = :status")
        params["status"] = status
    if source:
        where.append("source = :source")
        params["source"] = source

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    count_params = {k: v for k, v in params.items() if k not in {"limit", "offset"}}

    try:
        total_row = await database.fetch_one(
            f"SELECT COUNT(*) AS n FROM dispute_records {where_sql}", count_params
        )
        rows = await database.fetch_all(
            f"""
            SELECT
              merchant_id,
              source,
              source_dispute_id,
              order_id,
              platform_order_id,
              payment_intent_id,
              charge_id,
              currency,
              amount,
              reason,
              status_raw,
              status,
              evidence_due_by,
              opened_at,
              closed_at,
              created_at,
              updated_at
            FROM dispute_records
            {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit OFFSET :offset
            """,
            params,
        )
        return {"items": [dict(r) for r in (rows or [])], "total": _count_from_row(total_row)}
    except Exception as e:
        if _looks_like_missing_relation(e, "dispute_records"):
            return {"items": [], "total": 0, "not_ready": True, "reason": "missing_migration_or_schema"}
        debug_id = secrets.token_hex(8)
        logger.exception("list_disputes failed debug_id=%s err=%s", debug_id, str(e))
        details = _db_error_details(e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "Failed to list disputes",
                "debug_id": debug_id,
                **details,
            },
        )


@router.get("/returns", response_model=Dict[str, Any])
async def list_returns(
    merchantId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    where = []
    params: Dict[str, Any] = {"limit": limit, "offset": offset}

    if merchantId:
        where.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchantId
    if status:
        where.append("status = :status")
        params["status"] = status

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    count_params = {k: v for k, v in params.items() if k not in {"limit", "offset"}}

    try:
        total_row = await database.fetch_one(
            f"SELECT COUNT(*) AS n FROM return_records {where_sql}", count_params
        )
        rows = await database.fetch_all(
            f"""
            SELECT
              merchant_id,
              source,
              source_return_id,
              order_id,
              platform_order_id,
              status_raw,
              status,
              refund_status_raw,
              items_json,
              created_at,
              updated_at
            FROM return_records
            {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit OFFSET :offset
            """,
            params,
        )
        return {"items": [dict(r) for r in (rows or [])], "total": _count_from_row(total_row)}
    except Exception as e:
        if _looks_like_missing_relation(e, "return_records"):
            return {"items": [], "total": 0, "not_ready": True, "reason": "missing_migration_or_schema"}
        debug_id = secrets.token_hex(8)
        logger.exception("list_returns failed debug_id=%s err=%s", debug_id, str(e))
        details = _db_error_details(e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "Failed to list returns",
                "debug_id": debug_id,
                **details,
            },
        )


@router.post("/returns/sync", response_model=Dict[str, Any])
async def sync_returns(
    merchant_id: str = Query(..., alias="merchantId"),
    limit: int = Query(20, ge=1, le=100),
    api_version: str = Query("2024-07", alias="apiVersion"),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """
    Admin-key helper: best-effort sync latest Shopify returns into return_records.
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
            api_version=api_version,
            limit=limit,
        )
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": "SYNC_FAILED", "message": str(e)})
