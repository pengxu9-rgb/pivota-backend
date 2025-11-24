"""
Merchant Management Routes
Endpoints for merchant onboarding, KYB, and management
"""

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from utils.auth import get_current_user, require_admin
from db.merchants import (
    create_merchant, get_merchant, get_all_merchants,
    update_merchant_status, add_kyb_document, get_merchant_documents,
    verify_document, soft_delete_merchant
)
from datetime import datetime
import os
import shutil

router = APIRouter(prefix="/merchants", tags=["merchants"])

# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class MerchantOnboardingRequest(BaseModel):
    business_name: str
    legal_name: Optional[str] = None  # Optional - defaults to business_name if not provided
    platform: str  # shopify, wix, custom
    store_url: str
    contact_email: EmailStr
    contact_phone: str
    business_type: str
    country: str
    expected_monthly_volume: float
    description: Optional[str] = ""

class MerchantStatusUpdate(BaseModel):
    status: str  # approved, rejected
    rejection_reason: Optional[str] = None

# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/onboard")
async def onboard_merchant(
    merchant_data: MerchantOnboardingRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Onboard a new merchant (employee/agent creates application)
    """
    try:
        merchant_dict = merchant_data.dict()
        merchant_dict["status"] = "pending"
        merchant_dict["verification_status"] = "pending"
        
        # Use business_name as legal_name if not provided
        if not merchant_dict.get("legal_name"):
            merchant_dict["legal_name"] = merchant_dict["business_name"]
        
        merchant_id = await create_merchant(merchant_dict)
        
        return {
            "status": "success",
            "message": "Merchant application submitted successfully",
            "merchant_id": merchant_id,
            "next_step": "Upload KYB documents"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create merchant: {str(e)}"
        )

@router.post("/{merchant_id}/documents/upload")
async def upload_kyb_document(
    merchant_id: int,
    document_type: str = Form(...),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    """
    Upload a KYB document for a merchant
    """
    try:
        # Verify merchant exists
        merchant = await get_merchant(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Create upload directory if it doesn't exist
        upload_dir = f"uploads/kyb/{merchant_id}"
        os.makedirs(upload_dir, exist_ok=True)
        
        # Save file
        file_path = f"{upload_dir}/{file.filename}"
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Record in database
        doc_data = {
            "document_type": document_type,
            "file_name": file.filename,
            "file_path": file_path,
            "file_size": os.path.getsize(file_path)
        }
        
        doc_id = await add_kyb_document(merchant_id, doc_data)
        
        return {
            "status": "success",
            "message": "Document uploaded successfully",
            "document_id": doc_id,
            "file_name": file.filename
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload document: {str(e)}"
        )

@router.get("/{merchant_id}")
async def get_merchant_details(
    merchant_id: int,
    current_user: dict = Depends(get_current_user)
):
    """
    Get merchant details with documents
    """
    merchant = await get_merchant(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    documents = await get_merchant_documents(merchant_id)
    
    return {
        "status": "success",
        "merchant": merchant,
        "documents": documents
    }

@router.get("/")
async def list_merchants(
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """
    List all merchants (optionally filtered by status)
    """
    merchants = await get_all_merchants(status)
    return {
        "status": "success",
        "merchants": merchants,
        "count": len(merchants)
    }

@router.post("/{merchant_id}/approve")
async def approve_merchant(
    merchant_id: int,
    current_user: dict = Depends(require_admin)
):
    """
    Approve a merchant application (admin only)
    """
    merchant = await get_merchant(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    # Use user_id from current_user dict (from JWT token)
    admin_user_id = current_user.get("user_id", current_user.get("id", "unknown"))
    await update_merchant_status(merchant_id, "approved", admin_user_id)
    
    return {
        "status": "success",
        "message": f"Merchant {merchant['business_name']} approved successfully",
        "merchant_id": merchant_id
    }

@router.post("/{merchant_id}/reject")
async def reject_merchant(
    merchant_id: int,
    update_data: MerchantStatusUpdate,
    current_user: dict = Depends(require_admin)
):
    """
    Reject a merchant application (admin only)
    """
    merchant = await get_merchant(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    # Use user_id from current_user dict (from JWT token)
    admin_user_id = current_user.get("user_id", current_user.get("id", "unknown"))
    await update_merchant_status(merchant_id, "rejected", admin_user_id)
    
    return {
        "status": "success",
        "message": f"Merchant {merchant['business_name']} rejected",
        "reason": update_data.rejection_reason,
        "merchant_id": merchant_id
    }

@router.post("/documents/{doc_id}/verify")
async def verify_kyb_document(
    doc_id: int,
    current_user: dict = Depends(require_admin)
):
    """
    Verify a KYB document (admin only)
    """
    await verify_document(doc_id, current_user["id"])
    
    return {
        "status": "success",
        "message": "Document verified successfully",
        "document_id": doc_id
    }

# ---------------------------------------------------------------------------
# Soft delete merchant (admin only)
# ---------------------------------------------------------------------------
@router.delete("/{merchant_id}")
async def delete_merchant(
    merchant_id: int,
    reason: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """
    Soft-delete a merchant by setting status='deleted'.
    """
    merchant = await get_merchant(merchant_id)
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    admin_user_id = current_user.get("user_id", current_user.get("id", "unknown"))
    await soft_delete_merchant(merchant_id, admin_user_id, reason)

    return {
        "status": "success",
        "message": f"Merchant {merchant['business_name']} removed",
        "merchant_id": merchant_id
    }
