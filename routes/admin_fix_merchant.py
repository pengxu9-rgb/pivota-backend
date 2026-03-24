"""Admin endpoint to fix merchant identity mismatches and conversions"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from typing import Optional
from db.database import database
from routes.auth_routes import require_admin
from utils.auth import require_admin_or_key
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


class InspectIdentityRequest(BaseModel):
    email: EmailStr


class MergeIdentityToMerchantRequest(BaseModel):
    email: EmailStr
    merchant_id: Optional[str] = None
    new_password: Optional[str] = None
    force: bool = False


def _normalize_email(raw_email: str) -> str:
    return (raw_email or "").strip().lower()


@router.get("/identity/{email}")
async def inspect_identity_conflict(
    email: str,
    _: dict = Depends(require_admin_or_key),
):
    """
    Inspect the auth + merchant onboarding state for a single email.

    Useful before converting or merging an identity into the merchant role.
    """
    from routes.merchant_onboarding_routes import (
        INTERNAL_ACCOUNT_ROLES,
        get_active_onboarding_by_email,
        get_user_auth_binding,
        normalize_account_role,
    )

    normalized_email = _normalize_email(email)
    existing_user = await get_user_auth_binding(normalized_email)
    existing_onboarding = await get_active_onboarding_by_email(normalized_email)

    current_role = normalize_account_role((existing_user or {}).get("role"))
    public_signup_resolution = "ready"
    if current_role in INTERNAL_ACCOUNT_ROLES:
        public_signup_resolution = "admin_merge_required"
    elif current_role and current_role != "merchant":
        public_signup_resolution = "verify_current_password_or_admin_merge"

    return {
        "status": "success",
        "email": normalized_email,
        "existing_user": {
            "id": existing_user.get("id"),
            "email": existing_user.get("email"),
            "role": current_role,
            "merchant_id": existing_user.get("merchant_id"),
            "active": existing_user.get("active"),
        } if existing_user else None,
        "existing_onboarding": {
            "merchant_id": existing_onboarding.get("merchant_id"),
            "business_name": existing_onboarding.get("business_name"),
            "store_url": existing_onboarding.get("store_url"),
            "status": existing_onboarding.get("status"),
            "contact_email": existing_onboarding.get("contact_email"),
        } if existing_onboarding else None,
        "public_signup_resolution": public_signup_resolution,
    }

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


@router.post("/merge-to-merchant")
async def merge_identity_to_merchant(
    request: MergeIdentityToMerchantRequest,
    _: dict = Depends(require_admin_or_key),
):
    """
    Convert or merge an existing identity into the merchant role.

    This is the explicit ops path for emails that are already occupied by
    another account type and cannot be auto-converted from the public signup.
    """
    from routes.merchant_onboarding_routes import (
        INTERNAL_ACCOUNT_ROLES,
        get_active_onboarding_by_email,
        get_user_auth_binding,
        normalize_account_role,
        sync_merchant_auth_user,
    )

    normalized_email = _normalize_email(request.email)
    existing_user = await get_user_auth_binding(normalized_email)
    existing_onboarding = await get_active_onboarding_by_email(normalized_email)

    target_merchant_id = request.merchant_id or (existing_onboarding or {}).get("merchant_id")
    if not target_merchant_id:
        raise HTTPException(
            status_code=404,
            detail="No merchant onboarding record found for this email. Provide merchant_id explicitly.",
        )

    merchant_record = await database.fetch_one(
        """
        SELECT merchant_id, business_name, contact_email
        FROM merchant_onboarding
        WHERE merchant_id = :merchant_id
        LIMIT 1
        """,
        {"merchant_id": target_merchant_id},
    )
    if not merchant_record:
        raise HTTPException(status_code=404, detail="Target merchant onboarding record not found")

    current_role = normalize_account_role((existing_user or {}).get("role"))
    if current_role in INTERNAL_ACCOUNT_ROLES and not request.force:
        raise HTTPException(
            status_code=409,
            detail=(
                "This email is attached to an internal role. "
                "Re-submit with force=true if you explicitly want to convert it into the merchant identity."
            ),
        )

    if not existing_user and not request.new_password:
        raise HTTPException(
            status_code=400,
            detail="new_password is required when creating a merchant identity from onboarding only.",
        )

    await database.execute(
        """
        UPDATE merchant_onboarding
        SET contact_email = :email
        WHERE merchant_id = :merchant_id
        """,
        {"email": normalized_email, "merchant_id": target_merchant_id},
    )

    await sync_merchant_auth_user(
        contact_email=normalized_email,
        business_name=merchant_record["business_name"] or normalized_email.split("@")[0],
        merchant_id=target_merchant_id,
        password=request.new_password,
        existing_user=existing_user,
    )

    refreshed_user = await get_user_auth_binding(normalized_email)

    return {
        "status": "success",
        "message": "Identity merged into merchant successfully.",
        "previous_role": current_role,
        "merchant_id": target_merchant_id,
        "user": {
            "id": refreshed_user.get("id"),
            "email": refreshed_user.get("email"),
            "role": refreshed_user.get("role"),
            "merchant_id": refreshed_user.get("merchant_id"),
            "active": refreshed_user.get("active"),
        } if refreshed_user else None,
    }
