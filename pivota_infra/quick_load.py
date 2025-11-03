#!/usr/bin/env python3
"""
Quick Load Generator - Simple and Fast
Generates events and immediately shows results
"""

import requests
import time
import json

def generate_events_quickly(count=50):
    """Generate events quickly and show results"""
    print(f"🚀 Generating {count} events quickly...")
    
    base_url = "http://localhost:8000"
    
    # Generate sample data first
    try:
        print("📊 Generating sample data...")
        response = requests.post(f"{base_url}/api/test/generate-sample-data", timeout=10)
        if response.status_code == 200:
            result = response.json()
            print(f"✅ Generated {result['events_generated']} sample events")
        else:
            print(f"⚠️  Sample data failed: {response.status_code}")
    except Exception as e:
        print(f"⚠️  Error: {e}")
        return
    
    # Generate live events
    print(f"💥 Generating {count} live events...")
    for i in range(count):
        try:
            response = requests.post(f"{base_url}/api/test/generate-live-events?count=1", timeout=5)
            if i % 10 == 0:
                print(f"   Generated {i}/{count} events...")
        except Exception as e:
            print(f"⚠️  Error at event {i}: {e}")
            break
    
    print(f"✅ Generated {count} events!")
    
    # Show current stats
    try:
        response = requests.get(f"{base_url}/api/snapshot", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print("\n📊 **Current Dashboard Stats:**")
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
                
            print(f"\n🎯 **Perfect for Lovable Dashboard!**")
            print(f"   📊 API: http://localhost:8000/api/snapshot")
            print(f"   🌐 WebSocket: ws://localhost:8000/ws/metrics")
        else:
            print(f"⚠️  Could not fetch stats: {response.status_code}")
    except Exception as e:
        print(f"⚠️  Error fetching stats: {e}")

def continuous_generation(rate=3, duration=60):
    """Generate events continuously for a specific duration"""
    print(f"🚀 Generating events at {rate}/second for {duration} seconds...")
    print("Press Ctrl+C to stop early...")
    
    base_url = "http://localhost:8000"
    event_count = 0
    start_time = time.time()
    
    try:
        while time.time() - start_time < duration:
            try:
                response = requests.post(f"{base_url}/api/test/generate-live-events?count=1", timeout=5)
                if response.status_code == 200:
                    event_count += 1
                    if event_count % 10 == 0:
                        elapsed = int(time.time() - start_time)
                        print(f"📊 Generated {event_count} events in {elapsed}s...")
                else:
                    print(f"⚠️  API error: {response.status_code}")
            except Exception as e:
                print(f"⚠️  Error: {e}")
                break
            
            time.sleep(1.0 / rate)
            
    except KeyboardInterrupt:
        print(f"\n🛑 Stopped early after {event_count} events")
    
    print(f"✅ Generated {event_count} events in {int(time.time() - start_time)} seconds!")
    print("📊 Check your dashboard: http://localhost:8000/api/snapshot")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "continuous":
            rate = int(sys.argv[2]) if len(sys.argv) > 2 else 3
            duration = int(sys.argv[3]) if len(sys.argv) > 3 else 60
            continuous_generation(rate, duration)
        else:
            count = int(sys.argv[1])
            generate_events_quickly(count)
    else:
        # Default: generate 50 events
        generate_events_quickly(50)
