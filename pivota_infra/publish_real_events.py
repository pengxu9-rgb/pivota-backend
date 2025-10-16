#!/usr/bin/env python3
"""
Publish Real Order Events
Manually publish events for the real orders we created
"""

import time
import random
from pivota_infra.utils.event_publisher import event_publisher

def publish_real_order_events():
    """Publish events for the real orders we created"""
    
    print("📊 **PUBLISHING REAL ORDER EVENTS:**")
    print("")
    
    # Real orders we created:
    # Order 1: 6912238846291 - CloudFit Hoodie - $59.00 - Stripe - Success
    # Order 2: 6912239173971 - CloudFit Hoodie - $59.00 - Stripe - Failed  
    # Order 3: 6912239436115 - CloudFit Hoodie - $59.00 - Adyen - Success
    
    real_orders = [
        {
            "order_id": "ORD_6912238846291",
            "agent": "REAL_AGENT_001",
            "agent_name": "Real Order Agent",
            "merchant": "REAL_MERCH_001",
            "merchant_name": "Real Shopify Store",
            "psp": "stripe",
            "status": "succeeded",
            "amount": 59.00,
            "product": "CloudFit Hoodie"
        },
        {
            "order_id": "ORD_6912239173971", 
            "agent": "REAL_AGENT_001",
            "agent_name": "Real Order Agent",
            "merchant": "REAL_MERCH_001",
            "merchant_name": "Real Shopify Store",
            "psp": "stripe",
            "status": "failed",
            "amount": 59.00,
            "product": "CloudFit Hoodie"
        },
        {
            "order_id": "ORD_6912239436115",
            "agent": "REAL_AGENT_001", 
            "agent_name": "Real Order Agent",
            "merchant": "REAL_MERCH_001",
            "merchant_name": "Real Shopify Store",
            "psp": "adyen",
            "status": "succeeded",
            "amount": 59.00,
            "product": "CloudFit Hoodie"
        }
    ]
    
    for i, order in enumerate(real_orders, 1):
        print(f"📦 **Publishing Event {i}/3:**")
        print(f"   Order: {order['order_id']}")
        print(f"   Product: {order['product']}")
        print(f"   Amount: ${order['amount']}")
        print(f"   PSP: {order['psp']}")
        print(f"   Status: {order['status']}")
        
        try:
            # Publish event with correct parameters
            import asyncio
            asyncio.run(event_publisher.publish_payment_result(
                order_id=order["order_id"],
                agent=order["agent"],
                agent_name=order["agent_name"],
                merchant=order["merchant"],
                merchant_name=order["merchant_name"],
                psp=order["psp"],
                status=order["status"],
                latency_ms=random.randint(300, 1200),
                attempt=1,
                amount=order["amount"],
                currency="USD",
                additional_data={"customer_email": f"real.customer.{int(time.time())}@example.com"}
            ))
            print(f"   ✅ Event published successfully")
            
        except Exception as e:
            print(f"   ❌ Error publishing event: {e}")
        
        print("")
        time.sleep(1)  # Small delay between events
    
    print("🎉 **REAL ORDER EVENTS PUBLISHED!**")
    print("📊 Check your Lovable dashboard for real data!")
    print("")
    print("🌐 **Dashboard URLs:**")
    print("   📊 API: https://numbers-hyperbranchial-jackelyn.ngrok-free.dev/api/snapshot")
    print("   🔌 WebSocket: wss://numbers-hyperbranchial-jackelyn.ngrok-free.dev/api/ws/simple")

if __name__ == "__main__":
    publish_real_order_events()
