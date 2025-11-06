"""
Admin endpoint to fix incorrect PSP ID formats in database

Issue: Some psp_ids are formatted as psp_xxxxxxxxxxxx instead of psp_{provider}_xxxxxxxxxxxx
This violates the check_psp_id_format constraint
"""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from utils.auth import get_current_user
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/fix-psp-format", tags=["admin-fixes"])

@router.post("/execute")
async def fix_psp_format(current_user: dict = Depends(get_current_user)):
    """Fix all incorrectly formatted psp_ids in the database"""
    if current_user["role"] not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # 1. Find all incorrect PSP IDs
        wrong_psps = await database.fetch_all("""
            SELECT psp_id, merchant_id, provider
            FROM merchant_psp_configs
            WHERE psp_id !~ '^psp_[a-z0-9]+_[a-z0-9]{12}$'
        """)
        
        if not wrong_psps:
            return {
                "status": "success",
                "message": "No PSP IDs need fixing",
                "fixed_count": 0
            }
        
        fixed_psps = []
        fixed_orders = 0
        
        for psp in wrong_psps:
            old_id = psp['psp_id']
            provider = psp['provider']
            merchant_id = psp['merchant_id']
            
            # Extract suffix (remove psp_ prefix)
            suffix = old_id[4:] if old_id.startswith('psp_') else old_id
            
            # Generate new ID with provider
            new_id = f"psp_{provider}_{suffix}"
            
            logger.info(f"Fixing PSP ID: {old_id} → {new_id}")
            
            # Update merchant_psp_configs
            await database.execute("""
                UPDATE merchant_psp_configs
                SET psp_id = :new_id
                WHERE psp_id = :old_id
                    AND merchant_id = :merchant_id
            """, {"new_id": new_id, "old_id": old_id, "merchant_id": merchant_id})
            
            # Update orders table
            orders_updated = await database.execute("""
                UPDATE orders
                SET psp_id = :new_id
                WHERE psp_id = :old_id
            """, {"new_id": new_id, "old_id": old_id})
            
            fixed_psps.append({
                "old_id": old_id,
                "new_id": new_id,
                "provider": provider,
                "merchant_id": merchant_id
            })
            
            fixed_orders += orders_updated if orders_updated else 0
        
        logger.info(f"Fixed {len(fixed_psps)} PSP IDs and updated {fixed_orders} orders")
        
        return {
            "status": "success",
            "message": f"Fixed {len(fixed_psps)} PSP IDs",
            "fixed_count": len(fixed_psps),
            "orders_updated": fixed_orders,
            "details": fixed_psps
        }
    
    except Exception as e:
        logger.error(f"Failed to fix PSP formats: {e}")
        raise HTTPException(status_code=500, detail=f"Fix failed: {str(e)}")

@router.get("/check")
async def check_psp_format(current_user: dict = Depends(get_current_user)):
    """Check for PSP IDs that don't match the required format"""
    if current_user["role"] not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    try:
        # Count PSPs by format
        stats = await database.fetch_one("""
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN psp_id ~* '^psp_[a-z0-9]+_[a-z0-9]{12}$' THEN 1 END) as correct_format,
                COUNT(CASE WHEN psp_id !~ '^psp_[a-z0-9]+_[a-z0-9]{12}$' THEN 1 END) as wrong_format
            FROM merchant_psp_configs
        """)
        
        # Get examples of wrong format
        wrong_examples = await database.fetch_all("""
            SELECT psp_id, provider, merchant_id
            FROM merchant_psp_configs
            WHERE psp_id !~ '^psp_[a-z0-9]+_[a-z0-9]{12}$'
            LIMIT 10
        """)
        
        return {
            "status": "success",
            "total_psps": dict(stats)['total'],
            "correct_format": dict(stats)['correct_format'],
            "wrong_format": dict(stats)['wrong_format'],
            "examples": [dict(e) for e in wrong_examples]
        }
    
    except Exception as e:
        logger.error(f"Failed to check PSP formats: {e}")
        raise HTTPException(status_code=500, detail=f"Check failed: {str(e)}")

