"""
Admin helper to backfill commissions and payouts for existing orders.
Use when historical orders exist but commissions/payouts tables are empty.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import logging

from db.database import database
from db.payout_repo import PayoutRepo
from services.order_commission_service import OrderCommissionService
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/payouts", tags=["admin-payouts"])


def _success(status: str, message: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = {"status": status, "message": message}
    if extra:
        payload.update(extra)
    return payload


@router.post("/backfill")
async def backfill_commissions_and_payouts(
    merchant_id: str,
    days: int = Query(180, ge=1, le=365),
    current_user: dict = Depends(get_current_user)
):
    """
    Backfill commissions for historical paid orders and generate payouts for a merchant.
    - Calculates commission per order via OrderCommissionService (respects offers/expectations)
    - Creates payouts grouped per agent and links commission rows
    """
    if current_user.get("role") not in ["admin", "super_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")

    svc = OrderCommissionService(database)
    repo = PayoutRepo()

    period_end = datetime.utcnow()
    period_start = period_end - timedelta(days=days)

    try:
        # 1) Find eligible orders that have agent_id and are paid
        paid_statuses = ("paid", "captured", "succeeded", "completed", "fulfilled")
        orders = await database.fetch_all(
            """
            SELECT order_id
            FROM orders
            WHERE merchant_id = :mid
              AND agent_id IS NOT NULL
              AND payment_status = ANY(:statuses)
              AND created_at BETWEEN :start AND :end
            ORDER BY created_at DESC
            """,
            {
                "mid": merchant_id,
                "statuses": list(paid_statuses),
                "start": period_start,
                "end": period_end,
            },
        )

        if not orders:
            return _success("success", "No paid orders found for backfill", {"orders_checked": 0})

        # 2) Calculate commissions per order (idempotent; skips if already exists)
        commission_results: List[Dict[str, Any]] = []
        for row in orders:
            order_id = row["order_id"]
            result = await svc.calculate_commission_for_order(order_id)
            commission_results.append({"order_id": order_id, **result})

        # 3) Aggregate unpaid commissions and create payouts per agent
        aggregated = await database.fetch_all(
            """
            SELECT 
              c.id,
              c.agent_id,
              c.amount,
              c.created_at,
              c.currency
            FROM commissions c
            LEFT JOIN agent_payout_links apl ON c.id = apl.revenue_id
            WHERE c.merchant_id = :mid
              AND c.type = 'agent'
              AND c.agent_id IS NOT NULL
              AND c.created_at BETWEEN :start AND :end
              AND apl.revenue_id IS NULL
            ORDER BY c.created_at DESC
            """,
            {"mid": merchant_id, "start": period_start, "end": period_end},
        )

        if not aggregated:
            return _success(
                "success",
                "Commissions calculated but nothing pending for payouts (already linked?)",
                {"orders_checked": len(orders), "commissions_created": commission_results},
            )

        # group by agent
        grouped: Dict[str, Dict[str, Any]] = {}
        for row in aggregated:
            aid = row["agent_id"]
            entry = grouped.setdefault(
                aid,
                {
                    "agent_id": aid,
                    "total": 0.0,
                    "currency": row["currency"] or "USD",
                    "entries": [],
                    "earliest": row["created_at"],
                    "latest": row["created_at"],
                },
            )
            amt = float(row["amount"] or 0)
            entry["total"] += amt
            entry["entries"].append({"id": row["id"], "amount": amt})
            if row["created_at"] < entry["earliest"]:
                entry["earliest"] = row["created_at"]
            if row["created_at"] > entry["latest"]:
                entry["latest"] = row["created_at"]

        created_payouts: List[int] = []
        for data in grouped.values():
            payout_rows = [
                {
                    "agent_id": data["agent_id"],
                    "amount": data["total"],
                    "currency": data["currency"],
                    "period_start": data["earliest"],
                    "period_end": data["latest"],
                    "metadata": {"source": "admin-backfill"},
                }
            ]
            payout_ids = await repo.create_bulk(merchant_id, payout_rows)
            if not payout_ids:
                continue
            payout_id = payout_ids[0]
            created_payouts.extend(payout_ids)

            # link commissions
            for entry in data["entries"]:
                await database.execute(
                    """
                    INSERT INTO agent_payout_links (payout_id, revenue_id, amount)
                    VALUES (:pid, :rid, :amt)
                    ON CONFLICT DO NOTHING
                    """,
                    {"pid": payout_id, "rid": entry["id"], "amt": entry["amount"]},
                )

        return _success(
            "success",
            f"Processed {len(orders)} orders, created {len(created_payouts)} payouts",
            {
                "orders_checked": len(orders),
                "payout_ids": created_payouts,
                "commission_results": commission_results,
            },
        )

    except Exception as e:
        logger.error(f"Backfill payouts failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Backfill failed: {e}")
