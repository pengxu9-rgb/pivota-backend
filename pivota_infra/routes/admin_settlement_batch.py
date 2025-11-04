"""
Admin Settlement Batch Processing Endpoints
Allows administrators to trigger and manage settlement batch processing
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from db.database import database
from utils.auth import get_current_user, require_admin
from services.settlement_batch_service import run_monthly_settlement_batch

router = APIRouter(
    prefix="/admin/settlements",
    tags=["Admin - Settlement Processing"]
)


class TriggerBatchRequest(BaseModel):
    period_end: Optional[str] = None  # ISO format datetime


@router.post("/batch/trigger")
async def trigger_settlement_batch(
    request: TriggerBatchRequest = None,
    current_user: dict = Depends(require_admin)
):
    """
    Trigger monthly settlement batch processing for all agents
    
    This will:
    1. Find all agents with unsettled commissions
    2. Calculate total commissions for the period
    3. Create settlement records
    4. Mark commissions as processed
    
    **Admin only**
    """
    try:
        period_end = None
        if request and request.period_end:
            try:
                period_end = datetime.fromisoformat(request.period_end.replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid period_end format. Use ISO format.")
        
        # Run settlement batch
        result = await run_monthly_settlement_batch(database)
        
        if result.get("status") == "success":
            return {
                "status": "success",
                "message": "Settlement batch processed successfully",
                "period_start": result.get("period_start"),
                "period_end": result.get("period_end"),
                "settlements_created": result.get("settlements_created"),
                "total_amount": result.get("total_amount"),
                "settlements": result.get("settlements", [])
            }
        else:
            raise HTTPException(
                status_code=500,
                detail=result.get("message", "Settlement batch processing failed")
            )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Settlement batch failed: {str(e)}")


@router.get("/batch/status")
async def get_settlement_batch_status(
    current_user: dict = Depends(require_admin)
):
    """
    Get status of recent settlement batches
    
    **Admin only**
    """
    try:
        # Get recent settlements grouped by period
        query = """
            SELECT 
                settlement_period_start,
                settlement_period_end,
                COUNT(*) as settlement_count,
                SUM(settlement_amount) as total_amount,
                COUNT(*) FILTER (WHERE status = 'pending') as pending_count,
                COUNT(*) FILTER (WHERE status = 'completed') as completed_count,
                MIN(created_at) as batch_created_at
            FROM agent_settlements
            WHERE created_at >= NOW() - INTERVAL '90 days'
            GROUP BY settlement_period_start, settlement_period_end
            ORDER BY settlement_period_start DESC
            LIMIT 10
        """
        
        batches = await database.fetch_all(query)
        
        return {
            "status": "success",
            "recent_batches": [
                {
                    "period_start": batch['settlement_period_start'].isoformat(),
                    "period_end": batch['settlement_period_end'].isoformat(),
                    "settlement_count": batch['settlement_count'],
                    "total_amount": float(batch['total_amount']),
                    "pending": batch['pending_count'],
                    "completed": batch['completed_count'],
                    "created_at": batch['batch_created_at'].isoformat()
                }
                for batch in batches
            ]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get batch status: {str(e)}")


@router.get("/pending")
async def get_pending_settlements(
    current_user: dict = Depends(require_admin)
):
    """
    Get list of pending settlements
    
    **Admin only**
    """
    try:
        query = """
            SELECT 
                settlement_id,
                agent_id,
                settlement_amount,
                total_transactions,
                settlement_period_start,
                settlement_period_end,
                created_at
            FROM agent_settlements
            WHERE status = 'pending'
            ORDER BY created_at DESC
            LIMIT 100
        """
        
        settlements = await database.fetch_all(query)
        
        return {
            "status": "success",
            "count": len(settlements),
            "settlements": [
                {
                    "settlement_id": s['settlement_id'],
                    "agent_id": s['agent_id'],
                    "amount": float(s['settlement_amount']),
                    "transactions": s['total_transactions'],
                    "period_start": s['settlement_period_start'].isoformat(),
                    "period_end": s['settlement_period_end'].isoformat(),
                    "created_at": s['created_at'].isoformat()
                }
                for s in settlements
            ]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get pending settlements: {str(e)}")


@router.post("/settlements/{settlement_id}/complete")
async def mark_settlement_complete(
    settlement_id: str,
    current_user: dict = Depends(require_admin)
):
    """
    Mark a settlement as completed (paid out)
    
    **Admin only**
    """
    try:
        query = """
            UPDATE agent_settlements
            SET status = 'completed',
                payout_date = NOW(),
                updated_at = NOW()
            WHERE settlement_id = :settlement_id
            AND status = 'pending'
            RETURNING settlement_id, agent_id, settlement_amount
        """
        
        result = await database.fetch_one(query, {"settlement_id": settlement_id})
        
        if not result:
            raise HTTPException(
                status_code=404,
                detail="Settlement not found or already completed"
            )
        
        return {
            "status": "success",
            "message": "Settlement marked as completed",
            "settlement_id": result['settlement_id'],
            "agent_id": result['agent_id'],
            "amount": float(result['settlement_amount'])
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to complete settlement: {str(e)}"
        )

