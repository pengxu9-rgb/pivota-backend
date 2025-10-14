"""
Test Data Routes
Generate sample data for Lovable dashboard testing
"""

import asyncio
import time
import random
from typing import Dict, Any
from fastapi import APIRouter
from realtime.metrics_store import record_event

# Import WebSocket publishing function
try:
    from main import publish_event_to_ws
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

router = APIRouter(prefix="/api/test", tags=["test-data"])

@router.post("/generate-sample-data")
async def generate_sample_data():
    """Generate comprehensive sample data for dashboard testing"""
    
    # Sample agents, merchants, and PSPs
    agents = [
        {"id": "AGENT_001", "name": "TravelBot A"},
        {"id": "AGENT_002", "name": "ShoppingBot B"},
        {"id": "AGENT_003", "name": "FashionBot C"},
        {"id": "AGENT_004", "name": "ElectronicsBot D"}
    ]
    
    merchants = [
        {"id": "MERCH_001", "name": "Shopify Fashion Store"},
        {"id": "MERCH_002", "name": "Wix Electronics Store"},
        {"id": "MERCH_003", "name": "Shopify Travel Store"},
        {"id": "MERCH_004", "name": "Wix Home Decor Store"}
    ]
    
    psps = ["stripe", "adyen", "paypal", "square"]
    currencies = ["EUR", "USD", "GBP", "CAD"]
    statuses = ["succeeded", "failed", "queued_for_retry"]
    
    # Generate 50 sample events
    events_generated = 0
    for i in range(50):
        agent = random.choice(agents)
        merchant = random.choice(merchants)
        psp = random.choice(psps)
        status = random.choices(statuses, weights=[70, 20, 10])[0]  # 70% success, 20% fail, 10% retry
        
        event = {
            "type": "payment_result",
            "order_id": f"ORD_{i+1:03d}",
            "agent": agent["id"],
            "agent_name": agent["name"],
            "merchant": merchant["id"],
            "merchant_name": merchant["name"],
            "psp": psp,
            "status": status,
            "latency_ms": random.randint(100, 800),
            "attempt": random.randint(1, 3),
            "amount": round(random.uniform(10, 500), 2),
            "currency": random.choice(currencies),
            "timestamp": time.time() - random.randint(0, 3600)  # Events from last hour
        }
        
        record_event(event)
        
        # Also send individual event to WebSocket for EventFeed
        if WS_AVAILABLE:
            try:
                await publish_event_to_ws(event)
            except Exception as e:
                print(f"Error sending WebSocket event: {e}")
        
        events_generated += 1
    
    return {
        "status": "success",
        "message": f"Generated {events_generated} sample events",
        "events_generated": events_generated,
        "timestamp": time.time()
    }

@router.post("/generate-live-events")
async def generate_live_events(count: int = 5):
    """Generate live events for real-time testing"""
    
    agents = ["AGENT_001", "AGENT_002", "AGENT_003"]
    merchants = ["MERCH_001", "MERCH_002", "MERCH_003"]
    psps = ["stripe", "adyen", "paypal"]
    
    for i in range(count):
        event = {
            "type": "payment_result",
            "order_id": f"LIVE_{int(time.time())}_{i}",
            "agent": random.choice(agents),
            "agent_name": f"Agent {random.choice(agents)}",
            "merchant": random.choice(merchants),
            "merchant_name": f"Merchant {random.choice(merchants)}",
            "psp": random.choice(psps),
            "status": random.choice(["succeeded", "failed", "queued_for_retry"]),
            "latency_ms": random.randint(150, 600),
            "attempt": random.randint(1, 2),
            "amount": round(random.uniform(25, 200), 2),
            "currency": random.choice(["EUR", "USD"]),
            "timestamp": time.time()
        }
        
        record_event(event)
        
        # Also send individual event to WebSocket for EventFeed
        if WS_AVAILABLE:
            try:
                await publish_event_to_ws(event)
            except Exception as e:
                print(f"Error sending WebSocket event: {e}")
        
        await asyncio.sleep(0.1)  # Small delay between events
    
    return {
        "status": "success",
        "message": f"Generated {count} live events",
        "events_generated": count,
        "timestamp": time.time()
    }

@router.post("/generate-eventfeed-events")
async def generate_eventfeed_events(count: int = 10):
    """Generate individual events specifically for EventFeed testing"""
    
    agents = [
        {"id": "AGENT_001", "name": "TravelBot A"},
        {"id": "AGENT_002", "name": "ShoppingBot B"},
        {"id": "AGENT_003", "name": "FashionBot C"},
        {"id": "AGENT_004", "name": "ElectronicsBot D"}
    ]
    
    merchants = [
        {"id": "MERCH_001", "name": "Shopify Fashion Store"},
        {"id": "MERCH_002", "name": "Wix Electronics Store"},
        {"id": "MERCH_003", "name": "Shopify Travel Store"},
        {"id": "MERCH_004", "name": "Wix Home Decor Store"}
    ]
    
    psps = ["stripe", "adyen", "paypal", "square"]
    statuses = ["succeeded", "failed", "queued_for_retry"]
    
    events_sent = 0
    for i in range(count):
        agent = random.choice(agents)
        merchant = random.choice(merchants)
        psp = random.choice(psps)
        status = random.choices(statuses, weights=[75, 20, 5])[0]  # 75% success, 20% fail, 5% retry
        
        # Create event in the exact format expected by EventFeed
        event = {
            "type": "payment_result",
            "order_id": f"EVT_{int(time.time())}_{i}",
            "agent": agent["name"],
            "merchant": merchant["name"],
            "psp": psp.upper(),
            "status": status,
            "amount": round(random.uniform(15, 300), 2),
            "currency": random.choice(["EUR", "USD", "GBP"]),
            "timestamp": time.time(),
            "latency_ms": random.randint(120, 650),
            "attempt": random.randint(1, 3)
        }
        
        # Record to metrics store
        record_event(event)
        
        # Send individual event to WebSocket for EventFeed
        if WS_AVAILABLE:
            try:
                await publish_event_to_ws(event)
                events_sent += 1
            except Exception as e:
                print(f"Error sending WebSocket event: {e}")
        
        # Small delay between events for better visualization
        await asyncio.sleep(0.5)
    
    return {
        "status": "success",
        "message": f"Generated {count} EventFeed events",
        "events_sent_to_websocket": events_sent,
        "events_generated": count,
        "timestamp": time.time()
    }

@router.get("/sample-snapshot")
async def get_sample_snapshot():
    """Get a sample snapshot with realistic data structure"""
    
    sample_snapshot = {
        "summary": {
            "total": 150,
            "success": 120,
            "fail": 25,
            "retries": 5
        },
        "psp": {
            "stripe": {
                "success_count": 80,
                "fail_count": 10,
                "retry_count": 2,
                "avg_latency": 210.5,
                "total": 92
            },
            "adyen": {
                "success_count": 30,
                "fail_count": 8,
                "retry_count": 2,
                "avg_latency": 280.3,
                "total": 40
            },
            "paypal": {
                "success_count": 10,
                "fail_count": 7,
                "retry_count": 1,
                "avg_latency": 350.8,
                "total": 18
            }
        },
        "agent": {
            "AGENT_001": {
                "success_count": 45,
                "fail_count": 8,
                "retry_count": 2,
                "avg_latency": 220.1,
                "total": 55,
                "agent_name": "TravelBot A"
            },
            "AGENT_002": {
                "success_count": 35,
                "fail_count": 12,
                "retry_count": 1,
                "avg_latency": 250.7,
                "total": 48,
                "agent_name": "ShoppingBot B"
            },
            "AGENT_003": {
                "success_count": 25,
                "fail_count": 3,
                "retry_count": 2,
                "avg_latency": 195.3,
                "total": 30,
                "agent_name": "FashionBot C"
            }
        },
        "merchant": {
            "MERCH_001": {
                "success_count": 50,
                "fail_count": 10,
                "retry_count": 2,
                "avg_latency": 235.2,
                "total": 62,
                "merchant_name": "Shopify Fashion Store"
            },
            "MERCH_002": {
                "success_count": 40,
                "fail_count": 8,
                "retry_count": 1,
                "avg_latency": 245.8,
                "total": 49,
                "merchant_name": "Wix Electronics Store"
            },
            "MERCH_003": {
                "success_count": 30,
                "fail_count": 7,
                "retry_count": 2,
                "avg_latency": 220.5,
                "total": 39,
                "merchant_name": "Shopify Travel Store"
            }
        },
        "psp_usage": {
            "stripe": 92,
            "adyen": 40,
            "paypal": 18
        },
        "timestamp": time.time(),
        "window_size_seconds": 3600,
        "total_events": 150
    }
    
    return sample_snapshot
