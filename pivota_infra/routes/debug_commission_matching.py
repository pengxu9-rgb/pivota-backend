"""
Debug endpoint for commission matching
"""

from fastapi import APIRouter, Depends
from utils.auth import require_admin
from db.database import database
import logging

router = APIRouter(
    prefix="/debug/commission",
    tags=["Debug - Commission"]
)

logger = logging.getLogger(__name__)


@router.get("/merchant-offers/{merchant_id}")
async def debug_merchant_offers(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """查看商家的所有佣金规则"""
    
    try:
        # Get all offers for this merchant
        offers = await database.fetch_all(
            """
            SELECT 
                id,
                merchant_id,
                agent_type,
                offered_commission_rate,
                min_order_amount,
                max_order_amount,
                currency,
                is_active,
                valid_from,
                valid_until,
                created_at
            FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            ORDER BY created_at DESC
            """,
            {"merchant_id": merchant_id}
        )
        
        return {
            "success": True,
            "merchant_id": merchant_id,
            "total_offers": len(offers),
            "offers": [
                {
                    "id": o['id'],
                    "agent_type": o['agent_type'],
                    "rate": float(o['offered_commission_rate']),
                    "min_amount": float(o['min_order_amount']) if o['min_order_amount'] else None,
                    "max_amount": float(o['max_order_amount']) if o['max_order_amount'] else None,
                    "currency": o['currency'],
                    "is_active": o['is_active'],
                    "valid_from": o['valid_from'].isoformat() if o['valid_from'] else None,
                    "valid_until": o['valid_until'].isoformat() if o['valid_until'] else None,
                    "created_at": o['created_at'].isoformat()
                }
                for o in offers
            ]
        }
    
    except Exception as e:
        logger.error(f"Error fetching merchant offers: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }


@router.get("/test-query/{merchant_id}/{agent_type}/{amount}")
async def test_offer_query(
    merchant_id: str,
    agent_type: str,
    amount: float,
    currency: str = "USD",
    current_user: dict = Depends(require_admin)
):
    """测试商家佣金规则查询（模拟 RevenueShareService 的查询）"""
    
    try:
        # Query 1: Agent-type specific
        specific_offer = await database.fetch_one(
            """
            SELECT * FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            AND agent_type = :agent_type
            AND currency = :currency
            AND is_active = true
            AND (valid_from IS NULL OR valid_from <= NOW())
            AND (valid_until IS NULL OR valid_until >= NOW())
            AND (min_order_amount IS NULL OR :amount >= min_order_amount)
            AND (max_order_amount IS NULL OR :amount <= max_order_amount)
            ORDER BY offered_commission_rate DESC
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "agent_type": agent_type,
                "currency": currency,
                "amount": amount
            }
        )
        
        # Query 2: General offer (agent_type = NULL)
        general_offer = await database.fetch_one(
            """
            SELECT * FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            AND agent_type IS NULL
            AND currency = :currency
            AND is_active = true
            AND (valid_from IS NULL OR valid_from <= NOW())
            AND (valid_until IS NULL OR valid_until >= NOW())
            AND (min_order_amount IS NULL OR :amount >= min_order_amount)
            AND (max_order_amount IS NULL OR :amount <= max_order_amount)
            ORDER BY offered_commission_rate DESC
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "currency": currency,
                "amount": amount
            }
        )
        
        return {
            "success": True,
            "test_params": {
                "merchant_id": merchant_id,
                "agent_type": agent_type,
                "amount": amount,
                "currency": currency
            },
            "specific_offer_found": specific_offer is not None,
            "specific_offer": dict(specific_offer) if specific_offer else None,
            "general_offer_found": general_offer is not None,
            "general_offer": dict(general_offer) if general_offer else None,
            "recommended_rate": float(specific_offer['offered_commission_rate']) if specific_offer else (
                float(general_offer['offered_commission_rate']) if general_offer else 0.015
            )
        }
    
    except Exception as e:
        logger.error(f"Error testing query: {e}", exc_info=True)
        import traceback
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }

