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
import logging
import secrets
from datetime import datetime, timedelta
import os

# Database import for employee authentication
from config.platform import is_production
from db.database import database
from utils.auth import (
    ADMIN_ROLES,
    EMPLOYEE_STAFF_ROLES,
    get_current_user as shared_get_current_user,
    verify_password as verify_bcrypt_password,
)

router = APIRouter(prefix="/auth", tags=["authentication"])
security = HTTPBearer()
logger = logging.getLogger("auth_routes")

# JWT Configuration - Import from config for consistency
from config.settings import require_jwt_secret, settings
# See utils/auth.py: read at use, not at import.
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

def _demo_merchant_ids() -> Dict[str, str]:
    """Backfills merchant_id onto a real, password-verified users-table row
    that has none set. Same two conjuncts as the demo_accounts lane above:
    ENABLE_INTERNAL_DEMO_FIXTURES must be explicitly true, and the platform
    must not resolve to production (config.platform fails CLOSED to
    production on unlabeled managed hosts, and re-reads the environment on
    every call). Note settings.enable_internal_demo_fixtures itself is a
    field on the process-wide `settings` singleton resolved once at import,
    so flipping ENABLE_INTERNAL_DEMO_FIXTURES on a running process still
    requires a restart to take effect."""
    if not settings.enable_internal_demo_fixtures:
        return {}
    if is_production():
        logger.warning(
            "[Auth] ENABLE_INTERNAL_DEMO_FIXTURES is set but the environment "
            "resolves to production; demo merchant_id backfill stays disabled"
        )
        return {}
    demo_merchant_id = os.getenv("DEMO_MERCHANT_ID", "").strip()
    if not demo_merchant_id:
        return {}
    return {"merchant@test.com": demo_merchant_id}

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
    return jwt.encode(payload, require_jwt_secret(), algorithm=JWT_ALGORITHM)

async def verify_jwt_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    """Verify a bearer JWT and return the caller's claims.

    THIS IS A THIN ADAPTER OVER THE SHARED VALIDATOR, NOT A SECOND ONE, and
    that is the entire point of its existence.

    It used to call `jwt.decode()` itself, with no `audience=` and no
    `verify_aud` option. That is not the same as "does not check the
    audience": PyJWT's `_validate_aud` raises
    `InvalidAudienceError("Invalid audience")` -- an `InvalidTokenError`
    subclass, so it landed in the generic handler below -- whenever the token
    CARRIES an `aud` claim and the caller named none. Every token
    `/api/auth/login` mints carries one; `routes.auth._claims_for_membership`
    stamps `db.auth_identity.PORTAL_TO_AUDIENCE[membership_type]`
    (employee-portal / merchant-portal / agent-portal) onto all of them. So
    this function rejected, as malformed, every canonical portal token that
    `utils.auth.get_current_user` accepts -- and `utils.auth.decode_token`
    passes `options={"verify_aud": False}` precisely so it does not.

    Live consequence, observed 2026-09-05 on api.pivota.cc: a valid
    `super_admin` employee-portal token got
    `401 {"code":"UNAUTHORIZED","message":"Invalid token"}` from
    `GET /admin/cleanup/list-merchants`, while the same token answered 200 on
    routes wired to the shared validator. Nine route modules depend on this
    function (`admin_cleanup`, `admin_cleanup_rebuild`,
    `admin_cleanup_duplicates`, `admin_simple_fix`, `admin_migrations`,
    `admin_fix_merchant`, `init_orders_table`, `direct_db_check`,
    `psp_overview_routes`) plus `/auth/me` and `/auth/signout` here; all of
    them were reachable only with a legacy `/auth/signin` token, which is the
    one issuer that stamps no `aud`.

    Two validators that disagree about what a valid token is will always drift
    apart again, so this one no longer decides. `utils.auth.get_current_user`
    is the single answer; the only thing left here is the legacy RETURN SHAPE
    (`user_id` guaranteed, alongside the full claim set) that `/auth/me` and
    the nine modules above read.

    Audience is deliberately still not enforced, exactly as the shared
    validator does not enforce it. Binding a route to one portal is a
    different, useful control, but it belongs in one place for the whole app
    rather than being reintroduced here as a side effect -- which is how this
    defect happened. Until then, a merchant-portal token reaching an admin
    route is refused by the ROLE check (403), not mistaken for a forgery.
    """
    payload = await shared_get_current_user(credentials)

    # `get_current_user` requires sub/email/role. The legacy contract here is
    # user_id/role, and `sub` is what the canonical issuer fills for identity,
    # so fall back to it rather than 401-ing a token the rest of the app takes.
    user_id = payload.get("user_id") or payload.get("sub")
    role = payload.get("role")
    if not user_id or not role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

    return {**payload, "user_id": user_id, "role": role}

def require_admin(current_user: dict = Depends(verify_jwt_token)):
    """Require an admin role for access.

    `ADMIN_ROLES`, not the literal "admin", and that is a fix rather than a
    tidy-up: this was `current_user["role"] != "admin"`, which refused
    `super_admin` -- the MOST privileged role in the system, and one the
    employee portal issues (`routes.auth.EMPLOYEE_AUTH_ROLES`). Same defect
    #2031 fixed across 88 list-literal guards, in a spelling (`!= "admin"`)
    that the list-literal ratchet cannot match. `utils.auth.require_admin`,
    the shared equivalent, has always admitted both.

    Deliberately NOT widened to `EMPLOYEE_STAFF_ROLES`: the routes behind this
    dependency delete merchants, run migrations and reset system state.
    Admitting `super_admin` corrects an omission; admitting every staff role
    would be a new grant.
    """
    if current_user["role"] not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

def require_employee(current_user: dict = Depends(verify_jwt_token)):
    """Require employee or admin role for access"""
    if current_user["role"] not in EMPLOYEE_STAFF_ROLES:
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
      AND the platform environment does not resolve to production

    Every lane resolves the role from a datastore or a flag-gated fixture; no
    lane ever honours a role supplied by the caller.

    Preferred for real accounts: `POST /api/auth/login`.
    """
    try:
        normalized_email = normalize_email(login_data.email)
        demo_accounts = {}
        # Demo fixtures mint role=admin JWTs, so the flag alone is not enough:
        # the lane also refuses whenever the platform resolves to production
        # (config.platform fails CLOSED to production on unlabeled managed
        # hosts). Mirrors routes.auth._demo_employee_accounts().
        if settings.enable_internal_demo_fixtures and not is_production():
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
                merchant_id = _demo_merchant_ids().get(normalized_email)
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
        
        token = jwt.encode(token_payload, require_jwt_secret(), algorithm=JWT_ALGORITHM)
        
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
