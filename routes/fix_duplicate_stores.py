"""Fix duplicate stores - keep only unique ones"""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from utils.auth import get_current_user
from utils.runtime_safety import require_runtime_gate

router = APIRouter()

@router.post("/fix-duplicate-stores")
@router.get("/fix-duplicate-stores")
async def fix_duplicate_stores(current_user: dict = Depends(get_current_user)):
    """Remove duplicate stores, keep the newest one for each platform/domain"""
    require_runtime_gate("ENABLE_DUPLICATE_STORE_FIX")
    merchant_id = str(current_user.get("merchant_id") or "").strip()
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required in current user context")
    
    try:
        await database.execute(
            """
            DELETE FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND store_id IN (
                SELECT store_id
                FROM (
                  SELECT
                    store_id,
                    row_number() OVER (
                      PARTITION BY lower(coalesce(platform, '')), lower(coalesce(domain, ''))
                      ORDER BY connected_at DESC NULLS LAST, store_id DESC
                    ) AS rn
                  FROM merchant_stores
                  WHERE merchant_id = :merchant_id
                ) ranked
                WHERE rn > 1
              )
            """,
            {"merchant_id": merchant_id},
        )
        
        # Verify
        stores_query = "SELECT store_id, platform, name FROM merchant_stores WHERE merchant_id = :merchant_id ORDER BY platform, name"
        stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
        
        return {
            "status": "success",
            "message": "Duplicate stores removed",
            "remaining_stores": [{"id": s["store_id"], "platform": s["platform"], "name": s["name"]} for s in stores]
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }





