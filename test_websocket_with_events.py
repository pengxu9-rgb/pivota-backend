"""
Test WebSocket with live events
"""
import asyncio
import websockets
import json
import time
import aiohttp

async def test_websocket_with_events():
    uri = "wss://pivota-dashboard.onrender.com/api/ws/simple"
    
    try:
        print(f"Connecting to {uri}...")
        async with websockets.connect(uri) as websocket:
            print("✅ Connected to WebSocket!")
            
            # Send a ping message
            await websocket.send(json.dumps({"type": "ping"}))
            print("📤 Sent ping message")
            
            # Wait for initial messages
            for i in range(3):
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=3.0)
                    data = json.loads(message)
                    print(f"📥 Received: {data.get('type', 'unknown')}")
                except asyncio.TimeoutError:
                    break
            
            print("\n🎯 Now generating live events...")
            
            # Generate live events while WebSocket is connected
            async with aiohttp.ClientSession() as session:
                async with session.post("https://pivota-dashboard.onrender.com/api/test/generate-live-events") as response:
                    result = await response.json()
                    print(f"📤 Generated events: {result}")
            
            # Wait for event messages
            print("⏳ Waiting for event messages...")
            for i in range(10):
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                    data = json.loads(message)
                    print(f"📥 Event: {data}")
                except asyncio.TimeoutError:
                    print("⏰ No more messages")
                    break
                    
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_websocket_with_events())
