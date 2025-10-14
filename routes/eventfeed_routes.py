"""
EventFeed Routes
Simple REST API for EventFeed data
"""
import time
import random
from typing import List, Dict, Any
from fastapi import APIRouter
from realtime.metrics_store import record_event

router = APIRouter(prefix="/api/eventfeed", tags=["eventfeed"])

# In-memory event store for EventFeed
event_feed_events: List[Dict[str, Any]] = []

@router.get("/events")
async def get_eventfeed_events(limit: int = 20):
    """Get recent events for EventFeed"""
    # Return the most recent events
    recent_events = event_feed_events[-limit:] if len(event_feed_events) > limit else event_feed_events
    return {
        "events": recent_events,
        "total": len(event_feed_events),
        "timestamp": time.time()
    }

@router.post("/generate-events")
async def generate_eventfeed_events(count: int = 5):
    """Generate events specifically for EventFeed"""
    
    agents = [
        {"name": "TravelBot A"},
        {"name": "ShoppingBot B"},
        {"name": "FashionBot C"},
        {"name": "ElectronicsBot D"}
    ]
    
    merchants = [
        {"name": "Shopify Fashion Store"},
        {"name": "Wix Electronics Store"},
        {"name": "Shopify Travel Store"},
        {"name": "Wix Home Decor Store"}
    ]
    
    psps = ["STRIPE", "ADYEN", "PAYPAL", "SQUARE"]
    statuses = ["succeeded", "failed", "queued_for_retry"]
    
    generated_events = []
    
    for i in range(count):
        agent = random.choice(agents)
        merchant = random.choice(merchants)
        psp = random.choice(psps)
        status = random.choices(statuses, weights=[75, 20, 5])[0]
        
        # Create event in EventFeed format
        event = {
            "agent": agent["name"],
            "merchant": merchant["name"],
            "psp": psp,
            "status": status,
            "order_id": f"EVT_{int(time.time())}_{i}",
            "timestamp": time.strftime("%H:%M:%S"),
            "amount": round(random.uniform(15, 300), 2),
            "currency": random.choice(["EUR", "USD", "GBP"])
        }
        
        # Add to event feed
        event_feed_events.append(event)
        
        # Keep only last 100 events
        if len(event_feed_events) > 100:
            event_feed_events.pop(0)
        
        # Also record in metrics store
        metrics_event = {
            "type": "payment_result",
            "order_id": event["order_id"],
            "agent": "AGENT_001",  # Use consistent agent ID
            "agent_name": agent["name"],
            "merchant": "MERCH_001",  # Use consistent merchant ID
            "merchant_name": merchant["name"],
            "psp": psp.lower(),
            "status": status,
            "latency_ms": random.randint(120, 650),
            "attempt": random.randint(1, 3),
            "amount": event["amount"],
            "currency": event["currency"],
            "timestamp": time.time()
        }
        record_event(metrics_event)
        
        generated_events.append(event)
    
    return {
        "status": "success",
        "message": f"Generated {count} EventFeed events",
        "events": generated_events,
        "total_events": len(event_feed_events),
        "timestamp": time.time()
    }

@router.post("/clear-events")
async def clear_eventfeed_events():
    """Clear all EventFeed events"""
    global event_feed_events
    event_feed_events.clear()
    return {
        "status": "success",
        "message": "EventFeed events cleared",
        "timestamp": time.time()
    }
