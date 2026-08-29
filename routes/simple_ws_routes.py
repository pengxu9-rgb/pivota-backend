"""
Simple WebSocket Routes
Basic WebSocket without complex authentication
"""

import json
import logging
import time
from typing import Optional
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from realtime.metrics_store import snapshot
from utils.dashboard_auth import (
    DashboardPrincipal,
    authenticate_websocket,
    require_dashboard_admin,
)
from realtime.ws_guard import (
    WS_CLOSE_IDLE_TIMEOUT,
    WebSocketIdleTimeout,
    idle_receive_text,
    idle_timeout_seconds,
    keepalive_seconds,
    ws_admission,
)

logger = logging.getLogger("simple_ws_routes")

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
async def simple_websocket(websocket: WebSocket, token: Optional[str] = Query(None)):
    """Metrics WebSocket, authenticated and scoped to the caller.

    Was "without authentication", and served `snapshot()` on its admin default,
    so any anonymous socket received per-PSP, per-agent and per-merchant volumes
    and latencies. It now resolves an identity through utils.dashboard_auth
    before the socket is accepted, like /api/ws/metrics, and every snapshot it
    sends is scoped to that identity.

    Authentication runs BEFORE the ceiling is touched, so an unauthenticated
    flood cannot spend the shared budget. `ws_admission` and the idle deadline
    now bound what an authenticated caller can hold, which is a much smaller
    threat than the anonymous one they used to be the only defence against.

    The idle deadline is safe to apply here specifically because this endpoint
    never pushes: `simple_manager.broadcast` has no callers, so a client that
    sends nothing also receives nothing after the initial snapshot and is
    holding a slot for no one's benefit. `/api/ws/metrics` is not in that
    position — see the note on it in routes/dashboard_routes.py.
    """
    principal = await authenticate_websocket(websocket, token)
    if principal is None:
        return

    if not await ws_admission.reserve(websocket):
        return

    try:
        await simple_manager.connect(websocket)

        # Send initial snapshot. Both halves of the liveness contract ride
        # along, because a client cannot honour a deadline it was never told
        # about — and telling it ONLY the deadline is worse than telling it
        # nothing: a client that pings exactly that often always arrives late
        # and is dropped every time. keepalive_seconds is the interval to send
        # on; idle_timeout_seconds is when we give up.
        initial_snapshot = snapshot(
            role=principal.role, entity_id=principal.entity_id
        )
        await simple_manager.send_json(websocket, {
            "type": "snapshot",
            "data": initial_snapshot,
            "keepalive_seconds": keepalive_seconds(),
            "idle_timeout_seconds": idle_timeout_seconds(),
            "timestamp": time.time()
        })
        
        # Keep connection alive
        while True:
            try:
                message = await idle_receive_text(websocket)
                data = json.loads(message)
                
                if data.get("type") == "snapshot_request":
                    # Send fresh snapshot
                    current_snapshot = snapshot(
                        role=principal.role, entity_id=principal.entity_id
                    )
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
                
    except WebSocketIdleTimeout:
        # Reclaim the slot ourselves rather than letting the platform hold it
        # until --timeout 300. Told, then closed: a silent drop is
        # indistinguishable from a network fault and clients reconnect into it.
        # The close is guarded because a socket can be silent precisely because
        # the peer is already gone, and an exception raised on the way out would
        # be reported as a handler error rather than the routine reclaim it is.
        await simple_manager.send_json(websocket, {
            "type": "error",
            "message": "Idle timeout",
            "timestamp": time.time()
        })
        try:
            await websocket.close(code=WS_CLOSE_IDLE_TIMEOUT)
        except Exception as e:
            # logger, not print: this is the one new failure path an operator
            # would need to search for, and stdout carries no severity.
            logger.warning("Failed to close idle WebSocket: %s", e)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"❌ WebSocket error: {e}")
    finally:
        # Both cleanups belong in `finally`, not in the handlers above.
        # asyncio.CancelledError is a BaseException, so `except Exception` never
        # ran when the platform tore the connection down at the request timeout
        # — leaking an entry in active_connections on every such teardown, and
        # (once the ceiling exists) a slot that never comes back.
        simple_manager.disconnect(websocket)
        ws_admission.release()

@router.get("/ws/status")
async def websocket_status(
    principal: DashboardPrincipal = Depends(require_dashboard_admin),
):
    """WebSocket connection status. Admin only.

    Had no authentication. A bare count leaks little on its own, but it is the
    readout an attacker uses to watch its own sockets accumulate against the
    ws_guard ceiling, and its sibling /api/connection-stats is admin-only for
    the same reason.
    """
    return {
        "active_connections": len(simple_manager.active_connections),
        "timestamp": time.time()
    }
