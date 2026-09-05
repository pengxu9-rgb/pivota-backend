"""
Protocol Management API Routes - Phase 4
Endpoints for AP2, ACP, X-402 protocol support
"""
from fastapi import APIRouter, HTTPException, Depends, Query, Path
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import json

from db.database import database
from services.protocol_adapter_service import ProtocolAdapterService
from utils.auth import ADMIN_ROLES, get_current_user, get_current_employee

# Enabling/disabling a protocol for an agent changes what that agent can
# transact with, so this stays admin-only; ADMIN_ROLES only adds the
# super_admin that `!= "admin"` had excluded.


router = APIRouter(prefix="/protocols", tags=["Protocol Management"])


# Request/Response Models
class ProtocolTestRequest(BaseModel):
    protocol: str = Field(..., description="Protocol name: AP2, ACP, or X-402")
    test_payload: Dict[str, Any] = Field(..., description="Test payload for protocol validation")


class ProtocolTestResponse(BaseModel):
    success: bool
    protocol: str
    validation_result: Optional[str] = None
    transformed_request: Optional[Dict[str, Any]] = None
    mock_response: Optional[Dict[str, Any]] = None
    endpoints: Optional[Dict[str, str]] = None
    error: Optional[str] = None


class ProtocolValidationRequest(BaseModel):
    payload: Dict[str, Any] = Field(..., description="Payload to validate")


class ProtocolValidationResponse(BaseModel):
    valid: bool
    protocol: str
    version: str
    errors: Optional[List[str]] = None
    warnings: Optional[List[str]] = None


class ProtocolEventResponse(BaseModel):
    event_id: str
    agent_id: str
    protocol_name: str
    event_type: str
    status: str
    response_time_ms: Optional[int]
    created_at: str


class ProtocolDefinitionResponse(BaseModel):
    protocol_name: str
    version: str
    specification: Dict[str, Any]
    endpoints: Dict[str, str]
    required_fields: List[str]
    status: str
    created_at: str


# Initialize service
protocol_service = ProtocolAdapterService(database)


@router.get("/", response_model=List[ProtocolDefinitionResponse])
async def list_available_protocols(
    include_deprecated: bool = Query(False, description="Include deprecated protocols")
):
    """
    List all available protocols and their specifications
    """
    query = """
        SELECT 
            protocol_name, version, specification, endpoints,
            required_fields, status, created_at
        FROM protocol_definitions
    """
    
    if not include_deprecated:
        query += " WHERE status != 'deprecated'"
    
    query += " ORDER BY protocol_name, version DESC"
    
    protocols = await database.fetch_all(query)
    
    return [
        ProtocolDefinitionResponse(
            protocol_name=p["protocol_name"],
            version=p["version"],
            specification=json.loads(p["specification"]) if isinstance(p["specification"], str) else p["specification"],
            endpoints=json.loads(p["endpoints"]) if isinstance(p["endpoints"], str) else p["endpoints"],
            required_fields=json.loads(p["required_fields"]) if isinstance(p["required_fields"], str) else p["required_fields"],
            status=p["status"],
            created_at=p["created_at"].isoformat()
        )
        for p in protocols
    ]


@router.get("/agents/{agent_id}/protocols/{protocol_name}/events", response_model=List[ProtocolEventResponse])
async def get_protocol_events(
    agent_id: str = Path(..., description="Agent ID"),
    protocol_name: str = Path(..., description="Protocol name"),
    hours: int = Query(24, description="Hours to look back"),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get protocol events for an agent
    """
    # Verify agent access
    current_role = current_user.get("role")
    current_agent_id = current_user.get("agent_id") or current_user.get("user_id")

    if current_role not in ("agent", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    if current_role == "agent" and current_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    
    query = """
        SELECT 
            event_id, agent_id, protocol_name, event_type,
            status, response_time_ms, created_at
        FROM protocol_events
        WHERE agent_id = :agent_id
        AND protocol_name = :protocol_name
        AND created_at >= :cutoff
    """
    
    params = {
        "agent_id": agent_id,
        "protocol_name": protocol_name,
        "cutoff": cutoff
    }
    
    if event_type:
        query += " AND event_type = :event_type"
        params["event_type"] = event_type
    
    query += " ORDER BY created_at DESC LIMIT 100"
    
    events = await database.fetch_all(query, params)
    
    return [
        ProtocolEventResponse(
            event_id=e["event_id"],
            agent_id=e["agent_id"],
            protocol_name=e["protocol_name"],
            event_type=e["event_type"],
            status=e["status"],
            response_time_ms=e["response_time_ms"],
            created_at=e["created_at"].isoformat()
        )
        for e in events
    ]


@router.post("/{protocol_name}/validate", response_model=ProtocolValidationResponse)
async def validate_protocol_payload(
    protocol_name: str = Path(..., description="Protocol name: AP2, ACP, or X-402"),
    request: ProtocolValidationRequest = ...,
    version: Optional[str] = Query(None, description="Protocol version (uses latest if not specified)")
):
    """
    Validate a payload against protocol specification
    """
    # Get protocol definition
    query = """
        SELECT version, required_fields, validation_rules
        FROM protocol_definitions
        WHERE protocol_name = :protocol_name
        AND status != 'deprecated'
    """
    
    params = {"protocol_name": protocol_name}
    
    if version:
        query += " AND version = :version"
        params["version"] = version
    else:
        query += " ORDER BY version DESC LIMIT 1"
    
    protocol_def = await database.fetch_one(query, params)
    
    if not protocol_def:
        raise HTTPException(status_code=404, detail=f"Protocol {protocol_name} not found")
    
    # Validate using service
    is_valid, error = await protocol_service.validate_request(protocol_name, request.payload)
    
    errors = [error] if error else None
    warnings = []
    
    # Check for optional enhancements
    if protocol_name == "AP2" and "customer" not in request.payload:
        warnings.append("Customer information is recommended but not required")
    
    if protocol_name == "ACP" and "shipping" not in request.payload:
        warnings.append("Shipping information is recommended for commerce orders")
    
    return ProtocolValidationResponse(
        valid=is_valid,
        protocol=protocol_name,
        version=protocol_def["version"],
        errors=errors,
        warnings=warnings if warnings else None
    )


# Agent-specific protocol management
agent_router = APIRouter(prefix="/agents/{agent_id}/protocols", tags=["Agent Protocols"])


@agent_router.get("/", response_model=List[Dict[str, Any]])
async def get_agent_protocols(
    agent_id: str = Path(..., description="Agent ID"),
    current_user: dict = Depends(get_current_user)
):
    """
    Get protocols enabled for an agent
    """
    # Verify agent access
    current_role = current_user.get("role")
    current_agent_id = current_user.get("agent_id") or current_user.get("user_id")

    if current_role not in ("agent", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    if current_role == "agent" and current_agent_id != agent_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    protocols = await database.fetch_all(
        """
        SELECT 
            ap.protocol_name, ap.version, ap.status,
            ap.last_verified_at, ap.created_at,
            pd.specification, pd.endpoints
        FROM agent_protocols ap
        JOIN protocol_definitions pd 
            ON ap.protocol_name = pd.protocol_name 
            AND ap.version = pd.version
        WHERE ap.agent_id = :agent_id
        ORDER BY ap.protocol_name
        """,
        {"agent_id": agent_id}
    )
    
    return [
        {
            "protocol_name": p["protocol_name"],
            "version": p["version"],
            "status": p["status"],
            "last_verified_at": p["last_verified_at"].isoformat() if p["last_verified_at"] else None,
            "created_at": p["created_at"].isoformat(),
            "specification": json.loads(p["specification"]) if isinstance(p["specification"], str) else p["specification"],
            "endpoints": json.loads(p["endpoints"]) if isinstance(p["endpoints"], str) else p["endpoints"]
        }
        for p in protocols
    ]


@agent_router.post("/")
async def enable_protocol_for_agent(
    agent_id: str = Path(..., description="Agent ID"),
    protocol_name: str = Query(..., description="Protocol to enable"),
    version: Optional[str] = Query(None, description="Protocol version (uses latest if not specified)"),
    current_user: dict = Depends(get_current_user)
):
    """
    Enable a protocol for an agent
    """
    # Verify agent access (admin only for enabling protocols)
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Get protocol version if not specified
    if not version:
        latest = await database.fetch_one(
            """
            SELECT version FROM protocol_definitions
            WHERE protocol_name = :protocol_name
            AND status != 'deprecated'
            ORDER BY version DESC
            LIMIT 1
            """,
            {"protocol_name": protocol_name}
        )
        
        if not latest:
            raise HTTPException(status_code=404, detail=f"Protocol {protocol_name} not found")
        
        version = latest["version"]
    
    # Enable protocol for agent
    try:
        await database.execute(
            """
            INSERT INTO agent_protocols (
                agent_id, protocol_name, version, status, last_verified_at
            ) VALUES (
                :agent_id, :protocol_name, :version, 'active', NOW()
            )
            ON CONFLICT (agent_id, protocol_name, version) DO UPDATE
            SET status = 'active', last_verified_at = NOW()
            """,
            {
                "agent_id": agent_id,
                "protocol_name": protocol_name,
                "version": version
            }
        )
        
        return {
            "message": f"Protocol {protocol_name} v{version} enabled for agent {agent_id}",
            "protocol": protocol_name,
            "version": version,
            "status": "active"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to enable protocol: {str(e)}")


@agent_router.delete("/{protocol_name}")
async def disable_protocol_for_agent(
    agent_id: str = Path(..., description="Agent ID"),
    protocol_name: str = Path(..., description="Protocol to disable"),
    current_user: dict = Depends(get_current_user)
):
    """
    Disable a protocol for an agent
    """
    # Verify agent access (admin only for disabling protocols)
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    result = await database.execute(
        """
        UPDATE agent_protocols
        SET status = 'disabled'
        WHERE agent_id = :agent_id AND protocol_name = :protocol_name
        """,
        {"agent_id": agent_id, "protocol_name": protocol_name}
    )
    
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Protocol not found for this agent")
    
    return {"message": f"Protocol {protocol_name} disabled for agent {agent_id}"}


# Employee monitoring endpoints
employee_router = APIRouter(prefix="/employee/protocols", tags=["Employee Protocol Monitoring"])


@employee_router.get("/usage-stats")
async def get_protocol_usage_stats(
    hours: int = Query(24, description="Hours to look back"),
    current_user: dict = Depends(get_current_employee)
):
    """
    Get protocol usage statistics across all agents (Employee only)
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    
    # Get usage by protocol
    usage_stats = await database.fetch_all(
        """
        SELECT 
            protocol_name,
            COUNT(*) as total_events,
            COUNT(DISTINCT agent_id) as unique_agents,
            COUNT(CASE WHEN status = 'completed' THEN 1 END) as successful,
            COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed,
            AVG(response_time_ms) as avg_response_time
        FROM protocol_events
        WHERE created_at >= :cutoff
        GROUP BY protocol_name
        ORDER BY total_events DESC
        """,
        {"cutoff": cutoff}
    )
    
    # Get top agents by protocol usage
    top_agents = await database.fetch_all(
        """
        SELECT 
            pe.agent_id,
            a.name as agent_name,
            COUNT(*) as event_count,
            COUNT(DISTINCT pe.protocol_name) as protocols_used
        FROM protocol_events pe
        JOIN agents a ON pe.agent_id = a.agent_id
        WHERE pe.created_at >= :cutoff
        GROUP BY pe.agent_id, a.name
        ORDER BY event_count DESC
        LIMIT 10
        """,
        {"cutoff": cutoff}
    )
    
    return {
        "period_hours": hours,
        "protocol_usage": [
            {
                "protocol": u["protocol_name"],
                "total_events": u["total_events"],
                "unique_agents": u["unique_agents"],
                "success_rate": (u["successful"] / u["total_events"] * 100) if u["total_events"] > 0 else 0,
                "avg_response_time_ms": u["avg_response_time"]
            }
            for u in usage_stats
        ],
        "top_agents": [
            {
                "agent_id": a["agent_id"],
                "agent_name": a["agent_name"],
                "event_count": a["event_count"],
                "protocols_used": a["protocols_used"]
            }
            for a in top_agents
        ],
        "timestamp": datetime.utcnow().isoformat()
    }


@employee_router.get("/adoption")
async def get_protocol_adoption(
    current_user: dict = Depends(get_current_employee)
):
    """
    Get protocol adoption metrics (Employee only)
    """
    # Total agents
    total_agents = await database.fetch_one("SELECT COUNT(*) as count FROM agents")
    
    # Agents by protocol
    adoption = await database.fetch_all(
        """
        SELECT 
            protocol_name,
            COUNT(DISTINCT agent_id) as agent_count,
            COUNT(CASE WHEN status = 'active' THEN 1 END) as active_count
        FROM agent_protocols
        GROUP BY protocol_name
        """
    )
    
    # Recent activations
    recent_activations = await database.fetch_all(
        """
        SELECT 
            ap.protocol_name,
            ap.agent_id,
            a.name as agent_name,
            ap.created_at
        FROM agent_protocols ap
        JOIN agents a ON ap.agent_id = a.agent_id
        WHERE ap.created_at >= :cutoff
        ORDER BY ap.created_at DESC
        LIMIT 20
        """,
        {"cutoff": datetime.utcnow() - timedelta(days=7)}
    )
    
    total = dict(total_agents)["count"] if total_agents else 0
    
    return {
        "total_agents": total,
        "protocol_adoption": [
            {
                "protocol": a["protocol_name"],
                "agents_enabled": a["agent_count"],
                "adoption_rate": (a["agent_count"] / total * 100) if total > 0 else 0,
                "active_implementations": a["active_count"]
            }
            for a in adoption
        ],
        "recent_activations": [
            {
                "protocol": r["protocol_name"],
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"],
                "activated_at": r["created_at"].isoformat()
            }
            for r in recent_activations
        ]
    }

