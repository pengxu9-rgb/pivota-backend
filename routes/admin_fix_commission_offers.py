"""
临时的 Admin 端点来清理错误的 Commission Offers
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
from db.database import database
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user
from utils.logger import logger

router = APIRouter(prefix="/admin/commission", tags=["Admin Commission Fix"])

async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require admin or employee role"""
    if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Admin or employee access required")
    return current_user

@router.post("/cleanup-invalid-offers")
async def cleanup_invalid_commission_offers(
    payload: Dict[str, Any],
    current_user: dict = Depends(require_admin)
):
    """
    清理错误的 commission offers
    主要是删除不应该存在的 2% offer
    """
    merchant_id = payload.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id required")
    
    try:
        # 1. 查找并删除 2% 的 offers
        deleted_2pct = await database.execute(
            """
            DELETE FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
              AND offered_commission_rate = 0.02
            """,
            {"merchant_id": merchant_id}
        )
        
        # 2. 修复 2.5% offer 的最低金额
        updated_25pct = await database.execute(
            """
            UPDATE merchant_commission_offers
            SET min_order_amount = 50.0
            WHERE merchant_id = :merchant_id
              AND offered_commission_rate = 0.025
              AND agent_type IS NULL
              AND (min_order_amount < 50 OR min_order_amount IS NULL)
            """,
            {"merchant_id": merchant_id}
        )
        
        # 3. 修复 5% offer 的最低金额
        updated_5pct = await database.execute(
            """
            UPDATE merchant_commission_offers
            SET min_order_amount = 100.0
            WHERE merchant_id = :merchant_id
              AND offered_commission_rate = 0.05
              AND agent_type = 'premium'
              AND (min_order_amount < 100 OR min_order_amount IS NULL)
            """,
            {"merchant_id": merchant_id}
        )
        
        # 4. 查询清理后的 offers
        remaining_offers = await database.fetch_all(
            """
            SELECT 
                id,
                agent_type,
                offered_commission_rate * 100 as rate_percent,
                min_order_amount,
                is_active
            FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
              AND is_active = true
            ORDER BY min_order_amount
            """,
            {"merchant_id": merchant_id}
        )
        
        logger.info(
            f"[Commission Cleanup] merchant={merchant_id} "
            f"deleted_2pct={deleted_2pct} updated_2.5pct={updated_25pct} "
            f"updated_5pct={updated_5pct} by={current_user.get('email')}"
        )
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "cleanup_results": {
                "deleted_2pct_offers": deleted_2pct or 0,
                "updated_25pct_min_amount": updated_25pct or 0,
                "updated_5pct_min_amount": updated_5pct or 0
            },
            "remaining_offers": [dict(offer) for offer in remaining_offers],
            "message": "Commission offers cleaned up successfully"
        }
        
    except Exception as e:
        logger.error(f"Failed to cleanup commission offers: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/check-offers/{merchant_id}")
async def check_merchant_offers(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """检查商户的所有 commission offers"""
    try:
        offers = await database.fetch_all(
            """
            SELECT 
                id,
                agent_type,
                offered_commission_rate * 100 as rate_percent,
                min_order_amount,
                maximum_order_amount,
                is_active,
                created_at
            FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            ORDER BY offered_commission_rate, min_order_amount
            """,
            {"merchant_id": merchant_id}
        )
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "offers": [dict(offer) for offer in offers],
            "total": len(offers)
        }
        
    except Exception as e:
        logger.error(f"Failed to check offers: {e}")
        raise HTTPException(status_code=500, detail=str(e))
