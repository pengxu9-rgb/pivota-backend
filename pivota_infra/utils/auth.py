"""
Authentication utilities for Pivota
Handles JWT token creation, validation, and user authentication
"""

from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import bcrypt
from config.settings import settings

# JWT Configuration
JWT_SECRET = settings.jwt_secret_key
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440  # 24 hours

# Security scheme
security = HTTPBearer()

# ============================================================================
# PASSWORD HASHING
# ============================================================================

def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    try:
        return bcrypt.checkpw(
            plain_password.encode('utf-8'),
            hashed_password.encode('utf-8')
        )
    except Exception:
        return False


# ============================================================================
# JWT TOKEN MANAGEMENT
# ============================================================================

def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a JWT access token
    
    Args:
        data: Payload data to encode (should include sub, email, role)
        expires_delta: Optional custom expiration time
    
    Returns:
        Encoded JWT token string
    """
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({
        "exp": expire,
        "iat": datetime.utcnow()
    })
    
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Dict[str, Any]:
    """
    Decode and verify a JWT token
    
    Args:
        token: JWT token string
    
    Returns:
        Decoded payload dictionary
    
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


# ============================================================================
# AUTHENTICATION DEPENDENCIES
# ============================================================================

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> Dict[str, Any]:
    """
    Get current authenticated user from JWT token in Authorization header
    
    Args:
        credentials: HTTP Bearer token from request header
    
    Returns:
        User information dictionary containing:
        - sub: User ID
        - email: User email
        - role: User role
        - merchant_id: (optional) For merchant users
        - agent_id: (optional) For agent users
    
    Raises:
        HTTPException: If authentication fails
    """
    try:
        token = credentials.credentials
        payload = decode_token(token)
        
        # Validate required fields
        if "sub" not in payload or "email" not in payload or "role" not in payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload"
            )
        
        return payload
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(e)}"
        )


async def require_admin(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Require admin or super_admin role
    
    Args:
        current_user: Current authenticated user
    
    Returns:
        User information if authorized
    
    Raises:
        HTTPException: If user is not an admin
    """
    if current_user.get("role") not in ["admin", "super_admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


# Alias for backward compatibility
get_current_admin = require_admin


async def get_current_employee(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Require employee role (super_admin, admin, employee, or outsourced)
    
    Args:
        current_user: Current authenticated user
    
    Returns:
        User information if authorized
    
    Raises:
        HTTPException: If user is not an employee
    """
    if current_user.get("role") not in ["super_admin", "admin", "employee", "outsourced"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Employee access required"
        )
    return current_user


# ============================================================================
# ROLE & PERMISSION CHECKING
# ============================================================================

EMPLOYEE_ROLES = ["super_admin", "admin", "employee", "outsourced"]
ADMIN_ROLES = ["super_admin", "admin"]


def is_employee(role: str) -> bool:
    """Check if role is an employee role"""
    return role in EMPLOYEE_ROLES


def is_admin(role: str) -> bool:
    """Check if role is an admin role"""
    return role in ADMIN_ROLES


def check_permission(user_info: Dict[str, Any], required_permission: str) -> bool:
    """
    Check if user has a specific permission
    
    Args:
        user_info: User information from JWT token
        required_permission: Permission string to check
    
    Returns:
        True if user has permission, False otherwise
    """
    role = user_info.get("role", "")
    
    # Super admin has all permissions
    if role == "super_admin":
        return True
    
    # Admin has most permissions
    if role == "admin":
        # Admins can't modify super admin settings
        if "super_admin" not in required_permission:
            return True
    
    # Define permission mappings
    permission_map = {
        "employee": ["view_dashboard", "view_transactions", "view_merchants", "view_agents"],
        "merchant": ["view_own_orders", "view_own_transactions", "manage_own_products"],
        "agent": ["create_orders", "view_own_orders", "view_own_analytics"],
        "outsourced": ["view_dashboard", "view_transactions"]
    }
    
    allowed_permissions = permission_map.get(role, [])
    return required_permission in allowed_permissions


def can_access_merchant(user_info: Dict[str, Any], merchant_id: str) -> bool:
    """
    Check if user can access specific merchant data
    
    Args:
        user_info: User information from JWT token
        merchant_id: Merchant ID to check access for
    
    Returns:
        True if user can access merchant data
    """
    role = user_info.get("role", "")
    
    # Employees can access all merchants
    if role in EMPLOYEE_ROLES:
        return True
    
    # Merchants can only access their own data
    if role == "merchant":
        return user_info.get("merchant_id") == merchant_id
    
    # Agents can access their assigned merchants
    if role == "agent":
        # TODO: Check agent-merchant assignment in database
        return True
    
    return False


def can_access_agent(user_info: Dict[str, Any], agent_id: str) -> bool:
    """
    Check if user can access specific agent data
    
    Args:
        user_info: User information from JWT token
        agent_id: Agent ID to check access for
    
    Returns:
        True if user can access agent data
    """
    role = user_info.get("role", "")
    
    # Employees can access all agents
    if role in EMPLOYEE_ROLES:
        return True
    
    # Agents can only access their own data
    if role == "agent":
        return user_info.get("agent_id") == agent_id
    
    return False


def validate_entity_access(user_role: str, user_entity_id: str, requested_entity_id: str) -> bool:
    """
    Validate if user can access specific entity data
    
    Args:
        user_role: User's role (admin, merchant, agent, etc.)
        user_entity_id: User's entity ID (merchant_id or agent_id)
        requested_entity_id: The entity ID being requested
    
    Returns:
        True if user can access the entity
    """
    # Admins and employees have global access
    if user_role in ["super_admin", "admin", "employee", "operator", "viewer"]:
        return True
    
    # Merchants and agents can only access their own entity
    if user_role in ["merchant", "agent"]:
        return user_entity_id == requested_entity_id
    
    return False


# ============================================================================
# LEGACY COMPATIBILITY (for old code)
# ============================================================================

def verify_jwt_token(token: str) -> Dict[str, Any]:
    """
    Legacy function for backward compatibility
    Use get_current_user() instead for new code
    """
    return decode_token(token)


def create_jwt_token(user_id: str, role: str, entity_id: Optional[str] = None) -> str:
    """
    Legacy function for backward compatibility
    Use create_access_token() instead for new code
    """
    data = {
        "sub": user_id,
        "role": role
    }
    if entity_id:
        if role == "merchant":
            data["merchant_id"] = entity_id
        elif role == "agent":
            data["agent_id"] = entity_id
    
    return create_access_token(data)
