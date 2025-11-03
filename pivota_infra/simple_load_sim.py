#!/usr/bin/env python3
"""
Simple Load Simulator - No WebSocket dependencies
Generates events and stores them in metrics store for Lovable dashboard
"""

import asyncio
import random
import time
import sys
import os

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from realtime.metrics_store import record_event

# Configuration
AGENTS = [
    {"id": "AGENT_001", "name": "TravelBot A"},
    {"id": "AGENT_002", "name": "ShoppingBot B"}, 
    {"id": "AGENT_003", "name": "FashionBot C"},
    {"id": "AGENT_004", "name": "ElectronicsBot D"},
    {"id": "AGENT_005", "name": "HomeBot E"}
]

MERCHANTS = [
    {"id": "MERCH_001", "name": "Shopify Fashion Store"},
    {"id": "MERCH_002", "name": "Wix Electronics Store"},
    {"id": "MERCH_003", "name": "Shopify Travel Store"},
    {"id": "MERCH_004", "name": "Wix Home Decor Store"},
    {"id": "MERCH_005", "name": "Shopify Beauty Store"}
]

PSPS = ["stripe", "adyen", "paypal", "square"]
CURRENCIES = ["EUR", "USD", "GBP", "CAD"]

async def simple_continuous_load(rate_per_sec=3):
    """Simple continuous load simulation without WebSocket"""
    print(f"🚀 Starting simple load simulation at {rate_per_sec} events/second")
    print(f"📊 Agents: {len(AGENTS)}, Merchants: {len(MERCHANTS)}, PSPs: {len(PSPS)}")
    print("📈 Events will be stored in metrics store (check /api/snapshot)")
    print("Press Ctrl+C to stop...")
    print("-" * 60)
    
    event_count = 0
    
    try:
        while True:
            await asyncio.sleep(1.0 / rate_per_sec)
            
            # Generate realistic event
            agent = random.choice(AGENTS)
            merchant = random.choice(MERCHANTS)
            psp = random.choice(PSPS)
            
            # Realistic status distribution: 85% success, 10% fail, 5% retry
            status = random.choices(
                ["succeeded", "failed", "queued_for_retry"], 
                weights=[0.85, 0.1, 0.05]
            )[0]
            
            # PSP-specific latency patterns
            if psp == "stripe":
                latency_ms = random.randint(120, 350)
            elif psp == "adyen":
                latency_ms = random.randint(200, 500)
            elif psp == "paypal":
                latency_ms = random.randint(300, 600)
            else:  # square
                latency_ms = random.randint(150, 400)
            
            event = {
                "type": "payment_result",
                "order_id": f"ORD_{int(time.time()*1000)}",
                "agent": agent["id"],
                "agent_name": agent["name"],
                "merchant": merchant["id"],
                "merchant_name": merchant["name"],
                "psp": psp,
                "status": status,
                "latency_ms": latency_ms,
                "attempt": random.randint(1, 3),
                "amount": round(random.uniform(20, 300), 2),
                "currency": random.choice(CURRENCIES),
                "timestamp": time.time()
            }
            
            # Record event in metrics store only (no WebSocket)
            record_event(event)
            
            event_count += 1
            
            # Progress indicator every 10 events
            if event_count % 10 == 0:
                print(f"📊 Generated {event_count} events | Latest: {event['order_id']} -> {status} via {psp} ({latency_ms}ms)")
                
    except KeyboardInterrupt:
        print(f"\n🛑 Load simulation stopped after {event_count} events")
        print("📊 Check your dashboard at: http://localhost:8000/api/snapshot")
        print("🎯 Perfect for Lovable testing!")

async def quick_burst(count=50):
    """Quick burst of events"""
    print(f"💥 Generating {count} events quickly...")
    
    for i in range(count):
        agent = random.choice(AGENTS)
        merchant = random.choice(MERCHANTS)
        psp = random.choice(PSPS)
        
        event = {
            "type": "payment_result",
            "order_id": f"BURST_{int(time.time()*1000)}_{i}",
            "agent": agent["id"],
            "agent_name": agent["name"],
            "merchant": merchant["id"],
            "merchant_name": merchant["name"],
            "psp": psp,
            "status": random.choices(["succeeded", "failed", "queued_for_retry"], weights=[0.85, 0.1, 0.05])[0],
            "latency_ms": random.randint(120, 600),
            "attempt": random.randint(1, 3),
            "amount": round(random.uniform(20, 300), 2),
            "currency": random.choice(CURRENCIES),
            "timestamp": time.time()
        }
        
        record_event(event)
        
        if i % 10 == 0:
            print(f"💥 Generated {i}/{count} events...")
        
        await asyncio.sleep(0.1)  # Small delay
    
    print(f"✅ Burst complete: {count} events generated!")
    print("📊 Check: http://localhost:8000/api/snapshot")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Simple Load Simulator")
    parser.add_argument("--mode", choices=["continuous", "burst"], default="continuous")
    parser.add_argument("--rate", type=int, default=3, help="Events per second")
    parser.add_argument("--count", type=int, default=50, help="Events for burst mode")
    
    args = parser.parse_args()
    
    if args.mode == "continuous":
        asyncio.run(simple_continuous_load(args.rate))
    elif args.mode == "burst":
        asyncio.run(quick_burst(args.count))
