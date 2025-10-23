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
# Supabase logic removed. Using in-memory store for development/testing.

router = APIRouter(prefix="/auth", tags=["authentication"])
security = HTTPBearer()

# JWT Configuration - Import from config for consistency
from config.settings import settings
JWT_SECRET = settings.jwt_secret_key
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

def create_jwt_token(user_id: str, role: str, email: str = None) -> str:
    """Create JWT token for user"""
    payload = {
        "sub": user_id,  # Standard JWT claim for subject
        "user_id": user_id,  # For backward compatibility
        "email": email or user_id,
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
    """User signup with role selection (in-memory, no Supabase)."""
    try:
        if user_data.email in users_db:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User already exists")
        users_db[user_data.email] = {
            "id": user_data.email,
            "email": user_data.email,
            "password_hash": hash_password(user_data.password),
            "full_name": user_data.full_name or user_data.email,
        }
        user_roles_db[user_data.email] = {
            "user_id": user_data.email,
            "role": user_data.role.value,
            "approved": True,
            "created_at": datetime.utcnow().isoformat()
        }
        return {
            "status": "success",
            "message": "Account created",
            "user_id": user_data.email,
            "role": user_data.role.value,
            "approved": True
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Signup failed: {str(e)}")

@router.post("/signin")
async def signin(login_data: UserLogin):
    """User signin (in-memory, with built-in demo accounts)."""
    try:
        demo_accounts = {
            "merchant@test.com": {"password": "Admin123!", "role": "merchant", "merchant_id": "merch_6b90dc9838d5fd9c"},
            "employee@pivota.com": {"password": "Admin123!", "role": "admin"},
            "agent@test.com": {"password": "Admin123!", "role": "agent"},
            "superadmin@pivota.com": {"password": "admin123", "role": "admin"},
        }
        # In-memory users
        if login_data.email in users_db:
            stored = users_db[login_data.email]
            if not verify_password(login_data.password, stored["password_hash"]):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
            role_info = user_roles_db.get(login_data.email, {"role": "employee", "approved": True})
            primary_role = role_info["role"]
            token = create_jwt_token(login_data.email, primary_role, login_data.email)
            return {
                "status": "success",
                "message": "Login successful",
                "token": token,
                "user": {"id": login_data.email, "email": login_data.email, "full_name": stored.get("full_name", login_data.email), "role": primary_role}
            }
        # Demo accounts
        acct = demo_accounts.get(login_data.email)
        if not acct or acct["password"] != login_data.password:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        
        # For merchant accounts, verify they are not soft-deleted
        if acct["role"] == "merchant" and acct.get("merchant_id"):
            try:
                from db.merchant_onboarding import get_merchant_onboarding
                merchant = await get_merchant_onboarding(acct["merchant_id"])
                if merchant and merchant.get("status") == "deleted":
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN, 
                        detail="Account has been deactivated. Please contact support."
                    )
            except HTTPException:
                raise
            except Exception as e:
                # If merchant not found in onboarding table, allow login (backward compatibility)
                print(f"Warning: Could not verify merchant status: {e}")
        
        # Create token with merchant_id if available
        token_payload = {
            "sub": login_data.email,
            "user_id": login_data.email,
            "email": login_data.email,
            "role": acct["role"],
            "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS),
            "iat": datetime.utcnow()
        }
        
        # Add merchant_id or agent_id if available
        if acct["role"] == "merchant" and "merchant_id" in acct:
            token_payload["merchant_id"] = acct["merchant_id"]
        
        # For agent accounts, ensure agent record exists in agents table
        if acct["role"] == "agent":
            try:
                # Check if agent exists
                existing_agent = await database.fetch_one(
                    "SELECT agent_id FROM agents WHERE email = :email",
                    {"email": login_data.email}
                )
                
                if not existing_agent:
                    # Create agent record with initial API key
                    import secrets
                    api_key = f"pk_live_{secrets.token_urlsafe(32)[:32]}"
                    await database.execute(
                        """
                        INSERT INTO agents (agent_id, name, email, company, api_key, status)
                        VALUES (:agent_id, :name, :email, :company, :api_key, :status)
                        ON CONFLICT (email) DO NOTHING
                        """,
                        {
                            "agent_id": login_data.email,
                            "name": login_data.email.split('@')[0].title() + " Agent",
                            "email": login_data.email,
                            "company": "Agent Company",
                            "api_key": api_key,
                            "status": "active"
                        }
                    )
                    print(f"✅ Auto-created agent record for {login_data.email}")
            except Exception as e:
                # Don't fail login if agent creation fails
                print(f"⚠️ Could not create agent record: {e}")
        
        token = jwt.encode(token_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        
        user_data = {
            "id": login_data.email,
            "email": login_data.email,
            "full_name": login_data.email,
            "role": acct["role"]
        }
        
        if "merchant_id" in acct:
            user_data["merchant_id"] = acct["merchant_id"]
        
        return {
            "status": "success",
            "message": "Login successful",
            "token": token,
            "user": user_data
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Login failed: {str(e)}")

@router.get("/me")
async def get_current_user(current_user: dict = Depends(verify_jwt_token)):
    """Get current user information (from JWT / in-memory)."""
    try:
        # If user exists in our memory store, provide richer info
        stored = users_db.get(current_user["user_id"])
        if stored:
            return {
                "status": "success",
                "user": {
                    "id": stored["id"],
                    "email": stored["email"],
                    "full_name": stored.get("full_name", stored["email"]),
                    "role": user_roles_db.get(current_user["user_id"], {}).get("role", current_user["role"]),
                    "created_at": user_roles_db.get(current_user["user_id"], {}).get("created_at", datetime.utcnow().isoformat()),
                }
            }
        # Fallback to JWT-only info
        return {
            "status": "success",
            "user": {
                "id": current_user["user_id"],
                "email": current_user["user_id"],
                "full_name": current_user["user_id"],
                "role": current_user["role"],
                "created_at": datetime.utcnow().isoformat()
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to get user info: {str(e)}")

@router.get("/admin/users")
async def get_pending_users(admin_user: dict = Depends(require_admin)):
    """Get all users for admin management (admin only, in-memory)."""
    try:
        users_list: list[PendingUser] = []
        for email, role_data in user_roles_db.items():
            user = users_db.get(email, {"email": email, "full_name": email})
            users_list.append(PendingUser(
                id=email,
                user_id=role_data["user_id"],
                role=role_data["role"],
                approved=role_data.get("approved", True),
                email=user["email"],
                full_name=user.get("full_name", user["email"]),
                created_at=role_data.get("created_at", datetime.utcnow().isoformat())
            ))
        users_list.sort(key=lambda x: x.created_at, reverse=True)
        return {"status": "success", "users": users_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to get users: {str(e)}")

@router.post("/admin/users/{user_id}/approve")
async def approve_user(
    user_id: str,
    approval_data: ApprovalUpdate,
    admin_user: dict = Depends(require_admin)
):
    """Approve or reject user (admin only, in-memory)."""
    try:
        if user_id not in user_roles_db:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User role not found")
        user_roles_db[user_id]["approved"] = approval_data.approved
        return {
            "status": "success",
            "message": f"User {'approved' if approval_data.approved else 'rejected'}",
            "user_id": user_id,
            "approved": approval_data.approved
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to update approval: {str(e)}")

@router.put("/admin/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    role_data: RoleUpdate,
    admin_user: dict = Depends(require_admin)
):
    """Update user role (admin only, in-memory)."""
    try:
        if user_id not in user_roles_db:
            # If not present, create default entry
            user_roles_db[user_id] = {"user_id": user_id, "role": role_data.role.value if hasattr(role_data.role, 'value') else str(role_data.role), "approved": True, "created_at": datetime.utcnow().isoformat()}
        else:
            user_roles_db[user_id]["role"] = role_data.role.value if hasattr(role_data.role, 'value') else str(role_data.role)
        return {"status": "success", "message": f"Role updated to {role_data.role}", "user_id": user_id, "role": role_data.role}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to update role: {str(e)}")

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

# Supabase test endpoint removed.

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

@router.get("/admin-token")
async def get_admin_test_token():
    """
    Generate a test admin JWT token for dashboard access
    ⚠️ FOR DEVELOPMENT ONLY - Remove in production!
    """
    payload = {
        "user_id": "superadmin@pivota.com",  # Changed from "sub" to "user_id"
        "role": "admin",
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS),
        "iat": datetime.utcnow()
    }
    
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    
    return {
        "status": "success",
        "token": token,
        "expires_in": f"{JWT_EXPIRATION_HOURS} hours",
        "user": "superadmin@pivota.com",
        "role": "admin",
        "note": "⚠️ This is a test endpoint. Remove in production!"
    }