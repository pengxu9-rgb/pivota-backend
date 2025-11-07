"""
Merchant Payout Management Routes
Handles commission payout operations for merchants
"""

from fastapi import APIRouter, Depends, Query, Path, HTTPException
from typing import Optional, List, Dict, Any
from datetime import date, timedelta, datetime
from db.database import database
from db.payout_repo import PayoutRepo
from utils.auth import get_current_user
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/merchants/{merchant_id}/payouts",
    tags=["payouts"]
)

# ============================================================================
# Pending Commissions (NEW - to show what needs to be paid)
# ============================================================================

@router.get("/pending-commissions")
async def get_pending_commissions(
    merchant_id: str,
    days: int = Query(30, ge=1, le=365, description="Period to check (days)"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get summary of unpaid commissions grouped by agent
    This shows what commission is owed but hasn't been converted to payouts yet
    """
    try:
        # Query unpaid commissions from commissions table
        # Exclude commissions that are already linked to payouts
        query = """
            SELECT 
                c.agent_id,
                COUNT(DISTINCT c.id) as transaction_count,
                COALESCE(SUM(c.amount), 0) as total_commission,
                'USD' as currency,
                MIN(c.created_at) as earliest_transaction,
                MAX(c.created_at) as latest_transaction
            FROM commissions c
            LEFT JOIN agent_payout_links apl ON c.id = apl.revenue_id
            WHERE c.merchant_id = :merchant_id
            AND c.type = 'agent'
            AND c.created_at >= NOW() - make_interval(days => :days)
            AND apl.revenue_id IS NULL  -- Not yet linked to any payout
            GROUP BY c.agent_id
            ORDER BY total_commission DESC
        """
        
        results = await database.fetch_all(query, {
            "merchant_id": merchant_id,
            "days": days
        })
        
        # Calculate totals
        total_amount = sum(float(r['total_commission']) for r in results)
        total_transactions = sum(r['transaction_count'] for r in results)
        unique_agents = len(results)
        
        return {
            "status": "success",
            "summary": {
                "total_amount": total_amount,
                "total_transactions": total_transactions,
                "unique_agents": unique_agents,
                "period_days": days
            },
            "agents": [
                {
                    "agent_id": r['agent_id'],
                    "transaction_count": r['transaction_count'],
                    "total_commission": float(r['total_commission']),
                    "currency": r['currency'],
                    "earliest_transaction": r['earliest_transaction'].isoformat() if r['earliest_transaction'] else None,
                    "latest_transaction": r['latest_transaction'].isoformat() if r['latest_transaction'] else None
                }
                for r in results
            ]
        }
        
    except Exception as e:
        logger.error(f"Failed to get pending commissions: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get pending commissions: {str(e)}")


@router.post("/generate-from-commissions")
async def generate_payouts_from_commissions(
    merchant_id: str,
    days: int = Query(30, ge=1, le=365, description="Period to include (days)"),
    agent_ids: Optional[List[str]] = None,  # If None, generate for all agents
    current_user: dict = Depends(get_current_user)
):
    """
    Generate payouts from unpaid commissions
    Creates payout records and links them to the source commissions
    """
    try:
        # Calculate period
        period_end = datetime.now()
        period_start = period_end - timedelta(days=days)
        
        # Get unpaid commissions
        if agent_ids:
            # For specific agents
            agent_filter = "AND c.agent_id = ANY(:agent_ids)"
            params = {"merchant_id": merchant_id, "period_start": period_start, "period_end": period_end, "agent_ids": agent_ids}
        else:
            # For all agents with unpaid commissions
            agent_filter = ""
            params = {"merchant_id": merchant_id, "period_start": period_start, "period_end": period_end}
        
        query = f"""
            SELECT 
                c.agent_id,
                COALESCE(SUM(c.amount), 0) as total_commission,
                'USD' as currency,
                ARRAY_AGG(c.id) as revenue_ids
            FROM commissions c
            LEFT JOIN agent_payout_links apl ON c.id = apl.revenue_id
            WHERE c.merchant_id = :merchant_id
            AND c.type = 'agent'
            AND c.created_at >= :period_start
            AND c.created_at <= :period_end
            AND apl.revenue_id IS NULL
            {agent_filter}
            GROUP BY c.agent_id
            HAVING SUM(c.amount) > 0
        """
        
        commissions = await database.fetch_all(query, params)
        
        if not commissions:
            return {
                "status": "success",
                "message": "No unpaid commissions found for the selected period",
                "payouts_created": 0
            }
        
        # Create payouts
        repo = PayoutRepo()
        created_ids = []
        
        for comm in commissions:
            # Create payout record
            payout_data = {
                "agent_id": comm['agent_id'],
                "amount": float(comm['total_commission']),
                "currency": comm['currency'],
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat()
            }
            
            payout_id = await repo.create_bulk(merchant_id, [payout_data])
            
            if payout_id:
                created_ids.extend(payout_id)
                
                # Link commissions to payout
                for revenue_id in comm['revenue_ids']:
                    await database.execute(
                        """
                        INSERT INTO agent_payout_links (payout_id, revenue_id)
                        VALUES (:payout_id, :revenue_id)
                        ON CONFLICT DO NOTHING
                        """,
                        {"payout_id": payout_id[0], "revenue_id": revenue_id}
                    )
        
        return {
            "status": "success",
            "message": f"Created {len(created_ids)} payouts",
            "payouts_created": len(created_ids),
            "payout_ids": created_ids
        }
        
    except Exception as e:
        logger.error(f"Failed to generate payouts: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate payouts: {str(e)}")


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
