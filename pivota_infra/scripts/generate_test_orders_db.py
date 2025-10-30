import asyncio
import random
from datetime import datetime, timedelta
from databases import Database
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database connection
DATABASE_URL = os.getenv("DATABASE_URL")
database = Database(DATABASE_URL)

# Configuration
MERCHANT_ID = "merch_208139f7600dbf42"

# Sample data
PRODUCTS = [
    {"name": "Premium Laptop", "price": 1299.99, "sku": "LAPTOP-001"},
    {"name": "Wireless Mouse", "price": 49.99, "sku": "MOUSE-002"},
    {"name": "USB-C Hub", "price": 79.99, "sku": "HUB-003"},
    {"name": "Monitor Stand", "price": 129.99, "sku": "STAND-004"},
    {"name": "Mechanical Keyboard", "price": 159.99, "sku": "KB-005"}
]

PSPS = ["stripe", "adyen", "paypal", "checkout"]
PAYMENT_STATUSES = ["paid", "paid", "paid", "pending", "failed"]  # 60% success rate

def generate_order_id():
    """Generate a random order ID"""
    timestamp = int(datetime.now().timestamp())
    return f"order_{MERCHANT_ID[:8]}_{timestamp}_{random.randint(1000, 9999)}"

def generate_customer():
    """Generate random customer data"""
    first_names = ["John", "Jane", "Bob", "Alice", "Charlie", "Emma", "David", "Sophie"]
    last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
    
    first_name = random.choice(first_names)
    last_name = random.choice(last_names)
    
    return {
        "name": f"{first_name} {last_name}",
        "email": f"{first_name.lower()}.{last_name.lower()}@example.com",
        "phone": f"+1{random.randint(2000000000, 9999999999)}"
    }

async def create_orders():
    """Generate 5 test orders directly in database"""
    await database.connect()
    
    try:
        print(f"\n🚀 Generating 5 test orders for merchant {MERCHANT_ID}...\n")
        
        order_ids = []
        psp_counts = {}
        status_counts = {}
        
        for i in range(5):
            # Random date in the last 30 days
            days_ago = random.randint(0, 30)
            order_date = datetime.now() - timedelta(days=days_ago)
            
            # Random number of products (1-3)
            num_products = random.randint(1, 3)
            selected_products = random.sample(PRODUCTS, num_products)
            
            # Calculate totals
            subtotal = sum(p["price"] * random.randint(1, 2) for p in selected_products)
            tax = subtotal * 0.08  # 8% tax
            shipping = 10.00 if subtotal < 100 else 0  # Free shipping over $100
            total = subtotal + tax + shipping
            
            # Generate order data
            customer = generate_customer()
            order_id = generate_order_id()
            payment_status = random.choice(PAYMENT_STATUSES)
            psp_id = random.choice(PSPS)
            
            # Track counts
            psp_counts[psp_id] = psp_counts.get(psp_id, 0) + 1
            status_counts[payment_status] = status_counts.get(payment_status, 0) + 1
            
            # Insert order
            query = """
                INSERT INTO orders (
                    order_id, merchant_id, customer_name, customer_email,
                    total, currency, payment_status, psp_id, psp_used,
                    created_at, updated_at, is_deleted
                ) VALUES (
                    :order_id, :merchant_id, :customer_name, :customer_email,
                    :total, :currency, :payment_status, :psp_id, :psp_used,
                    :created_at, :updated_at, FALSE
                )
            """
            
            values = {
                "order_id": order_id,
                "merchant_id": MERCHANT_ID,
                "customer_name": customer["name"],
                "customer_email": customer["email"],
                "total": round(total, 2),
                "currency": "USD",
                "payment_status": payment_status,
                "psp_id": psp_id,
                "psp_used": psp_id,  # Same as psp_id for test data
                "created_at": order_date,
                "updated_at": order_date
            }
            
            await database.execute(query, values)
            order_ids.append(order_id)
            
            print(f"✅ Created order: {order_id} - ${total:.2f} ({psp_id}, {payment_status})")
        
        print(f"\n✨ Summary: 5/5 orders created successfully")
        
        # Show PSP distribution
        print("\n📊 PSP Distribution:")
        for psp, count in psp_counts.items():
            print(f"  - {psp}: {count} orders")
        
        # Show payment status distribution
        print("\n💳 Payment Status Distribution:")
        for status, count in status_counts.items():
            print(f"  - {status}: {count} orders")
        
        # Verify orders were created
        verify_query = """
            SELECT COUNT(*) as count, 
                   SUM(CASE WHEN payment_status IN ('paid', 'captured', 'succeeded') THEN total ELSE 0 END) as total_volume
            FROM orders 
            WHERE merchant_id = :merchant_id 
            AND order_id = ANY(:order_ids)
        """
        
        result = await database.fetch_one(verify_query, {
            "merchant_id": MERCHANT_ID,
            "order_ids": order_ids
        })
        
        print(f"\n✅ Verification: {result['count']} orders found, total volume: ${result['total_volume']:.2f}")
        
    except Exception as e:
        print(f"❌ Error creating orders: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await database.disconnect()

if __name__ == "__main__":
    asyncio.run(create_orders())

