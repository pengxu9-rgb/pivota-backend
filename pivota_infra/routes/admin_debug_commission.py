"""
Admin endpoint to debug commission calculation
"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
from db.database import database
from utils.auth import get_current_user
from utils.logger import logger
from decimal import Decimal
from services.revenue_share_service import RevenueShareService, PLATFORM_DEFAULT_COMMISSION

router = APIRouter(prefix="/admin/debug", tags=["Admin Debug"])

async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require admin or employee role"""
    if current_user.get("role") not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Admin or employee access required")
    return current_user

@router.post("/commission-calculation")
async def debug_commission_calculation(
    agent_id: str,
    merchant_id: str, 
    amount: float,
    current_user: dict = Depends(require_admin)
):
    """
    Debug commission calculation for specific scenario
    """
    try:
        # 1. Get agent details
        agent = await database.fetch_one(
            "SELECT agent_id, name, email, agent_type FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
            
        agent_dict = dict(agent)
        
        # 2. Get merchant offers
        merchant_offers = await database.fetch_all(
            """
            SELECT * FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            AND is_active = true
            AND (min_order_amount IS NULL OR min_order_amount <= :amount)
            AND (max_order_amount IS NULL OR max_order_amount >= :amount)
            ORDER BY offered_commission_rate DESC
            """,
            {"merchant_id": merchant_id, "amount": amount}
        )
        
        # 3. Try revenue service matching
        revenue_service = RevenueShareService(database)
        match_result = await revenue_service.match_commission(
            agent_id=agent_id,
            merchant_id=merchant_id,
            order_amount=Decimal(str(amount)),
            currency="USD"
        )
        
        # 4. Check what platform default would be used
        agent_type = agent_dict.get('agent_type')
        if agent_type is None:
            agent_type_display = "NULL → defaults to 'basic'"  # [Phase 6.2]
            effective_agent_type = 'basic'  # [Phase 6.2] 'standard' no longer exists
        else:
            agent_type_display = agent_type
            effective_agent_type = agent_type
            
        platform_default_rate = PLATFORM_DEFAULT_COMMISSION.get(
            effective_agent_type, 
            revenue_service.platform_default
        )
        
        return {
            "status": "success",
            "debug_info": {
                "agent": {
                    "agent_id": agent_dict['agent_id'],
                    "email": agent_dict['email'],
                    "agent_type_raw": agent_dict.get('agent_type'),
                    "agent_type_display": agent_type_display,
                    "effective_agent_type": effective_agent_type
                },
                "merchant_offers": [
                    {
                        "id": dict(o).get('id'),
                        "rate": float(dict(o).get('offered_commission_rate', 0)),
                        "min": dict(o).get('min_order_amount'),
                        "agent_type": dict(o).get('agent_type')
                    }
                    for o in merchant_offers
                ],
                "order": {
                    "amount": amount,
                    "currency": "USD"
                },
                "match_result": match_result,
                "platform_defaults": {
                    "would_use": f"{float(platform_default_rate) * 100}%",
                    "all_defaults": {
                        k: f"{float(v) * 100}%" 
                        for k, v in PLATFORM_DEFAULT_COMMISSION.items()
                    }
                },
                "analysis": {
                    "why_2_percent": analyze_2_percent(
                        agent_dict, 
                        merchant_offers, 
                        match_result
                    )
                }
            }
        }
        
    except Exception as e:
        logger.error(f"Debug commission calculation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _analyze_2_percent(self, agent_dict, merchant_offers, match_result):
    """Analyze why we might be getting 2%"""
    reasons = []
    
    if match_result.get('actual_rate') == 0.02:
        # [Phase 6.2] 'standard' no longer exists, only basic/premium
        reasons.append("Getting 2% rate but 'standard' no longer exists!")
        reasons.append("This should not happen after Phase 6.2 migration")
            
        if not merchant_offers:
            reasons.append("No merchant offers found, using platform default")
    
    return reasons if reasons else ["Not getting 2% commission"]

"""
from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any
from db.database import database
from utils.auth import get_current_user
from utils.logger import logger
from decimal import Decimal
from services.revenue_share_service import RevenueShareService, PLATFORM_DEFAULT_COMMISSION

router = APIRouter(prefix="/admin/debug", tags=["Admin Debug"])

async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require admin or employee role"""
    if current_user.get("role") not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Admin or employee access required")
    return current_user

@router.post("/commission-calculation")
async def debug_commission_calculation(
    agent_id: str,
    merchant_id: str, 
    amount: float,
    current_user: dict = Depends(require_admin)
):
    """
    Debug commission calculation for specific scenario
    """
    try:
        # 1. Get agent details
        agent = await database.fetch_one(
            "SELECT agent_id, name, email, agent_type FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
            
        agent_dict = dict(agent)
        
        # 2. Get merchant offers
        merchant_offers = await database.fetch_all(
            """
            SELECT * FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            AND is_active = true
            AND (min_order_amount IS NULL OR min_order_amount <= :amount)
            AND (max_order_amount IS NULL OR max_order_amount >= :amount)
            ORDER BY offered_commission_rate DESC
            """,
            {"merchant_id": merchant_id, "amount": amount}
        )
        
        # 3. Try revenue service matching
        revenue_service = RevenueShareService(database)
        match_result = await revenue_service.match_commission(
            agent_id=agent_id,
            merchant_id=merchant_id,
            order_amount=Decimal(str(amount)),
            currency="USD"
        )
        
        # 4. Check what platform default would be used
        agent_type = agent_dict.get('agent_type')
        if agent_type is None:
            agent_type_display = "NULL → defaults to 'basic'"  # [Phase 6.2]
            effective_agent_type = 'basic'  # [Phase 6.2] 'standard' no longer exists
        else:
            agent_type_display = agent_type
            effective_agent_type = agent_type
            
        platform_default_rate = PLATFORM_DEFAULT_COMMISSION.get(
            effective_agent_type, 
            revenue_service.platform_default
        )
        
        return {
            "status": "success",
            "debug_info": {
                "agent": {
                    "agent_id": agent_dict['agent_id'],
                    "email": agent_dict['email'],
                    "agent_type_raw": agent_dict.get('agent_type'),
                    "agent_type_display": agent_type_display,
                    "effective_agent_type": effective_agent_type
                },
                "merchant_offers": [
                    {
                        "id": dict(o).get('id'),
                        "rate": float(dict(o).get('offered_commission_rate', 0)),
                        "min": dict(o).get('min_order_amount'),
                        "agent_type": dict(o).get('agent_type')
                    }
                    for o in merchant_offers
                ],
                "order": {
                    "amount": amount,
                    "currency": "USD"
                },
                "match_result": match_result,
                "platform_defaults": {
                    "would_use": f"{float(platform_default_rate) * 100}%",
                    "all_defaults": {
                        k: f"{float(v) * 100}%" 
                        for k, v in PLATFORM_DEFAULT_COMMISSION.items()
                    }
                },
                "analysis": {
                    "why_2_percent": analyze_2_percent(
                        agent_dict, 
                        merchant_offers, 
                        match_result
                    )
                }
            }
        }
        
    except Exception as e:
        logger.error(f"Debug commission calculation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _analyze_2_percent(self, agent_dict, merchant_offers, match_result):
    """Analyze why we might be getting 2%"""
    reasons = []
    
    if match_result.get('actual_rate') == 0.02:
        # [Phase 6.2] 'standard' no longer exists, only basic/premium
        reasons.append("Getting 2% rate but 'standard' no longer exists!")
        reasons.append("This should not happen after Phase 6.2 migration")
            
        if not merchant_offers:
            reasons.append("No merchant offers found, using platform default")
    
    return reasons if reasons else ["Not getting 2% commission"]