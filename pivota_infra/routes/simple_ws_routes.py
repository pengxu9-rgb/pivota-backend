"""
Simple WebSocket Routes
Basic WebSocket without complex authentication
"""

import json
import time
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from realtime.metrics_store import snapshot

router = APIRouter(prefix="/api", tags=["simple-websocket"])

# Simple connection manager
class SimpleConnectionManager:
    def __init__(self):
        self.active_connections = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"✅ WebSocket connected. Total connections: {len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        print(f"❌ WebSocket disconnected. Total connections: {len(self.active_connections)}")
    
    async def send_json(self, websocket: WebSocket, data: dict):
        try:
            await websocket.send_text(json.dumps(data))
        except Exception as e:
            print(f"❌ Failed to send message: {e}")
    
    async def broadcast(self, data: dict):
        for connection in self.active_connections.copy():
            try:
                await self.send_json(connection, data)
            except:
                self.disconnect(connection)

# Global simple manager
simple_manager = SimpleConnectionManager()

@router.websocket("/ws/simple")
async def simple_websocket(websocket: WebSocket):
    """Simple WebSocket endpoint without authentication"""
    await simple_manager.connect(websocket)
    
    try:
        # Send initial snapshot
        initial_snapshot = snapshot()
        await simple_manager.send_json(websocket, {
            "type": "snapshot",
            "data": initial_snapshot,
            "timestamp": time.time()
        })
        
        # Keep connection alive
        while True:
            try:
                message = await websocket.receive_text()
                data = json.loads(message)
                
                if data.get("type") == "snapshot_request":
                    # Send fresh snapshot
                    current_snapshot = snapshot()
                    await simple_manager.send_json(websocket, {
                        "type": "snapshot",
                        "data": current_snapshot,
                        "timestamp": time.time()
                    })
                elif data.get("type") == "ping":
                    # Respond to ping
                    await simple_manager.send_json(websocket, {
                        "type": "pong",
                        "timestamp": time.time()
                    })
                    
            except json.JSONDecodeError:
                await simple_manager.send_json(websocket, {
                    "type": "error",
                    "message": "Invalid JSON format",
                    "timestamp": time.time()
                })
                
    except WebSocketDisconnect:
        simple_manager.disconnect(websocket)
    except Exception as e:
        print(f"❌ WebSocket error: {e}")
        simple_manager.disconnect(websocket)

@router.get("/ws/status")
async def websocket_status():
    """Get WebSocket connection status"""
    return {
        "active_connections": len(simple_manager.active_connections),
        "timestamp": time.time()
    }
