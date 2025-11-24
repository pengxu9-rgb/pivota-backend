from fastapi import APIRouter, HTTPException
from db.database import database
import random
from datetime import datetime, timedelta
import logging

router = APIRouter(prefix="/test", tags=["test"])
logger = logging.getLogger(__name__)

@router.post("/generate-orders/{merchant_id}")
async def generate_test_orders_public(
    merchant_id: str,
    count: int = 5
):
    """Generate test orders (public endpoint for demo purposes)"""
    try:
        # Sample data
        products = [
            {"name": "Premium Laptop", "price": 1299.99},
            {"name": "Wireless Mouse", "price": 49.99},
            {"name": "USB-C Hub", "price": 79.99},
            {"name": "Monitor Stand", "price": 129.99},
            {"name": "Mechanical Keyboard", "price": 159.99}
        ]
        
        customers = [
            {"name": "John Smith", "email": "john.smith@example.com"},
            {"name": "Jane Doe", "email": "jane.doe@example.com"},
            {"name": "Bob Johnson", "email": "bob.johnson@example.com"},
            {"name": "Alice Williams", "email": "alice.williams@example.com"},
            {"name": "Charlie Brown", "email": "charlie.brown@example.com"}
        ]
        
        psps = ["stripe", "adyen", "paypal", "checkout"]
        payment_statuses = ["paid", "paid", "paid", "pending"]  # 75% success rate
        
        created_orders = []
        
        for i in range(count):
            # Random date in the last 30 days
            days_ago = random.randint(0, 30)
            order_date = datetime.now() - timedelta(days=days_ago)
            
            # Random customer
            customer = random.choice(customers)
            
            # Random products (1-3)
            num_products = random.randint(1, 3)
            selected_products = random.sample(products, num_products)
            
            # Calculate totals
            subtotal = sum(p["price"] * random.randint(1, 2) for p in selected_products)
            tax = subtotal * 0.08  # 8% tax
            shipping = 10.00 if subtotal < 100 else 0
            total = subtotal + tax + shipping
            
            # Generate order
            order_id = f"order_{merchant_id[:8]}_{int(order_date.timestamp())}_{random.randint(1000, 9999)}"
            payment_status = random.choice(payment_statuses)
            psp_id = random.choice(psps)
            
            # Insert order
            query = """
                INSERT INTO orders (
                    order_id, merchant_id, customer_name, customer_email,
                    total, currency, payment_status, psp_id, psp_used,
                    created_at, updated_at, is_deleted, order_status
                ) VALUES (
                    :order_id, :merchant_id, :customer_name, :customer_email,
                    :total, :currency, :payment_status, :psp_id, :psp_used,
                    :created_at, :updated_at, FALSE, 'completed'
                )
            """
            
            values = {
                "order_id": order_id,
                "merchant_id": merchant_id,
                "customer_name": customer["name"],
                "customer_email": customer["email"],
                "total": round(total, 2),
                "currency": "USD",
                "payment_status": payment_status,
                "psp_id": psp_id,
                "psp_used": psp_id,
                "created_at": order_date,
                "updated_at": order_date
            }
            
            await database.execute(query, values)
            
            created_orders.append({
                "order_id": order_id,
                "total": round(total, 2),
                "payment_status": payment_status,
                "psp": psp_id,
                "created_at": order_date.isoformat()
            })
            
            logger.info(f"Created test order: {order_id}")
        
        return {
            "status": "success",
            "message": f"Created {count} test orders for merchant {merchant_id}",
            "orders": created_orders
        }
        
    except Exception as e:
        logger.error(f"Error creating test orders: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
