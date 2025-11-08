"""
Debug endpoint to generate test commissions
Temporary - for testing payout flow
"""

from fastapi import APIRouter, Depends
from datetime import datetime
from db.database import database
from utils.auth import get_current_user
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug/generate", tags=["Debug"])

MERCHANT_ID = "merch_208139f7600dbf42"
AGENT_ID = "agent_ee38f2b3645a2ec2"

@router.post("/test-commissions")
async def generate_test_commissions(
    count: int = 10,
    current_user: dict = Depends(get_current_user)
):
    """
    Generate test orders with commissions for testing payout flow
    This is a temporary debug endpoint
    """
    try:
        created_orders = 0
        created_commissions = 0
        
        for i in range(count):
            order_amount = 100.00 + (i * 10)  # $100, $110, $120, ...
            commission_rate = 0.01  # 1%
            commission_amount = order_amount * commission_rate
            
            # Create order
            order_id = f"TEST_ORD_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{i}"
            
            try:
                await database.execute(
                    """
                    INSERT INTO orders (
                        order_id, merchant_id, agent_id,
                        customer_email, customer_name,
                        total, subtotal, currency,
                        payment_status, status,
                        items, shipping_address,
                        created_at
                    )
                    VALUES (
                        :order_id, :merchant_id, :agent_id,
                        'test@example.com', 'Test Customer',
                        :total, :subtotal, 'USD',
                        'paid', 'completed',
                        '[]'::jsonb, '{}'::jsonb,
                        NOW()
                    )
                    """,
                    {
                        "order_id": order_id,
                        "merchant_id": MERCHANT_ID,
                        "agent_id": AGENT_ID,
                        "total": order_amount,
                        "subtotal": order_amount
                    }
                )
                created_orders += 1
            except Exception as e:
                logger.warning(f"Order {order_id} might already exist: {e}")
            
            # Create commission
            try:
                await database.execute(
                    """
                    INSERT INTO commissions (
                        order_id, merchant_id, agent_id,
                        amount, rate, currency, type,
                        created_at
                    )
                    VALUES (
                        :order_id, :merchant_id, :agent_id,
                        :amount, :rate, 'USD', 'agent',
                        NOW()
                    )
                    """,
                    {
                        "order_id": order_id,
                        "merchant_id": MERCHANT_ID,
                        "agent_id": AGENT_ID,
                        "amount": commission_amount,
                        "rate": commission_rate
                    }
                )
                created_commissions += 1
            except Exception as e:
                logger.warning(f"Commission for {order_id} might already exist: {e}")
        
        # Get summary
        summary = await database.fetch_one(
            """
            SELECT 
                COUNT(*) as order_count,
                SUM(amount) as total_commission
            FROM commissions
            WHERE merchant_id = :merchant_id
              AND agent_id = :agent_id
              AND type = 'agent'
              AND created_at >= NOW() - INTERVAL '1 hour'
            """,
            {"merchant_id": MERCHANT_ID, "agent_id": AGENT_ID}
        )
        
        # Check unpaid commissions
        unpaid = await database.fetch_one(
            """
            SELECT 
                COUNT(*) as unpaid_count,
                SUM(c.amount) as unpaid_amount
            FROM commissions c
            LEFT JOIN agent_payout_links apl ON c.id = apl.revenue_id
            WHERE c.merchant_id = :merchant_id
              AND c.type = 'agent'
              AND apl.revenue_id IS NULL
            """,
            {"merchant_id": MERCHANT_ID}
        )
        
        return {
            "status": "success",
            "created": {
                "orders": created_orders,
                "commissions": created_commissions
            },
            "recent_summary": {
                "order_count": summary["order_count"],
                "total_commission": float(summary["total_commission"] or 0)
            },
            "unpaid_commissions": {
                "count": unpaid["unpaid_count"],
                "amount": float(unpaid["unpaid_amount"] or 0)
            },
            "message": f"Created {created_commissions} test commissions. Total unpaid: ${float(unpaid['unpaid_amount'] or 0):.2f}"
        }
        
    except Exception as e:
        logger.error(f"Failed to generate test commissions: {e}")
        return {
            "status": "error",
            "error": str(e)
        }
