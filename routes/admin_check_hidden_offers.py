"""
Admin endpoint to check hidden commission offers
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
from db.database import database
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user
from utils.logger import logger
from decimal import Decimal

router = APIRouter(prefix="/admin/commission", tags=["Admin Commission Debug"])

async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require admin or employee role"""
    if current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Admin or employee access required")
    return current_user

@router.get("/debug-offers/{merchant_id}")
async def debug_commission_offers(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """
    Debug endpoint to check ALL commission offers for a merchant
    Including hidden ones (is_active=false, min_amount=0, etc)
    """
    try:
        # 1. Get ALL offers (including inactive)
        all_offers = await database.fetch_all(
            """
            SELECT 
                id,
                merchant_id,
                offered_commission_rate,
                min_order_amount,
                max_order_amount,
                agent_type,
                is_active,
                created_at,
                updated_at
            FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            ORDER BY offered_commission_rate DESC
            """,
            {"merchant_id": merchant_id}
        )
        
        # 2. Check for suspicious offers
        suspicious_offers = []
        for offer in all_offers:
            offer_dict = dict(offer)
            rate = float(offer_dict.get('offered_commission_rate', 0))
            min_amount = offer_dict.get('min_order_amount')
            
            # Flag suspicious offers
            if (rate == 0.02 or  # 2% offer
                min_amount is None or  # NULL min
                min_amount == 0 or  # Zero min
                min_amount <= 45):  # Would match $45 order
                suspicious_offers.append(offer_dict)
        
        # 3. Test what would match a $45 order
        test_matches = []
        for offer in all_offers:
            offer_dict = dict(offer)
            min_amt = offer_dict.get('min_order_amount') or 0
            max_amt = offer_dict.get('max_order_amount')
            
            if min_amt <= 45:
                if max_amt is None or max_amt >= 45:
                    test_matches.append({
                        "id": offer_dict.get('id'),
                        "rate": float(offer_dict.get('offered_commission_rate', 0)) * 100,
                        "min": min_amt,
                        "max": max_amt,
                        "agent_type": offer_dict.get('agent_type'),
                        "is_active": offer_dict.get('is_active')
                    })
        
        # 4. Get agent's current type
        agent_result = await database.fetch_one(
            "SELECT agent_id, agent_type FROM agents WHERE agent_id = :agent_id",
            {"agent_id": "agent_ee38f2b3645a2ec2"}
        )
        agent_type = dict(agent_result).get('agent_type') if agent_result else None
        
        return {
            "status": "success",
            "merchant_id": merchant_id,
            "agent_type": agent_type,
            "total_offers": len(all_offers),
            "all_offers": [
                {
                    "id": dict(o).get('id'),
                    "rate_percent": float(dict(o).get('offered_commission_rate', 0)) * 100,
                    "min_order": dict(o).get('min_order_amount'),
                    "max_order": dict(o).get('max_order_amount'),
                    "agent_type": dict(o).get('agent_type'),
                    "is_active": dict(o).get('is_active')
                }
                for o in all_offers
            ],
            "suspicious_offers": suspicious_offers,
            "would_match_45_dollar_order": test_matches,
            "analysis": {
                "has_2_percent_offer": any(float(dict(o).get('offered_commission_rate', 0)) == 0.02 for o in all_offers),
                "has_zero_min_offer": any(dict(o).get('min_order_amount') == 0 for o in all_offers),
                "has_null_min_offer": any(dict(o).get('min_order_amount') is None for o in all_offers)
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to debug commission offers: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/fix-hidden-offers/{merchant_id}")
async def fix_hidden_commission_offers(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """
    Fix hidden offers that might cause 2% commission
    """
    try:
        # Delete any 2% offers
        deleted_2pct = await database.execute(
            """
            DELETE FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            AND offered_commission_rate = 0.02
            """,
            {"merchant_id": merchant_id}
        )
        
        # Delete offers with NULL or 0 min_order_amount
        deleted_null_min = await database.execute(
            """
            DELETE FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            AND (min_order_amount IS NULL OR min_order_amount = 0)
            """,
            {"merchant_id": merchant_id}
        )
        
        # Fix any offer with min < 50 (except premium 5% which should be 100+)
        updated_mins = await database.execute(
            """
            UPDATE merchant_commission_offers
            SET min_order_amount = CASE
                WHEN offered_commission_rate = 0.05 AND agent_type = 'premium' THEN 100.0
                WHEN offered_commission_rate = 0.025 THEN 50.0
                ELSE min_order_amount
            END
            WHERE merchant_id = :merchant_id
            AND (
                (offered_commission_rate = 0.025 AND (min_order_amount < 50 OR min_order_amount IS NULL))
                OR (offered_commission_rate = 0.05 AND agent_type = 'premium' AND (min_order_amount < 100 OR min_order_amount IS NULL))
            )
            """,
            {"merchant_id": merchant_id}
        )
        
        return {
            "status": "success",
            "fixes": {
                "deleted_2_percent_offers": deleted_2pct or 0,
                "deleted_null_min_offers": deleted_null_min or 0,
                "updated_min_amounts": updated_mins or 0
            },
            "message": "Hidden offers cleaned up"
        }
        
    except Exception as e:
        logger.error(f"Failed to fix hidden offers: {e}")
        raise HTTPException(status_code=500, detail=str(e))
