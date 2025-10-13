"""
WebSocket Connection Manager
Handles WebSocket connections, broadcasting, and authentication
"""

import json
import logging
from typing import Dict, List, Any, Optional
from fastapi import WebSocket, WebSocketDisconnect
import jwt
import time

logger = logging.getLogger("ws_manager")

class ConnectionManager:
    """Manages WebSocket connections with authentication and broadcasting"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.connection_metadata: Dict[str, Dict[str, Any]] = {}
        self.jwt_secret = "your-secret-key"  # In production, use environment variable
        
    async def connect(self, websocket: WebSocket, token: Optional[str] = None) -> str:
        """Accept a WebSocket connection and optionally validate JWT token"""
        await websocket.accept()
        
        connection_id = f"conn_{int(time.time() * 1000)}"
        
        # Always allow connection, validate token if provided
        user_info = {
            "user_id": "anonymous",
            "role": "viewer",
            "entity_id": None
        }
        
        if token:
            try:
                payload = jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
                user_info = {
                    "user_id": payload.get("sub"),
                    "role": payload.get("role", "viewer"),
                    "entity_id": payload.get("entity_id")
                }
                logger.info(f"Authenticated WebSocket connection for user {user_info['user_id']} with role {user_info['role']}")
            except jwt.InvalidTokenError:
                logger.warning(f"Invalid JWT token for WebSocket connection, using anonymous")
                # Don't close connection, just use anonymous
            except Exception as e:
                logger.warning(f"JWT validation error: {e}, using anonymous")
        else:
            logger.info(f"Anonymous WebSocket connection established")
        
        self.active_connections[connection_id] = websocket
        self.connection_metadata[connection_id] = {
            "connected_at": time.time(),
            "user_info": user_info
        }
        
        logger.info(f"WebSocket connection {connection_id} established")
        return connection_id
    
    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection"""
        connection_id = None
        for cid, ws in self.active_connections.items():
            if ws == websocket:
                connection_id = cid
                break
        
        if connection_id:
            del self.active_connections[connection_id]
            del self.connection_metadata[connection_id]
            logger.info(f"WebSocket connection {connection_id} disconnected")
    
    async def send_json(self, websocket: WebSocket, data: Dict[str, Any]) -> None:
        """Send JSON data to a specific WebSocket"""
        try:
            await websocket.send_text(json.dumps(data))
        except Exception as e:
            logger.error(f"Failed to send JSON to WebSocket: {e}")
    
    async def broadcast(self, data: Dict[str, Any], role_filter: Optional[str] = None, entity_filter: Optional[str] = None) -> None:
        """Broadcast data to all connected clients, optionally filtered by role/entity"""
        disconnected_connections = []
        
        for connection_id, websocket in self.active_connections.items():
            try:
                metadata = self.connection_metadata.get(connection_id, {})
                user_info = metadata.get("user_info", {})
                
                # Apply filters
                if role_filter and user_info.get("role") != role_filter:
                    continue
                
                if entity_filter and user_info.get("entity_id") != entity_filter:
                    continue
                
                await self.send_json(websocket, data)
                
            except Exception as e:
                logger.error(f"Failed to broadcast to connection {connection_id}: {e}")
                disconnected_connections.append(connection_id)
        
        # Clean up disconnected connections
        for connection_id in disconnected_connections:
            if connection_id in self.active_connections:
                del self.active_connections[connection_id]
            if connection_id in self.connection_metadata:
                del self.connection_metadata[connection_id]
    
    async def broadcast_to_role(self, data: Dict[str, Any], role: str) -> None:
        """Broadcast data to all connections with a specific role"""
        await self.broadcast(data, role_filter=role)
    
    async def broadcast_to_entity(self, data: Dict[str, Any], entity_id: str) -> None:
        """Broadcast data to all connections for a specific entity"""
        await self.broadcast(data, entity_filter=entity_id)
    
    def get_connection_count(self) -> int:
        """Get the number of active connections"""
        return len(self.active_connections)
    
    def get_connections_by_role(self) -> Dict[str, int]:
        """Get connection count by role"""
        role_counts = {}
        for metadata in self.connection_metadata.values():
            role = metadata.get("user_info", {}).get("role", "unknown")
            role_counts[role] = role_counts.get(role, 0) + 1
        return role_counts

# Global connection manager
_manager = ConnectionManager()

def get_connection_manager() -> ConnectionManager:
    """Get the global connection manager"""
    return _manager

async def publish_event_to_ws(event: Dict[str, Any]) -> None:
    """Publish an event to WebSocket clients"""
    from .metrics_store import record_event, snapshot
    
    # Record the event in metrics store
    record_event(event)
    
    # Generate updated snapshot
    current_snapshot = snapshot()
    
    # Broadcast the event
    event_data = {
        "type": "event",
        "event": event,
        "snapshot": current_snapshot,
        "timestamp": time.time()
    }
    
    # Send to all connected clients
    await _manager.broadcast(event_data)
    
    logger.debug(f"Published event to WebSocket clients: {event.get('type', 'unknown')}")

async def broadcast_snapshot() -> None:
    """Broadcast current snapshot to all connected clients"""
    from .metrics_store import snapshot
    
    snapshot_data = snapshot()
    data = {
        "type": "snapshot",
        "data": snapshot_data,
        "timestamp": time.time()
    }
    
    await _manager.broadcast(data)
    logger.debug("Broadcasted snapshot to all WebSocket clients")
