"""
Pivota Agent SDK - Quick Start Example
"""
from pivota_agent import PivotaAgentClient

def main():
    print("🤖 Pivota Agent SDK - Quick Start")
    print("=" * 50)
    print()
    
    # Step 1: Create agent (or use existing API key)
    print("1️⃣ Creating agent...")
    
    # Option A: Create new agent
    # client = PivotaAgentClient.create_agent(
    #     agent_name="MyShoppingBot",
    #     agent_email="bot@mycompany.com"
    # )
    # print(f"✅ Agent created! API Key: {client.api_key[:20]}...")
    
    # Option B: Use existing API key
    client = PivotaAgentClient(
        api_key="ak_live_your_key_here"  # Replace with your key
    )
    print("✅ Client initialized")
    print()
    
    # Step 2: Check API health
    print("2️⃣ Checking API health...")
    health = client.health_check()
    print(f"✅ API Status: {health['status']}")
    print()
    
    # Step 3: List merchants
    print("3️⃣ Listing merchants...")
    merchants = client.list_merchants(status="active", limit=5)
    print(f"✅ Found {len(merchants)} merchants:")
    for m in merchants[:3]:
        print(f"   • {m['business_name']} ({m['merchant_id']})")
    print()
    
    # Step 4: Search products
    print("4️⃣ Searching products...")
    result = client.search_products(
        query="coffee",
        max_price=50,
        limit=5
    )
    products = result["products"]
    print(f"✅ Found {result['pagination']['total']} products (showing {len(products)}):")
    for p in products[:3]:
        print(f"   • {p['name']} - ${p['price']} ({p['merchant_name']})")
    print()
    
    # Step 5: Create order (example - don't actually run)
    print("5️⃣ Order creation example:")
    print("   (Commented out - uncomment to actually create order)")
    print("""
    order = client.create_order(
        merchant_id=merchants[0]['merchant_id'],
        items=[{"product_id": products[0]['id'], "quantity": 1}],
        customer_email="buyer@example.com"
    )
    print(f"Order created: {order['order_id']}")
    """)
    print()
    
    # Step 6: Payment example
    print("6️⃣ Payment example:")
    print("   (After creating an order)")
    print("""
    payment = client.create_payment(
        order_id=order['order_id'],
        payment_method={"type": "card", "token": "tok_visa"}
    )
    print(f"Payment status: {payment['status']}")
    """)
    print()
    
    print("=" * 50)
    print("✅ Quick start complete!")
    print()
    print("📝 Next steps:")
    print("   1. Get your API key from Employee Portal or /auth endpoint")
    print("   2. Replace 'ak_live_your_key_here' with your key")
    print("   3. Uncomment order/payment examples to test")
    print()

if __name__ == "__main__":
    main()




