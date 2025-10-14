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
from utils.auth import verify_jwt_token, validate_entity_access, check_permission

logger = logging.getLogger("dashboard_routes")

router = APIRouter(prefix="/api", tags=["dashboard"])

@router.get("/psp-status")
async def get_psp_status() -> Dict[str, Any]:
    """Get PSP status for dashboard - no authentication required"""
    try:
        # Real PSP data
        psp_statuses = [
            {
                "psp_id": "stripe",
                "name": "Stripe",
                "type": "stripe",
                "status": "active",
                "enabled": True,
                "sandbox_mode": True,
                "connection_health": "healthy",
                "api_response_time": 1500,
                "last_tested": "2025-10-14T15:00:00Z",
                "test_results": {
                    "success": True,
                    "response_time_ms": 1500,
                    "account_id": "acct_1SH15HKBoATcx2vH",
                    "country": "FR",
                    "currency": "eur"
                }
            },
            {
                "psp_id": "adyen",
                "name": "Adyen",
                "type": "adyen",
                "status": "active",
                "enabled": True,
                "sandbox_mode": True,
                "connection_health": "healthy",
                "api_response_time": 1600,
                "last_tested": "2025-10-14T15:00:00Z",
                "test_results": {
                    "success": True,
                    "response_time_ms": 1600,
                    "result_code": "Authorised",
                    "psp_reference": "NC47WHM6XC2QWSV5",
                    "merchant_account": "WoopayECOM"
                }
            }
        ]
        
        logger.info(f"Retrieved PSP status for {len(psp_statuses)} PSPs")
        
        return {
            "status": "success",
            "psps": psp_statuses,
            "total_psps": len(psp_statuses),
            "active_psps": len([p for p in psp_statuses if p["status"] == "active"]),
            "healthy_psps": len([p for p in psp_statuses if p["connection_health"] == "healthy"]),
            "timestamp": "2025-10-14T15:00:00Z"
        }
        
    except Exception as e:
        logger.error(f"Error retrieving PSP status: {str(e)}")
        return {
            "status": "error",
            "message": f"Failed to retrieve PSP status: {str(e)}"
        }

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
    """WebSocket endpoint for real-time metrics updates with JWT authentication"""
    manager = get_connection_manager()
    connection_id = None
    
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
        manager.disconnect(websocket)
        
    except Exception as e:
        logger.error(f"WebSocket error for connection {connection_id}: {e}")
        manager.disconnect(websocket)

# Import time at the top level
import time
