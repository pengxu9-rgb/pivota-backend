"""
Authenticated WebSocket Routes
Simple JWT-authenticated WebSocket endpoints
"""

import json
import time
import logging
from typing import Optional, Dict, Any
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException
from realtime.metrics_store import snapshot
from pivota_infra.utils.auth import verify_jwt_token

logger = logging.getLogger("auth_ws_routes")

router = APIRouter(prefix="/api", tags=["authenticated-websocket"])

# Simple connection manager for authenticated WebSockets
class AuthConnectionManager:
    def __init__(self):
        self.active_connections = []
        self.connection_metadata = {}
    
    async def connect(self, websocket: WebSocket, user_info: Dict[str, Any]):
        await websocket.accept()
        connection_id = f"auth_conn_{int(time.time() * 1000)}"
        
        self.active_connections.append(websocket)
        self.connection_metadata[connection_id] = {
            "websocket": websocket,
            "user_info": user_info,
            "connected_at": time.time()
        }
        
        logger.info(f"Authenticated WebSocket connection {connection_id} established for user {user_info.get('user_id')} with role {user_info.get('role')}")
        return connection_id
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"Authenticated WebSocket disconnected. Total connections: {len(self.active_connections)}")
    
    async def send_json(self, websocket: WebSocket, data: Dict[str, Any]):
        try:
            await websocket.send_text(json.dumps(data))
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            self.disconnect(websocket)
    
    async def broadcast(self, data: Dict[str, Any]):
        for connection in self.active_connections.copy():
            try:
                await self.send_json(connection, data)
            except:
                self.disconnect(connection)

# Global auth connection manager
auth_manager = AuthConnectionManager()

@router.websocket("/ws/auth")
async def authenticated_websocket(websocket: WebSocket, token: Optional[str] = Query(None)):
    """Authenticated WebSocket endpoint with JWT validation"""
    connection_id = None
    
    try:
        # Validate JWT token
        user_info = None
        if token:
            try:
                payload = verify_jwt_token(token)
                user_info = {
                    "user_id": payload["sub"],
                    "role": payload["role"],
                    "entity_id": payload.get("entity_id")
                }
                logger.info(f"Authenticated user: {user_info['user_id']} with role {user_info['role']}")
            except HTTPException as e:
                logger.warning(f"Authentication failed: {e.detail}")
                await websocket.close(code=1008, reason="Invalid token")
                return
        else:
            # Allow anonymous connections for development
            user_info = {
                "user_id": "anonymous",
                "role": "viewer",
                "entity_id": None
            }
            logger.info("Anonymous WebSocket connection established")
        
        # Connect with user info
        connection_id = await auth_manager.connect(websocket, user_info)
        
        # Send initial snapshot
        initial_snapshot = snapshot()
        await auth_manager.send_json(websocket, {
            "type": "snapshot",
            "data": initial_snapshot,
            "timestamp": time.time(),
            "user_info": user_info
        })
        
        logger.info(f"Authenticated WebSocket {connection_id} sent initial snapshot")
        
        # Keep connection alive and handle messages
        while True:
            try:
                message = await websocket.receive_text()
                data = json.loads(message)
                
                if data.get("type") == "snapshot_request":
                    # Send fresh snapshot
                    current_snapshot = snapshot()
                    await auth_manager.send_json(websocket, {
                        "type": "snapshot",
                        "data": current_snapshot,
                        "timestamp": time.time(),
                        "user_info": user_info
                    })
                elif data.get("type") == "ping":
                    # Respond to ping
                    await auth_manager.send_json(websocket, {
                        "type": "pong",
                        "timestamp": time.time(),
                        "user_info": user_info
                    })
                elif data.get("type") == "get_user_info":
                    # Send user info
                    await auth_manager.send_json(websocket, {
                        "type": "user_info",
                        "data": user_info,
                        "timestamp": time.time()
                    })
                    
            except json.JSONDecodeError:
                await auth_manager.send_json(websocket, {
                    "type": "error",
                    "message": "Invalid JSON format",
                    "timestamp": time.time()
                })
                
    except WebSocketDisconnect:
        auth_manager.disconnect(websocket)
        logger.info(f"Authenticated WebSocket {connection_id} disconnected")
    except Exception as e:
        logger.error(f"Authenticated WebSocket error: {e}")
        auth_manager.disconnect(websocket)

@router.get("/ws/auth/status")
async def auth_websocket_status():
    """Get authenticated WebSocket connection status"""
    return {
        "active_connections": len(auth_manager.active_connections),
        "timestamp": time.time()
    }
