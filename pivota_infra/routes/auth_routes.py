"""
Authentication Routes
JWT token management and authentication endpoints
"""

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Dict, Any
from utils.auth import (
    create_jwt_token, 
    verify_jwt_token, 
    create_demo_tokens,
    get_role_permissions,
    ROLES
)
from realtime.metrics_store import snapshot

router = APIRouter(prefix="/api/auth", tags=["authentication"])
security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Dict[str, Any]:
    """Get current authenticated user from JWT token"""
    token = credentials.credentials
    payload = verify_jwt_token(token)
    return payload

async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require admin role for endpoint access"""
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

@router.post("/login")
async def login(username: str, password: str, role: str = "viewer"):
    """Login endpoint - creates JWT token"""
    # In production, validate against real user database
    # For demo, accept any username/password
    
    if role not in ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Available roles: {list(ROLES.keys())}"
        )
    
    # Create token based on role
    entity_id = None
    if role in ["agent", "merchant"]:
        entity_id = f"{role.upper()}_001"  # Demo entity ID
    
    token = create_jwt_token(username, role, entity_id)
    
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": username,
        "role": role,
        "entity_id": entity_id,
        "permissions": get_role_permissions(role)
    }

@router.get("/me")
async def get_current_user_info(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Get current user information"""
    return {
        "user_id": current_user["sub"],
        "role": current_user["role"],
        "entity_id": current_user.get("entity_id"),
        "permissions": get_role_permissions(current_user["role"]),
        "expires_at": current_user["exp"]
    }

@router.get("/demo-tokens")
async def get_demo_tokens():
    """Get demo tokens for testing different roles"""
    tokens = create_demo_tokens()
    return {
        "message": "Demo tokens created for testing",
        "tokens": tokens,
        "usage": {
            "admin_token": "Full system access",
            "operator_token": "Read/write access",
            "viewer_token": "Read-only access",
            "agent_token": "Agent-specific access (AGENT_001)",
            "merchant_token": "Merchant-specific access (MERCH_001)"
        }
    }

@router.get("/roles")
async def get_available_roles():
    """Get available roles and their permissions"""
    return {
        "roles": ROLES,
        "message": "Available roles and their permissions"
    }

@router.post("/validate-token")
async def validate_token(token: str):
    """Validate a JWT token"""
    try:
        payload = verify_jwt_token(token)
        return {
            "valid": True,
            "user_id": payload["sub"],
            "role": payload["role"],
            "entity_id": payload.get("entity_id"),
            "expires_at": payload["exp"]
        }
    except HTTPException as e:
        return {
            "valid": False,
            "error": e.detail
        }
