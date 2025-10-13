#!/usr/bin/env python3
"""
Simple WebSocket Test
Test WebSocket connection without authentication
"""

import asyncio
import websockets
import json

async def test_websocket_simple():
    """Test WebSocket connection"""
    try:
        print("🔌 Testing WebSocket connection...")
        
        # Try connecting without token
        uri = "ws://localhost:8000/ws/metrics"
        print(f"Connecting to: {uri}")
        
        async with websockets.connect(uri) as websocket:
            print("✅ WebSocket connected!")
            
            # Wait for initial message
            print("📥 Waiting for initial message...")
            message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            data = json.loads(message)
            print(f"📊 Received: {data.get('type', 'unknown')}")
            
            if data.get('type') == 'snapshot':
                snapshot = data.get('data', {})
                print(f"📈 Snapshot data: {snapshot.get('summary', {})}")
            
            # Send ping
            print("📤 Sending ping...")
            await websocket.send(json.dumps({"type": "ping"}))
            
            # Wait for pong
            print("📥 Waiting for pong...")
            response = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            response_data = json.loads(response)
            print(f"📊 Received: {response_data.get('type', 'unknown')}")
            
            print("✅ WebSocket test successful!")
            
    except asyncio.TimeoutError:
        print("❌ WebSocket timeout - no response received")
    except websockets.exceptions.ConnectionClosed as e:
        print(f"❌ WebSocket connection closed: {e}")
    except Exception as e:
        print(f"❌ WebSocket error: {e}")

if __name__ == "__main__":
    asyncio.run(test_websocket_simple())
