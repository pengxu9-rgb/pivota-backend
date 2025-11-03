#!/usr/bin/env python3
"""
API-based Load Simulator
Generates events by calling the test data API endpoints
"""

import asyncio
import requests
import time
import json

async def generate_events_via_api(rate_per_sec=3):
    """Generate events by calling the API endpoints"""
    print(f"🚀 Starting API-based load simulation at {rate_per_sec} events/second")
    print("📊 Using test data API endpoints...")
    print("Press Ctrl+C to stop...")
    print("-" * 60)
    
    event_count = 0
    base_url = "http://localhost:8000"
    
    try:
        while True:
            await asyncio.sleep(1.0 / rate_per_sec)
            
            # Generate live events via API
            try:
                response = requests.post(f"{base_url}/api/test/generate-live-events?count=1", timeout=5)
                if response.status_code == 200:
                    event_count += 1
                    if event_count % 10 == 0:
                        print(f"📊 Generated {event_count} events via API")
                else:
                    print(f"⚠️  API error: {response.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"⚠️  Connection error: {e}")
                
    except KeyboardInterrupt:
        print(f"\n🛑 Load simulation stopped after {event_count} events")
        print("📊 Check your dashboard at: http://localhost:8000/api/snapshot")

async def quick_api_burst(count=50):
    """Quick burst using API"""
    print(f"💥 Generating {count} events via API...")
    
    base_url = "http://localhost:8000"
    
    # First, generate sample data
    try:
        response = requests.post(f"{base_url}/api/test/generate-sample-data", timeout=10)
        if response.status_code == 200:
            result = response.json()
            print(f"✅ Generated {result['events_generated']} sample events")
        else:
            print(f"⚠️  Sample data generation failed: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"⚠️  Connection error: {e}")
        return
    
    # Then generate live events
    for i in range(count):
        try:
            response = requests.post(f"{base_url}/api/test/generate-live-events?count=1", timeout=5)
            if response.status_code == 200:
                if i % 10 == 0:
                    print(f"💥 Generated {i}/{count} live events...")
            else:
                print(f"⚠️  API error: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"⚠️  Connection error: {e}")
        
        await asyncio.sleep(0.1)
    
    print(f"✅ Burst complete: {count} events generated!")
    print("📊 Check: http://localhost:8000/api/snapshot")

async def show_current_stats():
    """Show current dashboard stats"""
    base_url = "http://localhost:8000"
    
    try:
        response = requests.get(f"{base_url}/api/snapshot", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print("📊 **Current Dashboard Stats:**")
            print(f"   📈 Total Events: {data['summary']['total']}")
            print(f"   ✅ Success: {data['summary']['success']}")
            print(f"   ❌ Failed: {data['summary']['fail']}")
            print(f"   🔄 Retries: {data['summary']['retries']}")
            
            if data['psp']:
                print(f"   📊 PSPs: {list(data['psp'].keys())}")
                for psp, stats in data['psp'].items():
                    print(f"      {psp}: {stats['success_count']} success, {stats['avg_latency']:.1f}ms avg")
            
            if data['agent']:
                print(f"   🤖 Agents: {len(data['agent'])} active")
            
            if data['merchant']:
                print(f"   🏪 Merchants: {len(data['merchant'])} active")
        else:
            print(f"⚠️  Could not fetch stats: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"⚠️  Connection error: {e}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="API-based Load Simulator")
    parser.add_argument("--mode", choices=["continuous", "burst", "stats"], default="continuous")
    parser.add_argument("--rate", type=int, default=3, help="Events per second")
    parser.add_argument("--count", type=int, default=50, help="Events for burst mode")
    
    args = parser.parse_args()
    
    if args.mode == "continuous":
        asyncio.run(generate_events_via_api(args.rate))
    elif args.mode == "burst":
        asyncio.run(quick_api_burst(args.count))
    elif args.mode == "stats":
        asyncio.run(show_current_stats())
