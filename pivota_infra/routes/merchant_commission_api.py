"""
[Phase 5.5] Merchant Commission API
Endpoints for merchants to manage commission offers
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Path
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from decimal import Decimal

from db.database import database
from utils.auth import get_current_user

router = APIRouter(
    prefix="/merchants/{merchant_id}/commission",
    tags=["[Phase 5.5] Merchant Commission"]
)


class CommissionOfferRequest(BaseModel):
    agent_type: Optional[str] = Field(None, description="Agent type (premium/standard/basic) or NULL for all")
    offered_commission_rate: float = Field(..., ge=0, le=1, description="Commission rate (0.0-1.0)")
    min_order_amount: float = Field(default=0, description="Minimum order amount")
    max_order_amount: Optional[float] = Field(None, description="Maximum order amount")
    currency: str = Field(default="USD")
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    notes: Optional[str] = None


@router.post("/offers")
async def create_commission_offer(
    merchant_id: str = Path(...),
    request: CommissionOfferRequest = ...,
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.5] Create commission offer"""
    
    try:
        await database.execute(
            """
            INSERT INTO merchant_commission_offers (
                merchant_id, agent_type, offered_commission_rate,
                min_order_amount, max_order_amount, currency,
                valid_from, valid_until, created_by, notes
            ) VALUES (
                :merchant_id, :agent_type, :rate,
                :min_amount, :max_amount, :currency,
                :valid_from, :valid_until, :created_by, :notes
            )
            """,
            {
                "merchant_id": merchant_id,
                "agent_type": request.agent_type,
                "rate": request.offered_commission_rate,
                "min_amount": request.min_order_amount,
                "max_amount": request.max_order_amount,
                "currency": request.currency,
                "valid_from": request.valid_from,
                "valid_until": request.valid_until,
                "created_by": current_user.get("email"),
                "notes": request.notes
            }
        )
        
        return {"status": "success", "message": "Commission offer created"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/offers")
async def get_commission_offers(
    merchant_id: str = Path(...),
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.5] Get all commission offers"""
    
    try:
        offers = await database.fetch_all(
            "SELECT * FROM merchant_commission_offers WHERE merchant_id = :merchant_id ORDER BY created_at DESC",
            {"merchant_id": merchant_id}
        )
        
        return {
            "merchant_id": merchant_id,
            "offers": [
                {
                    "id": o["id"],
                    "agent_type": o["agent_type"],
                    "rate": float(o["offered_commission_rate"]),
                    "min_amount": float(o["min_order_amount"]),
                    "is_active": o["is_active"],
                    "created_at": o["created_at"].isoformat()
                }
                for o in offers
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


print("[Phase 5.5] Merchant commission API initialized")
[Phase 5.5] Merchant Commission API
Endpoints for merchants to manage commission offers
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Path
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from decimal import Decimal

from db.database import database
from utils.auth import get_current_user

router = APIRouter(
    prefix="/merchants/{merchant_id}/commission",
    tags=["[Phase 5.5] Merchant Commission"]
)


class CommissionOfferRequest(BaseModel):
    agent_type: Optional[str] = Field(None, description="Agent type (premium/standard/basic) or NULL for all")
    offered_commission_rate: float = Field(..., ge=0, le=1, description="Commission rate (0.0-1.0)")
    min_order_amount: float = Field(default=0, description="Minimum order amount")
    max_order_amount: Optional[float] = Field(None, description="Maximum order amount")
    currency: str = Field(default="USD")
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    notes: Optional[str] = None


@router.post("/offers")
async def create_commission_offer(
    merchant_id: str = Path(...),
    request: CommissionOfferRequest = ...,
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.5] Create commission offer"""
    
    try:
        await database.execute(
            """
            INSERT INTO merchant_commission_offers (
                merchant_id, agent_type, offered_commission_rate,
                min_order_amount, max_order_amount, currency,
                valid_from, valid_until, created_by, notes
            ) VALUES (
                :merchant_id, :agent_type, :rate,
                :min_amount, :max_amount, :currency,
                :valid_from, :valid_until, :created_by, :notes
            )
            """,
            {
                "merchant_id": merchant_id,
                "agent_type": request.agent_type,
                "rate": request.offered_commission_rate,
                "min_amount": request.min_order_amount,
                "max_amount": request.max_order_amount,
                "currency": request.currency,
                "valid_from": request.valid_from,
                "valid_until": request.valid_until,
                "created_by": current_user.get("email"),
                "notes": request.notes
            }
        )
        
        return {"status": "success", "message": "Commission offer created"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/offers")
async def get_commission_offers(
    merchant_id: str = Path(...),
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.5] Get all commission offers"""
    
    try:
        offers = await database.fetch_all(
            "SELECT * FROM merchant_commission_offers WHERE merchant_id = :merchant_id ORDER BY created_at DESC",
            {"merchant_id": merchant_id}
        )
        
        return {
            "merchant_id": merchant_id,
            "offers": [
                {
                    "id": o["id"],
                    "agent_type": o["agent_type"],
                    "rate": float(o["offered_commission_rate"]),
                    "min_amount": float(o["min_order_amount"]),
                    "is_active": o["is_active"],
                    "created_at": o["created_at"].isoformat()
                }
                for o in offers
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


print("[Phase 5.5] Merchant commission API initialized")
