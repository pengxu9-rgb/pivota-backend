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
    
    This updates both users table and merchant_onboarding table
    to ensure the email is linked to the correct merchant_id
    """
    try:
        # Step 1: Update users table
        users_query = """
        UPDATE users
        SET merchant_id = :new_merchant_id
        WHERE email = :email
        RETURNING id, email, merchant_id, role
        """
        
        user_result = await database.fetch_one(users_query, {
            "email": request.email,
            "new_merchant_id": request.correct_merchant_id
        })
        
        if not user_result:
            raise HTTPException(status_code=404, detail=f"User {request.email} not found")
        
        # Step 2: Update merchant_onboarding table contact_email
        # This is critical because login API queries merchant_onboarding by contact_email
        merchant_query = """
        UPDATE merchant_onboarding
        SET contact_email = :email
        WHERE merchant_id = :merchant_id
        RETURNING merchant_id, business_name, contact_email
        """
        
        merchant_result = await database.fetch_one(merchant_query, {
            "email": request.email,
            "merchant_id": request.correct_merchant_id
        })
        
        logger.info(f"✅ Updated user {request.email} to merchant_id {request.correct_merchant_id}")
        if merchant_result:
            logger.info(f"✅ Updated merchant_onboarding contact_email for {request.correct_merchant_id}")
        
        return {
            "status": "success",
            "message": f"Updated merchant linkage for {request.email}",
            "user": {
                "id": user_result["id"],
                "email": user_result["email"],
                "merchant_id": user_result["merchant_id"],
                "role": user_result["role"]
            },
            "merchant": {
                "merchant_id": merchant_result["merchant_id"] if merchant_result else None,
                "business_name": merchant_result["business_name"] if merchant_result else None,
                "contact_email": merchant_result["contact_email"] if merchant_result else None
            } if merchant_result else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fix merchant_id: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update merchant_id: {str(e)}")

