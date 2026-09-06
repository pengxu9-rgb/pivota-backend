"""
Admin endpoints to reset merchant-related data with safety (backup + confirm)
"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_admin_or_key
from pydantic import BaseModel, Field
from typing import List, Optional
from db.database import database
import datetime
import logging

logger = logging.getLogger(__name__)
# AUTHENTICATION. Every route on this router was reachable with NO credentials
# of any kind: no Depends, no header check, no role check. The guard is applied
# at the ROUTER, not per-handler, so a route added here later inherits it
# instead of having to remember it -- which is how this file got here.
# require_admin_or_key accepts an X-ADMIN-KEY header or an admin/super_admin
# JWT and fails closed (401) when neither is present.
#
# POST /admin/merchants/reset-all was an anonymous bulk merchant reset.
router = APIRouter(prefix="/admin/merchants", tags=["admin-merchants-reset"], dependencies=[Depends(require_admin_or_key)])

class ResetRequest(BaseModel):
    confirm: str = Field(..., description="Type EXACTLY: DELETE ALL MERCHANT DATA")
    scope: str = Field(
        default="cache_and_stores",
        description="Reset scope: cache_only | cache_and_stores | all_orders_and_merchants",
    )
    merchant_ids: Optional[List[str]] = Field(
        default=None, description="Optional explicit list; if omitted applies to all"
    )
    backup: bool = Field(default=True, description="Whether to create backup tables before delete")

@router.post("/reset-all")
async def reset_all_merchants(payload: ResetRequest):
    if payload.confirm != "DELETE ALL MERCHANT DATA":
        raise HTTPException(status_code=400, detail="Confirmation phrase mismatch")

    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    try:
        where_clause = ""
        params = {}
        if payload.merchant_ids:
            where_clause = " WHERE merchant_id = ANY(:mids)"
            params = {"mids": payload.merchant_ids}

        # Backup tables
        backups = []
        if payload.backup:
            try:
                # products_cache backup
                await database.execute(
                    f"CREATE TABLE IF NOT EXISTS backup_products_cache_{ts} AS SELECT * FROM products_cache"
                )
                backups.append(f"backup_products_cache_{ts}")
            except Exception as e:
                logger.warning(f"Backup products_cache failed: {e}")
            try:
                await database.execute(
                    f"CREATE TABLE IF NOT EXISTS backup_merchant_stores_{ts} AS SELECT * FROM merchant_stores"
                )
                backups.append(f"backup_merchant_stores_{ts}")
            except Exception as e:
                logger.warning(f"Backup merchant_stores failed: {e}")
            try:
                await database.execute(
                    f"CREATE TABLE IF NOT EXISTS backup_orders_{ts} AS SELECT * FROM orders"
                )
                backups.append(f"backup_orders_{ts}")
            except Exception as e:
                logger.warning(f"Backup orders failed: {e}")
            try:
                await database.execute(
                    f"CREATE TABLE IF NOT EXISTS backup_merchant_onboarding_{ts} AS SELECT * FROM merchant_onboarding"
                )
                backups.append(f"backup_merchant_onboarding_{ts}")
            except Exception as e:
                logger.warning(f"Backup merchant_onboarding failed: {e}")

        # Execute deletions based on scope
        deleted = {}
        if payload.scope in ("cache_only", "cache_and_stores", "all_orders_and_merchants"):
            q = f"DELETE FROM products_cache{where_clause}"
            await database.execute(q, params)
            deleted["products_cache"] = "deleted"
        if payload.scope in ("cache_and_stores", "all_orders_and_merchants"):
            q = f"DELETE FROM merchant_stores{where_clause}"
            await database.execute(q, params)
            deleted["merchant_stores"] = "deleted"
        if payload.scope == "all_orders_and_merchants":
            q = f"DELETE FROM orders{where_clause}"
            await database.execute(q, params)
            deleted["orders"] = "deleted"
            q = f"DELETE FROM merchant_onboarding{where_clause}"
            await database.execute(q, params)
            deleted["merchant_onboarding"] = "deleted"

        return {
            "success": True,
            "message": "Merchant data reset completed",
            "scope": payload.scope,
            "merchant_ids": payload.merchant_ids or "ALL",
            "backups": backups,
            "deleted": deleted,
        }
    except Exception as e:
        logger.error(f"Reset failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))




