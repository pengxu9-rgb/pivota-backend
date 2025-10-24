"""
Debug orders by agent_id
"""
from fastapi import APIRouter
from db.database import database
from datetime import datetime, timedelta

router = APIRouter(prefix="/admin/debug", tags=["debug"])

@router.get("/orders-by-agent/{agent_id}")
async def debug_orders_by_agent(agent_id: str, days: int = 7):
    """Check orders for a specific agent"""
    try:
        since = datetime.now() - timedelta(days=days)
        
        # Get all orders for this agent
        orders = await database.fetch_all(
            """
            SELECT order_id, agent_id, payment_status, status, created_at, total
            FROM orders
            WHERE agent_id = :agent_id 
            AND created_at >= :since
            ORDER BY created_at DESC
            LIMIT 50
            """,
            {"agent_id": agent_id, "since": since}
        )
        
        # Count by payment_status
        status_counts = await database.fetch_all(
            """
            SELECT payment_status, COUNT(*) as count
            FROM orders
            WHERE agent_id = :agent_id 
            AND created_at >= :since
            GROUP BY payment_status
            """,
            {"agent_id": agent_id, "since": since}
        )
        
        # Total count
        total = await database.fetch_val(
            """
            SELECT COUNT(*) FROM orders
            WHERE agent_id = :agent_id 
            AND created_at >= :since
            """,
            {"agent_id": agent_id, "since": since}
        )
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "period_days": days,
            "since_cutoff": since.isoformat(),
            "total_orders": total,
            "by_payment_status": [dict(row) for row in status_counts],
            "sample_orders": [
                {
                    "order_id": order["order_id"],
                    "agent_id": order["agent_id"],
                    "payment_status": order["payment_status"],
                    "status": order["status"],
                    "total": float(order["total"]) if order["total"] else 0,
                    "created_at": order["created_at"].isoformat()
                }
                for order in orders[:10]
            ]
        }
    except Exception as e:
        import traceback
        return {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()
        }
