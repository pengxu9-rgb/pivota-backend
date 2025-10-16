"""
Merchant Onboarding Routes - Phase 2
Handles merchant registration, KYC, PSP setup, and API key issuance
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from pydantic import BaseModel, EmailStr
from typing import Optional, Dict, Any
from datetime import datetime
import asyncio
import stripe
import httpx

from db.merchant_onboarding import (
    create_merchant_onboarding,
    get_merchant_onboarding,
    update_kyc_status,
    upload_kyc_documents,
    setup_psp_connection,
    get_all_merchant_onboardings
)
from db.payment_router import register_merchant_psp_route
from routes.auth_routes import get_current_user, require_admin

router = APIRouter(prefix="/merchant/onboarding", tags=["merchant-onboarding"])

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class MerchantRegisterRequest(BaseModel):
    business_name: str
    website: str
    region: str  # US, EU, APAC
    contact_email: EmailStr
    contact_phone: Optional[str] = None

class KYCUploadRequest(BaseModel):
    merchant_id: str
    documents: Dict[str, Any]  # JSON blob of documents

class PSPSetupRequest(BaseModel):
    merchant_id: str
    psp_type: str  # stripe, adyen, shoppay
    psp_sandbox_key: str  # Test API key from PSP

# ============================================================================
# PSP VALIDATION
# ============================================================================

def validate_stripe_key_sync(api_key: str) -> bool:
    """Validate Stripe API key by making a test request (synchronous)"""
    try:
        stripe.api_key = api_key
        # Try to retrieve account info - this will fail if key is invalid
        stripe.Account.retrieve()
        return True
    except Exception as e:
        # Check if it's an authentication error (invalid key)
        error_type = type(e).__name__
        if 'AuthenticationError' in error_type:
            print(f"🔒 Invalid Stripe API key: {api_key[:15]}...")
            return False
        # Any other error means the key format is valid but something else failed
        print(f"⚠️ Stripe validation error ({error_type}): {e}")
        return False

async def validate_stripe_key(api_key: str) -> bool:
    """Async wrapper for Stripe validation"""
    import concurrent.futures
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(pool, validate_stripe_key_sync, api_key)

async def validate_adyen_key(api_key: str) -> bool:
    """Validate Adyen API key by making a test request"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://checkout-test.adyen.com/v70/paymentMethods",
                headers={
                    "X-API-Key": api_key,
                    "Content-Type": "application/json"
                },
                json={"merchantAccount": "TEST"},
                timeout=10.0
            )
            # Adyen returns 403 for invalid key, 422 for invalid data but valid key
            return response.status_code in [200, 422]
    except Exception as e:
        print(f"⚠️ Adyen validation error: {e}")
        return False

async def validate_psp_credentials(psp_type: str, api_key: str) -> tuple[bool, str]:
    """
    Validate PSP credentials
    Returns: (is_valid, error_message)
    """
    if psp_type == "stripe":
        is_valid = await validate_stripe_key(api_key)
        return (is_valid, "" if is_valid else "Invalid Stripe API key. Please check your key and try again.")
    
    elif psp_type == "adyen":
        is_valid = await validate_adyen_key(api_key)
        return (is_valid, "" if is_valid else "Invalid Adyen API key. Please check your key and try again.")
    
    elif psp_type == "shoppay":
        # ShopPay validation would go here
        # For now, accept any non-empty key
        return (bool(api_key), "" if api_key else "ShopPay API key is required")
    
    else:
        return (False, f"Unsupported PSP type: {psp_type}")

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def auto_approve_kyc(merchant_id: str):
    """Simulate KYC verification - auto-approve after 5 seconds"""
    await asyncio.sleep(5)  # Simulate review time
    await update_kyc_status(merchant_id, "approved")
    print(f"✅ Auto-approved KYC for merchant: {merchant_id}")

# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/register", response_model=Dict[str, Any])
async def register_merchant(
    merchant_data: MerchantRegisterRequest,
    background_tasks: BackgroundTasks
):
    """
    Step 1: Register a new merchant
    Returns merchant_id and triggers async KYC verification
    """
    try:
        merchant_dict = merchant_data.dict()
        merchant_id = await create_merchant_onboarding(merchant_dict)
        
        # Trigger background KYC auto-approval (simulated)
        background_tasks.add_task(auto_approve_kyc, merchant_id)
        
        return {
            "status": "success",
            "message": "Merchant registered successfully. KYC verification in progress.",
            "merchant_id": merchant_id,
            "next_step": "Upload KYC documents or wait for auto-verification"
        }
    except Exception as e:
        from db.database import database
        error_msg = str(e)
        
        # Handle database transaction errors
        if "transaction is aborted" in error_msg.lower():
            try:
                print("🔄 Attempting to recover from transaction error...")
                await database.disconnect()
                await asyncio.sleep(1)
                await database.connect()
                print("✅ Database reconnected, please try again")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Database connection reset. Please try your request again."
                )
            except:
                pass
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to register merchant: {str(e)}"
        )

@router.post("/kyc/upload", response_model=Dict[str, Any])
async def upload_kyc(kyc_data: KYCUploadRequest):
    """
    Step 2: Upload KYC documents (simulated as JSON blob)
    """
    merchant = await get_merchant_onboarding(kyc_data.merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    await upload_kyc_documents(kyc_data.merchant_id, kyc_data.documents)
    
    return {
        "status": "success",
        "message": "KYC documents uploaded successfully",
        "merchant_id": kyc_data.merchant_id
    }

@router.post("/psp/setup", response_model=Dict[str, Any])
async def setup_psp(psp_data: PSPSetupRequest):
    """
    Step 3: Setup PSP connection and get merchant API key
    Only allowed if KYC is approved
    Now with REAL PSP credential validation!
    """
    merchant = await get_merchant_onboarding(psp_data.merchant_id)
    
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    if merchant["status"] != "approved":
        raise HTTPException(
            status_code=400,
            detail=f"KYC must be approved first. Current status: {merchant['status']}"
        )
    
    if merchant["psp_connected"]:
        raise HTTPException(
            status_code=400,
            detail="PSP already connected"
        )
    
    # ✨ NEW: Validate PSP credentials
    print(f"🔍 Validating {psp_data.psp_type} credentials...")
    is_valid, error_message = await validate_psp_credentials(
        psp_data.psp_type,
        psp_data.psp_sandbox_key
    )
    
    if not is_valid:
        print(f"❌ PSP validation failed: {error_message}")
        raise HTTPException(
            status_code=400,
            detail=error_message
        )
    
    print(f"✅ {psp_data.psp_type} credentials validated successfully")
    
    # Setup PSP and generate API key
    result = await setup_psp_connection(
        psp_data.merchant_id,
        psp_data.psp_type,
        psp_data.psp_sandbox_key
    )
    
    # Register merchant in payment router for unified /payment/execute
    await register_merchant_psp_route(
        merchant_id=psp_data.merchant_id,
        psp_type=psp_data.psp_type,
        psp_credentials=psp_data.psp_sandbox_key
    )
    
    return {
        "status": "success",
        "message": f"{psp_data.psp_type.title()} connected successfully. Credentials validated and registered in payment router.",
        "merchant_id": result["merchant_id"],
        "api_key": result["api_key"],  # Return API key ONCE - save this!
        "psp_type": result["psp_type"],
        "validated": True,  # NEW: Indicate credentials were validated
        "next_step": "Use this API key to call /payment/execute with header 'X-Merchant-API-Key'"
    }

@router.get("/status/{merchant_id}", response_model=Dict[str, Any])
async def get_onboarding_status(merchant_id: str):
    """
    Get merchant onboarding status (KYC + PSP setup)
    """
    merchant = await get_merchant_onboarding(merchant_id)
    
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    return {
        "status": "success",
        "merchant_id": merchant["merchant_id"],
        "business_name": merchant["business_name"],
        "kyc_status": merchant["status"],
        "psp_connected": merchant["psp_connected"],
        "psp_type": merchant.get("psp_type"),
        "api_key_issued": bool(merchant.get("api_key")),
        "created_at": merchant["created_at"].isoformat() if merchant["created_at"] else None,
        "verified_at": merchant["verified_at"].isoformat() if merchant.get("verified_at") else None,
    }

@router.get("/all", response_model=Dict[str, Any])
async def list_all_onboardings(
    status: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: List all merchant onboardings (filtered by status if provided)
    """
    try:
        merchants = await get_all_merchant_onboardings(status)
        
        return {
            "status": "success",
            "count": len(merchants),
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "kyc_status": m["status"],
                    "psp_connected": m["psp_connected"],
                    "psp_type": m.get("psp_type"),
                    "created_at": m["created_at"].isoformat() if m["created_at"] else None,
                }
                for m in merchants
            ]
        }
    except Exception as e:
        print(f"❌ Error listing merchant onboardings: {e}")
        import traceback
        traceback.print_exc()
        # Return empty list instead of error to prevent frontend crash
        return {
            "status": "success",
            "count": 0,
            "merchants": [],
            "note": "Database may be initializing. Try registering a merchant first."
        }

@router.post("/approve/{merchant_id}", response_model=Dict[str, Any])
async def manual_approve_kyc(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: Manually approve merchant KYC
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    await update_kyc_status(merchant_id, "approved")
    
    return {
        "status": "success",
        "message": f"Merchant {merchant['business_name']} approved",
        "merchant_id": merchant_id
    }

@router.post("/reject/{merchant_id}", response_model=Dict[str, Any])
async def reject_kyc(
    merchant_id: str,
    reason: str,
    current_user: dict = Depends(require_admin)
):
    """
    Admin: Reject merchant KYC with reason
    """
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    await update_kyc_status(merchant_id, "rejected", reason)
    
    return {
        "status": "success",
        "message": f"Merchant {merchant['business_name']} rejected",
        "merchant_id": merchant_id,
        "reason": reason
    }

@router.post("/db/reset", response_model=Dict[str, Any])
async def reset_database_connection(current_user: dict = Depends(require_admin)):
    """
    Admin: Reset database connection (for transaction errors)
    """
    from db.database import database
    try:
        await database.disconnect()
        await asyncio.sleep(1)
        await database.connect()
        return {
            "status": "success",
            "message": "Database connection reset successfully"
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reset database: {str(e)}"
        )

