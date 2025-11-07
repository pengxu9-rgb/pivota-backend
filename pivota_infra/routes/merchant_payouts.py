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
        period_end = datetime.now()
        period_start = period_end - timedelta(days=days)

        aggregated = await _fetch_unpaid_commission_entries(
            merchant_id=merchant_id,
            period_start=period_start,
            period_end=period_end
        )

        if not aggregated:
            return {
                "status": "success",
                "summary": {
                    "total_amount": 0,
                    "total_transactions": 0,
                    "unique_agents": 0,
                    "period_days": days
                },
                "agents": []
            }

        agents = []
        total_amount = 0.0
        total_transactions = 0

        for agent_data in aggregated.values():
            total_amount += agent_data["total_commission"]
            total_transactions += agent_data["transaction_count"]
            agents.append({
                "agent_id": agent_data["agent_id"],
                "transaction_count": agent_data["transaction_count"],
                "total_commission": agent_data["total_commission"],
                "currency": agent_data["currency"],
                "earliest_transaction": agent_data["earliest"].isoformat() if agent_data["earliest"] else None,
                "latest_transaction": agent_data["latest"].isoformat() if agent_data["latest"] else None
            })

        agents.sort(key=lambda item: item["total_commission"], reverse=True)

        return {
            "status": "success",
            "summary": {
                "total_amount": total_amount,
                "total_transactions": total_transactions,
                "unique_agents": len(aggregated),
                "period_days": days
            },
            "agents": agents
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
        
        aggregated = await _fetch_unpaid_commission_entries(
            merchant_id=merchant_id,
            period_start=period_start,
            period_end=period_end,
            agent_ids=agent_ids
        )
        
        if not aggregated:
            return {
                "status": "success",
                "message": "No unpaid commissions found for the selected period",
                "payouts_created": 0
            }
        
        # Create payouts per agent
        repo = PayoutRepo()
        created_ids: List[int] = []
        
        for agent_id, agent_data in aggregated.items():
            payout_data = {
                "agent_id": agent_id,
                "amount": float(agent_data["total_commission"]),
                "currency": agent_data["currency"],
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat()
            }
            payout_ids = await repo.create_bulk(merchant_id, [payout_data])
            if not payout_ids:
                continue
            created_ids.extend(payout_ids)
            payout_id = payout_ids[0]
            
            # Link each commission entry to the newly created payout
            for entry in agent_data["entries"]:
                await database.execute(
                    """
                    INSERT INTO agent_payout_links (payout_id, revenue_id, amount)
                    VALUES (:payout_id, :revenue_id, :amount)
                    ON CONFLICT DO NOTHING
                    """,
                    {
                        "payout_id": payout_id,
                        "revenue_id": entry["id"],
                        "amount": entry["amount"]
                    }
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
