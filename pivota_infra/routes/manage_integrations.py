"""Manage Integrations - Delete and Update"""
from fastapi import APIRouter, Depends, HTTPException, Body
from typing import Dict, Any
from db.database import database
from utils.auth import get_current_user

router = APIRouter()

async def get_merchant_id_from_user(current_user: dict) -> str:
    """Get merchant ID from JWT token"""
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        row = await database.fetch_one(
            "SELECT merchant_id FROM merchant_onboarding WHERE contact_email = :email LIMIT 1",
            {"email": current_user.get("email")}
        )
        if row:
            merchant_id = row["merchant_id"]
    
    if not merchant_id:
        raise HTTPException(status_code=404, detail="Merchant ID not found")
    
    return merchant_id

# ============================================================================
# Store Management
# ============================================================================

@router.delete("/merchant/integrations/store/{store_id}")
async def delete_store(
    store_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a connected store"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Verify ownership before deleting
        check_query = "SELECT store_id FROM merchant_stores WHERE store_id = :store_id AND merchant_id = :merchant_id"
        store = await database.fetch_one(check_query, {"store_id": store_id, "merchant_id": merchant_id})
        
        if not store:
            raise HTTPException(status_code=404, detail="Store not found or not owned by this merchant")
        
        # Delete the store
        delete_query = "DELETE FROM merchant_stores WHERE store_id = :store_id AND merchant_id = :merchant_id"
        await database.execute(delete_query, {"store_id": store_id, "merchant_id": merchant_id})
        
        return {
            "status": "success",
            "message": "Store disconnected successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete store: {str(e)}")

@router.put("/merchant/integrations/store/{store_id}")
async def update_store(
    store_id: str,
    store_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Update store settings (e.g., name, api_key)"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Verify ownership
        check_query = "SELECT store_id FROM merchant_stores WHERE store_id = :store_id AND merchant_id = :merchant_id"
        store = await database.fetch_one(check_query, {"store_id": store_id, "merchant_id": merchant_id})
        
        if not store:
            raise HTTPException(status_code=404, detail="Store not found or not owned by this merchant")
        
        # Build update query dynamically based on provided fields
        update_fields = []
        values = {"store_id": store_id, "merchant_id": merchant_id}
        
        if "name" in store_data:
            update_fields.append("name = :name")
            values["name"] = store_data["name"]
        
        if "api_key" in store_data:
            update_fields.append("api_key = :api_key")
            values["api_key"] = store_data["api_key"]
        
        if "status" in store_data:
            update_fields.append("status = :status")
            values["status"] = store_data["status"]
        
        if not update_fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        update_query = f"""
            UPDATE merchant_stores 
            SET {", ".join(update_fields)}
            WHERE store_id = :store_id AND merchant_id = :merchant_id
        """
        await database.execute(update_query, values)
        
        return {
            "status": "success",
            "message": "Store updated successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update store: {str(e)}")

# ============================================================================
# PSP Management
# ============================================================================

@router.delete("/merchant/integrations/psp/{psp_id}")
async def delete_psp(
    psp_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a connected PSP"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Verify ownership before deleting
        check_query = "SELECT psp_id FROM merchant_psps WHERE psp_id = :psp_id AND merchant_id = :merchant_id"
        psp = await database.fetch_one(check_query, {"psp_id": psp_id, "merchant_id": merchant_id})
        
        if not psp:
            raise HTTPException(status_code=404, detail="PSP not found or not owned by this merchant")
        
        # Delete the PSP
        delete_query = "DELETE FROM merchant_psps WHERE psp_id = :psp_id AND merchant_id = :merchant_id"
        await database.execute(delete_query, {"psp_id": psp_id, "merchant_id": merchant_id})
        
        return {
            "status": "success",
            "message": "PSP disconnected successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete PSP: {str(e)}")

@router.put("/merchant/integrations/psp/{psp_id}")
async def update_psp(
    psp_id: str,
    psp_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Update PSP settings (e.g., api_key, status)"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Verify ownership
        check_query = "SELECT psp_id FROM merchant_psps WHERE psp_id = :psp_id AND merchant_id = :merchant_id"
        psp = await database.fetch_one(check_query, {"psp_id": psp_id, "merchant_id": merchant_id})
        
        if not psp:
            raise HTTPException(status_code=404, detail="PSP not found or not owned by this merchant")
        
        # Build update query dynamically
        update_fields = []
        values = {"psp_id": psp_id, "merchant_id": merchant_id}
        
        if "api_key" in psp_data:
            update_fields.append("api_key = :api_key")
            values["api_key"] = psp_data["api_key"]
        
        if "status" in psp_data:
            update_fields.append("status = :status")
            values["status"] = psp_data["status"]
        
        if "name" in psp_data:
            update_fields.append("name = :name")
            values["name"] = psp_data["name"]
        
        if not update_fields:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        update_query = f"""
            UPDATE merchant_psps 
            SET {", ".join(update_fields)}
            WHERE psp_id = :psp_id AND merchant_id = :merchant_id
        """
        await database.execute(update_query, values)
        
        return {
            "status": "success",
            "message": "PSP updated successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update PSP: {str(e)}")

# ============================================================================
# Batch Operations
# ============================================================================

@router.post("/merchant/integrations/cleanup")
async def cleanup_integrations(current_user: dict = Depends(get_current_user)):
    """Remove all inactive or test integrations"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Delete inactive stores
        await database.execute(
            "DELETE FROM merchant_stores WHERE merchant_id = :merchant_id AND status = 'inactive'",
            {"merchant_id": merchant_id}
        )
        
        # Delete inactive PSPs
        await database.execute(
            "DELETE FROM merchant_psps WHERE merchant_id = :merchant_id AND status = 'inactive'",
            {"merchant_id": merchant_id}
        )
        
        return {
            "status": "success",
            "message": "Inactive integrations cleaned up"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cleanup: {str(e)}")






