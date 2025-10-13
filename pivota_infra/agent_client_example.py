#!/usr/bin/env python3
"""
Agent Client Example
Demonstrates how AI agents can connect to the MCP server
"""

import asyncio
import json
from mcp_server import handle_agent_request, mcp_server

class AgentClient:
    """Example AI agent client that connects to MCP server"""
    
    def __init__(self, agent_id: str, agent_name: str):
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.customer_id = None
    
    async def search_products(self, query: str, category: str = None) -> list:
        """Search for products across merchant network"""
        request = {
            "method": "search_products",
            "params": {
                "query": query,
                "category": category
            }
        }
        
        response = await handle_agent_request(request)
        if "error" in response:
            print(f"❌ Error searching products: {response['error']}")
            return []
        
        return response["result"]
    
    async def get_product_details(self, product_id: str) -> dict:
        """Get detailed product information"""
        request = {
            "method": "get_product_details",
            "params": {
                "product_id": product_id
            }
        }
        
        response = await handle_agent_request(request)
        if "error" in response:
            print(f"❌ Error getting product details: {response['error']}")
            return {}
        
        return response["result"]
    
    async def create_customer(self, name: str, email: str) -> str:
        """Create a new customer"""
        request = {
            "method": "create_customer",
            "params": {
                "name": name,
                "email": email
            }
        }
        
        response = await handle_agent_request(request)
        if "error" in response:
            print(f"❌ Error creating customer: {response['error']}")
            return None
        
        self.customer_id = response["result"]
        return self.customer_id
    
    async def create_order(self, items: list, merchant_id: str) -> str:
        """Create an order"""
        if not self.customer_id:
            print("❌ No customer created yet")
            return None
        
        request = {
            "method": "create_order",
            "params": {
                "customer_id": self.customer_id,
                "items": items,
                "merchant_id": merchant_id
            }
        }
        
        response = await handle_agent_request(request)
        if "error" in response:
            print(f"❌ Error creating order: {response['error']}")
            return None
        
        return response["result"]
    
    async def process_payment(self, order_id: str, psp: str = None) -> dict:
        """Process payment for an order"""
        request = {
            "method": "process_payment",
            "params": {
                "order_id": order_id,
                "psp": psp
            }
        }
        
        response = await handle_agent_request(request)
        if "error" in response:
            print(f"❌ Error processing payment: {response['error']}")
            return {}
        
        return response["result"]
    
    async def get_order_status(self, order_id: str) -> dict:
        """Get order status"""
        request = {
            "method": "get_order_status",
            "params": {
                "order_id": order_id
            }
        }
        
        response = await handle_agent_request(request)
        if "error" in response:
            print(f"❌ Error getting order status: {response['error']}")
            return {}
        
        return response["result"]
    
    async def get_merchant_network(self) -> dict:
        """Get merchant network information"""
        request = {
            "method": "get_merchant_network",
            "params": {}
        }
        
        response = await handle_agent_request(request)
        if "error" in response:
            print(f"❌ Error getting merchant network: {response['error']}")
            return {}
        
        return response["result"]

async def demo_agent_workflow():
    """Demonstrate complete agent workflow"""
    print("🤖 **AI AGENT WORKFLOW DEMO:**")
    print("")
    
    # Create agent
    agent = AgentClient("agent_001", "Shopping Assistant")
    print(f"🤖 Agent: {agent.agent_name} ({agent.agent_id})")
    print("")
    
    # Step 1: Get merchant network
    print("📋 **Step 1: Getting Merchant Network**")
    network = await agent.get_merchant_network()
    print(f"   🏪 Available Merchants: {network['total_merchants']}")
    for merchant in network['merchants']:
        print(f"      • {merchant['name']} ({merchant['type']}) - {merchant['currency']}")
    print("")
    
    # Step 2: Search for products
    print("🔍 **Step 2: Searching for Products**")
    products = await agent.search_products("tee", "Fashion")
    print(f"   📦 Found {len(products)} products")
    if products:
        for product in products[:3]:  # Show first 3
            print(f"      • {product['name']} - {product['currency']}{product['price']} ({product['merchant_name']})")
    else:
        # Try broader search
        products = await agent.search_products("clothing")
        print(f"   📦 Broader search found {len(products)} products")
        if products:
            for product in products[:3]:  # Show first 3
                print(f"      • {product['name']} - {product['currency']}{product['price']} ({product['merchant_name']})")
    print("")
    
    # Step 3: Get product details
    if products:
        print("📋 **Step 3: Getting Product Details**")
        product_details = await agent.get_product_details(products[0]['id'])
        print(f"   📦 Product: {product_details['name']}")
        print(f"   💰 Price: {product_details['currency']}{product_details['price']}")
        print(f"   📝 Description: {product_details['description']}")
        print(f"   🏪 Merchant: {product_details['merchant_name']}")
        print("")
    
    # Step 4: Create customer
    print("👤 **Step 4: Creating Customer**")
    customer_id = await agent.create_customer("John Doe", "john.doe@example.com")
    print(f"   👤 Customer ID: {customer_id}")
    print("")
    
    # Step 5: Create order
    print("🛒 **Step 5: Creating Order**")
    order_id = None
    if products:
        items = [{
            "product_id": products[0]['id'],
            "quantity": 2
        }]
        order_id = await agent.create_order(items, products[0]['merchant_id'])
        print(f"   🛒 Order ID: {order_id}")
        print("")
    
    # Step 6: Process payment
    print("💳 **Step 6: Processing Payment**")
    if order_id:
        payment_result = await agent.process_payment(order_id)
        print(f"   💳 Payment Success: {payment_result.success}")
        print(f"   🆔 Payment ID: {payment_result.payment_id}")
        print(f"   💰 PSP: {payment_result.psp}")
        print("")
    
    # Step 7: Check order status
    print("📊 **Step 7: Checking Order Status**")
    if order_id:
        order_status = await agent.get_order_status(order_id)
        print(f"   📊 Order Status: {order_status['status']}")
        print(f"   💳 Payment Status: {order_status['payment_status']}")
        print(f"   💰 Total Amount: {order_status['currency']}{order_status['total_amount']}")
        print("")
    
    print("🎉 **AGENT WORKFLOW COMPLETE!**")
    print("   ✅ Product search and selection")
    print("   ✅ Customer creation")
    print("   ✅ Order creation")
    print("   ✅ Payment processing")
    print("   ✅ Order tracking")

async def demo_multi_agent_scenario():
    """Demonstrate multi-agent scenario"""
    print("🤖🤖 **MULTI-AGENT SCENARIO:**")
    print("")
    
    # Create multiple agents
    agents = [
        AgentClient("agent_001", "Fashion Assistant"),
        AgentClient("agent_002", "Tech Assistant"),
        AgentClient("agent_003", "General Shopping Assistant")
    ]
    
    # Each agent searches for different products
    search_queries = [
        ("clothing", "Fashion"),
        ("electronics", "Electronics"),
        ("accessories", None)
    ]
    
    for i, agent in enumerate(agents):
        print(f"🤖 **{agent.agent_name}**")
        query, category = search_queries[i]
        products = await agent.search_products(query, category)
        print(f"   🔍 Searched for '{query}' - Found {len(products)} products")
        
        if products:
            # Create customer and order
            customer_id = await agent.create_customer(f"Customer {i+1}", f"customer{i+1}@example.com")
            items = [{"product_id": products[0]['id'], "quantity": 1}]
            order_id = await agent.create_order(items, products[0]['merchant_id'])
            payment_result = await agent.process_payment(order_id)
            print(f"   ✅ Order created and payment processed: {payment_result['success']}")
        print("")
    
    print("🎉 **MULTI-AGENT SCENARIO COMPLETE!**")
    print("   ✅ Multiple agents working simultaneously")
    print("   ✅ Different product searches")
    print("   ✅ Independent order processing")
    print("   ✅ Scalable agent infrastructure")

def main():
    """Run agent client examples"""
    print("🚀 **MCP SERVER - AGENT CLIENT EXAMPLES:**")
    print("")
    
    # Run single agent workflow
    asyncio.run(demo_agent_workflow())
    
    print("")
    print("=" * 60)
    print("")
    
    # Run multi-agent scenario
    asyncio.run(demo_multi_agent_scenario())
    
    print("")
    print("🌐 **MCP SERVER CAPABILITIES:**")
    print("   🤖 AI Agent Integration")
    print("   🏪 Multi-Merchant Network")
    print("   💳 Payment Processing")
    print("   📊 Analytics & Reporting")
    print("   🔄 Order Management")
    print("   👤 Customer Management")
    print("")
    print("🎯 **Perfect for:**")
    print("   • AI Shopping Assistants")
    print("   • E-commerce Bots")
    print("   • Recommendation Systems")
    print("   • Multi-merchant Aggregators")
    print("   • Payment Infrastructure")

if __name__ == "__main__":
    main()
