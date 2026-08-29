"""
Dashboard Routes
REST API and WebSocket endpoints for the Lovable dashboard
"""

import json
import logging
from typing import Optional, Dict, Any
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from realtime.metrics_store import get_metrics_store, snapshot
from realtime.ws_manager import get_connection_manager
from realtime.ws_guard import ws_admission
from utils.auth import verify_jwt_token, validate_entity_access, check_permission

logger = logging.getLogger("dashboard_routes")

router = APIRouter(prefix="/api", tags=["dashboard"])

@router.get("/snapshot")
async def get_snapshot(
    role: str = Query("admin", description="User role: admin, agent, merchant"),
    id: str = Query(None, description="Entity ID for filtered views"),
    token: Optional[str] = Query(None, description="JWT token for authentication")
) -> Dict[str, Any]:
    """Get current metrics snapshot with optional role-based filtering and JWT authentication"""
    try:
        # Authenticate if token provided
        user_info = None
        if token:
            try:
                user_info = verify_jwt_token(token)
                logger.info(f"Authenticated user: {user_info['sub']} with role {user_info['role']}")
                
                # Override role and id with authenticated user's info
                role = user_info["role"]
                if user_info.get("entity_id"):
                    id = user_info["entity_id"]
                    
            except HTTPException as e:
                logger.warning(f"Authentication failed: {e.detail}")
                raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        metrics_store = get_metrics_store()
        snapshot_data = snapshot(role=role, entity_id=id)
        
        logger.info(f"Generated snapshot for role={role}, id={id}")
        return snapshot_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to generate snapshot: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate snapshot: {str(e)}")

@router.get("/recent-events")
async def get_recent_events(
    limit: int = Query(100, description="Number of recent events to return")
) -> Dict[str, Any]:
    """Get recent events for live feed"""
    try:
        metrics_store = get_metrics_store()
        events = metrics_store.get_recent_events(limit=limit)
        
        return {
            "events": events,
            "count": len(events),
            "timestamp": time.time()
        }
        
    except Exception as e:
        logger.error(f"Failed to get recent events: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get recent events: {str(e)}")

@router.get("/connection-stats")
async def get_connection_stats() -> Dict[str, Any]:
    """Get WebSocket connection statistics"""
    try:
        manager = get_connection_manager()
        
        return {
            "total_connections": manager.get_connection_count(),
            "connections_by_role": manager.get_connections_by_role(),
            "timestamp": time.time()
        }
        
    except Exception as e:
        logger.error(f"Failed to get connection stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get connection stats: {str(e)}")

@router.post("/reset-metrics")
async def reset_metrics() -> Dict[str, Any]:
    """Reset all metrics (for testing purposes)"""
    try:
        metrics_store = get_metrics_store()
        metrics_store.reset_metrics()
        
        logger.info("Metrics reset requested")
        return {
            "status": "success",
            "message": "Metrics reset successfully",
            "timestamp": time.time()
        }
        
    except Exception as e:
        logger.error(f"Failed to reset metrics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to reset metrics: {str(e)}")

@router.websocket("/ws/metrics")
async def websocket_metrics(websocket: WebSocket, token: Optional[str] = Query(None)):
    """WebSocket endpoint for real-time metrics updates with JWT authentication.

    "with JWT authentication" overstates it: `ConnectionManager.connect` accepts
    the socket first and downgrades to an anonymous session when the token is
    missing OR fails to decode, so reaching this handler needs no credential.
    That is a separate question from this one and is left alone here; what
    matters for slot exhaustion is that an anonymous caller can hold a Cloud Run
    concurrency slot on this path exactly as it can on /api/ws/simple.

    So the ceiling below is the same object /api/ws/simple reserves from, not a
    second budget. A per-route ceiling would leave the attack intact — capping
    one path while the other stays open just moves it, and two routes capped at
    N each still add up to 2N held slots.

    No idle deadline here, and that asymmetry is deliberate rather than an
    oversight: unlike /api/ws/simple, this endpoint really is pushed to, so a
    client that never sends is a legitimate listener rather than a squatter and
    a deadline keyed on client messages would disconnect it. A deadline keyed on
    traffic in EITHER direction would instead be reset by the very broadcasts it
    was watching for. Either way the ceiling, not a deadline, bounds this path.

    The live push path, traced rather than assumed (an earlier version of this
    comment also cited main.py, which merely DEFINES a publish_event_to_ws
    wrapper that nothing calls):
    orchestrator/payment_orchestrator.py:149,173 -> utils/event_publisher.py ->
    realtime/ws_manager.publish_event_to_ws -> _manager.broadcast(), unfiltered,
    reachable via POST /api/payments/process.

    The cost of that asymmetry is in realtime/ws_guard's RESIDUAL EXPOSURE note:
    this is the route an attacker parks idle sockets on, because it is the one
    nothing reclaims them from.
    """
    manager = get_connection_manager()
    connection_id = None

    if not await ws_admission.reserve(websocket):
        return

    try:
        # Connect first, then authenticate
        connection_id = await manager.connect(websocket, token)
        if not connection_id:
            return  # Connection was rejected
        
        # Send initial snapshot
        initial_snapshot = snapshot()
        await manager.send_json(websocket, {
            "type": "snapshot",
            "data": initial_snapshot,
            "timestamp": time.time()
        })
        
        logger.info(f"WebSocket connection {connection_id} established and sent initial snapshot")
        
        # Keep connection alive and handle incoming messages
        while True:
            try:
                message = await websocket.receive_text()
                data = json.loads(message)
                
                if data.get("type") == "snapshot_request":
                    # Client requested a fresh snapshot
                    current_snapshot = snapshot()
                    await manager.send_json(websocket, {
                        "type": "snapshot",
                        "data": current_snapshot,
                        "timestamp": time.time()
                    })
                    
                elif data.get("type") == "ping":
                    # Respond to ping with pong
                    await manager.send_json(websocket, {
                        "type": "pong",
                        "timestamp": time.time()
                    })
                    
                else:
                    logger.warning(f"Unknown message type from WebSocket {connection_id}: {data.get('type')}")
                    
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON received from WebSocket {connection_id}")
                await manager.send_json(websocket, {
                    "type": "error",
                    "message": "Invalid JSON format",
                    "timestamp": time.time()
                })
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket connection {connection_id} disconnected")

    except Exception as e:
        logger.error(f"WebSocket error for connection {connection_id}: {e}")

    finally:
        # Cleanup moved out of the handlers above. asyncio.CancelledError is a
        # BaseException, so neither handler ran when the platform tore the
        # connection down at --timeout 300 or during a shutdown drain — leaking
        # an entry in ConnectionManager.active_connections every time, and (now
        # that a ceiling exists) a slot that would never be returned.
        manager.disconnect(websocket)
        ws_admission.release()

# Import time at the top level
import time
