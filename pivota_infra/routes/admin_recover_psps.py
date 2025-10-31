"""
Admin endpoint to recover missing PSP configurations
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from db.database import database
from datetime import datetime
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/recover", tags=["admin-recovery"])

class PSPToAdd(BaseModel):
    provider: str = Field(..., description="PSP provider: stripe, adyen, checkout, paypal, etc.")
    name: Optional[str] = None
    account_id: Optional[str] = None
    api_key: Optional[str] = None
    capabilities: Optional[str] = "card,bank_transfer"
    status: str = "active"

class RecoverPSPsRequest(BaseModel):
    merchant_id: str
    psps: List[PSPToAdd]
    fix_order_associations: bool = Field(default=True, description="Also fix orders table to link to PSPs")

@router.post("/psps")
async def recover_psps(payload: RecoverPSPsRequest):
    """
    Recover/re-add missing PSP configurations for a merchant
    Optionally fixes orders table to associate orphaned orders with correct PSPs
    """
    try:
        added = []
        errors = []
        
        for psp in payload.psps:
            try:
                psp_id = f"psp_{psp.provider}_{payload.merchant_id[:8]}"
                
                # Insert or update PSP
                await database.execute("""
                    INSERT INTO merchant_psps 
                    (psp_id, merchant_id, provider, name, account_id, api_key, capabilities, status, connected_at)
                    VALUES (:psp_id, :merchant_id, :provider, :name, :account_id, :api_key, :capabilities, :status, NOW())
                    ON CONFLICT (psp_id) 
                    DO UPDATE SET
                        name = :name,
                        account_id = :account_id,
                        api_key = :api_key,
                        capabilities = :capabilities,
                        status = :status,
                        connected_at = NOW()
                """, {
                    "psp_id": psp_id,
                    "merchant_id": payload.merchant_id,
                    "provider": psp.provider.lower(),
                    "name": psp.name or f"{psp.provider.title()} Account",
                    "account_id": psp.account_id,
                    "api_key": psp.api_key,
                    "capabilities": psp.capabilities,
                    "status": psp.status
                })
                
                added.append({
                    "psp_id": psp_id,
                    "provider": psp.provider,
                    "name": psp.name or f"{psp.provider.title()} Account"
                })
                
            except Exception as e:
                logger.error(f"Failed to add PSP {psp.provider}: {e}")
                errors.append(f"{psp.provider}: {str(e)}")
        
        # Fix order associations if requested
        orders_fixed = 0
        if payload.fix_order_associations:
            try:
                # Get the primary PSP (first active one or Stripe as default)
                primary_psp = await database.fetch_one("""
                    SELECT psp_id FROM merchant_psps
                    WHERE merchant_id = :merchant_id
                    AND status = 'active'
                    ORDER BY 
                        CASE WHEN provider = 'stripe' THEN 1 ELSE 2 END,
                        connected_at DESC
                    LIMIT 1
                """, {"merchant_id": payload.merchant_id})
                
                if primary_psp:
                    # Update orders with NULL or invalid psp_id
                    result = await database.execute("""
                        UPDATE orders
                        SET psp_id = :psp_id
                        WHERE merchant_id = :merchant_id
                        AND (psp_id IS NULL OR psp_id NOT IN (
                            SELECT psp_id FROM merchant_psps WHERE merchant_id = :merchant_id
                        ))
                    """, {
                        "psp_id": primary_psp["psp_id"],
                        "merchant_id": payload.merchant_id
                    })
                    orders_fixed = result if result else 0
                    logger.info(f"Fixed {orders_fixed} orders with primary PSP {primary_psp['psp_id']}")
                    
            except Exception as e:
                logger.error(f"Failed to fix order associations: {e}")
                errors.append(f"Order fix: {str(e)}")
        
        return {
            "success": True,
            "merchant_id": payload.merchant_id,
            "psps_added": len(added),
            "psps": added,
            "orders_fixed": orders_fixed,
            "errors": errors if errors else None
        }
        
    except Exception as e:
        logger.error(f"PSP recovery failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/check-orphaned-orders/{merchant_id}")
async def check_orphaned_orders(merchant_id: str):
    """
    Check for orders with missing or invalid psp_id references
    """
    try:
        # Count orders with NULL psp_id
        null_count = await database.fetch_one("""
            SELECT COUNT(*) as count
            FROM orders
            WHERE merchant_id = :merchant_id
            AND psp_id IS NULL
        """, {"merchant_id": merchant_id})
        
        # Count orders with invalid psp_id (not in merchant_psps)
        invalid_count = await database.fetch_one("""
            SELECT COUNT(*) as count
            FROM orders
            WHERE merchant_id = :merchant_id
            AND psp_id IS NOT NULL
            AND psp_id NOT IN (
                SELECT psp_id FROM merchant_psps WHERE merchant_id = :merchant_id
            )
        """, {"merchant_id": merchant_id})
        
        # Total orders
        total_count = await database.fetch_one("""
            SELECT COUNT(*) as count
            FROM orders
            WHERE merchant_id = :merchant_id
        """, {"merchant_id": merchant_id})
        
        return {
            "merchant_id": merchant_id,
            "total_orders": total_count["count"] if total_count else 0,
            "orders_with_null_psp": null_count["count"] if null_count else 0,
            "orders_with_invalid_psp": invalid_count["count"] if invalid_count else 0,
            "orphaned_total": (null_count["count"] if null_count else 0) + (invalid_count["count"] if invalid_count else 0)
        }
        
    except Exception as e:
        logger.error(f"Check orphaned orders failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

