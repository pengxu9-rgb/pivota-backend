"""
Agent Protocol Testing Routes - Phase 4
Separate router for /agents/{agent_id}/protocols/{protocol_name}/test endpoint
"""
from fastapi import APIRouter, HTTPException, Depends, Path
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional

from db.database import database
from services.protocol_adapter_service import ProtocolAdapterService
from utils.auth import ADMIN_ROLES, get_current_user

# `caller is the agent OR caller is an admin` -- only the admin conjunct is
# corrected for super_admin; the ownership test is untouched.


router = APIRouter(prefix="/agents", tags=["Agent Protocol Testing"])


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


# Initialize service
protocol_service = ProtocolAdapterService(database)


@router.post("/{agent_id}/protocols/{protocol_name}/test", response_model=ProtocolTestResponse)
async def test_protocol_call(
    agent_id: str = Path(..., description="Agent ID"),
    protocol_name: str = Path(..., description="Protocol name: AP2, ACP, or X-402"),
    request: ProtocolTestRequest = ...,
    current_user: dict = Depends(get_current_user)
):
    """
    Test a protocol call in sandbox mode
    """
    # Verify agent access
    if current_user.get("user_id") != agent_id and current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Verify agent has this protocol enabled
    protocol_enabled = await database.fetch_one(
        """
        SELECT status FROM agent_protocols
        WHERE agent_id = :agent_id AND protocol_name = :protocol_name
        """,
        {"agent_id": agent_id, "protocol_name": protocol_name}
    )
    
    if not protocol_enabled or protocol_enabled["status"] != "active":
        raise HTTPException(status_code=400, detail=f"Protocol {protocol_name} is not enabled for this agent")
    
    # Test protocol
    result = await protocol_service.test_protocol(
        agent_id=agent_id,
        protocol_name=protocol_name,
        test_payload=request.test_payload
    )
    
    return ProtocolTestResponse(
        success=result.get("success", False),
        protocol=protocol_name,
        validation_result="Valid" if result.get("success") else "Invalid",
        transformed_request=result.get("transformed_request"),
        mock_response=result.get("response"),
        endpoints=result.get("endpoints"),
        error=result.get("error")
    )
