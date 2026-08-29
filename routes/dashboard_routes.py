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
from utils.dashboard_auth import (
    DashboardPrincipal,
    authenticate_websocket,
    require_dashboard_admin,
    require_dashboard_principal,
)

logger = logging.getLogger("dashboard_routes")

router = APIRouter(prefix="/api", tags=["dashboard"])

@router.get("/snapshot")
async def get_snapshot(
    role: Optional[str] = Query(None, description="Narrow the view (admins only)"),
    id: Optional[str] = Query(None, description="Entity ID (admins only)"),
    principal: DashboardPrincipal = Depends(require_dashboard_principal),
) -> Dict[str, Any]:
    """Current metrics snapshot, scoped to the authenticated caller.

    Authentication used to run ONLY when a token happened to be supplied. With
    none, `role` came straight off the query string and defaulted to "admin", so
    an anonymous caller named its own authority and received the whole
    platform's figures. Scope now comes from the credential.

    `role`/`id` survive as a NARROWING filter for admins — the operator use case
    of inspecting one merchant's slice is real — and are ignored for everybody
    else, whose scope is already fixed by their token.
    """
    if id and not role:
        # `?id=` alone did nothing at all: effective_role fell back to the
        # admin's own role, which is platform-wide, so entity_id was ignored and
        # the caller got the WHOLE platform — the exact opposite of the
        # narrowing this parameter advertises, silently.
        raise HTTPException(
            status_code=400,
            detail="id requires role (e.g. role=merchant&id=<merchant_id>)",
        )

    if principal.is_admin and (role or id):
        effective_role, effective_id = (role or principal.role), id
    else:
        if role or id:
            logger.info(
                "ignoring caller-supplied scope for %s (role=%s)",
                principal.sub, principal.role,
            )
        effective_role, effective_id = principal.role, principal.entity_id

    try:
        return snapshot(role=effective_role, entity_id=effective_id)
    except Exception as e:
        logger.error(f"Failed to generate snapshot: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate snapshot")

@router.get("/recent-events")
async def get_recent_events(
    limit: int = Query(100, description="Number of recent events to return"),
    principal: DashboardPrincipal = Depends(require_dashboard_admin),
) -> Dict[str, Any]:
    """Get recent events for live feed. Admin only.

    Had no authentication at all. Raw events carry agent, merchant and PSP
    identifiers with no role filtering anywhere in the path — there is no
    scoped version of this to hand a merchant — so it is admin-only rather than
    principal-scoped.
    """
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
async def get_connection_stats(
    principal: DashboardPrincipal = Depends(require_dashboard_admin),
) -> Dict[str, Any]:
    """WebSocket connection statistics. Admin only.

    Had no authentication. Also the readout an attacker would use to watch its
    own progress against the ws_guard ceiling, which is why it is not merely
    authenticated but restricted to admins.
    """
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
async def reset_metrics(
    principal: DashboardPrincipal = Depends(require_dashboard_admin),
) -> Dict[str, Any]:
    """Reset all metrics. Admin only.

    The worst of the set: an unauthenticated MUTATION. Any anonymous caller
    could wipe the store, which is both destructive and an easy way to erase
    evidence of whatever else they had been doing.
    """
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
    """Real-time metrics for an authenticated caller.

    The docstring here used to say "with JWT authentication" and that was false
    in both directions: ConnectionManager accepted the socket first and
    downgraded a missing OR undecodable token to an anonymous viewer, and it
    checked signatures against a hardcoded "your-secret-key" that no token this
    system issues is signed with — so the token parameter had never once
    authenticated anybody. Both halves are gone; utils.dashboard_auth resolves
    one identity for this surface.

    Authentication runs BEFORE the slot is reserved, and the ordering is the
    fix rather than a detail. It closes the residual recorded in
    realtime/ws_guard: while anonymous sockets could reserve, eight of them
    parked here — the route with no idle deadline — held the shared ceiling and
    refused the dashboard to everyone. An unauthenticated flood now never
    reaches the ceiling, so the budget is spendable only by credential holders.

    Still no idle deadline — but the reason given for that in the previous two
    commits was WRONG and is corrected here rather than quietly dropped. Those
    said this endpoint "really is pushed to" via payment_orchestrator ->
    utils/event_publisher -> ws_manager.publish_event_to_ws. The wiring exists;
    the calls do not work. orchestrator/payment_orchestrator.py:149 passes
    payment_id/success/fees/transaction_id to publish_payment_result, which
    accepts none of them and requires `status` besides, so it raises TypeError
    into the `except Exception` at :189 and is swallowed. Same at :173 for
    publish_order_event. So publish_event_to_ws has NO working caller today and
    broadcast_snapshot has none at all: nothing is pushed, and a silent client
    here is currently a squatter rather than a listener.

    The deadline stays off anyway, deliberately. Whoever repairs those call sites
    restores real pushes, and a deadline added now on the strength of the path
    being broken would become a bug the moment it is fixed. The ceiling bounds
    this route, and sockets must now authenticate to reach it at all, so what a
    squatter can hold is small and credentialled. Repairing the orchestrator is
    NOT done here: it would switch on a dormant production path inside a
    security change.

    When those pushes do resume they are built per connection via
    broadcast_scoped, and the raw event goes only to platform-wide roles — a
    single unscoped payload fanned out to everyone would have made
    authenticating the handshake pointless for exactly the data it protects.
    """
    principal = await authenticate_websocket(websocket, token)
    if principal is None:
        return

    manager = get_connection_manager()
    connection_id = None

    if not await ws_admission.reserve(websocket):
        return

    try:
        connection_id = await manager.connect(websocket, principal)

        # Send initial snapshot, scoped to this caller
        initial_snapshot = snapshot(
            role=principal.role, entity_id=principal.entity_id
        )
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
                    current_snapshot = snapshot(
                        role=principal.role, entity_id=principal.entity_id
                    )
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
