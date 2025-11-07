"""
Merchant Payout Management Routes
Allows merchants to create, upload, and manage agent payouts
Phase 6 - Payouts & Banking
"""

from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import date, datetime
import logging

from db.database import database
from db.payout_repo import PayoutRepo
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/merchants/{merchant_id}/payouts", tags=["merchant-payouts"])

# Request/Response Models
class PayoutItem(BaseModel):
    agent_id: str
    amount: float = Field(gt=0, description="Payout amount")
    currency: Optional[str] = "USD"
    period_start: date
    period_end: date
    metadata: Optional[dict] = None

class CreatePayoutRequest(BaseModel):
    items: List[PayoutItem]

class UploadPayoutRequest(BaseModel):
    reference: str = Field(..., description="Payment reference number")
    file_url: Optional[str] = Field(None, description="URL to payment proof")
    method: Optional[str] = Field(None, description="Payment method (wire, ach, paypal)")
    provider: Optional[str] = Field(None, description="Payment provider name")
    external_id: Optional[str] = Field(None, description="External transaction ID")

class PayoutResponse(BaseModel):
    id: int
    merchant_id: str
    agent_id: str
    amount: float
    currency: str
    status: str
    payout_reference: Optional[str]
    file_url: Optional[str]
    method: Optional[str]
    provider: Optional[str]
    external_id: Optional[str]
    period_start: date
    period_end: date
    uploaded_at: Optional[datetime]
    confirmed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

@router.get("", response_model=dict)
async def list_payouts(
    merchant_id: str,
    status: Optional[str] = Query(None, description="Filter by status: pending, uploaded, paid"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(get_current_user)
):
    """
    List all payouts for a merchant with optional status filter
    """
    # Verify merchant access
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can access this endpoint")
    
    if current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this merchant's payouts")
    
    try:
        repo = PayoutRepo()
        
        # Get paginated results
        items = await repo.list(
            merchant_id=merchant_id,
            agent_id=None,
            status=status,
            limit=size,
            offset=(page-1)*size
        )
        
        # Get total count for pagination
        count_query = "SELECT COUNT(*) as total FROM agent_payouts WHERE merchant_id = :mid"
        params = {"mid": merchant_id}
        if status:
            count_query += " AND status = :st"
            params["st"] = status
        
        total_result = await database.fetch_one(query=count_query, values=params)
        total = total_result["total"] if total_result else 0
        
        # Get summary statistics
        summary = await repo.get_summary_by_merchant(merchant_id, status)
        
        return {
            "items": items,
            "total": total,
            "page": page,
            "pages": (total + size - 1) // size if size > 0 else 0,
            "summary": summary
        }
        
    except Exception as e:
        logger.error(f"Error listing payouts for merchant {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to list payouts")

@router.post("/bulk", response_model=dict)
async def create_bulk_payouts(
    merchant_id: str,
    request: CreatePayoutRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Create multiple payouts in bulk
    """
    # Verify merchant access
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can access this endpoint")
    
    if current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Validate items
        if not request.items:
            raise HTTPException(status_code=400, detail="No payout items provided")
        
        if len(request.items) > 1000:
            raise HTTPException(status_code=400, detail="Maximum 1000 payouts per batch")
        
        # Convert Pydantic models to dicts
        items_data = [item.dict() for item in request.items]
        
        # Create payouts
        repo = PayoutRepo()
        ids = await repo.create_bulk(merchant_id, items_data)
        
        logger.info(f"Merchant {merchant_id} created {len(ids)} payouts")
        
        return {
            "status": "success",
            "created": len(ids),
            "ids": ids,
            "message": f"Created {len(ids)} payouts successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating bulk payouts: {e}")
        raise HTTPException(status_code=500, detail="Failed to create payouts")

@router.get("/{payout_id}", response_model=PayoutResponse)
async def get_payout_details(
    merchant_id: str,
    payout_id: int,
    current_user: dict = Depends(get_current_user)
):
    """
    Get details of a specific payout
    """
    # Verify merchant access
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can access this endpoint")
    
    if current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        repo = PayoutRepo()
        payout = await repo.get_by_id(payout_id)
        
        if not payout:
            raise HTTPException(status_code=404, detail="Payout not found")
        
        if payout["merchant_id"] != merchant_id:
            raise HTTPException(status_code=403, detail="Not authorized to access this payout")
        
        return payout
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting payout {payout_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payout details")

@router.post("/{payout_id}/upload", response_model=dict)
async def upload_payout_proof(
    merchant_id: str,
    payout_id: int,
    request: UploadPayoutRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Upload payment proof and mark payout as uploaded
    """
    # Verify merchant access
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can access this endpoint")
    
    if current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Verify payout belongs to merchant and is pending
        repo = PayoutRepo()
        payout = await repo.get_by_id(payout_id)
        
        if not payout:
            raise HTTPException(status_code=404, detail="Payout not found")
        
        if payout["merchant_id"] != merchant_id:
            raise HTTPException(status_code=403, detail="Not authorized to update this payout")
        
        if payout["status"] != "pending":
            raise HTTPException(
                status_code=400, 
                detail=f"Payout is already {payout['status']}. Only pending payouts can be uploaded."
            )
        
        # Upload payment proof
        await repo.upload(
            payout_id=payout_id,
            reference=request.reference,
            file_url=request.file_url,
            method=request.method,
            provider=request.provider,
            external_id=request.external_id
        )
        
        logger.info(f"Merchant {merchant_id} uploaded proof for payout {payout_id}")
        
        return {
            "status": "success",
            "message": "Payment proof uploaded successfully",
            "payout_id": payout_id,
            "new_status": "uploaded"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading proof for payout {payout_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload payment proof")

@router.get("/export/csv", response_model=dict)
async def export_payouts_csv(
    merchant_id: str,
    status: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    """
    Export payouts as CSV (returns data, frontend handles download)
    """
    # Verify merchant access
    if current_user.get("role") != "merchant":
        raise HTTPException(status_code=403, detail="Only merchants can access this endpoint")
    
    if current_user.get("merchant_id") != merchant_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Build query with filters
        conditions = ["merchant_id = :mid"]
        params = {"mid": merchant_id}
        
        if status:
            conditions.append("status = :st")
            params["st"] = status
        
        if start_date:
            conditions.append("created_at >= :start")
            params["start"] = start_date
        
        if end_date:
            conditions.append("created_at <= :end")
            params["end"] = end_date
        
        where_clause = " AND ".join(conditions)
        
        query = f"""
        SELECT 
            id,
            agent_id,
            amount,
            currency,
            status,
            payout_reference,
            method,
            provider,
            period_start,
            period_end,
            created_at,
            uploaded_at,
            confirmed_at
        FROM agent_payouts
        WHERE {where_clause}
        ORDER BY created_at DESC
        """
        
        results = await database.fetch_all(query=query, values=params)
        
        # Convert to list of dicts for CSV export
        data = []
        for row in results:
            data.append({
                "Payout ID": row["id"],
                "Agent ID": row["agent_id"],
                "Amount": row["amount"],
                "Currency": row["currency"],
                "Status": row["status"],
                "Reference": row["payout_reference"] or "",
                "Method": row["method"] or "",
                "Provider": row["provider"] or "",
                "Period Start": row["period_start"],
                "Period End": row["period_end"],
                "Created": row["created_at"],
                "Uploaded": row["uploaded_at"] or "",
                "Confirmed": row["confirmed_at"] or ""
            })
        
        return {
            "status": "success",
            "count": len(data),
            "data": data,
            "filename": f"payouts_{merchant_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        }
        
    except Exception as e:
        logger.error(f"Error exporting payouts: {e}")
        raise HTTPException(status_code=500, detail="Failed to export payouts")

# Note: File upload endpoint would typically be separate for actual file storage
# This is a simplified version where frontend provides the URL after uploading to cloud storage
