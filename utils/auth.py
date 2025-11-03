"""
Authentication and Authorization Utilities
JWT-based authentication for REST and WebSocket endpoints
"""

import jwt
import time
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from fastapi import HTTPException, status

# JWT Configuration
JWT_SECRET = "pivota-dashboard-secret-key-2024"  # In production, use environment variable
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# Role definitions
ROLES = {
    "admin": {
        "permissions": ["read", "write", "delete", "manage"],
        "description": "Full system access"
    },
    "operator": {
        "permissions": ["read", "write"],
        "description": "Operational access"
    },
    "viewer": {
        "permissions": ["read"],
        "description": "Read-only access"
    },
    "agent": {
        "permissions": ["read_own", "write_own"],
        "description": "Agent-specific access"
    },
    "merchant": {
        "permissions": ["read_own", "write_own"],
        "description": "Merchant-specific access"
    }
}

def create_jwt_token(
    user_id: str,
    role: str,
    entity_id: Optional[str] = None,
    expires_delta: Optional[timedelta] = None
) -> str:
    """Create a JWT token with user information"""
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    
    payload = {
        "sub": user_id,
        "role": role,
        "entity_id": entity_id,
        "exp": expire,
        "iat": datetime.utcnow(),
        "iss": "pivota-dashboard"
    }
    
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token

def verify_jwt_token(token: str) -> Dict[str, Any]:
    """Verify and decode a JWT token"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

def check_permission(user_role: str, required_permission: str) -> bool:
    """Check if user role has required permission"""
    if user_role not in ROLES:
        return False
    
    user_permissions = ROLES[user_role]["permissions"]
    return required_permission in user_permissions

def get_role_permissions(role: str) -> list:
    """Get permissions for a specific role"""
    return ROLES.get(role, {}).get("permissions", [])

def create_demo_tokens() -> Dict[str, str]:
    """Create demo tokens for testing different roles"""
    return {
        "admin_token": create_jwt_token("admin_user", "admin"),
        "operator_token": create_jwt_token("operator_user", "operator"),
        "viewer_token": create_jwt_token("viewer_user", "viewer"),
        "agent_token": create_jwt_token("agent_user", "agent", "AGENT_001"),
        "merchant_token": create_jwt_token("merchant_user", "merchant", "MERCH_001")
    }

def validate_entity_access(user_role: str, user_entity_id: str, requested_entity_id: str) -> bool:
    """Validate if user can access specific entity data"""
    if user_role in ["admin", "operator", "viewer"]:
        return True  # Global access
    
    if user_role in ["agent", "merchant"]:
        return user_entity_id == requested_entity_id
    
    return False
