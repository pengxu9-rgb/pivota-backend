"""
Legacy authentication routes (`/auth/*`).

Kept only for backward compatibility with older frontends that still call
`/auth/signin`. Authentication is resolved against real datastores: the legacy
`employees` table and the canonical `users` table. Preferred for new callers:
`POST /api/auth/login`.

The in-memory `users_db` / `user_roles_db` / `sessions_db` dev fixtures and the
`/auth/signup` + `/auth/admin-token` endpoints they backed were removed: they let
an anonymous caller mint a JWT with a self-chosen `role` (including "admin"),
which satisfied every `require_admin` / `ADMIN_ROLES` check in the codebase.
`main._guard_legacy_inmemory_auth_routes()` fails startup if they come back.
"""

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Any, Dict, Optional
import jwt
import hashlib
import secrets
from datetime import datetime, timedelta
import os

# Database import for employee authentication
from db.database import database
from utils.auth import verify_password as verify_bcrypt_password

router = APIRouter(prefix="/auth", tags=["authentication"])
security = HTTPBearer()

# JWT Configuration - Import from config for consistency
from config.settings import settings
JWT_SECRET = settings.jwt_secret_key
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

DEMO_MERCHANT_IDS = {
    "merchant@test.com": os.getenv("DEMO_MERCHANT_ID", "").strip(),
} if settings.enable_internal_demo_fixtures and os.getenv("DEMO_MERCHANT_ID", "").strip() else {}

# Pydantic Models
class UserLogin(BaseModel):
    email: str
    password: str

def normalize_email(raw_email: str) -> str:
    """Normalize email so legacy auth uses the same lookup key as canonical auth."""
    return (raw_email or "").strip().lower()

def create_jwt_token(
    user_id: str,
    role: str,
    email: str = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    """Create JWT token for user"""
    payload = {
        "sub": user_id,  # Standard JWT claim for subject
        "user_id": user_id,  # For backward compatibility
        "email": email or user_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS),
        "iat": datetime.utcnow()
    }
    if extra_claims:
        payload.update(extra_claims)
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

def require_employee(current_user: dict = Depends(verify_jwt_token)):
    """Require employee or admin role for access"""
    if current_user["role"] not in ["admin", "employee"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Employee access required"
        )
    return current_user

@router.post("/signin")
async def signin(login_data: UserLogin):
    """
    Legacy signin endpoint kept for backward compatibility with older frontends.

    Supports:
    - Legacy `employees` table auth (SHA256 + static salt)
    - Canonical `users` table auth (bcrypt) as a fallback
    - Hardcoded demo accounts, only when `ENABLE_INTERNAL_DEMO_FIXTURES=true`

    Every lane resolves the role from a datastore or a flag-gated fixture; no
    lane ever honours a role supplied by the caller.

    Preferred for real accounts: `POST /api/auth/login`.
    """
    try:
        normalized_email = normalize_email(login_data.email)
        demo_accounts = {}
        if settings.enable_internal_demo_fixtures:
            demo_merchant_id = os.getenv("DEMO_MERCHANT_ID", "").strip()
            demo_accounts = {
                **(
                    {
                        "merchant@test.com": {
                            "password": "Admin123!",
                            "role": "merchant",
                            "merchant_id": demo_merchant_id,
                        }
                    }
                    if demo_merchant_id
                    else {}
                ),
                "employee@pivota.com": {"password": "Admin123!", "role": "admin"},
                "agent@test.com": {"password": "Admin123!", "role": "agent"},
                "superadmin@pivota.com": {"password": "admin123", "role": "admin"},
            }
        # Legacy employees table (optional). If the table doesn't exist in the new DB,
        # swallow the error and fall back to demo accounts instead of returning 500.
        employee = None
        try:
            employee_query = """
                SELECT employee_id, name, email, password, role
                FROM employees
                WHERE email = :email AND status = 'active'
            """
            employee = await database.fetch_one(employee_query, {"email": normalized_email})
        except Exception as e:
            # Log and ignore missing legacy table or other DB errors
            print(f"Employees lookup skipped: {e}")
            employee = None
        
        if employee:
            salt = "pivota_employee_salt_v1"
            hashed_input = hashlib.sha256(f"{login_data.password}{salt}".encode()).hexdigest()
            
            if employee["password"] and hashed_input == employee["password"]:
                token = create_jwt_token(employee["employee_id"], employee["role"], employee["email"])
                return {
                    "status": "success",
                    "message": "Login successful",
                    "token": token,
                    "user": {
                        "id": employee["employee_id"],
                        "email": employee["email"],
                        "full_name": employee["name"],
                        "role": employee["role"]
                    }
                }
        
        # Canonical users table (bcrypt). For backward compatibility with older
        # frontends that still call `/auth/signin`, allow authenticating against
        # the modern `/api/auth/*` user store as well.
        user = None
        try:
            user_query = """
                SELECT id, email, password_hash, full_name, role, active, merchant_id
                FROM users
                WHERE email = :email
                LIMIT 1
            """
            user = await database.fetch_one(user_query, {"email": normalized_email})
        except Exception as e:
            print(f"Users lookup skipped: {e}")
            user = None

        user_row = None
        if user:
            # `database.fetch_one` returns a Record which supports `__getitem__`
            # but not `.get()`; convert to dict for safe access.
            try:
                user_row = dict(user)
            except Exception:
                user_row = None

        if user_row and user_row.get("password_hash") and verify_bcrypt_password(
            login_data.password,
            user_row["password_hash"],
        ):
            if user_row.get("active") is False:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account has been deactivated",
                )

            merchant_id = user_row.get("merchant_id")
            if user_row.get("role") == "merchant" and not merchant_id:
                merchant_id = DEMO_MERCHANT_IDS.get(normalized_email)
            token = create_jwt_token(
                user_row["email"],
                user_row["role"],
                user_row["email"],
                {"merchant_id": merchant_id} if merchant_id else None,
            )
            user_payload = {
                "id": str(user_row["id"]),
                "email": user_row["email"],
                "full_name": user_row.get("full_name") or user_row["email"],
                "role": user_row["role"],
            }
            if merchant_id:
                user_payload["merchant_id"] = merchant_id

            return {
                "status": "success",
                "message": "Login successful",
                "token": token,
                "user": user_payload,
            }

        # Demo accounts fallback
        acct = demo_accounts.get(normalized_email)
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
            "sub": normalized_email,
            "user_id": normalized_email,
            "email": normalized_email,
            "role": acct["role"],
            "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS),
            "iat": datetime.utcnow()
        }
        
        # Add merchant_id or agent_id if available
        if acct["role"] == "merchant" and "merchant_id" in acct:
            token_payload["merchant_id"] = acct["merchant_id"]
        
        # For agent accounts, ensure agent record exists in agents table
        agent_api_key = None
        if acct["role"] == "agent":
            try:
                # Check if agent exists
                existing_agent = await database.fetch_one(
                    "SELECT agent_id, api_key FROM agents WHERE email = :email",
                    {"email": normalized_email}
                )
                
                if not existing_agent:
                    # Create agent record with initial API key
                    api_key = f"ak_live_{secrets.token_hex(32)}"  # 64 hex chars
                    await database.execute(
                        """
                        INSERT INTO agents (agent_id, name, email, company, api_key, status)
                        VALUES (:agent_id, :name, :email, :company, :api_key, :status)
                        ON CONFLICT (email) DO NOTHING
                        """,
                        {
                            "agent_id": normalized_email,
                            "name": normalized_email.split('@')[0].title() + " Agent",
                            "email": normalized_email,
                            "company": "Agent Company",
                            "api_key": api_key,
                            "status": "active"
                        }
                    )
                    agent_api_key = api_key
                    print(f"✅ Auto-created agent record for {normalized_email}")
                else:
                    # Return existing API key
                    agent_api_key = existing_agent["api_key"]
            except Exception as e:
                # Don't fail login if agent creation fails
                print(f"⚠️ Could not create agent record: {e}")
        
        token = jwt.encode(token_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        
        user_data = {
            "id": normalized_email,
            "email": normalized_email,
            "full_name": normalized_email,
            "role": acct["role"]
        }
        
        if "merchant_id" in acct:
            user_data["merchant_id"] = acct["merchant_id"]
        
        # Include agent_api_key in response for agents (only on login)
        response_data = {
            "status": "success",
            "message": "Login successful",
            "token": token,
            "user": user_data
        }
        
        if agent_api_key:
            response_data["agent_api_key"] = agent_api_key
        
        return response_data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Login failed: {str(e)}")

@router.get("/me")
async def get_current_user(current_user: dict = Depends(verify_jwt_token)):
    """Get current user information, derived from the verified JWT."""
    try:
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
            "signin": "POST /auth/signin",
            "me": "GET /auth/me (requires Authorization header)",
            "signout": "POST /auth/signout (requires Authorization header)",
            "register": "POST /api/auth/register",
        },
        "note": "For /me and /signout, include Authorization: Bearer <token> header",
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
