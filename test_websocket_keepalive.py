"""
Test WebSocket with keep-alive
"""
import asyncio
import websockets
import json
import time
import aiohttp

async def test_websocket_keepalive():
    uri = "wss://pivota-dashboard.onrender.com/api/ws/simple"
    
    try:
        print(f"Connecting to {uri}...")
        async with websockets.connect(uri) as websocket:
            print("✅ Connected to WebSocket!")
            
            # Send a ping to establish connection
            await websocket.send(json.dumps({"type": "ping"}))
            print("📤 Sent ping message")
            
            # Wait for initial messages
            for i in range(3):
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                    data = json.loads(message)
                    print(f"📥 Received: {data.get('type', 'unknown')}")
                except asyncio.TimeoutError:
                    break
            
            print("\n🎯 Generating events in background...")
            
            # Generate events in background
            async def generate_events():
                await asyncio.sleep(1)  # Wait a bit
                async with aiohttp.ClientSession() as session:
                    for i in range(3):
                        async with session.post("https://pivota-dashboard.onrender.com/api/test/generate-live-events") as response:
                            result = await response.json()
                            print(f"📤 Generated events batch {i+1}: {result}")
                        await asyncio.sleep(1)
            
            # Start event generation task
            event_task = asyncio.create_task(generate_events())
            
            # Listen for messages
            print("⏳ Listening for messages...")
            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    data = json.loads(message)
                    print(f"📥 Received: {data}")
                except asyncio.TimeoutError:
                    print("⏰ Timeout waiting for messages")
                    break
                except websockets.exceptions.ConnectionClosed:
                    print("❌ WebSocket connection closed")
                    break
            
            # Cancel the event generation task
            event_task.cancel()
                    
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_websocket_keepalive())
