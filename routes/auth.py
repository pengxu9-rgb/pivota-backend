"""
Authentication API Routes
Clean and simple authentication system for Pivota
"""

import logging
import asyncio
import json
import os
import hashlib
from datetime import datetime
from textwrap import dedent
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, field_validator

from config.settings import settings
from db.auth_identity import (
    PORTAL_TO_AUDIENCE,
    PORTAL_TO_MEMBERSHIP_TYPE,
    ensure_identity,
    get_active_membership_by_email,
    record_identity_event,
    upsert_credential_hash,
    upsert_membership,
    verify_identity_password,
)
from db.database import database
from utils.auth import (
    ADMIN_ROLES,
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
    optional_security,
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
EMPLOYEE_AUTH_ROLES = {"super_admin", "admin", "employee", "outsourced"}
SUPPORTED_LOGIN_PORTALS = set(PORTAL_TO_MEMBERSHIP_TYPE)

DEMO_MERCHANT_IDS = {
    "merchant@test.com": os.getenv("DEMO_MERCHANT_ID", "").strip(),
} if settings.enable_internal_demo_fixtures and os.getenv("DEMO_MERCHANT_ID", "").strip() else {}

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


# Roles a caller is allowed to hand themselves at registration time. Everything
# else in `_validate_role_value` is privileged -- `admin`/`super_admin` satisfy
# `ADMIN_ROLES` and `require_admin`, and `employee`/`outsourced` satisfy
# `require_employee` -- so those may only be granted by an authenticated admin.
# `/api/auth/register` is mounted unauthenticated, so without this split an
# anonymous caller could persist themselves an admin row and then log in.
SELF_SERVICE_REGISTRATION_ROLES = frozenset({"merchant", "agent"})


async def _optional_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
) -> Optional[Dict[str, Any]]:
    """Resolve the caller when a Bearer token is present; None when anonymous.

    A non-empty token that is presented must still be valid -- an unparseable or
    expired one raises 401 rather than silently degrading to the anonymous path.
    An EMPTY credential (`Authorization: Bearer `) is not a token at all:
    `HTTPBearer(auto_error=False)` yields None for it, so it takes the anonymous
    path. That is safe, because anonymous is the least-privileged caller here
    and still cannot self-assign a privileged role.
    """
    if credentials is None:
        return None
    return await get_current_user(credentials)


def _authorize_requested_role(role: str, caller: Optional[Dict[str, Any]]) -> None:
    """Reject any privileged role that the caller is not an admin to grant."""
    if role in SELF_SERVICE_REGISTRATION_ROLES:
        return
    if (caller or {}).get("role") in ADMIN_ROLES:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"Role '{role}' may only be granted by an authenticated admin. "
            "Self-service registration accepts: "
            + ", ".join(sorted(SELF_SERVICE_REGISTRATION_ROLES))
            + "."
        ),
    )

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
    portal: Optional[str] = None

    @field_validator('portal')
    @classmethod
    def validate_portal(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        portal = value.strip().lower()
        if not portal:
            return None
        if portal not in SUPPORTED_LOGIN_PORTALS:
            raise ValueError(f'Invalid portal. Must be one of: {", ".join(sorted(SUPPORTED_LOGIN_PORTALS))}')
        return portal

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


def _record_to_dict(record: Any) -> Optional[dict]:
    if not record:
        return None
    if isinstance(record, dict):
        return dict(record)
    try:
        return dict(record)
    except Exception:
        return {key: record[key] for key in getattr(record, "keys", lambda: [])()}


def _normalize_permissions(raw_permissions: Any) -> list:
    if raw_permissions is None:
        return []
    if isinstance(raw_permissions, list):
        return raw_permissions
    if isinstance(raw_permissions, str):
        try:
            parsed = json.loads(raw_permissions)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    try:
        return list(raw_permissions)
    except Exception:
        return []


async def _fetch_active_employee_identity(email: str) -> Optional[dict]:
    try:
        row = await _auth_fetch_one(
            """
            SELECT employee_id, name, email, password, role, status, permissions
            FROM employees
            WHERE LOWER(email) = LOWER(:email)
              AND COALESCE(status, 'active') = 'active'
            LIMIT 1
            """,
            {"email": email},
        )
    except Exception as exc:
        if "permissions" not in str(exc) and "employees" not in str(exc):
            raise
        try:
            row = await _auth_fetch_one(
                """
                SELECT employee_id, name, email, password, role, status
                FROM employees
                WHERE LOWER(email) = LOWER(:email)
                  AND COALESCE(status, 'active') = 'active'
                LIMIT 1
                """,
                {"email": email},
            )
        except Exception:
            return None

    employee = _record_to_dict(row)
    if not employee:
        return None
    employee["role"] = (employee.get("role") or "employee").strip().lower()
    if employee["role"] not in EMPLOYEE_AUTH_ROLES:
        return None
    employee["permissions"] = _normalize_permissions(employee.get("permissions"))
    return employee


def _verify_legacy_employee_password(plain_password: str, employee: Optional[dict]) -> bool:
    if not employee or not employee.get("password"):
        return False
    legacy_hash = hashlib.sha256(f"{plain_password}pivota_employee_salt_v1".encode()).hexdigest()
    return legacy_hash == employee.get("password")


async def _safe_verify_identity_password(email: str, plain_password: str) -> Optional[dict]:
    try:
        return await verify_identity_password(email, plain_password)
    except Exception as exc:
        logger.warning("[Auth] Canonical credential lookup skipped: %s", exc)
        return None


async def _safe_get_active_membership(email: str, membership_type: str) -> Optional[dict]:
    try:
        return await get_active_membership_by_email(
            email=email,
            membership_type=membership_type,
        )
    except Exception as exc:
        logger.warning("[Auth] Canonical membership lookup skipped: %s", exc)
        return None


async def _safe_upsert_membership(**kwargs) -> Optional[dict]:
    try:
        return await upsert_membership(**kwargs)
    except Exception as exc:
        logger.warning("[Auth] Canonical membership sync skipped: %s", exc)
        return None


async def _safe_upsert_credential_hash(
    *,
    identity_id: str,
    password_hash: str,
    source: str,
) -> None:
    try:
        await upsert_credential_hash(
            identity_id=identity_id,
            password_hash=password_hash,
            source=source,
        )
    except Exception as exc:
        logger.warning("[Auth] Canonical credential sync skipped: %s", exc)


async def _safe_ensure_identity(
    *,
    email: str,
    full_name: Optional[str] = None,
    status: str = "active",
) -> Optional[dict]:
    try:
        return await ensure_identity(email=email, full_name=full_name, status=status)
    except Exception as exc:
        logger.warning("[Auth] Canonical identity sync skipped: %s", exc)
        return None


async def _safe_record_identity_event(**kwargs) -> None:
    try:
        await record_identity_event(**kwargs)
    except Exception:
        pass


async def _resolve_agent_id_for_email(email: str) -> Optional[str]:
    try:
        row = await _auth_fetch_one(
            """
            SELECT agent_id
            FROM agents
            WHERE LOWER(COALESCE(email, owner_email, '')) = LOWER(:email)
            LIMIT 1
            """,
            {"email": email},
        )
    except Exception:
        try:
            row = await _auth_fetch_one(
                """
                SELECT agent_id
                FROM agents
                WHERE LOWER(COALESCE(owner_email, '')) = LOWER(:email)
                LIMIT 1
                """,
                {"email": email},
            )
        except Exception:
            row = None
    if not row:
        return None
    try:
        return str(row["agent_id"])
    except Exception:
        return None


async def _resolve_merchant_id_for_user(user: Optional[dict], email: str) -> Optional[str]:
    merchant_id = user.get("merchant_id") if user else None
    if merchant_id:
        return str(merchant_id)
    try:
        merchant_record = await _auth_fetch_one(
            """
            SELECT merchant_id
            FROM merchant_onboarding
            WHERE LOWER(contact_email) = LOWER(:email)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"email": email},
        )
    except Exception:
        merchant_record = None
    if merchant_record:
        return str(merchant_record["merchant_id"])
    return DEMO_MERCHANT_IDS.get(email)


def _fallback_membership(
    *,
    email: str,
    membership_type: str,
    role: str,
    entity_id: str,
    full_name: Optional[str],
    permissions: Optional[list] = None,
) -> dict:
    identity_id = f"legacy:{email}"
    return {
        "membership_id": f"legacy:{membership_type}:{entity_id}",
        "identity_id": identity_id,
        "membership_type": membership_type,
        "role": role,
        "status": "active",
        "entity_id": str(entity_id),
        "permissions": permissions or [],
        "identity": {
            "identity_id": identity_id,
            "email": email,
            "email_normalized": email,
            "full_name": full_name,
            "status": "active",
        },
    }


def _claims_for_membership(membership: dict) -> dict:
    membership_type = membership.get("membership_type")
    entity_id = str(membership.get("entity_id") or "")
    claims = {
        "membership_id": membership.get("membership_id"),
        "membership_type": membership_type,
        "role": membership.get("role"),
        "aud": PORTAL_TO_AUDIENCE.get(membership_type, membership_type),
        "scope": membership_type,
    }
    if membership_type == "employee":
        claims["employee_id"] = entity_id
    elif membership_type == "merchant":
        claims["merchant_id"] = entity_id
    elif membership_type == "agent":
        claims["agent_id"] = entity_id
    permissions = _normalize_permissions(membership.get("permissions"))
    if permissions:
        claims["permissions"] = permissions
    return claims


async def _sync_employee_auth_user(
    *,
    employee: dict,
    plain_password: Optional[str] = None,
) -> None:
    password_hash = hash_password(plain_password) if plain_password else None
    values = {
        "email": _normalize_email(employee["email"]),
        "password_hash": password_hash,
        "full_name": employee.get("name") or _normalize_email(employee["email"]).split("@")[0],
        "role": employee["role"],
    }
    if not password_hash:
        await _auth_execute(
            """
            UPDATE users
            SET full_name = :full_name,
                role = :role,
                active = TRUE,
                merchant_id = NULL
            WHERE LOWER(email) = LOWER(:email)
            """,
            values,
        )
    else:
        await _auth_execute(
            """
            INSERT INTO users (email, password_hash, full_name, role, active, merchant_id)
            VALUES (:email, :password_hash, :full_name, :role, TRUE, NULL)
            ON CONFLICT (email) DO UPDATE SET
                password_hash = COALESCE(:password_hash, users.password_hash),
                full_name = EXCLUDED.full_name,
                role = EXCLUDED.role,
                active = TRUE,
                merchant_id = NULL
            """,
            values,
        )

    await _safe_upsert_membership(
        email=values["email"],
        membership_type="employee",
        role=employee["role"],
        entity_id=str(employee.get("employee_id") or ""),
        status="active",
        permissions=employee.get("permissions"),
        full_name=values["full_name"],
        password_hash=password_hash,
        credential_source="employee_login" if password_hash else None,
        source="employees_login_sync",
    )


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


def _portal_reset_base_url(portal: Optional[str]) -> Optional[str]:
    """Reset-link base URL for an explicitly-requested portal, or None.

    A single email can be both an employee and a merchant, so the portal the
    request actually came from is the only reliable signal for where to send
    the reset link. Returns None when no (or an unknown) portal is supplied so
    the caller can fall back to legacy role-based inference.
    """
    if portal == "employee":
        return getattr(settings, "employee_portal_base_url", "https://employee.pivota.cc")
    if portal == "agent":
        return getattr(settings, "agent_portal_base_url", "https://developer.pivota.cc")
    if portal == "merchant":
        return getattr(settings, "merchant_portal_base_url", "https://merchant.pivota.cc")
    return None


async def _ensure_password_reset_tokens_table() -> None:
    """Create the reset-token table if missing and ensure the portal column.

    Idempotent; safe to call on every forgot/reset request. The portal column
    records which portal minted a token so the reset can be scoped to an active
    membership for that portal.
    """
    try:
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token VARCHAR(255) PRIMARY KEY,
                user_email VARCHAR(255) NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    except Exception:
        pass  # Table might already exist
    try:
        await database.execute(
            "ALTER TABLE password_reset_tokens ADD COLUMN IF NOT EXISTS portal VARCHAR(32)"
        )
    except Exception:
        pass  # Column might already exist


async def _email_has_portal_membership(
    portal: str,
    email: str,
    user: Optional[dict],
    employee_identity: Optional[dict],
) -> bool:
    """Whether `email` has an active membership for `portal`.

    Mirrors the signals the login flow trusts (canonical membership rows plus
    the legacy employees / users.role / merchant_onboarding fallbacks) so the
    reset flow can neither issue nor redeem a link for a portal the account
    does not belong to.
    """
    membership_type = PORTAL_TO_MEMBERSHIP_TYPE.get(portal)
    if not membership_type:
        return False
    if await _safe_get_active_membership(email, membership_type):
        return True

    role = ((user or {}).get("role") or "").strip().lower()
    if membership_type == "employee":
        return bool(employee_identity) or role in EMPLOYEE_AUTH_ROLES
    if membership_type == "merchant":
        if role == "merchant":
            return True
        merchant = await database.fetch_one(
            "SELECT merchant_id FROM merchant_onboarding WHERE contact_email = :email LIMIT 1",
            {"email": email},
        )
        return bool(merchant)
    if membership_type == "agent":
        if role == "agent":
            return True
        return bool(await _resolve_agent_id_for_email(email))
    return False


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
async def register(
    data: RegisterRequest,
    caller: Optional[Dict[str, Any]] = Depends(_optional_current_user),
):
    """
    Register a new user

    - **email**: Valid email address
    - **password**: At least 8 characters, with uppercase, lowercase, and digit
    - **full_name**: Optional full name
    - **role**: `merchant` or `agent` for self-service registration. The
      privileged roles (`super_admin`, `admin`, `employee`, `outsourced`) --
      including the `employee` default -- require an admin Bearer token.
    """
    try:
        _authorize_requested_role(data.role, caller)
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
        identity = await _safe_ensure_identity(
            email=normalized_email,
            full_name=values["full_name"],
            status="active",
        )
        if identity:
            await _safe_upsert_credential_hash(
                identity_id=identity["identity_id"],
                password_hash=password_hash,
                source="api_auth_register",
            )
        
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
        requested_portal = data.portal
        requested_membership_type = (
            PORTAL_TO_MEMBERSHIP_TYPE[requested_portal]
            if requested_portal
            else None
        )
        employee_identity = await _fetch_active_employee_identity(normalized_email)
        canonical_identity = await _safe_verify_identity_password(normalized_email, data.password)

        # Find user by email
        query = """
            SELECT id, email, password_hash, full_name, role, active, merchant_id
            FROM users
            WHERE LOWER(email) = LOWER(:email)
            LIMIT 1
        """
        user = _record_to_dict(await _auth_fetch_one(query, {"email": normalized_email}))
        
        if not user and not employee_identity:
            legacy_response = await _legacy_employee_login_response(normalized_email, data.password)
            if legacy_response is not None and not requested_portal:
                return legacy_response
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )

        user_role = (user.get("role") if user else "") or ""
        user_role = user_role.strip().lower()
        verified_with_user_password = bool(
            user and verify_password(data.password, user.get("password_hash") or "")
        )
        verified_with_employee_password = (
            bool(employee_identity)
            and not verified_with_user_password
            and _verify_legacy_employee_password(data.password, employee_identity)
        )
        
        if not canonical_identity and not verified_with_user_password and not verified_with_employee_password:
            legacy_response = await _legacy_employee_login_response(normalized_email, data.password)
            if legacy_response is not None and not requested_portal:
                return legacy_response
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )

        # Check if user is active. An active employee row can repair a stale or
        # deactivated users row created by another portal.
        if user and not user['active'] and not employee_identity:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account has been deactivated"
            )

        if employee_identity:
            try:
                await _sync_employee_auth_user(
                    employee=employee_identity,
                    plain_password=data.password if (not user or verified_with_employee_password) else None,
                )
            except Exception as exc:
                logger.warning("[Auth] Failed to sync employee auth user: %s", exc)
            if verified_with_user_password and user and user.get("password_hash"):
                await _safe_upsert_membership(
                    email=normalized_email,
                    membership_type="employee",
                    role=employee_identity["role"],
                    entity_id=str(employee_identity.get("employee_id") or ""),
                    status="active",
                    permissions=employee_identity.get("permissions"),
                    full_name=employee_identity.get("name"),
                    password_hash=user["password_hash"],
                    credential_source="users_login",
                    source="employees_login_sync",
                )

        merchant_id = await _resolve_merchant_id_for_user(user, normalized_email)
        agent_id = await _resolve_agent_id_for_email(normalized_email)

        if canonical_identity and user and user.get("password_hash") and not verified_with_user_password:
            await _safe_upsert_credential_hash(
                identity_id=canonical_identity["identity_id"],
                password_hash=user["password_hash"],
                source="users_login_reconcile",
            )

        if verified_with_user_password and user and user.get("password_hash"):
            membership_type = user_role if user_role in {"merchant", "agent"} else "employee"
            entity_id = (
                employee_identity.get("employee_id")
                if user_role in EMPLOYEE_AUTH_ROLES and employee_identity
                else merchant_id if user_role == "merchant"
                else agent_id if user_role == "agent"
                else str(user.get("id"))
            )
            if entity_id:
                seeded = await _safe_upsert_membership(
                    email=normalized_email,
                    membership_type=membership_type,
                    role=user_role,
                    entity_id=str(entity_id),
                    status="active" if user.get("active") is not False else "inactive",
                    permissions=employee_identity.get("permissions") if employee_identity else None,
                    full_name=employee_identity.get("name") if employee_identity else user.get("full_name"),
                    password_hash=user["password_hash"],
                    credential_source="users_login",
                    source="users_login_sync",
                )
                if seeded:
                    canonical_identity = seeded.get("identity")

        if user_role == "merchant" and merchant_id:
            await _safe_upsert_membership(
                email=normalized_email,
                membership_type="merchant",
                role="merchant",
                entity_id=merchant_id,
                status="active" if user and user.get("active") is not False else "inactive",
                full_name=user.get("full_name") if user else None,
                source="users_login_sync",
            )
        elif user_role == "agent" and agent_id:
            await _safe_upsert_membership(
                email=normalized_email,
                membership_type="agent",
                role="agent",
                entity_id=agent_id,
                status="active" if user and user.get("active") is not False else "inactive",
                full_name=user.get("full_name") if user else None,
                source="users_login_sync",
            )

        # Update last login
        if user:
            update_query = """
                UPDATE users
                SET last_login = :last_login
                WHERE id = :user_id
            """
            await _auth_execute(update_query, {"last_login": datetime.utcnow(), "user_id": user['id']})

        inferred_membership_type = requested_membership_type
        if not inferred_membership_type:
            if employee_identity:
                inferred_membership_type = "employee"
            elif user_role in {"merchant", "agent"}:
                inferred_membership_type = user_role
            elif user_role in EMPLOYEE_AUTH_ROLES:
                inferred_membership_type = "employee"

        membership = None
        if inferred_membership_type:
            membership = await _safe_get_active_membership(
                normalized_email,
                inferred_membership_type,
            )

        if not membership and inferred_membership_type == "employee" and employee_identity:
            membership = _fallback_membership(
                email=normalized_email,
                membership_type="employee",
                role=employee_identity["role"],
                entity_id=str(employee_identity.get("employee_id") or ""),
                full_name=employee_identity.get("name"),
                permissions=employee_identity.get("permissions") or [],
            )
        elif not membership and inferred_membership_type == "merchant" and user_role == "merchant" and merchant_id:
            membership = _fallback_membership(
                email=normalized_email,
                membership_type="merchant",
                role="merchant",
                entity_id=merchant_id,
                full_name=user.get("full_name") if user else None,
            )
        elif not membership and inferred_membership_type == "agent" and user_role == "agent" and agent_id:
            membership = _fallback_membership(
                email=normalized_email,
                membership_type="agent",
                role="agent",
                entity_id=agent_id,
                full_name=user.get("full_name") if user else None,
            )

        if requested_portal and not membership:
            await _safe_record_identity_event(
                event_type="portal_membership_denied",
                email=normalized_email,
                details={"portal": requested_portal, "reason": "missing_active_membership"},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"No active {requested_portal} membership for this account",
            )

        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No active portal membership for this account",
            )

        identity = membership.get("identity") or {}
        identity_id = str(identity.get("identity_id") or f"legacy:{normalized_email}")
        effective_email = _normalize_email(identity.get("email") or normalized_email)
        effective_name = identity.get("full_name") or (
            employee_identity.get("name") if employee_identity else user.get("full_name") if user else None
        )
        membership_claims = _claims_for_membership(membership)

        # Create JWT token
        token_data = {
            "sub": identity_id,
            "identity_id": identity_id,
            "user_id": str(user['id']) if user else identity_id,
            "email": effective_email,
            "role": membership_claims["role"],
            **membership_claims,
        }

        token = create_access_token(token_data)
        
        user_response = {
            "id": identity_id,
            "identity_id": identity_id,
            "email": effective_email,
            "full_name": effective_name,
            "role": membership_claims["role"],
            "membership_type": membership_claims["membership_type"],
        }
        for key in ("employee_id", "merchant_id", "agent_id", "permissions", "membership_id"):
            if key in token_data and token_data[key]:
                user_response[key] = token_data[key]
        
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
    portal: Optional[str] = None

    @field_validator('portal')
    @classmethod
    def validate_portal(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        portal = value.strip().lower()
        if not portal:
            return None
        if portal not in SUPPORTED_LOGIN_PORTALS:
            raise ValueError(f'Invalid portal. Must be one of: {", ".join(sorted(SUPPORTED_LOGIN_PORTALS))}')
        return portal

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
        identity_id = current_user.get("identity_id")
        if not identity_id:
            identity = await _safe_ensure_identity(
                email=current_user["email"],
                full_name=current_user.get("full_name"),
            )
            identity_id = identity.get("identity_id") if identity else None
        if identity_id:
            await _safe_upsert_credential_hash(
                identity_id=identity_id,
                password_hash=new_password_hash,
                source="change_password",
            )

        employee_identity = await _fetch_active_employee_identity(current_user["email"])
        if employee_identity:
            legacy_password_hash = hashlib.sha256(
                f"{data.new_password}pivota_employee_salt_v1".encode()
            ).hexdigest()
            try:
                await database.execute(
                    "UPDATE employees SET password = :password WHERE LOWER(email) = LOWER(:email)",
                    {"password": legacy_password_hash, "email": current_user["email"]},
                )
            except Exception as exc:
                logger.warning("[Auth] Failed to sync employee legacy password: %s", exc)
        
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
        user = _record_to_dict(await database.fetch_one(
            "SELECT email, role FROM users WHERE email = :email",
            {"email": normalized_email},
        ))
        employee_identity = await _fetch_active_employee_identity(normalized_email)

        # Always return the same response so callers can't enumerate which
        # emails / portals exist.
        generic_response = MessageResponse(
            success=True,
            message="If the email exists, a password reset link has been sent",
        )

        # Membership guardrail: when the caller tells us which portal they're
        # resetting from, only proceed if the account actually has an active
        # membership for THAT portal. This stops reset links (and the merchant
        # backfill below) from being issued for a portal the account doesn't
        # belong to.
        if data.portal and not await _email_has_portal_membership(
            data.portal, normalized_email, user, employee_identity
        ):
            await _safe_record_identity_event(
                event_type="password_reset_denied",
                email=normalized_email,
                details={"portal": data.portal, "reason": "no_active_membership"},
            )
            return generic_response

        if not user:
            # Backfill a login user for a known employee so the reset has a row
            # to write. Scoped to employee resets (or legacy portal-less calls).
            if employee_identity and data.portal in (None, "employee"):
                try:
                    await _sync_employee_auth_user(
                        employee=employee_identity,
                        plain_password=secrets.token_urlsafe(12),
                    )
                    user = {
                        "email": normalized_email,
                        "role": employee_identity["role"],
                    }
                except Exception as exc:
                    logger.warning("[Auth] Failed to backfill employee user for reset: %s", exc)

            # Merchant bootstrap: a known merchant-onboarding contact with no
            # users row yet. Restricted to an explicit merchant-portal reset, so
            # forgot-password can no longer silently mint merchant accounts for
            # other (or unspecified) portals.
            if not user and data.portal == "merchant":
                merchant = await database.fetch_one(
                    "SELECT merchant_id, business_name FROM merchant_onboarding WHERE contact_email = :email LIMIT 1",
                    {"email": normalized_email},
                )
                if merchant:
                    from utils.auth import hash_password

                    password_hash = hash_password(secrets.token_urlsafe(12))
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
                    user = {
                        "email": normalized_email,
                        "role": "merchant",
                    }

            if not user:
                # Don't reveal if email exists or not (security best practice)
                return generic_response

        # Generate reset token (valid for 1 hour)
        reset_token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=1)
        
        # Store reset token in database, remembering which portal minted it.
        await _ensure_password_reset_tokens_table()
        await database.execute(
            """
            INSERT INTO password_reset_tokens (token, user_email, portal, expires_at)
            VALUES (:token, :email, :portal, :expires_at)
            """,
            {
                "token": reset_token,
                "email": normalized_email,
                "portal": data.portal,
                "expires_at": expires_at,
            },
        )
        
        # Build reset link. Prefer the portal the request actually came from;
        # only fall back to inferring it from the user's role for legacy callers
        # that don't send a portal. Role alone is wrong for dual-role accounts
        # (e.g. an employee who also has a merchant users row), which would
        # otherwise be routed to the merchant portal regardless of where they
        # asked to reset from.
        base_url = _portal_reset_base_url(data.portal)
        if not base_url:
            base_url = getattr(settings, "merchant_portal_base_url", "https://merchant.pivota.cc")
            try:
                role = user["role"] if user else None
            except Exception:
                role = None
            if role in {"super_admin", "admin", "employee", "outsourced"}:
                base_url = getattr(settings, "employee_portal_base_url", "https://employee.pivota.cc")
            elif role == "agent":
                base_url = getattr(settings, "agent_portal_base_url", "https://developer.pivota.cc")
        base_url = base_url.rstrip("/")

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
        await _ensure_password_reset_tokens_table()
        # Verify token exists and is valid
        token_record = await database.fetch_one(
            """
            SELECT token, user_email, portal, expires_at, used
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

        # Membership guardrail: a token minted for a specific portal may only be
        # redeemed while the account still holds an active membership for it.
        token_portal = (token_record["portal"] or "").strip().lower() or None
        if token_portal:
            token_user = _record_to_dict(await database.fetch_one(
                "SELECT email, role FROM users WHERE email = :email",
                {"email": token_record["user_email"]},
            ))
            token_employee = await _fetch_active_employee_identity(token_record["user_email"])
            if not await _email_has_portal_membership(
                token_portal, token_record["user_email"], token_user, token_employee
            ):
                raise HTTPException(
                    status_code=403,
                    detail=f"No active {token_portal} membership for this account",
                )

        # Hash new password
        new_password_hash = hash_password(data.new_password)
        
        # Update user password
        await database.execute(
            "UPDATE users SET password_hash = :password_hash WHERE email = :email",
            {"password_hash": new_password_hash, "email": token_record["user_email"]}
        )
        identity = await _safe_ensure_identity(
            email=token_record["user_email"],
        )
        if identity:
            await _safe_upsert_credential_hash(
                identity_id=identity["identity_id"],
                password_hash=new_password_hash,
                source="reset_password",
            )

        employee_identity = await _fetch_active_employee_identity(token_record["user_email"])
        if employee_identity:
            legacy_password_hash = hashlib.sha256(
                f"{data.new_password}pivota_employee_salt_v1".encode()
            ).hexdigest()
            try:
                await database.execute(
                    "UPDATE employees SET password = :password WHERE LOWER(email) = LOWER(:email)",
                    {"password": legacy_password_hash, "email": token_record["user_email"]},
                )
            except Exception as exc:
                logger.warning("[Auth] Failed to sync employee legacy reset password: %s", exc)
        
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
