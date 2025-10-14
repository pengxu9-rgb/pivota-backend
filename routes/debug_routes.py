"""
Debug Routes for EventFeed Testing
"""
import asyncio
import time
import random
from fastapi import APIRouter
from realtime.metrics_store import record_event

router = APIRouter(prefix="/api/debug", tags=["debug"])

@router.post("/test-websocket-event")
async def test_websocket_event():
    """Test WebSocket event publishing directly"""
    
    # Create a test event
    event = {
        "type": "payment_result",
        "order_id": f"DEBUG_{int(time.time())}",
        "agent": "DebugBot",
        "agent_name": "Debug Bot",
        "merchant": "DEBUG_MERCH",
        "merchant_name": "Debug Merchant",
        "psp": "STRIPE",
        "status": "succeeded",
        "latency_ms": 250,
        "attempt": 1,
        "amount": 99.99,
        "currency": "USD",
        "timestamp": time.time()
    }
    
    # Record the event
    record_event(event)
    
    # Try to publish to WebSocket
    try:
        from main import publish_event_to_ws
        await publish_event_to_ws(event)
        return {
            "status": "success",
            "message": "WebSocket event published",
            "event": event,
            "timestamp": time.time()
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to publish WebSocket event: {e}",
            "event": event,
            "timestamp": time.time()
        }

@router.get("/websocket-status")
async def websocket_status():
    """Get WebSocket connection status"""
    try:
        from routes.simple_ws_routes import simple_manager
        from realtime.ws_manager import get_connection_manager
        
        simple_connections = len(simple_manager.active_connections)
        auth_manager = get_connection_manager()
        auth_connections = len(auth_manager.active_connections)
        
        return {
            "simple_websocket_connections": simple_connections,
            "authenticated_websocket_connections": auth_connections,
            "total_connections": simple_connections + auth_connections,
            "timestamp": time.time()
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to get WebSocket status: {e}",
            "timestamp": time.time()
        }
