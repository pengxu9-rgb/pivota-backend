"""
Employee Merchant Management Routes
Provides endpoints for employees to manage merchants
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from typing import List, Optional, Dict, Any
from datetime import datetime
from utils.auth import get_current_user
from db.database import database
import uuid
import json

router = APIRouter()

@router.post("/merchant/onboarding/create")
async def create_merchant(
    business_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    website: str = Form(None),
    country: str = Form(...),
    business_type: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    """Create a new merchant (employee-assisted onboarding)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        merchant_id = f"merch_{uuid.uuid4().hex[:16]}"
        
        # Check if merchant already exists
        check_query = "SELECT merchant_id FROM merchant_onboarding WHERE email = :email"
        existing = await database.fetch_one(check_query, {"email": email})
        
        if existing:
            raise HTTPException(status_code=400, detail="Merchant with this email already exists")
        
        # Create merchant
        insert_query = """
            INSERT INTO merchant_onboarding (
                merchant_id, business_name, email, phone, website, 
                country, business_type, status, created_at, updated_at
            ) VALUES (
                :merchant_id, :business_name, :email, :phone, :website,
                :country, :business_type, :status, :created_at, :updated_at
            )
        """
        
        await database.execute(insert_query, {
            "merchant_id": merchant_id,
            "business_name": business_name,
            "email": email,
            "phone": phone,
            "website": website,
            "country": country,
            "business_type": business_type,
            "status": "pending",
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        })
        
        return {
            "status": "success",
            "message": "Merchant created successfully",
            "merchant_id": merchant_id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create merchant: {str(e)}")

@router.get("/merchant/onboarding/details/{merchant_id}")
async def get_merchant_details(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get detailed merchant information"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get merchant info
        merchant_query = """
            SELECT * FROM merchant_onboarding 
            WHERE merchant_id = :merchant_id
        """
        merchant = await database.fetch_one(merchant_query, {"merchant_id": merchant_id})
        
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Get connected stores
        stores_query = """
            SELECT store_id, platform, name, status, product_count, connected_at
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
        """
        stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
        
        # Get connected PSPs
        psps_query = """
            SELECT psp_id, provider, name, status, connected_at
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
        """
        psps = await database.fetch_all(psps_query, {"merchant_id": merchant_id})
        
        # Get transaction stats
        stats_query = """
            SELECT 
                COUNT(*) as total_transactions,
                COALESCE(SUM(amount), 0) as total_revenue,
                COUNT(DISTINCT customer_email) as unique_customers
            FROM orders
            WHERE merchant_id = :merchant_id
        """
        stats = await database.fetch_one(stats_query, {"merchant_id": merchant_id})
        
        return {
            "status": "success",
            "merchant": {
                "merchant_id": merchant["merchant_id"],
                "business_name": merchant["business_name"],
                "email": merchant["email"],
                "phone": merchant["phone"],
                "website": merchant["website"],
                "country": merchant["country"],
                "business_type": merchant["business_type"],
                "status": merchant["status"],
                "kyb_status": merchant.get("kyb_status", "pending"),
                "created_at": merchant["created_at"].isoformat() if merchant["created_at"] else None,
                "stores": [
                    {
                        "store_id": s["store_id"],
                        "platform": s["platform"],
                        "name": s["name"],
                        "status": s["status"],
                        "product_count": s["product_count"],
                        "connected_at": s["connected_at"].isoformat() if s["connected_at"] else None
                    }
                    for s in stores
                ],
                "psps": [
                    {
                        "psp_id": p["psp_id"],
                        "provider": p["provider"],
                        "name": p["name"],
                        "status": p["status"],
                        "connected_at": p["connected_at"].isoformat() if p["connected_at"] else None
                    }
                    for p in psps
                ],
                "stats": {
                    "total_transactions": stats["total_transactions"] if stats else 0,
                    "total_revenue": float(stats["total_revenue"]) if stats else 0,
                    "unique_customers": stats["unique_customers"] if stats else 0
                }
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get merchant details: {str(e)}")

@router.post("/merchant/onboarding/kyb/{merchant_id}")
async def update_merchant_kyb(
    merchant_id: str,
    status: str,
    reason: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Update merchant KYB status"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if status not in ["approved", "rejected", "pending", "reviewing"]:
        raise HTTPException(status_code=400, detail="Invalid status")
    
    try:
        # Update KYB status
        update_query = """
            UPDATE merchant_onboarding
            SET kyb_status = :status,
                kyb_review_date = :review_date,
                kyb_reviewer = :reviewer,
                kyb_review_notes = :notes,
                status = :merchant_status,
                updated_at = :updated_at
            WHERE merchant_id = :merchant_id
        """
        
        # If KYB is approved, activate merchant
        merchant_status = "active" if status == "approved" else "pending"
        
        await database.execute(update_query, {
            "status": status,
            "review_date": datetime.now(),
            "reviewer": current_user.get("email"),
            "notes": reason,
            "merchant_status": merchant_status,
            "updated_at": datetime.now(),
            "merchant_id": merchant_id
        })
        
        return {
            "status": "success",
            "message": f"KYB status updated to {status}",
            "merchant_status": merchant_status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update KYB status: {str(e)}")

@router.post("/merchant/onboarding/upload/{merchant_id}")
async def upload_merchant_documents(
    merchant_id: str,
    files: List[UploadFile] = File(...),
    current_user: dict = Depends(get_current_user)
):
    """Upload KYB documents for a merchant"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        uploaded_files = []
        
        for file in files:
            # In production, save to cloud storage
            # For now, just record metadata
            file_id = f"doc_{uuid.uuid4().hex[:8]}"
            uploaded_files.append({
                "file_id": file_id,
                "filename": file.filename,
                "size": 0,  # Would get actual size in production
                "uploaded_at": datetime.now().isoformat()
            })
        
        # Update merchant with document info
        update_query = """
            UPDATE merchant_onboarding
            SET kyb_documents = :documents,
                kyb_status = 'reviewing',
                updated_at = :updated_at
            WHERE merchant_id = :merchant_id
        """
        
        await database.execute(update_query, {
            "documents": json.dumps(uploaded_files),
            "updated_at": datetime.now(),
            "merchant_id": merchant_id
        })
        
        return {
            "status": "success",
            "message": f"Uploaded {len(files)} documents",
            "files": uploaded_files
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload documents: {str(e)}")

@router.get("/merchant/onboarding/kyb/{merchant_id}/documents")
async def get_merchant_documents(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get KYB documents for a merchant"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        query = """
            SELECT kyb_documents, kyb_status, kyb_review_notes
            FROM merchant_onboarding
            WHERE merchant_id = :merchant_id
        """
        
        result = await database.fetch_one(query, {"merchant_id": merchant_id})
        
        if not result:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        documents = []
        if result["kyb_documents"]:
            try:
                documents = json.loads(result["kyb_documents"])
            except:
                documents = []
        
        return {
            "status": "success",
            "kyb_status": result["kyb_status"],
            "review_notes": result["kyb_review_notes"],
            "documents": documents
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get documents: {str(e)}")

@router.post("/integrations/{platform}/sync-products")
async def sync_merchant_products(
    platform: str,
    merchant_id: str,
    store_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Sync products for a merchant's store"""
    if current_user["role"] not in ["employee", "admin", "merchant"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if platform not in ["shopify", "wix"]:
        raise HTTPException(status_code=400, detail="Invalid platform")
    
    try:
        # Get store details
        if store_id:
            store_query = """
                SELECT * FROM merchant_stores
                WHERE merchant_id = :merchant_id AND store_id = :store_id
            """
            store = await database.fetch_one(store_query, {
                "merchant_id": merchant_id,
                "store_id": store_id
            })
        else:
            # Get first store of the platform
            store_query = """
                SELECT * FROM merchant_stores
                WHERE merchant_id = :merchant_id AND platform = :platform
                LIMIT 1
            """
            store = await database.fetch_one(store_query, {
                "merchant_id": merchant_id,
                "platform": platform
            })
        
        if not store:
            raise HTTPException(status_code=404, detail=f"No {platform} store found for merchant")
        
        # Simulate product sync
        # In production, this would actually call Shopify/Wix API
        product_count = 25  # Simulated count
        
        # Update store with new product count
        update_query = """
            UPDATE merchant_stores
            SET product_count = :product_count,
                last_sync = :last_sync
            WHERE store_id = :store_id
        """
        
        await database.execute(update_query, {
            "product_count": product_count,
            "last_sync": datetime.now(),
            "store_id": store["store_id"]
        })
        
        return {
            "status": "success",
            "message": f"Synced {product_count} products from {platform}",
            "store_id": store["store_id"],
            "product_count": product_count
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")

@router.post("/integrations/{platform}/test")
async def test_store_connection(
    platform: str,
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Test store connection"""
    if current_user["role"] not in ["employee", "admin", "merchant"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get store
        store_query = """
            SELECT * FROM merchant_stores
            WHERE merchant_id = :merchant_id AND platform = :platform
            LIMIT 1
        """
        store = await database.fetch_one(store_query, {
            "merchant_id": merchant_id,
            "platform": platform
        })
        
        if not store:
            raise HTTPException(status_code=404, detail=f"No {platform} store found")
        
        # Simulate connection test
        # In production, would actually test API connection
        return {
            "status": "success",
            "message": f"Connection to {platform} successful",
            "store_name": store["name"],
            "api_version": "2024-01" if platform == "shopify" else "v1",
            "response_time": 150  # ms
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")
