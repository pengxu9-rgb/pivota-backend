#!/usr/bin/env python3
"""
Load Simulator for Dashboard Testing
Generates realistic payment events for Lovable dashboard testing
"""

import asyncio
import random
import time
import sys
import os

# Add current directory to path for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from realtime.metrics_store import record_event
from realtime.ws_manager import publish_event_to_ws

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

async def simulate_loop(rate_per_sec=1):
    """Simulate realistic payment events"""
    print(f"🚀 Starting load simulation at {rate_per_sec} events/second")
    print(f"📊 Agents: {len(AGENTS)}, Merchants: {len(MERCHANTS)}, PSPs: {len(PSPS)}")
    print("Press Ctrl+C to stop...")
    
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
            
            # Record event in metrics store
            record_event(event)
            
            # Publish to WebSocket clients
            await publish_event_to_ws(event)
            
            event_count += 1
            
            # Progress indicator
            if event_count % 10 == 0:
                print(f"📊 Generated {event_count} events (Latest: {event['order_id']} -> {status})")
                
    except KeyboardInterrupt:
        print(f"\n🛑 Load simulation stopped after {event_count} events")
        print("📊 Dashboard should now have realistic data!")

async def simulate_burst(events_count=50, duration_seconds=10):
    """Simulate a burst of events over a short period"""
    print(f"💥 Simulating burst: {events_count} events over {duration_seconds} seconds")
    
    events_per_second = events_count / duration_seconds
    delay = 1.0 / events_per_second
    
    for i in range(events_count):
        agent = random.choice(AGENTS)
        merchant = random.choice(MERCHANTS)
        psp = random.choice(PSPS)
        
        status = random.choices(
            ["succeeded", "failed", "queued_for_retry"], 
            weights=[0.8, 0.15, 0.05]
        )[0]
        
        event = {
            "type": "payment_result",
            "order_id": f"BURST_{int(time.time()*1000)}_{i}",
            "agent": agent["id"],
            "agent_name": agent["name"],
            "merchant": merchant["id"],
            "merchant_name": merchant["name"],
            "psp": psp,
            "status": status,
            "latency_ms": random.randint(100, 500),
            "attempt": random.randint(1, 2),
            "amount": round(random.uniform(15, 250), 2),
            "currency": random.choice(CURRENCIES),
            "timestamp": time.time()
        }
        
        record_event(event)
        await publish_event_to_ws(event)
        
        if i % 10 == 0:
            print(f"💥 Burst progress: {i}/{events_count} events")
        
        await asyncio.sleep(delay)
    
    print(f"✅ Burst simulation complete: {events_count} events sent")

async def simulate_realistic_pattern():
    """Simulate realistic traffic patterns (busy hours, quiet hours)"""
    print("🕐 Simulating realistic traffic patterns...")
    
    # Simulate different time periods
    patterns = [
        {"name": "Morning Rush", "rate": 5, "duration": 30, "description": "High morning traffic"},
        {"name": "Quiet Period", "rate": 1, "duration": 60, "description": "Low afternoon traffic"},
        {"name": "Evening Peak", "rate": 8, "duration": 45, "description": "Peak evening traffic"},
        {"name": "Night Quiet", "rate": 0.5, "duration": 30, "description": "Late night quiet"}
    ]
    
    for pattern in patterns:
        print(f"\n🔄 {pattern['name']}: {pattern['description']} ({pattern['rate']}/sec for {pattern['duration']}s)")
        
        end_time = time.time() + pattern['duration']
        event_count = 0
        
        while time.time() < end_time:
            await asyncio.sleep(1.0 / pattern['rate'])
            
            agent = random.choice(AGENTS)
            merchant = random.choice(MERCHANTS)
            psp = random.choice(PSPS)
            
            event = {
                "type": "payment_result",
                "order_id": f"PATTERN_{int(time.time()*1000)}",
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
            await publish_event_to_ws(event)
            event_count += 1
        
        print(f"✅ {pattern['name']} complete: {event_count} events")

def main():
    """Main function with different simulation modes"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Load Simulator for Dashboard Testing")
    parser.add_argument("--mode", choices=["continuous", "burst", "pattern"], default="continuous", 
                       help="Simulation mode")
    parser.add_argument("--rate", type=float, default=3, 
                       help="Events per second for continuous mode")
    parser.add_argument("--burst-events", type=int, default=50,
                       help="Number of events for burst mode")
    parser.add_argument("--burst-duration", type=int, default=10,
                       help="Duration in seconds for burst mode")
    
    args = parser.parse_args()
    
    if args.mode == "continuous":
        asyncio.run(simulate_loop(rate_per_sec=args.rate))
    elif args.mode == "burst":
        asyncio.run(simulate_burst(args.burst_events, args.burst_duration))
    elif args.mode == "pattern":
        asyncio.run(simulate_realistic_pattern())

if __name__ == "__main__":
    # Quick start examples:
    # python3 simulate_load.py --mode continuous --rate 3
    # python3 simulate_load.py --mode burst --burst-events 100 --burst-duration 15
    # python3 simulate_load.py --mode pattern
    
    main()
