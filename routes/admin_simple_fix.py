"""Simple admin fix - just update contact_email"""
from fastapi import APIRouter, Depends, HTTPException
from db.database import database
from routes.auth_routes import require_admin
from utils.runtime_safety import configured_env_value, require_runtime_gate
import logging

router = APIRouter(prefix="/admin/simple-fix", tags=["Admin Simple Fix"])
logger = logging.getLogger(__name__)

@router.post("/update-contact-email")
async def update_contact_email(current_user: dict = Depends(require_admin)):
    """Update a configured merchant contact email. Disabled in production by default."""
    require_runtime_gate("ENABLE_ADMIN_SIMPLE_FIX")
    merchant_id = configured_env_value("ADMIN_SIMPLE_FIX_MERCHANT_ID")
    contact_email = configured_env_value("ADMIN_SIMPLE_FIX_CONTACT_EMAIL")
    if not merchant_id or not contact_email:
        raise HTTPException(
            status_code=400,
            detail="ADMIN_SIMPLE_FIX_MERCHANT_ID and ADMIN_SIMPLE_FIX_CONTACT_EMAIL are required",
        )
    try:
        result = await database.fetch_one(
            """
            UPDATE merchant_onboarding
            SET contact_email = :contact_email
            WHERE merchant_id = :merchant_id
            RETURNING merchant_id, business_name, contact_email, mcp_shop_domain
            """,
            {"merchant_id": merchant_id, "contact_email": contact_email},
        )
        
        user_result = await database.fetch_one(
            """
            UPDATE users
            SET merchant_id = :merchant_id
            WHERE email = :contact_email
            RETURNING id, email, merchant_id
            """,
            {"merchant_id": merchant_id, "contact_email": contact_email},
        )
        
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


