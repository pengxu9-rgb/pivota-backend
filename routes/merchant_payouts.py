"""
Merchant Payout Management Routes
Handles commission payout operations for merchants
"""

import os

from fastapi import APIRouter, Depends, Query, Path, HTTPException
from typing import Optional, List, Dict, Any
from datetime import date, datetime
from db.database import database
from db.payout_repo import PayoutRepo
from utils.auth import get_current_user
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/merchants/{merchant_id}/payouts",
    tags=["payouts"]
)


async def _fetch_unpaid_commission_entries(
    merchant_id: str,
    period_start: datetime,
    period_end: datetime,
    agent_ids: Optional[List[str]] = None
) -> Dict[str, Dict[str, Any]]:
    """Load unpaid commission rows from commissions table and aggregate per agent."""
    agent_filter = "AND c.agent_id = ANY(:agent_ids)" if agent_ids else ""
    params: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "period_start": period_start,
        "period_end": period_end,
    }
    if agent_ids:
        params["agent_ids"] = agent_ids

    rows = await database.fetch_all(
        f"""
        SELECT 
            c.id,
            c.agent_id,
            c.amount,
            c.created_at,
            'USD' as currency
        FROM commissions c
        LEFT JOIN agent_payout_links apl ON c.id = apl.revenue_id
        WHERE c.merchant_id = :merchant_id
          AND c.type = 'agent'
          AND c.agent_id IS NOT NULL
          AND c.created_at >= :period_start
          AND c.created_at <= :period_end
          AND apl.revenue_id IS NULL
          {agent_filter}
        ORDER BY c.created_at DESC
        """,
        params
    )

    aggregated: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        agent_id = row["agent_id"]
        if not agent_id:
            continue
        amount = float(row["amount"] or 0)
        created_at = row["created_at"]
        currency = row["currency"] or "USD"

        agent_data = aggregated.setdefault(agent_id, {
            "agent_id": agent_id,
            "total_commission": 0.0,
            "transaction_count": 0,
            "currency": currency,
            "earliest": created_at,
            "latest": created_at,
            "entries": []
        })

        agent_data["total_commission"] += amount
        agent_data["transaction_count"] += 1
        agent_data["currency"] = currency
        if created_at and created_at < agent_data["earliest"]:
            agent_data["earliest"] = created_at
        if created_at and created_at > agent_data["latest"]:
            agent_data["latest"] = created_at
        agent_data["entries"].append({
            "id": row["id"],
            "amount": amount
        })

    return aggregated

# ============================================================================
# Pending Commissions (NEW - to show what needs to be paid)
# ============================================================================

@router.get("/pending-commissions")
async def get_pending_commissions_gone(merchant_id: str):
    if os.getenv("LEGACY_SETTLEMENT_LIVE", "").strip().lower() == "true":
        raise HTTPException(status_code=503, detail="legacy bypass requested but legacy handlers are removed")
    raise HTTPException(status_code=410, detail={
        "status": "gone",
        "message": "legacy commission endpoints retired; use Stage-1 monetization endpoints",
    })


@router.post("/generate-from-commissions")
async def generate_payouts_from_commissions_gone(merchant_id: str):
    if os.getenv("LEGACY_SETTLEMENT_LIVE", "").strip().lower() == "true":
        raise HTTPException(status_code=503, detail="legacy bypass requested but legacy handlers are removed")
    raise HTTPException(status_code=410, detail={
        "status": "gone",
        "message": "legacy commission endpoints retired; use Stage-1 monetization endpoints",
    })


# ============================================================================
# Payout Management (Existing functionality)
# ============================================================================

@router.get("")
async def list_payouts(
    merchant_id: str,
    status: Optional[str] = Query(None, description="Filter by status: pending, uploaded, paid"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(get_current_user)
):
    """List all payouts for a merchant"""
    try:
        repo = PayoutRepo()
        offset = (page - 1) * size
        
        items = await repo.list(
            merchant_id=merchant_id,
            status=status,
            limit=size,
            offset=offset
        )
        
        # Get summary
        summary = await repo.get_summary_by_merchant(merchant_id)
        
        return {
            "status": "success",
            "items": items,
            "summary": summary,
            "pagination": {
                "page": page,
                "size": size,
                "total": len(items)
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to list payouts: {e}")
        raise HTTPException(status_code=500, detail="Failed to list payouts")


@router.post("/bulk")
async def create_bulk_payouts(
    merchant_id: str,
    body: Dict[str, Any],
    current_user: dict = Depends(get_current_user)
):
    """Create multiple payouts at once"""
    try:
        items = body.get("items", [])
        if not items:
            raise HTTPException(status_code=400, detail="No payout items provided")
        
        repo = PayoutRepo()
        ids = await repo.create_bulk(merchant_id, items)
        
        return {
            "status": "success",
            "created": len(ids),
            "ids": ids
        }
        
    except Exception as e:
        logger.error(f"Failed to create payouts: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create payouts: {str(e)}")


@router.get("/{payout_id}")
async def get_payout_details(
    merchant_id: str,
    payout_id: int,
    current_user: dict = Depends(get_current_user)
):
    """Get detailed information about a specific payout"""
    try:
        query = """
            SELECT * FROM agent_payouts
            WHERE id = :payout_id AND merchant_id = :merchant_id
        """
        
        payout = await database.fetch_one(query, {
            "payout_id": payout_id,
            "merchant_id": merchant_id
        })
        
        if not payout:
            raise HTTPException(status_code=404, detail="Payout not found")
        
        return {
            "status": "success",
            "payout": dict(payout)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get payout: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payout")


@router.post("/{payout_id}/upload")
async def upload_payment_proof(
    merchant_id: str,
    payout_id: int,
    body: Dict[str, Any],
    current_user: dict = Depends(get_current_user)
):
    """Upload payment proof and mark payout as uploaded"""
    try:
        repo = PayoutRepo()
        await repo.upload(
            payout_id=payout_id,
            reference=body.get("reference"),
            file_url=body.get("file_url"),
            method=body.get("method"),
            provider=body.get("provider"),
            external_id=body.get("external_id")
        )
        
        return {
            "status": "success",
            "message": "Payment proof uploaded"
        }
        
    except Exception as e:
        logger.error(f"Failed to upload proof: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload payment proof")


@router.get("/export/csv")
async def export_payouts_csv(
    merchant_id: str,
    status: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user)
):
    """Export payouts as CSV data"""
    try:
        repo = PayoutRepo()
        items = await repo.list(merchant_id=merchant_id, status=status, limit=10000, offset=0)
        
        return {
            "status": "success",
            "count": len(items),
            "data": items,
            "filename": f"payouts_{merchant_id}_{date.today().isoformat()}.csv"
        }
        
    except Exception as e:
        logger.error(f"Failed to export payouts: {e}")
        raise HTTPException(status_code=500, detail="Failed to export payouts")
