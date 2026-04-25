"""
Authentication API Routes
Clean and simple authentication system for Pivota
"""

import logging
import asyncio
import json
from datetime import datetime
from textwrap import dedent
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr, field_validator

from config.settings import settings
from db.database import database
from utils.auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
    require_admin,
)
from utils.database_readiness import (
    DatabaseUnavailableError,
    database_unavailable_http_exception,
    ensure_database_ready,
)
from utils.transient_errors import is_asyncpg_busy_error

router = APIRouter(prefix="/api/auth", tags=["authentication"])
logger = logging.getLogger("auth_routes")

# Keep the historical demo merchant usable when a canonical users row exists
# without a merchant_id binding in older production databases.
DEMO_MERCHANT_IDS = {
    "merchant@test.com": "merch_6b90dc9838d5fd9c",
}

# Backward-compat shim for tests and historical imports.
# Some code/tests patch `routes.auth.require_admin_user`.
require_admin_user = require_admin


def _validate_password_strength(value: str) -> str:
    if len(value) < 8:
        raise ValueError('Password must be at least 8 characters long')
    if not any(c.isupper() for c in value):
        raise ValueError('Password must contain at least one uppercase letter')
    if not any(c.islower() for c in value):
        raise ValueError('Password must contain at least one lowercase letter')
    if not any(c.isdigit() for c in value):
        raise ValueError('Password must contain at least one digit')
    return value


def _validate_role_value(value: str) -> str:
    valid_roles = ['super_admin', 'admin', 'employee', 'outsourced', 'merchant', 'agent']
    if value not in valid_roles:
        raise ValueError(f'Invalid role. Must be one of: {", ".join(valid_roles)}')
    return value

# Request/Response Models
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    role: str = "employee"
    
    @field_validator('password')
    @classmethod
    def validate_password(cls, value: str) -> str:
        return _validate_password_strength(value)
    
    @field_validator('role')
    @classmethod
    def validate_role(cls, value: str) -> str:
        return _validate_role_value(value)

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class LoginResponse(BaseModel):
    success: bool
    token: str
    user: dict

class UserResponse(BaseModel):
    success: bool
    user: dict

class MessageResponse(BaseModel):
    success: bool
    message: str


def _normalize_email(raw_email: str) -> str:
    return (raw_email or "").strip().lower()


DEMO_EMPLOYEE_ACCOUNTS = {
    "employee@pivota.com": {
        "password": "Admin123!",
        "role": "admin",
        "full_name": "Pivota Employee",
    },
    "superadmin@pivota.com": {
        "password": "admin123",
        "role": "admin",
        "full_name": "Pivota Super Admin",
    },
}

_AUTH_DB_TIMEOUT_SECONDS = 5.0


async def _ensure_auth_database_ready() -> None:
    try:
        await ensure_database_ready(
            connect_timeout_seconds=2.0,
            probe_timeout_seconds=2.0,
            disconnect_timeout_seconds=1.0,
        )
    except DatabaseUnavailableError as exc:
        logger.warning(
            "[Auth] Database readiness failed phase=%s error=%s",
            exc.phase,
            exc.error_type,
        )
        raise database_unavailable_http_exception(retry_after_seconds=2) from exc


async def _auth_fetch_one(query: str, values: dict):
    for attempt in range(2):
        try:
            return await asyncio.wait_for(
                database.fetch_one(query=query, values=values),
                timeout=_AUTH_DB_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise database_unavailable_http_exception(retry_after_seconds=2) from exc
        except Exception as exc:
            if attempt == 0 and is_asyncpg_busy_error(exc):
                await _ensure_auth_database_ready()
                continue
            raise


async def _auth_execute(query: str, values: dict):
    for attempt in range(2):
        try:
            return await asyncio.wait_for(
                database.execute(query=query, values=values),
                timeout=_AUTH_DB_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise database_unavailable_http_exception(retry_after_seconds=2) from exc
        except Exception as exc:
            if attempt == 0 and is_asyncpg_busy_error(exc):
                await _ensure_auth_database_ready()
                continue
            raise


def _build_employee_login_response(
    *,
    user_id: str,
    email: str,
    full_name: str,
    role: str,
    employee_id: Optional[str] = None,
    permissions: Optional[list] = None,
) -> LoginResponse:
    token_data = {
        "sub": email,
        "user_id": user_id,
        "email": email,
        "role": role,
    }
    if employee_id:
        token_data["employee_id"] = employee_id
    if permissions:
        token_data["permissions"] = permissions

    user_response = {
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "role": role,
    }
    if employee_id:
        user_response["employee_id"] = employee_id
    if permissions:
        user_response["permissions"] = permissions

    return LoginResponse(
        success=True,
        token=create_access_token(token_data),
        user=user_response,
    )


async def _legacy_employee_login_response(normalized_email: str, password: str) -> Optional[LoginResponse]:
    try:
        employee = await _auth_fetch_one(
            """
                SELECT employee_id, name, email, password, role
                FROM employees
                WHERE email = :email AND status = 'active'
            """,
            {"email": normalized_email},
        )
    except Exception as exc:
        if "employees" not in str(exc):
            raise
        employee = None

    if employee:
        import hashlib

        employee_row = dict(employee)
        salt = "pivota_employee_salt_v1"
        hashed_input = hashlib.sha256(f"{password}{salt}".encode()).hexdigest()
        if employee_row.get("password") and hashed_input == employee_row["password"]:
            return _build_employee_login_response(
                user_id=str(employee_row["employee_id"]),
                employee_id=str(employee_row["employee_id"]),
                email=str(employee_row["email"]),
                full_name=str(employee_row.get("name") or employee_row["email"]),
                role=str(employee_row["role"]),
            )

    demo = DEMO_EMPLOYEE_ACCOUNTS.get(normalized_email)
    if demo and password == demo["password"]:
        return _build_employee_login_response(
            user_id=normalized_email,
            email=normalized_email,
            full_name=demo["full_name"],
            role=demo["role"],
        )

    return None


def _send_reset_password_email(email: str, reset_link: str) -> None:
    """
    Best-effort email sender for password reset links.

    Uses Amazon SES by default (see utils.email_sender). Failures are logged but never
    propagated to the caller (to avoid leaking account existence).
    """
    from_email = getattr(settings, "from_email", "noreply@pivota.ai")

    subject = "Reset your Pivota password"
    text_content = (
        "You requested to reset your password.\n\n"
        f"Click the link below to choose a new password:\n{reset_link}\n\n"
        "This link will expire in 1 hour. "
        "If you did not request a password reset, you can ignore this email."
    )
    html_content = dedent(
        f"""
        <p>You requested to reset your password.</p>
        <p>
          Click the link below to choose a new password:<br/>
          <a href="{reset_link}">{reset_link}</a>
        </p>
        <p>This link will expire in 1 hour.</p>
        <p>If you did not request a password reset, you can ignore this email.</p>
        """
    ).strip()

    try:
        from utils.email_sender import send_email, mask_email

        res = send_email(
            to_email=email,
            subject=subject,
            text_body=text_content,
            html_body=html_content,
            from_email=from_email,
            from_name="Pivota",
            tags={"type": "reset_password"},
        )
        if not getattr(res, "ok", False):
            logger.warning(
                "[Auth] Reset-password email delivery failed provider=%s error=%s to=%s",
                getattr(res, "provider", None),
                getattr(res, "error", None),
                mask_email(email),
            )
    except Exception as exc:
        logger.warning("[Auth] Reset-password email send raised error=%s", type(exc).__name__)

@router.post("/register", response_model=MessageResponse)
async def register(data: RegisterRequest):
    """
    Register a new user
    
    - **email**: Valid email address
    - **password**: At least 8 characters, with uppercase, lowercase, and digit
    - **full_name**: Optional full name
    - **role**: super_admin, admin, employee, outsourced, merchant, or agent (default: employee)
    """
    try:
        normalized_email = _normalize_email(data.email)
        # Check if user already exists
        query = "SELECT id FROM users WHERE email = :email"
        existing_user = await database.fetch_one(query=query, values={"email": normalized_email})
        
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # Hash password
        password_hash = hash_password(data.password)
        
        # Insert user
        query = """
            INSERT INTO users (email, password_hash, full_name, role)
            VALUES (:email, :password_hash, :full_name, :role)
            RETURNING id
        """
        values = {
            "email": normalized_email,
            "password_hash": password_hash,
            "full_name": data.full_name or normalized_email.split('@')[0],
            "role": data.role
        }
        
        user_id = await database.fetch_val(query=query, values=values)
        
        return MessageResponse(
            success=True,
            message=f"User registered successfully with role: {data.role}"
        )
        
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Registration failed: {str(e)}"
        )

@router.post("/login", response_model=LoginResponse)
async def login(data: LoginRequest):
    """
    Login with email and password
    
    - **email**: User's email
    - **password**: User's password
    
    Returns JWT token and user information
    """
    try:
        await _ensure_auth_database_ready()
        normalized_email = _normalize_email(data.email)
        # Find user by email
        query = """
            SELECT id, email, password_hash, full_name, role, active, merchant_id
            FROM users
            WHERE email = :email
        """
        user = await _auth_fetch_one(query, {"email": normalized_email})
        
        if not user:
            legacy_response = await _legacy_employee_login_response(normalized_email, data.password)
            if legacy_response is not None:
                return legacy_response
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        
        # Check if user is active
        if not user['active']:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account has been deactivated"
            )
        
        # Verify password
        if not verify_password(data.password, user['password_hash']):
            legacy_response = await _legacy_employee_login_response(normalized_email, data.password)
            if legacy_response is not None:
                return legacy_response
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
        
        # Update last login
        update_query = """
            UPDATE users
            SET last_login = :last_login
            WHERE id = :user_id
        """
        await _auth_execute(update_query, {"last_login": datetime.utcnow(), "user_id": user['id']})
        
        # For merchants, resolve their merchant_id
        # Prefer the explicit binding from users.merchant_id; fall back to
        # merchant_onboarding.contact_email only when not set.
        merchant_id = user["merchant_id"]
        if user['role'] == 'merchant' and not merchant_id:
            merchant_record = await _auth_fetch_one(
                "SELECT merchant_id FROM merchant_onboarding WHERE contact_email = :email",
                {"email": user['email']}
            )
            if merchant_record:
                merchant_id = merchant_record['merchant_id']
        if user['role'] == 'merchant' and not merchant_id:
            merchant_id = DEMO_MERCHANT_IDS.get(normalized_email)
        
        # [Phase 6.2] For agents, get their agent_id from agents table
        agent_id = None
        if user['role'] == 'agent':
            try:
                agent_record = await _auth_fetch_one(
                    "SELECT agent_id FROM agents WHERE email = :email LIMIT 1",
                    {"email": user['email']}
                )
            except Exception as e:
                # Backward compatibility: older deployments had an `agents` table
                # without an `email` column. Add it non-destructively and retry.
                if 'column "email" does not exist' in str(e):
                    try:
                        await _auth_execute(
                            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS email VARCHAR(255)",
                            {},
                        )
                        agent_record = await _auth_fetch_one(
                            "SELECT agent_id FROM agents WHERE email = :email LIMIT 1",
                            {"email": user['email']},
                        )
                    except Exception:
                        agent_record = None
                else:
                    raise
            if agent_record:
                agent_id = agent_record['agent_id']

        employee_id = None
        employee_permissions: list = []
        if user['role'] in ['super_admin', 'admin', 'employee', 'outsourced']:
            try:
                employee_record = await _auth_fetch_one(
                    "SELECT employee_id, permissions FROM employees WHERE email = :email",
                    {"email": user['email']}
                )
                if employee_record:
                    employee_row = dict(employee_record)
                    employee_id = employee_row.get('employee_id')
                    raw_permissions = employee_row.get('permissions')
                    if raw_permissions is None:
                        employee_permissions = []
                    elif isinstance(raw_permissions, list):
                        employee_permissions = raw_permissions
                    elif isinstance(raw_permissions, str):
                        try:
                            employee_permissions = json.loads(raw_permissions)
                        except Exception:
                            employee_permissions = []
                    else:
                        try:
                            employee_permissions = list(raw_permissions)
                        except Exception:
                            employee_permissions = []
            except Exception as e:
                if 'permissions' in str(e) or 'employees' in str(e):
                    try:
                        employee_record = await _auth_fetch_one(
                            "SELECT employee_id FROM employees WHERE email = :email",
                            {"email": user['email']}
                        )
                        if employee_record:
                            employee_id = dict(employee_record).get('employee_id')
                    except Exception:
                        pass
                else:
                    raise

        # Create JWT token
        token_data = {
            "sub": user['email'],
            "user_id": str(user['id']),
            "email": user['email'],
            "role": user['role']
        }
        if merchant_id:
            token_data["merchant_id"] = merchant_id
        if agent_id:
            token_data["agent_id"] = agent_id
        if employee_id:
            token_data["employee_id"] = employee_id
        if employee_permissions:
            token_data["permissions"] = employee_permissions
        
        token = create_access_token(token_data)
        
        user_response = {
            "id": str(user['id']),
            "email": user['email'],
            "full_name": user['full_name'],
            "role": user['role']
        }
        if merchant_id:
            user_response["merchant_id"] = merchant_id
        if agent_id:
            user_response["agent_id"] = agent_id
        if employee_id:
            user_response["employee_id"] = employee_id
        if employee_permissions:
            user_response["permissions"] = employee_permissions
        
        return LoginResponse(
            success=True,
            token=token,
            user=user_response
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login failed: {str(e)}"
        )

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)):
    """
    Get current user information
    
    Requires Authorization header with Bearer token
    """
    try:
        # Fetch fresh user data from database
        # Tokens in this system use the email as the primary subject (`sub`)
        # so we look up the user record by email instead of a numeric ID.
        query = """
            SELECT id, email, full_name, role, created_at, last_login
            FROM users
            WHERE email = :email AND active = true
        """
        user = await database.fetch_one(
            query=query,
            values={"email": current_user["email"]}
        )
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        return UserResponse(
            success=True,
            user={
                "id": str(user['id']),
                "email": user['email'],
                "full_name": user['full_name'],
                "role": user['role'],
                "created_at": user['created_at'].isoformat() if user['created_at'] else None,
                "last_login": user['last_login'].isoformat() if user['last_login'] else None
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch user: {str(e)}"
        )

@router.post("/logout", response_model=MessageResponse)
async def logout(current_user: dict = Depends(get_current_user)):
    """
    Logout (client-side token removal)
    
    In a JWT system, logout is primarily handled client-side by removing the token.
    This endpoint exists for consistency and future token blacklisting if needed.
    """
    return MessageResponse(
        success=True,
        message="Logged out successfully"
    )

@router.get("/test")
async def test_auth():
    """Test if auth API is accessible"""
    return {
        "success": True,
        "message": "Authentication API is running",
        "endpoints": {
            "register": "POST /api/auth/register",
            "login": "POST /api/auth/login",
            "me": "GET /api/auth/me (requires Authorization header)",
            "logout": "POST /api/auth/logout (requires Authorization header)"
        },
        "test_credentials": {
            "super_admin": {
                "email": "superadmin@pivota.com",
                "password": "Admin123!",
                "role": "super_admin"
            },
            "admin": {
                "email": "admin@pivota.com",
                "password": "Admin123!",
                "role": "admin"
            },
            "employee": {
                "email": "employee@pivota.com",
                "password": "Admin123!",
                "role": "employee"
            },
            "outsourced": {
                "email": "outsourced@pivota.com",
                "password": "Admin123!",
                "role": "outsourced"
            },
            "merchant": {
                "email": "merchant@test.com",
                "password": "Admin123!",
                "role": "merchant"
            },
            "agent": {
                "email": "agent@test.com",
                "password": "Admin123!",
                "role": "agent"
            }
        },
        "employee_roles": {
            "super_admin": "Complete control over the system",
            "admin": "Manage merchants, agents, and most settings",
            "employee": "View and basic operations",
            "outsourced": "Limited read-only access"
        }
    }


# ============================================================================
# Password Management
# ============================================================================

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    
    @field_validator('new_password')
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_password_strength(value)

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str
    
    @field_validator('new_password')
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_password_strength(value)


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    data: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Change password for authenticated user
    Requires current password for verification
    """
    try:
        # Get user from database
        user = await database.fetch_one(
            # Some legacy deployments used `user_id`; the canonical column is `id`
            # but we only need the email and password hash here.
            "SELECT email, password_hash FROM users WHERE email = :email",
            {"email": current_user["email"]}
        )
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Verify current password
        if not verify_password(data.current_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        
        # Hash new password
        new_password_hash = hash_password(data.new_password)
        
        # Update password
        await database.execute(
            "UPDATE users SET password_hash = :password_hash WHERE email = :email",
            {"password_hash": new_password_hash, "email": current_user["email"]}
        )
        
        return MessageResponse(
            success=True,
            message="Password changed successfully"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to change password: {str(e)}"
        )


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(data: ForgotPasswordRequest):
    """
    Request password reset
    Generates a reset token and stores it in database
    """
    try:
        normalized_email = _normalize_email(data.email)
        import secrets
        from datetime import timedelta
        
        # Check if user exists
        # Some deployments use `id` instead of `user_id`, but for this flow
        # we only need to know whether an email exists, so select email only
        user = await database.fetch_one(
            "SELECT email, role FROM users WHERE email = :email",
            {"email": normalized_email},
        )
        
        if not user:
            # Legacy backfill: if this email matches a merchant contact_email but
            # has no corresponding users row yet, create a login user on the fly
            merchant = await database.fetch_one(
                "SELECT merchant_id, business_name FROM merchant_onboarding WHERE contact_email = :email LIMIT 1",
                {"email": normalized_email},
            )

            if merchant:
                from utils.auth import hash_password
                import secrets

                password = secrets.token_urlsafe(12)
                password_hash = hash_password(password)

                await database.execute(
                    """
                    INSERT INTO users (email, password_hash, full_name, role, active, merchant_id)
                    VALUES (:email, :password_hash, :full_name, :role, :active, :merchant_id)
                    """,
                    {
                        "email": normalized_email,
                        "password_hash": password_hash,
                        "full_name": merchant["business_name"] or normalized_email.split("@")[0],
                        "role": "merchant",
                        "active": True,
                        "merchant_id": merchant["merchant_id"],
                    },
                )
                logger.info(
                    "[Auth] Auto-created merchant user for %s to support password reset",
                    normalized_email,
                )
            else:
                # Don't reveal if email exists or not (security best practice)
                return MessageResponse(
                    success=True,
                    message="If the email exists, a password reset link has been sent",
                )
        
        # Generate reset token (valid for 1 hour)
        reset_token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=1)
        
        # Store reset token in database
        # First, create password_reset_tokens table if needed
        try:
            await database.execute("""
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    token VARCHAR(255) PRIMARY KEY,
                    user_email VARCHAR(255) NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    used BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        except:
            pass  # Table might already exist
        
        await database.execute(
            """
            INSERT INTO password_reset_tokens (token, user_email, expires_at)
            VALUES (:token, :email, :expires_at)
            """,
            {"token": reset_token, "email": normalized_email, "expires_at": expires_at}
        )
        
        # Build reset link. Choose portal base URL based on user role (if known).
        base_url = getattr(settings, "merchant_portal_base_url", "https://merchant.pivota.cc").rstrip("/")
        try:
            role = user["role"] if user else None
        except Exception:
            role = None

        if role in {"super_admin", "admin", "employee", "outsourced"}:
            base_url = getattr(settings, "employee_portal_base_url", "https://employee.pivota.cc").rstrip("/")
        elif role == "agent":
            base_url = getattr(settings, "agent_portal_base_url", "https://developer.pivota.cc").rstrip("/")

        reset_link = f"{base_url}/reset-password?token={reset_token}"

        # Best-effort email delivery; failures are logged only
        _send_reset_password_email(normalized_email, reset_link)
        
        return MessageResponse(
            success=True,
            message="If the email exists, a password reset link has been sent"
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process password reset: {str(e)}"
        )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(data: ResetPasswordRequest):
    """
    Reset password using token from forgot-password flow
    """
    try:
        # Verify token exists and is valid
        token_record = await database.fetch_one(
            """
            SELECT token, user_email, expires_at, used 
            FROM password_reset_tokens 
            WHERE token = :token
            """,
            {"token": data.token}
        )
        
        if not token_record:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        
        if token_record["used"]:
            raise HTTPException(status_code=400, detail="Reset token has already been used")
        
        if datetime.utcnow() > token_record["expires_at"]:
            raise HTTPException(status_code=400, detail="Reset token has expired")
        
        # Hash new password
        new_password_hash = hash_password(data.new_password)
        
        # Update user password
        await database.execute(
            "UPDATE users SET password_hash = :password_hash WHERE email = :email",
            {"password_hash": new_password_hash, "email": token_record["user_email"]}
        )
        
        # Mark token as used
        await database.execute(
            "UPDATE password_reset_tokens SET used = TRUE WHERE token = :token",
            {"token": data.token}
        )
        
        return MessageResponse(
            success=True,
            message="Password reset successfully. You can now login with your new password."
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reset password: {str(e)}"
        )
