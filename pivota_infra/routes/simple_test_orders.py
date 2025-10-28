from fastapi import APIRouter
from db.database import database
import random
from datetime import datetime, timedelta

router = APIRouter(prefix="/test", tags=["test"])

@router.post("/orders/{merchant_id}")
async def create_simple_test_orders(merchant_id: str, count: int = 5):
    """Generate simple test orders"""
    try:
        orders = []
        psps = ["stripe", "adyen", "paypal", "checkout"]
        statuses = ["paid", "paid", "paid", "pending"]
        
        for i in range(count):
            days_ago = random.randint(0, 30)
            order_date = datetime.now() - timedelta(days=days_ago)
            total = round(random.uniform(50, 1500), 2)
            psp = random.choice(psps)
            status = random.choice(statuses)
            
            order_id = f"test_{merchant_id[:8]}_{int(order_date.timestamp())}_{i}"
            
            await database.execute(
                """INSERT INTO orders (
                    order_id, merchant_id, customer_name, customer_email,
                    total, currency, payment_status, psp_id, psp_used,
                    created_at, updated_at, is_deleted
                ) VALUES (
                    :oid, :mid, :name, :email, :total, 'USD', :status, :psp, :psp,
                    :created, :created, FALSE
                )""",
                {
                    "oid": order_id,
                    "mid": merchant_id,
                    "name": f"Customer {i+1}",
                    "email": f"customer{i+1}@example.com",
                    "total": total,
                    "status": status,
                    "psp": psp,
                    "created": order_date
                }
            )
            
            orders.append({"order_id": order_id, "total": total, "psp": psp, "status": status})
        
        return {"status": "success", "orders": orders, "count": count}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
