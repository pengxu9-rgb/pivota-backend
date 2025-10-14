"""
Test broadcast functionality
"""
import asyncio
import websockets
import json
import time

async def test_broadcast():
    uri = "wss://pivota-dashboard.onrender.com/api/ws/simple"
    
    try:
        print(f"Connecting to {uri}...")
        async with websockets.connect(uri) as websocket:
            print("✅ Connected to WebSocket!")
            
            # Send a test message to trigger a broadcast
            await websocket.send(json.dumps({"type": "test_broadcast"}))
            print("📤 Sent test message")
            
            # Wait for any messages
            for i in range(5):
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=3.0)
                    data = json.loads(message)
                    print(f"📥 Received: {data}")
                except asyncio.TimeoutError:
                    print("⏰ Timeout")
                    break
                    
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_broadcast())
