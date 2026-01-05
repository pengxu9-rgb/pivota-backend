"""Admin endpoint to fix merchant_id mismatch"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from db.database import database
from routes.auth_routes import require_admin
import logging

router = APIRouter(prefix="/admin/fix", tags=["Admin Fixes"])
logger = logging.getLogger(__name__)

class FixMerchantIDRequest(BaseModel):
    email: str
    correct_merchant_id: str

class MergeMerchantsRequest(BaseModel):
    keep_merchant_id: str
    remove_merchant_id: str
    user_email: str

class FixUserRoleRequest(BaseModel):
    email: str
    role: str
    merchant_id: Optional[str] = None

@router.post("/merchant-id")
async def fix_merchant_id(
    request: FixMerchantIDRequest,
    current_user: dict = Depends(require_admin)
):
    """
    Fix merchant linkage by updating merchant_onboarding
    
    Since login queries merchant_onboarding by contact_email,
    we need to update the correct merchant record's contact_email
    """
    try:
        # Update the target merchant's contact_email
        merchant_query = """
        UPDATE merchant_onboarding
        SET contact_email = :email
        WHERE merchant_id = :merchant_id
        RETURNING merchant_id, business_name, contact_email, mcp_shop_domain
        """
        
        merchant_result = await database.fetch_one(merchant_query, {
            "email": request.email,
            "merchant_id": request.correct_merchant_id
        })
        
        if not merchant_result:
            raise HTTPException(status_code=404, detail=f"Merchant {request.correct_merchant_id} not found")
        
        # Also update users table if merchant_id column exists
        try:
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
        except Exception as e:
            logger.warning(f"Failed to update users.merchant_id (column may not exist yet): {e}")
            user_result = None
        
        logger.info(f"✅ Updated merchant {request.correct_merchant_id} contact_email to {request.email}")
        
        return {
            "status": "success",
            "message": f"Updated merchant linkage for {request.email}",
            "merchant": {
                "merchant_id": merchant_result["merchant_id"],
                "business_name": merchant_result["business_name"],
                "contact_email": merchant_result["contact_email"],
                "shop_domain": merchant_result["mcp_shop_domain"]
            },
            "user_updated": user_result is not None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fix merchant_id: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update merchant_id: {str(e)}")

@router.post("/merge-merchants")
async def merge_merchants(
    request: MergeMerchantsRequest,
    current_user: dict = Depends(require_admin)
):
    """
    Merge two merchant records - keep one, delete the other
    Transfer all data to the kept merchant
    """
    try:
        async with database.transaction():
            # Update orders
            await database.execute(
                "UPDATE orders SET merchant_id = :keep WHERE merchant_id = :remove",
                {"keep": request.keep_merchant_id, "remove": request.remove_merchant_id}
            )
            
            # Update PSPs
            await database.execute(
                "UPDATE merchant_psps SET merchant_id = :keep WHERE merchant_id = :remove",
                {"keep": request.keep_merchant_id, "remove": request.remove_merchant_id}
            )
            
            # Update user
            await database.execute(
                "UPDATE users SET merchant_id = :keep WHERE email = :email",
                {"keep": request.keep_merchant_id, "email": request.user_email}
            )
            
            # Delete old merchant record
            await database.execute(
                "DELETE FROM merchant_onboarding WHERE merchant_id = :remove",
                {"remove": request.remove_merchant_id}
            )
            
            # Update kept merchant's contact_email
            await database.execute(
                "UPDATE merchant_onboarding SET contact_email = :email WHERE merchant_id = :keep",
                {"email": request.user_email, "keep": request.keep_merchant_id}
            )
        
        return {
            "status": "success",
            "message": f"Merged {request.remove_merchant_id} into {request.keep_merchant_id}",
            "kept_merchant": request.keep_merchant_id,
            "removed_merchant": request.remove_merchant_id
        }
        
    except Exception as e:
        logger.error(f"Failed to merge merchants: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to merge: {str(e)}")


@router.post("/user-role")
async def fix_user_role(
    request: FixUserRoleRequest,
    current_user: dict = Depends(require_admin),
):
    """
    Fix a user's role (and merchant_id when role=merchant).

    Useful when a merchant was accidentally created with role=agent and cannot
    login due to agent lookup/schema mismatch.
    """
    valid_roles = {"super_admin", "admin", "employee", "outsourced", "merchant", "agent"}
    if request.role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role: {request.role}")

    try:
        merchant_id = request.merchant_id
        if request.role == "merchant" and not merchant_id:
            merchant = await database.fetch_one(
                """
                SELECT merchant_id
                FROM merchant_onboarding
                WHERE contact_email = :email
                ORDER BY created_at DESC
                LIMIT 1
                """,
                {"email": request.email},
            )
            if merchant:
                merchant_id = merchant["merchant_id"]

        # Update users row
        try:
            if merchant_id and request.role == "merchant":
                user = await database.fetch_one(
                    """
                    UPDATE users
                    SET role = :role, merchant_id = :merchant_id
                    WHERE email = :email
                    RETURNING id, email, role, merchant_id
                    """,
                    {"email": request.email, "role": request.role, "merchant_id": merchant_id},
                )
            else:
                user = await database.fetch_one(
                    """
                    UPDATE users
                    SET role = :role
                    WHERE email = :email
                    RETURNING id, email, role, merchant_id
                    """,
                    {"email": request.email, "role": request.role},
                )
        except Exception as e:
            # Some legacy schemas may not have users.merchant_id yet
            logger.warning(f"Failed to update users.merchant_id (column may not exist): {e}")
            user = await database.fetch_one(
                """
                UPDATE users
                SET role = :role
                WHERE email = :email
                RETURNING id, email, role
                """,
                {"email": request.email, "role": request.role},
            )

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        return {
            "status": "success",
            "user": dict(user),
            "resolved_merchant_id": merchant_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fix user role: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fix user role: {str(e)}")
