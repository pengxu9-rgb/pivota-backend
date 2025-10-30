"""Simple admin fix - just update contact_email"""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from routes.auth_routes import require_admin
import logging

router = APIRouter(prefix="/admin/simple-fix", tags=["Admin Simple Fix"])
logger = logging.getLogger(__name__)

@router.post("/update-contact-email")
async def update_contact_email(current_user: dict = Depends(require_admin)):
    """
    Simple fix: Update merch_208139f7600dbf42's contact_email to merchant@test.com
    """
    try:
        # Update merchant_onboarding
        result = await database.fetch_one(
            """
            UPDATE merchant_onboarding
            SET contact_email = 'merchant@test.com'
            WHERE merchant_id = 'merch_208139f7600dbf42'
            RETURNING merchant_id, business_name, contact_email, mcp_shop_domain
            """
        )
        
        # Update users table
        user_result = await database.fetch_one(
            """
            UPDATE users
            SET merchant_id = 'merch_208139f7600dbf42'
            WHERE email = 'merchant@test.com'
            RETURNING id, email, merchant_id
            """
        )
        
        # Delete the duplicate merchant if it exists
        try:
            await database.execute(
                """
                DELETE FROM merchant_onboarding
                WHERE merchant_id = 'merch_6b90dc9838d5fd9c'
                """
            )
            logger.info("Deleted duplicate merchant merch_6b90dc9838d5fd9c")
        except:
            pass
        
        return {
            "status": "success",
            "message": "Contact email updated",
            "merchant": {
                "merchant_id": result["merchant_id"] if result else None,
                "business_name": result["business_name"] if result else None,
                "contact_email": result["contact_email"] if result else None,
                "shop_domain": result["mcp_shop_domain"] if result else None
            },
            "user": {
                "email": user_result["email"] if user_result else None,
                "merchant_id": user_result["merchant_id"] if user_result else None
            }
        }
        
    except Exception as e:
        logger.error(f"Fix failed: {e}")
        raise HTTPException(status_code=500, detail=f"Fix failed: {str(e)}")


