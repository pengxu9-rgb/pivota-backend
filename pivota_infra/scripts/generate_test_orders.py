import httpx
import asyncio
import random
import string
from datetime import datetime, timedelta
import json

# Configuration
API_BASE_URL = "https://web-production-fedb.up.railway.app"
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
    return f"order_{datetime.now().strftime('%Y%m%d')}_{random.randint(1000, 9999)}"

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

async def create_order(client: httpx.AsyncClient, order_data: dict):
    """Create a single order via API"""
    try:
        response = await client.post(
            f"{API_BASE_URL}/api/orders/create",
            json=order_data,
            headers={"Content-Type": "application/json"}
        )
        
        if response.status_code == 200:
            print(f"✅ Created order: {order_data['order_id']} - ${order_data['total']:.2f}")
            return response.json()
        else:
            print(f"❌ Failed to create order {order_data['order_id']}: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error creating order: {e}")
        return None

async def generate_orders():
    """Generate 5 test orders"""
    orders = []
    
    for i in range(5):
        # Random date in the last 30 days
        days_ago = random.randint(0, 30)
        order_date = datetime.now() - timedelta(days=days_ago)
        
        # Random number of products (1-3)
        num_products = random.randint(1, 3)
        selected_products = random.sample(PRODUCTS, num_products)
        
        # Calculate totals
        subtotal = sum(p["price"] for p in selected_products)
        tax = subtotal * 0.08  # 8% tax
        shipping = 10.00 if subtotal < 100 else 0  # Free shipping over $100
        total = subtotal + tax + shipping
        
        # Generate order data
        customer = generate_customer()
        order_data = {
            "merchant_id": MERCHANT_ID,
            "order_id": generate_order_id(),
            "customer_name": customer["name"],
            "customer_email": customer["email"],
            "customer_phone": customer["phone"],
            "items": [
                {
                    "product_id": p["sku"],
                    "name": p["name"],
                    "quantity": random.randint(1, 2),
                    "price": p["price"]
                }
                for p in selected_products
            ],
            "subtotal": round(subtotal, 2),
            "tax": round(tax, 2),
            "shipping": round(shipping, 2),
            "total": round(total, 2),
            "currency": "USD",
            "payment_status": random.choice(PAYMENT_STATUSES),
            "psp_id": random.choice(PSPS),
            "created_at": order_date.isoformat(),
            "updated_at": order_date.isoformat(),
            "metadata": {
                "source": "test_script",
                "test": True
            }
        }
        
        orders.append(order_data)
    
    # Create orders via API
    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"\n🚀 Generating 5 test orders for merchant {MERCHANT_ID}...\n")
        
        tasks = [create_order(client, order) for order in orders]
        results = await asyncio.gather(*tasks)
        
        successful = sum(1 for r in results if r is not None)
        print(f"\n✨ Summary: {successful}/5 orders created successfully")
        
        # Show PSP distribution
        psp_counts = {}
        for order in orders:
            psp = order["psp_id"]
            psp_counts[psp] = psp_counts.get(psp, 0) + 1
        
        print("\n📊 PSP Distribution:")
        for psp, count in psp_counts.items():
            print(f"  - {psp}: {count} orders")
        
        # Show payment status distribution
        status_counts = {}
        for order in orders:
            status = order["payment_status"]
            status_counts[status] = status_counts.get(status, 0) + 1
        
        print("\n💳 Payment Status Distribution:")
        for status, count in status_counts.items():
            print(f"  - {status}: {count} orders")

if __name__ == "__main__":
    asyncio.run(generate_orders())
