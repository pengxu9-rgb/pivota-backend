"""
Initialize orders table with real data
"""
from fastapi import APIRouter
from db.database import database
from datetime import datetime, timedelta
import random
import uuid

router = APIRouter()

@router.post("/reinit-orders-table")
async def reinit_orders_table():
    """Reinitialize orders table with Stripe-only transactions"""
    return await init_orders_table()

@router.post("/init-orders-table")
async def init_orders_table():
    """Create and populate orders table with initial data"""
    try:
        # Create orders table if not exists
        async with database.transaction():
            # Drop existing table to ensure clean state
            try:
                await database.execute("DROP TABLE IF EXISTS orders CASCADE")
            except:
                pass
            
            # Create orders table
            create_table_query = """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id VARCHAR(50) PRIMARY KEY,
                    merchant_id VARCHAR(50) NOT NULL,
                    store_id VARCHAR(50),
                    psp_id VARCHAR(50),
                    amount DECIMAL(10, 2) NOT NULL,
                    currency VARCHAR(10) DEFAULT 'USD',
                    status VARCHAR(50) NOT NULL,
                    payment_method VARCHAR(50),
                    customer_name VARCHAR(100),
                    customer_email VARCHAR(100),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (merchant_id) REFERENCES merchant_onboarding(merchant_id) ON DELETE CASCADE
                )
            """
            await database.execute(create_table_query)
            
            # Create indexes for better performance
            await database.execute("CREATE INDEX IF NOT EXISTS idx_orders_merchant ON orders(merchant_id)")
            await database.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
            await database.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")
            
            # Generate initial orders for test merchant
            merchant_id = "merch_6b90dc9838d5fd9c"
            statuses = ['completed', 'processing', 'pending', 'failed', 'delivered', 'cancelled']
            payment_methods = ['credit_card', 'debit_card', 'paypal', 'apple_pay', 'google_pay', 'bank_transfer', 'alipay', 'wechat_pay']
            
            # Get connected stores and PSPs
            stores_query = "SELECT store_id, platform FROM merchant_stores WHERE merchant_id = :merchant_id"
            stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
            store_ids = [s["store_id"] for s in stores] if stores else ["store_shopify_main", "store_wix_main"]
            
            # Only Stripe is actually used for transactions, Adyen is connected but not used yet
            psps_query = "SELECT psp_id, provider FROM merchant_psps WHERE merchant_id = :merchant_id AND provider = 'stripe'"
            psps = await database.fetch_all(psps_query, {"merchant_id": merchant_id})
            # If no Stripe found, use default
            psp_ids = [p["psp_id"] for p in psps] if psps else ["psp_stripe_main"]
            
            # Generate 100 orders over the past 30 days
            orders_to_insert = []
            base_date = datetime.now() - timedelta(days=30)
            
            for i in range(100):
                order_id = f"ORD{str(uuid.uuid4())[:8].upper()}"
                
                # Random date within last 30 days
                days_ago = random.randint(0, 30)
                hours_ago = random.randint(0, 23)
                created_at = base_date + timedelta(days=days_ago, hours=hours_ago)
                
                # Status distribution: 40% completed, 20% delivered, 15% processing, 10% pending, 10% failed, 5% cancelled
                status_weights = [40, 20, 15, 10, 10, 5]
                status = random.choices(statuses, weights=status_weights)[0]
                
                # Amount between $10 and $1000
                amount = round(random.uniform(10, 1000), 2)
                
                # Random store and PSP
                store_id = random.choice(store_ids) if store_ids else None
                psp_id = random.choice(psp_ids) if psp_ids else None
                
                # Random payment method
                payment_method = random.choice(payment_methods)
                
                # Customer info
                customer_num = random.randint(1, 50)
                customer_name = f"Customer {customer_num}"
                customer_email = f"customer{customer_num}@example.com"
                
                orders_to_insert.append({
                    "order_id": order_id,
                    "merchant_id": merchant_id,
                    "store_id": store_id,
                    "psp_id": psp_id,
                    "amount": amount,
                    "currency": "USD",
                    "status": status,
                    "payment_method": payment_method,
                    "customer_name": customer_name,
                    "customer_email": customer_email,
                    "created_at": created_at,
                    "updated_at": created_at
                })
            
            # Insert all orders
            if orders_to_insert:
                insert_query = """
                    INSERT INTO orders (
                        order_id, merchant_id, store_id, psp_id, amount, currency, 
                        status, payment_method, customer_name, customer_email, 
                        created_at, updated_at
                    ) VALUES (
                        :order_id, :merchant_id, :store_id, :psp_id, :amount, :currency,
                        :status, :payment_method, :customer_name, :customer_email,
                        :created_at, :updated_at
                    )
                """
                
                for order in orders_to_insert:
                    await database.execute(insert_query, order)
            
            # Calculate summary statistics
            stats_query = """
                SELECT 
                    COUNT(*) as total_orders,
                    SUM(amount) as total_revenue,
                    AVG(amount) as avg_order_value,
                    SUM(CASE WHEN status IN ('completed', 'delivered') THEN 1 ELSE 0 END) as successful_orders,
                    SUM(CASE WHEN status IN ('completed', 'delivered') THEN amount ELSE 0 END) as successful_revenue
                FROM orders 
                WHERE merchant_id = :merchant_id
            """
            stats = await database.fetch_one(stats_query, {"merchant_id": merchant_id})
            
            success_rate = 0
            if stats and stats["total_orders"] > 0:
                success_rate = (stats["successful_orders"] / stats["total_orders"]) * 100
            
            return {
                "status": "success",
                "message": "Orders table initialized with real data",
                "statistics": {
                    "total_orders": stats["total_orders"] if stats else 0,
                    "total_revenue": float(stats["total_revenue"]) if stats and stats["total_revenue"] else 0,
                    "avg_order_value": float(stats["avg_order_value"]) if stats and stats["avg_order_value"] else 0,
                    "successful_orders": stats["successful_orders"] if stats else 0,
                    "success_rate": round(success_rate, 1),
                    "stores_used": store_ids,
                    "psps_used": psp_ids
                }
            }
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "status": "error",
            "message": str(e)
        }

@router.get("/orders-stats")
async def get_orders_stats():
    """Get current orders statistics"""
    try:
        merchant_id = "merch_6b90dc9838d5fd9c"
        
        # Check if orders table exists
        check_query = """
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'orders'
            )
        """
        table_exists = await database.fetch_one(check_query)
        
        if not table_exists or not table_exists["exists"]:
            return {"status": "error", "message": "Orders table does not exist"}
        
        # Get statistics
        stats_query = """
            SELECT 
                COUNT(*) as total_orders,
                SUM(amount) as total_revenue,
                AVG(amount) as avg_order_value,
                SUM(CASE WHEN status IN ('completed', 'delivered') THEN 1 ELSE 0 END) as successful_orders,
                SUM(CASE WHEN created_at >= CURRENT_DATE THEN amount ELSE 0 END) as revenue_today,
                COUNT(DISTINCT customer_email) as unique_customers
            FROM orders 
            WHERE merchant_id = :merchant_id
        """
        stats = await database.fetch_one(stats_query, {"merchant_id": merchant_id})
        
        # Get status distribution
        status_query = """
            SELECT status, COUNT(*) as count, SUM(amount) as revenue
            FROM orders
            WHERE merchant_id = :merchant_id
            GROUP BY status
            ORDER BY count DESC
        """
        status_dist = await database.fetch_all(status_query, {"merchant_id": merchant_id})
        
        return {
            "status": "success",
            "statistics": {
                "total_orders": stats["total_orders"] if stats else 0,
                "total_revenue": float(stats["total_revenue"]) if stats and stats["total_revenue"] else 0,
                "avg_order_value": float(stats["avg_order_value"]) if stats and stats["avg_order_value"] else 0,
                "successful_orders": stats["successful_orders"] if stats else 0,
                "revenue_today": float(stats["revenue_today"]) if stats and stats["revenue_today"] else 0,
                "unique_customers": stats["unique_customers"] if stats else 0,
                "status_distribution": [
                    {
                        "status": s["status"],
                        "count": s["count"],
                        "revenue": float(s["revenue"]) if s["revenue"] else 0
                    }
                    for s in status_dist
                ] if status_dist else []
            }
        }
        
    except Exception as e:
        return {"status": "error", "message": str(e)}
