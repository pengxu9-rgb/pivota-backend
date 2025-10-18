"""
Authentication Utilities
Simple JWT-based authentication for Pivota platform
"""

import jwt
import bcrypt
import os
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# JWT Configuration
JWT_SECRET = os.getenv("JWT_SECRET", "pivota-secret-key-change-in-production-2024")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# Security
security = HTTPBearer()

def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash"""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False

def create_access_token(user_id: str, email: str, role: str) -> str:
    """
    Create a JWT access token
    
    Args:
        user_id: User's UUID
        email: User's email
        role: User's role (employee, merchant, agent)
    
    Returns:
        JWT token string
    """
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": expire,
        "iat": datetime.utcnow()
    }
    
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_access_token(token: str) -> Dict[str, Any]:
    """
    Decode and verify a JWT token
    
    Args:
        token: JWT token string
    
    Returns:
        Decoded payload
    
    Raises:
        HTTPException: If token is invalid or expired
    """
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

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Dict[str, Any]:
    """
    Get current user from JWT token
    
    Args:
        credentials: HTTP Authorization credentials
    
    Returns:
        User payload from token
    
    Raises:
        HTTPException: If token is invalid
    """
    token = credentials.credentials
    payload = decode_access_token(token)
    return payload

async def require_role(required_role: str, current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Require a specific role for access
    
    Args:
        required_role: Required role (employee, merchant, agent)
        current_user: Current user from token
    
    Returns:
        Current user if role matches
    
    Raises:
        HTTPException: If user doesn't have required role
    """
    if current_user["role"] != required_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Required role: {required_role}"
        )
    return current_user

# Role constants
EMPLOYEE_ROLES = ["super_admin", "admin", "employee", "outsourced"]
ADMIN_ROLES = ["super_admin", "admin"]

# Permission checking helpers
def is_employee(role: str) -> bool:
    """Check if role is any employee level"""
    return role in EMPLOYEE_ROLES

def is_admin(role: str) -> bool:
    """Check if role has admin privileges"""
    return role in ADMIN_ROLES

def is_super_admin(role: str) -> bool:
    """Check if role is super admin"""
    return role == "super_admin"

# Helper functions for specific roles
async def get_current_employee(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Require any employee role (super_admin, admin, employee, outsourced)"""
    if not is_employee(current_user["role"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Employee access required"
        )
    return current_user

async def get_current_admin(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Require admin role (super_admin or admin)"""
    if not is_admin(current_user["role"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

async def get_current_super_admin(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Require super admin role"""
    if not is_super_admin(current_user["role"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin access required"
        )
    return current_user

async def get_current_merchant(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Require merchant role"""
    return await require_role("merchant", current_user)

async def get_current_agent(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Require agent role"""
    return await require_role("agent", current_user)


# ==================== Backward Compatibility Functions ====================
# These functions provide compatibility with the old auth system

def verify_jwt_token(token: str) -> Dict[str, Any]:
    """
    Verify JWT token and return payload (backward compatibility)
    
    Raises:
        HTTPException: If token is invalid or expired
    """
    return decode_access_token(token)

def create_jwt_token(
    user_id: str,
    role: str,
    entity_id: Optional[str] = None,
    expires_delta: Optional[timedelta] = None
) -> str:
    """
    Create JWT token (backward compatibility)
    
    Args:
        user_id: User ID
        role: User role
        entity_id: Entity ID (merchant_id, agent_id, etc.)
        expires_delta: Token expiration time
    
    Returns:
        Encoded JWT token
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    
    payload = {
        "sub": user_id,
        "role": role,
        "entity_id": entity_id,
        "exp": expire,
        "iat": datetime.utcnow()
    }
    
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
