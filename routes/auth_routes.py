"""
Authentication and User Management Routes
Supports the Lovable admin approval system
"""

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List
import jwt
import hashlib
import secrets
from datetime import datetime, timedelta
import os
from utils.supabase_client import supabase, create_user_in_supabase, get_user_role
from utils.supabase_client import update_user_role as update_user_role_in_supabase

router = APIRouter(prefix="/auth", tags=["authentication"])
security = HTTPBearer()

# JWT Configuration
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# User Role Types
from enum import Enum

class UserRole(str, Enum):
    EMPLOYEE = "employee"
    AGENT = "agent"
    MERCHANT = "merchant"
    OPERATOR = "operator"
    ADMIN = "admin"

# Pydantic Models
class UserSignup(BaseModel):
    email: str
    password: str
    role: UserRole = UserRole.EMPLOYEE  # Default to employee role
    full_name: Optional[str] = None

class UserLogin(BaseModel):
    email: str
    password: str

class UserProfile(BaseModel):
    id: str
    email: str
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    created_at: str

class UserRoleInfo(BaseModel):
    id: str
    user_id: str
    role: UserRole
    approved: bool
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    created_at: str

class PendingUser(BaseModel):
    id: str
    user_id: str
    role: UserRole
    approved: bool
    email: str
    full_name: Optional[str] = None
    created_at: str

class RoleUpdate(BaseModel):
    role: UserRole

class ApprovalUpdate(BaseModel):
    approved: bool

# In-memory storage for demo (replace with database in production)
users_db = {}
user_roles_db = {}
sessions_db = {}

def hash_password(password: str) -> str:
    """Hash password using SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash"""
    return hash_password(password) == hashed

def create_jwt_token(user_id: str, role: str) -> str:
    """Create JWT token for user"""
    payload = {
        "user_id": user_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_jwt_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify JWT token and return user info"""
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("user_id")
        role = payload.get("role")
        
        if not user_id or not role:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )
        
        return {"user_id": user_id, "role": role}
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

def require_admin(current_user: dict = Depends(verify_jwt_token)):
    """Require admin role for access"""
    if current_user["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

@router.post("/signup")
async def signup(user_data: UserSignup):
    """User signup with role selection"""
    try:
        # Create user in Supabase
        supabase_result = await create_user_in_supabase(
            email=user_data.email,
            password=user_data.password,
            role=user_data.role
        )
        
        if not supabase_result["success"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to create user: {supabase_result['error']}"
            )
        
        return {
            "status": "success",
            "message": "Account created successfully. Awaiting admin approval.",
            "user_id": supabase_result["user_id"],
            "role": user_data.role,
            "approved": False
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Signup failed: {str(e)}"
        )

@router.post("/signin")
async def signin(login_data: UserLogin):
    """User signin"""
    try:
        # Authenticate with Supabase
        if not supabase:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Supabase not configured"
            )
        
        try:
            # Sign in with Supabase Auth
            auth_response = supabase.auth.sign_in_with_password({
                "email": login_data.email,
                "password": login_data.password
            })
            
            if not auth_response.user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials"
                )
            
            user_id = auth_response.user.id
            user_email = auth_response.user.email
            
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials"
            )
        
        # Check if user has approved role using Supabase
        user_role = await get_user_role(user_id)
        
        if not user_role or not user_role.get("approved", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account pending admin approval"
            )
        
        # Get primary role
        primary_role = user_role["role"]
        
        # Create JWT token
        token = create_jwt_token(user_id, primary_role)
        
        return {
            "status": "success",
            "message": "Login successful",
            "token": token,
            "user": {
                "id": user_id,
                "email": user_email,
                "full_name": user_email,  # Will be updated from profiles table
                "role": primary_role
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login failed: {str(e)}"
        )

@router.get("/me")
async def get_current_user(current_user: dict = Depends(verify_jwt_token)):
    """Get current user information"""
    try:
        # Get user from Supabase
        if not supabase:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Supabase not configured"
            )
        
        try:
            # Get user from Supabase profiles table
            result = supabase.table("profiles").select("*").eq("id", current_user["user_id"]).execute()
            
            if not result.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found"
                )
            
            user = result.data[0]
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user: {str(e)}"
            )
        
        return {
            "status": "success",
            "user": {
                "id": user["id"],
                "email": user["email"],
                "full_name": user["full_name"],
                "role": current_user["role"],
                "created_at": user["created_at"]
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get user info: {str(e)}"
        )

@router.get("/admin/users")
async def get_pending_users(admin_user: dict = Depends(require_admin)):
    """Get all users for admin approval (admin only)"""
    try:
        pending_users = []
        
        for role_id, role_data in user_roles_db.items():
            # Find user data
            user = None
            for email, user_data in users_db.items():
                if user_data["id"] == role_data["user_id"]:
                    user = user_data
                    break
            
            if user:
                pending_users.append(PendingUser(
                    id=role_id,
                    user_id=role_data["user_id"],
                    role=role_data["role"],
                    approved=role_data["approved"],
                    email=user["email"],
                    full_name=user["full_name"],
                    created_at=role_data["created_at"]
                ))
        
        # Sort by creation date (newest first)
        pending_users.sort(key=lambda x: x.created_at, reverse=True)
        
        return {
            "status": "success",
            "users": pending_users
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get users: {str(e)}"
        )

@router.post("/admin/users/{user_id}/approve")
async def approve_user(
    user_id: str,
    approval_data: ApprovalUpdate,
    admin_user: dict = Depends(require_admin)
):
    """Approve or reject user (admin only)"""
    try:
        # Get current user role from Supabase
        user_role_info = await get_user_role(user_id)
        
        if not user_role_info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User role not found"
            )
        
        # Update approval status in Supabase, keeping the existing role
        result = await update_user_role_in_supabase(
            user_id=user_id,
            role=user_role_info.get("role", "employee"),
            approved=approval_data.approved
        )
        
        if not result["success"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to update approval: {result['error']}"
            )
        
        return {
            "status": "success",
            "message": f"User {'approved' if approval_data.approved else 'rejected'}",
            "user_id": user_id,
            "approved": approval_data.approved
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update approval: {str(e)}"
        )

@router.put("/admin/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    role_data: RoleUpdate,
    admin_user: dict = Depends(require_admin)
):
    """Update user role (admin only)"""
    try:
        # Find user role
        role_found = None
        for role_id, role_info in user_roles_db.items():
            if role_info["user_id"] == user_id:
                role_found = role_info
                break
        
        if not role_found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User role not found"
            )
        
        # Update role
        role_found["role"] = role_data.role
        
        return {
            "status": "success",
            "message": f"Role updated to {role_data.role}",
            "user_id": user_id,
            "role": role_data.role
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update role: {str(e)}"
        )

@router.post("/signout")
async def signout(current_user: dict = Depends(verify_jwt_token)):
    """User signout"""
    try:
        # In a real implementation, you might blacklist the token
        return {
            "status": "success",
            "message": "Signed out successfully"
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Signout failed: {str(e)}"
        )

@router.get("/test-supabase")
async def test_supabase_connection():
    """Test Supabase connection and configuration"""
    try:
        if not supabase:
            return {
                "status": "error",
                "message": "Supabase not configured",
                "supabase_url": os.getenv("SUPABASE_URL", "Not set"),
                "supabase_service_role_key": "Not set" if not os.getenv("SUPABASE_SERVICE_ROLE_KEY") else "Set"
            }
        
        # Test Supabase connection by trying to query the profiles table
        result = supabase.table("profiles").select("id").limit(1).execute()
        
        return {
            "status": "success",
            "message": "Supabase connection successful",
            "supabase_url": os.getenv("SUPABASE_URL", "Not set"),
            "supabase_service_role_key": "Set" if os.getenv("SUPABASE_SERVICE_ROLE_KEY") else "Not set",
            "database_accessible": True,
            "test_query_result": result.data if result.data else []
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Supabase connection failed: {str(e)}",
            "supabase_url": os.getenv("SUPABASE_URL", "Not set"),
            "supabase_service_role_key": "Set" if os.getenv("SUPABASE_SERVICE_ROLE_KEY") else "Not set",
            "database_accessible": False
        }

@router.get("/test-auth")
async def test_auth_flow():
    """Test authentication flow without requiring token"""
    return {
        "status": "success",
        "message": "Auth endpoints are accessible",
        "endpoints": {
            "signup": "POST /auth/signup",
            "signin": "POST /auth/signin", 
            "me": "GET /auth/me (requires Authorization header)",
            "admin_users": "GET /auth/admin/users (requires admin token)"
        },
        "note": "For /me and /admin/users, include Authorization: Bearer <token> header"
    }

@router.get("/test-post")
async def test_post_method():
    """Test if POST methods are working"""
    return {
        "status": "success",
        "message": "GET method works, testing POST method availability",
        "test_post_endpoint": "POST /auth/test-post-response"
    }

@router.post("/test-post-response")
async def test_post_response():
    """Test POST method response"""
    return {
        "status": "success",
        "message": "POST method is working correctly",
        "method": "POST",
        "endpoint": "/auth/test-post-response"
    }

@router.get("/test-get-simple")
async def test_get_simple():
    """Simple GET test"""
    return {"status": "success", "message": "GET works"}

@router.post("/test-post-simple")
async def test_post_simple():
    """Simple POST test"""
    return {"status": "success", "message": "POST works"}

@router.post("/test-post-minimal")
async def test_post_minimal():
    """Minimal POST test with no dependencies"""
    return {"message": "Minimal POST endpoint works"}

@router.options("/test-post-simple")
async def test_post_simple_options():
    """Handle OPTIONS request for CORS"""
    return {"message": "OPTIONS handled"}