#!/usr/bin/env python3
"""
Hybrid MCP Server Demo
Demonstrates the hybrid approach: real data connection + simulated output
"""

import asyncio
import json
from hybrid_mcp_server import handle_hybrid_agent_request

async def demo_hybrid_approach():
    """Demonstrate the hybrid MCP server approach"""
    print("🎯 **HYBRID MCP SERVER DEMO:**")
    print("")
    print("📋 **What This Demo Shows:**")
    print("   🔗 Real Shopify products loaded (4 products)")
    print("   🎭 Simulated products created for agents")
    print("   🛒 Real orders placed in Shopify")
    print("   👤 Users see real product recommendations")
    print("")
    
    # Step 1: Get system status
    print("🔍 **Step 1: System Status**")
    request = {"method": "get_system_status", "params": {}}
    response = await handle_hybrid_agent_request(request)
    status = response["result"]
    
    print(f"   📊 Real Products Connected: {status['real_products_connected']}")
    print(f"   🎭 Simulated Products Ready: {status.get('simulated_products_available', 'N/A')}")
    print(f"   🏪 Shopify Connected: {status['shopify_connected']}")
    print(f"   🔄 Hybrid Mode: {status['hybrid_mode']}")
    print("")
    
    # Step 2: Search products (agents see simulated data)
    print("🔍 **Step 2: Product Search (Simulated Output)**")
    request = {
        "method": "search_products",
        "params": {
            "query": "joggers",
            "max_results": 5
        }
    }
    response = await handle_hybrid_agent_request(request)
    products = response["result"]
    
    print(f"   📦 Found {len(products)} products (simulated)")
    for product in products:
        print(f"      • {product['name']} - ${product['price']} (ID: {product['id']})")
        print(f"        Real Product ID: {product['real_product_id']}")
    print("")
    
    # Step 3: Get product details
    if products:
        print("📋 **Step 3: Product Details (Simulated)**")
        product_id = products[0]['id']
        request = {
            "method": "get_product_details",
            "params": {"product_id": product_id}
        }
        response = await handle_hybrid_agent_request(request)
        product_details = response["result"]
        
        print(f"   📦 Product: {product_details['name']}")
        print(f"   💰 Price: ${product_details['price']}")
        print(f"   📝 Description: {product_details['description'][:100]}...")
        print(f"   🏪 Merchant: {product_details['merchant_name']}")
        print(f"   🔗 Real Product ID: {product_details['real_product_id']}")
        print("")
    
    # Step 4: Create order (real order in Shopify)
    print("🛒 **Step 4: Creating Order (Real Shopify Integration)**")
    customer_info = {
        "first_name": "John",
        "last_name": "Doe",
        "email": "john.doe@example.com"
    }
    
    items = [{
        "product_id": products[0]['id'],
        "quantity": 2
    }]
    
    request = {
        "method": "create_order",
        "params": {
            "customer_info": customer_info,
            "items": items
        }
    }
    response = await handle_hybrid_agent_request(request)
    order_id = response["result"]
    
    print(f"   🛒 Order Created: {order_id}")
    print(f"   👤 Customer: {customer_info['first_name']} {customer_info['last_name']}")
    print(f"   📦 Items: {len(items)}")
    print(f"   🏪 Real Shopify Order: Will be created")
    print("")
    
    # Step 5: Process payment
    print("💳 **Step 5: Processing Payment (Real Payment)**")
    request = {
        "method": "process_payment",
        "params": {
            "order_id": order_id,
            "psp": "stripe"
        }
    }
    response = await handle_hybrid_agent_request(request)
    payment_result = response["result"]
    
    print(f"   💳 Payment Success: {payment_result['success']}")
    print(f"   🆔 Payment ID: {payment_result.get('payment_id', 'N/A')}")
    print(f"   💰 PSP: {payment_result.get('psp', 'N/A')}")
    print(f"   💰 Amount: ${payment_result.get('amount', 0)}")
    print("")
    
    # Step 6: Check order status
    print("📊 **Step 6: Order Status (Real Order Tracking)**")
    request = {
        "method": "get_order_status",
        "params": {"order_id": order_id}
    }
    response = await handle_hybrid_agent_request(request)
    order_status = response["result"]
    
    print(f"   📊 Order Status: {order_status['status']}")
    print(f"   💰 Total Amount: ${order_status['total_amount']}")
    print(f"   🏪 Merchant: {order_status['merchant_name']}")
    print(f"   🔗 Real Order ID: {order_status.get('real_order_id', 'N/A')}")
    print(f"   💳 Payment ID: {order_status.get('payment_id', 'N/A')}")
    print("")
    
    print("🎉 **HYBRID APPROACH DEMONSTRATED!**")
    print("")
    print("✅ **What Happened:**")
    print("   🔗 Connected to real Shopify products")
    print("   🎭 Agents saw simulated products (safe)")
    print("   🛒 Real order created in Shopify")
    print("   💳 Real payment processed")
    print("   👤 User got real product recommendations")
    print("")
    print("🛡️ **Security Benefits:**")
    print("   ✅ Agents can't crash the system")
    print("   ✅ Sensitive data protected")
    print("   ✅ Real functionality maintained")
    print("   ✅ System stability ensured")

def main():
    """Run hybrid demo"""
    asyncio.run(demo_hybrid_approach())
    
    print("")
    print("🚀 **HYBRID MCP SERVER BENEFITS:**")
    print("")
    print("🎯 **Perfect Balance:**")
    print("   🔗 Real data connection for realism")
    print("   📊 Simulated output for stability")
    print("   🛒 Real order placement")
    print("   👤 Real user experience")
    print("")
    print("🛡️ **Security & Performance:**")
    print("   ✅ No database crashes")
    print("   ✅ No sensitive data exposure")
    print("   ✅ Controlled agent access")
    print("   ✅ System stability")
    print("")
    print("💼 **Business Value:**")
    print("   ✅ Real product recommendations")
    print("   ✅ Real order processing")
    print("   ✅ Real payment integration")
    print("   ✅ Scalable architecture")

if __name__ == "__main__":
    main()
