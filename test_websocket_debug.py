"""
Debug WebSocket Connection
"""
import asyncio
import websockets
import json
import time

async def test_websocket():
    uri = "wss://pivota-dashboard.onrender.com/api/ws/simple"
    
    try:
        print(f"Connecting to {uri}...")
        async with websockets.connect(uri) as websocket:
            print("✅ Connected to WebSocket!")
            
            # Send a ping message
            await websocket.send(json.dumps({"type": "ping"}))
            print("📤 Sent ping message")
            
            # Wait for responses
            for i in range(5):
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                    data = json.loads(message)
                    print(f"📥 Received: {data}")
                except asyncio.TimeoutError:
                    print("⏰ Timeout waiting for message")
                    break
                    
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_websocket())
