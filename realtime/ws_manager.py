"""
WebSocket Connection Manager
Handles WebSocket connections, broadcasting, and authentication
"""

import json
import logging
from typing import Callable, Dict, List, Any, Optional

from realtime.metrics_store import PLATFORM_WIDE_ROLES
from fastapi import WebSocket, WebSocketDisconnect
import time

logger = logging.getLogger("ws_manager")

class ConnectionManager:
    """Manages WebSocket connections with authentication and broadcasting"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.connection_metadata: Dict[str, Dict[str, Any]] = {}
        self._sequence = 0

    async def connect(self, websocket: WebSocket, principal) -> str:
        """Accept an ALREADY-AUTHENTICATED connection and register it.

        This used to do its own JWT check, and that check had never once
        authenticated anybody: it verified against a hardcoded
        `"your-secret-key"` while every token this system issues is signed with
        `utils.auth.JWT_SECRET`. Real tokens therefore always failed to decode
        and were silently downgraded to an anonymous `viewer`, while a token
        forged with the literal from the source decoded fine. Authentication now
        happens in utils.dashboard_auth BEFORE the socket is accepted, and this
        method takes the resolved principal — it cannot fall back to anonymous
        because there is no longer a fallback to reach.
        """
        await websocket.accept()

        # Monotonic counter, not a millisecond timestamp. Two sockets opened in
        # the same millisecond used to collide on the same id, and the second
        # silently evicted the first from active_connections — it stopped
        # receiving broadcasts and its disconnect() became a no-op, leaking the
        # entry. Mass reconnects are exactly when that happens.
        self._sequence += 1
        connection_id = f"conn_{self._sequence}"

        self.active_connections[connection_id] = websocket
        self.connection_metadata[connection_id] = {
            "connected_at": time.time(),
            "principal": principal,
            "user_info": {
                "user_id": principal.sub,
                "role": principal.role,
                "entity_id": principal.entity_id,
            },
        }

        logger.info(
            "WebSocket connection %s established for %s (role=%s)",
            connection_id, principal.sub, principal.role,
        )
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
        """Broadcast ONE payload to all connected clients.

        `role_filter` / `entity_filter` choose RECIPIENTS; they do not scope the
        payload. Only use this for data that every recipient may see. To push
        anything derived from the metrics store, use `broadcast_scoped`, which
        builds a separate payload per connection.
        """
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
    
    async def broadcast_scoped(
        self, build: Callable[[Dict[str, Any]], Dict[str, Any]]
    ) -> None:
        """Push a payload built SEPARATELY for each connection's principal.

        Recipient filtering is not payload scoping, and conflating the two is
        how authenticating the handshake would have bought nothing: the pushed
        snapshot was generated once, unscoped, and sent to everyone, so a
        merchant-scoped socket received the whole platform's figures anyway.
        `build` receives that connection's user_info and returns what that
        connection alone may see.
        """
        disconnected_connections = []

        for connection_id, websocket in list(self.active_connections.items()):
            try:
                metadata = self.connection_metadata.get(connection_id, {})
                await self.send_json(websocket, build(metadata.get("user_info", {})))
            except Exception as e:
                logger.error(f"Failed to broadcast to connection {connection_id}: {e}")
                disconnected_connections.append(connection_id)

        for connection_id in disconnected_connections:
            self.connection_metadata.pop(connection_id, None)
            self.active_connections.pop(connection_id, None)

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
    """Publish an event to WebSocket clients, scoped per recipient."""
    from .metrics_store import record_event, snapshot

    # Record the event in metrics store
    record_event(event)

    now = time.time()

    def _for(user_info: Dict[str, Any]) -> Dict[str, Any]:
        role = user_info.get("role", "")
        payload = {
            "type": "event",
            "snapshot": snapshot(role=role, entity_id=user_info.get("entity_id")),
            "timestamp": now,
        }
        # The raw event is PLATFORM-WIDE ONLY. Scoping the snapshot while
        # emitting `event` verbatim to everyone left the actual leak intact: a
        # merchant-scoped socket received another tenant's order id, amount,
        # transaction id and customer email in full. There is no scoped version
        # of a raw event — that is exactly why /api/recent-events is admin-only
        # (routes/dashboard_routes.py) — so a scoped caller gets the effect of
        # the event on its OWN figures, via the snapshot, and not the payload.
        if role in PLATFORM_WIDE_ROLES:
            payload["event"] = event
        return payload

    await _manager.broadcast_scoped(_for)

    logger.debug(f"Published event to WebSocket clients: {event.get('type', 'unknown')}")

async def broadcast_snapshot() -> None:
    """Broadcast the current snapshot, scoped per recipient."""
    from .metrics_store import snapshot

    now = time.time()

    def _for(user_info: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "snapshot",
            "data": snapshot(
                role=user_info.get("role", ""),
                entity_id=user_info.get("entity_id"),
            ),
            "timestamp": now,
        }

    await _manager.broadcast_scoped(_for)
    logger.debug("Broadcasted snapshot to all WebSocket clients")
