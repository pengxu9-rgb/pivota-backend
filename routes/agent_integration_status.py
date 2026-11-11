"""
[Phase 5.6] Agent Integration Status Routes
Aggregates EXISTING data for Agent Portal integration visualization
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Path

from db.database import database
from services.agent_integration_bridge import AgentIntegrationBridge
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user
from utils.tenant_scope import assert_agent_scope

router = APIRouter(
    prefix="/agents/{agent_id}/integration",
    tags=["[Phase 5.6] Agent Integration"]
)

# Initialize service
integration_bridge = AgentIntegrationBridge(database)


@router.get("/overview")
async def get_integration_overview(
    agent_id: str = Path(...),
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.6] Integration overview - aggregates existing routing/protocol/revenue logs"""
    
    # Allow: agent accessing own data, or admin/employee
    assert_agent_scope(current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    
    try:
        overview = await integration_bridge.get_integration_overview(agent_id)
        return overview
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/routing-trace")
async def get_routing_trace(
    agent_id: str = Path(...),
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user)
):
    """[Phase 5.6] Routing trace - uses EXISTING routing_logs table"""
    
    # Allow: agent accessing own data, or admin/employee
    assert_agent_scope(current_user, agent_id, staff_roles=EMPLOYEE_STAFF_ROLES)
    
    try:
        # Query existing routing_logs (Phase 4++)
        logs = await database.fetch_all(
            """
            SELECT * FROM routing_logs
            WHERE agent_id = :agent_id
            AND created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            {"agent_id": agent_id, "days": days, "limit": limit}
        )
        
        return {
            "agent_id": agent_id,
            "trace": [dict(log) for log in logs]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


print("[Phase 5.6] Agent integration status routes initialized - aggregates existing data")
