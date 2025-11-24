"""
Admin endpoint to simulate payment completion for testing
"""
from fastapi import APIRouter
from db.database import database
from datetime import datetime

router = APIRouter(prefix="/admin/simulate", tags=["simulate"])

@router.post("/payments/{agent_id}")
async def simulate_payments_for_agent(agent_id: str, percentage: int = 80):
    """
    Simulate payment completion for agent's orders
    Updates payment_status to 'succeeded' for a percentage of unpaid orders
    """
    try:
        # Get unpaid orders for this agent
        unpaid_orders = await database.fetch_all(
            """
            SELECT order_id, total
            FROM orders
            WHERE agent_id = :agent_id
            AND payment_status = 'unpaid'
            ORDER BY created_at DESC
            LIMIT 100
            """,
            {"agent_id": agent_id}
        )
        
        if not unpaid_orders:
            return {
                "status": "success",
                "message": "No unpaid orders found",
                "processed": 0
            }
        
        # Calculate how many to mark as paid
        total_unpaid = len(unpaid_orders)
        to_pay_count = int(total_unpaid * (percentage / 100))
        orders_to_pay = unpaid_orders[:to_pay_count]
        
        # Update each order
        updated = 0
        for order in orders_to_pay:
            order_id = order["order_id"]
            
            await database.execute(
                """
                UPDATE orders
                SET 
                    payment_status = 'succeeded',
                    status = 'completed'
                WHERE order_id = :order_id
                """,
                {
                    "order_id": order_id
                }
            )
            updated += 1
        
        return {
            "status": "success",
            "agent_id": agent_id,
            "total_unpaid": total_unpaid,
            "marked_as_paid": updated,
            "percentage": percentage,
            "message": f"Successfully simulated payment for {updated} orders"
        }
        
    except Exception as e:
        import traceback
        return {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()
        }

@router.post("/payments/all")
async def simulate_all_payments(percentage: int = 70):
    """Simulate payments for all agents (for testing)"""
    try:
        # Get all agents with unpaid orders
        agents = await database.fetch_all(
            """
            SELECT DISTINCT agent_id, COUNT(*) as unpaid_count
            FROM orders
            WHERE payment_status = 'unpaid'
            AND agent_id IS NOT NULL
            GROUP BY agent_id
            """
        )
        
        results = []
        total_updated = 0
        
        for agent in agents:
            agent_id = agent["agent_id"]
            unpaid_count = agent["unpaid_count"]
            to_pay = int(unpaid_count * (percentage / 100))
            
            # Update orders
            await database.execute(
                f"""
                UPDATE orders
                SET 
                    payment_status = 'succeeded',
                    status = 'completed'
                WHERE order_id IN (
                    SELECT order_id
                    FROM orders
                    WHERE agent_id = :agent_id
                    AND payment_status = 'unpaid'
                    ORDER BY created_at DESC
                    LIMIT :limit
                )
                """,
                {"agent_id": agent_id, "limit": to_pay}
            )
            
            total_updated += to_pay
            results.append({
                "agent_id": agent_id,
                "unpaid": unpaid_count,
                "marked_paid": to_pay
            })
        
        return {
            "status": "success",
            "total_agents": len(agents),
            "total_updated": total_updated,
            "percentage": percentage,
            "details": results
        }
        
    except Exception as e:
        import traceback
        return {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()
        }

