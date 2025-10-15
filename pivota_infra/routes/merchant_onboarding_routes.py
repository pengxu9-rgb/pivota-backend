"""
Merchant Onboarding Routes - Phase 2
Handles merchant registration, KYC, PSP setup, and API key issuance
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from pydantic import BaseModel, EmailStr
from typing import Optional, Dict, Any
from datetime import datetime
import asyncio

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
        "message": f"{psp_data.psp_type.title()} connected successfully. Registered in payment router.",
        "merchant_id": result["merchant_id"],
        "api_key": result["api_key"],  # Return API key ONCE - save this!
        "psp_type": result["psp_type"],
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

