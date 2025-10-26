"""Admin endpoint to fix merchant_id mismatch"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db.database import database
from routes.auth_routes import require_admin
import logging

router = APIRouter(prefix="/admin/fix", tags=["Admin Fixes"])
logger = logging.getLogger(__name__)

class FixMerchantIDRequest(BaseModel):
    email: str
    correct_merchant_id: str

@router.post("/merchant-id")
async def fix_merchant_id(
    request: FixMerchantIDRequest,
    current_user: dict = Depends(require_admin)
):
    """
    Fix merchant_id for a user account
    
    This fixes the issue where a user's merchant_id doesn't match 
    the merchant_id in orders/merchant_psps/merchant_onboarding
    """
    try:
        # Update users table
        query = """
        UPDATE users
        SET merchant_id = :new_merchant_id
        WHERE email = :email
        RETURNING id, email, merchant_id, role
        """
        
        result = await database.fetch_one(query, {
            "email": request.email,
            "new_merchant_id": request.correct_merchant_id
        })
        
        if not result:
            raise HTTPException(status_code=404, detail=f"User {request.email} not found")
        
        logger.info(f"✅ Updated merchant_id for {request.email} to {request.correct_merchant_id}")
        
        return {
            "status": "success",
            "message": f"Updated merchant_id for {request.email}",
            "user": {
                "id": result["id"],
                "email": result["email"],
                "merchant_id": result["merchant_id"],
                "role": result["role"]
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fix merchant_id: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update merchant_id: {str(e)}")

