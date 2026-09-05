"""
Authentication utilities for Pivota
Handles JWT token creation, validation, and user authentication
"""

from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Iterable, List
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import bcrypt
from config.platform import pytest_bypass_allowed
import logging

from config.settings import require_jwt_secret, settings
import os

# JWT Configuration
# No module-level JWT_SECRET. Binding it here read the secret at IMPORT, which
# is what dragged every importer — including batch jobs that never touch a
# token — into the strength check. require_jwt_secret() reads it at use.
JWT_ALGORITHM = "HS256"

logger = logging.getLogger("utils.auth")
ACCESS_TOKEN_EXPIRE_MINUTES = 1440  # 24 hours

# Security scheme
security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)

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
    
    encoded_jwt = jwt.encode(to_encode, require_jwt_secret(), algorithm=JWT_ALGORITHM)
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
        payload = jwt.decode(
            token,
            require_jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            options={"verify_aud": False},
        )
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

        # Test-only bypass: allow unit tests to use a stable placeholder token
        # without requiring JWT signing/secrets. Fails closed on ANY DEPLOYED
        # host — staging included — as well as anything resolving to
        # production; since #1900 the gate is `not (is_deployed() or
        # is_production())`, and since the Cloud Run **Jobs** markers were
        # added it covers job containers too. See
        # config.platform.pytest_bypass_allowed.
        if token == "test-token" and pytest_bypass_allowed(bypass_name="the test-token bypass"):
            return {
                "sub": "test-user",
                "email": "test@example.com",
                "role": "admin",
                "merchant_id": "test-merchant",
                "agent_id": "test-agent",
            }

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
    except RuntimeError:
        # require_jwt_secret() refuses on a weak secret, and its message names
        # the secret's exact byte length. The generic handler below echoed that
        # into a 401 body, so an anonymous request published how long the shared
        # signing key is. Logged, never returned.
        logger.error(
            "authentication unavailable: the JWT signing secret is not usable "
            "on this host", exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable",
        )
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
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


async def get_current_merchant(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> str:
    """Return the merchant_id from a merchant-role JWT.

    **Strict** — only reads the `merchant_id` claim. No email-based
    DB fallback. The login route (`routes/auth.py`) is responsible
    for resolving merchant_id at token-mint time and putting it in
    the JWT; if a token reaches us without the claim, the right fix
    is for the merchant to log out + log back in (or for the login
    route's resolution chain to be made more reliable), NOT to
    paper over with a per-request DB lookup.

    Raises:
      403 — token role isn't `merchant`
      401 — `merchant_id` claim missing (token is stale or login
            failed to resolve merchant_id; ask the merchant to
            log out + log back in to refresh the JWT)
    """
    if current_user.get("role") != "merchant":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Merchant access required",
        )
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Merchant ID missing from token. Log out and log back in "
                "to refresh your session."
            ),
        )
    return merchant_id


async def require_admin_or_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
    x_admin_key: Optional[str] = Header(None, alias="X-ADMIN-KEY"),
) -> Dict[str, Any]:
    """
    Allow either:
    - `X-ADMIN-KEY` header (ADMIN_API_KEY or PROMOTIONS_ADMIN_KEY), or
    - Bearer JWT with admin/super_admin role
    """
    expected_keys = [
        (os.getenv("ADMIN_API_KEY") or "").strip(),
        (os.getenv("PROMOTIONS_ADMIN_KEY") or "").strip(),
    ]
    expected_keys = [k for k in expected_keys if k]

    if x_admin_key and expected_keys and x_admin_key in expected_keys:
        return {"sub": "admin_key", "email": "admin_key@local", "role": "admin"}

    if credentials and credentials.credentials:
        # Reuse existing JWT decode logic.
        payload = decode_token(credentials.credentials)
        if payload.get("role") not in ["admin", "super_admin"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin access required",
            )
        return payload

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
    )


async def get_current_admin(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Alias for require_admin (backward compatibility)
    Require admin or super_admin role
    """
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


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
    membership_type = str(current_user.get("membership_type") or "").strip().lower()
    if membership_type and membership_type != "employee":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Employee access required"
        )

    if current_user.get("role") not in EMPLOYEE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Employee access required"
        )
    if membership_type == "employee" and not (
        current_user.get("employee_id") or current_user.get("employeeId")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Employee identity required"
        )
    return current_user


# ============================================================================
# ROLE & PERMISSION CHECKING
# ============================================================================

EMPLOYEE_ROLES = ["super_admin", "admin", "employee", "outsourced"]
ADMIN_ROLES = ["super_admin", "admin"]

# Staff-only employee-portal surfaces: everything in EMPLOYEE_ROLES except
# `outsourced`. Route guards used to spell this inline as ["employee", "admin"],
# which silently omitted `super_admin` -- the MOST privileged role, and one that
# `/auth/signin` happily issues (see routes.auth.EMPLOYEE_AUTH_ROLES). A
# super_admin could therefore sign into the employee portal and then be 403'd by
# 73 of the staff-only pages behind it (88 guards in total, once the
# merchant-inclusive variant below is counted). Guard against that spelling
# drift with this constant rather than another literal; `outsourced` stays out
# deliberately, so contractor access keeps whatever narrower scope each route
# already gave it.
EMPLOYEE_STAFF_ROLES = ["super_admin", "admin", "employee"]

# Same set, plus merchants -- for surfaces a merchant reaches for their own data
# and staff reach for anyone's. Was spelled ["merchant", "employee", "admin"],
# and carried the identical super_admin omission.
#
# "for their own data" is a description of the SURFACE, not a guarantee this
# constant provides. Membership here says only who may ATTEMPT the route; it
# admits every merchant, not the owning one. Any handler that then reads a
# merchant_id/store_id OUT OF THE REQUEST must pair this with
# can_access_merchant() -- otherwise merchant_A reaches merchant_B's row with a
# perfectly valid token. That pairing was missing on five routes shipped to
# prod (setup-psp overwrote another merchant's PSP credentials; the Wix
# connect/test/sync/sync-status routes hijacked and read another merchant's
# store), which is why this note exists. See
# routes/merchant_onboarding_shopify_verify_routes.py for the shape.
MERCHANT_OR_EMPLOYEE_STAFF_ROLES = ["merchant"] + EMPLOYEE_STAFF_ROLES

# Same set, plus agents -- for surfaces an agent reaches for their OWN record
# and staff reach for anyone's. The caller still has to prove ownership
# separately with can_access_agent(); this only decides who may attempt the
# route at all. "Separately" is load-bearing and was not happening on
# GET /agents/{agent_id}, which let any agent read any other agent's
# owner_email, webhook_url, allowed_merchants and quotas. Redacting a secret
# from the response is not an ownership check.
AGENT_OR_EMPLOYEE_STAFF_ROLES = ["agent"] + EMPLOYEE_STAFF_ROLES

# Same idea, but over the FULL employee set -- for surfaces `outsourced` staff
# already reach. can_access_agent() grants every EMPLOYEE_ROLES member blanket
# agent access, so a gate spelled with the STAFF variant would refuse
# `outsourced` a list its own ownership helper says it may read. Use this where
# the route is a roster read that outsourced staff have today (GET /agents/),
# and the STAFF variant where the narrower contractor scope is deliberate.
#
# The ownership rule is identical: this decides who may ATTEMPT the route.
# can_access_agent() decides WHOSE records come back -- and on a LIST route
# "whose" is a WHERE clause, not a 403. GET /agents/ had neither: it depended on
# get_current_user alone and selected every column of every row, which made the
# ownership check added to GET /agents/{agent_id} a no-op -- the same
# owner_email, webhook_url, allowed_merchants, metadata and quotas were one
# request away on the sibling route.
AGENT_OR_EMPLOYEE_ROLES = ["agent"] + EMPLOYEE_ROLES

# Permission guarding /api/operations/* (merchant & agent onboarding, approval,
# verification, API-key issuance, audit log). A named permission, not a role
# name — see the note in check_permission's permission_map.
MANAGE_OPERATIONS = "manage_operations"


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
    
    # Define permission mappings.
    #
    # NOTE: this map is keyed by ROLE and its values are PERMISSION strings.
    # Passing a role name (e.g. "operator") as `required_permission` matches
    # nothing here and silently denies — which is what every caller in
    # routes/operations_routes.py used to do. Permissions are the vocabulary;
    # add one here rather than passing a role name through.
    permission_map = {
        "employee": [
            "view_dashboard", "view_transactions", "view_merchants",
            "view_agents", MANAGE_OPERATIONS,
        ],
        # "operator" is a real role token elsewhere in the system (see
        # UserRole.OPERATOR in dashboard/core.py, the staff list at
        # validate_entity_access below, and realtime/metrics_store.py) but had
        # no entry here at all, so an operator held zero permissions.
        "operator": ["view_dashboard", "view_transactions", MANAGE_OPERATIONS],
        "merchant": ["view_own_orders", "view_own_transactions", "manage_own_products"],
        "agent": ["create_orders", "view_own_orders", "view_own_analytics"],
        # Deliberately NOT granted MANAGE_OPERATIONS: the operations routes
        # approve merchants and issue API keys.
        "outsourced": ["view_dashboard", "view_transactions"]
    }
    
    if not isinstance(role, str):
        # permission_map.get(role, []) raises TypeError on a list or dict, which
        # escaped require_permission as an unhandled 500 rather than a 403. It
        # denied either way, but a malformed claim is a refusal, not a crash.
        return False

    allowed_permissions = permission_map.get(role, [])
    return required_permission in allowed_permissions


def require_permission(user_info: Dict[str, Any], required_permission: str) -> None:
    """Raise 403 unless the caller holds `required_permission`.

    `check_permission` RETURNS a bool and never raises, so calling it as a bare
    statement authorizes nothing. Use this at route call sites; it is the only
    one of the two that is a guard.
    """
    if not check_permission(user_info, required_permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required permission: {required_permission}",
        )


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

    # A falsy target is never an answerable question, and both branches below
    # answered YES to it. `routes/auth.py` mints merchant tokens with an
    # Optional merchant_id, so a `merchant` token can carry merchant_id=None --
    # and `None == None` is True, handing that caller a target it proved
    # nothing about. The agent branch reaches its "no scoping claims means all
    # merchants" fallback and returns True for the same target. Every caller
    # today rejects an empty merchant_id before asking (they 400), so this
    # guards the next one rather than closing a live path.
    if not merchant_id:
        return False

    # Employees can access all merchants
    if role in EMPLOYEE_ROLES:
        return True
    
    # Merchants can only access their own data
    if role == "merchant":
        return user_info.get("merchant_id") == merchant_id
    
    # Agents can access their assigned merchants
    if role == "agent":
        scoped_values = (
            user_info.get("assigned_merchant_ids")
            or user_info.get("merchant_ids")
            or user_info.get("merchant_scopes")
            or user_info.get("merchants")
        )
        if isinstance(scoped_values, str):
            scoped_list = [item.strip() for item in scoped_values.split(",") if item.strip()]
            if scoped_list:
                return merchant_id in scoped_list
        elif isinstance(scoped_values, list):
            scoped_list = [str(item).strip() for item in scoped_values if str(item).strip()]
            if scoped_list:
                return merchant_id in scoped_list
        single_merchant = str(user_info.get("merchant_id") or "").strip()
        if single_merchant:
            return single_merchant == merchant_id
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
# Employee permissions (Reviews Center + other employee-only APIs)
# ============================================================================


def has_permission(current_user: Dict[str, Any], required_permission: str) -> bool:
    """
    Permission matcher for employee-only APIs.

    Semantics:
    - super_admin/admin: allow all (but still require employee_id on employee routes).
    - employee: allow all `reviews.*` permissions (MVP: avoid blocking core internal workflows).
    - explicit permissions list supports:
      - exact match: "reviews.read"
      - wildcard prefix: "reviews.*" matches "reviews.group.manage"
    """
    perm = (required_permission or "").strip()
    if not perm:
        return True

    role = (current_user.get("role") or "").strip().lower()
    if role in {"super_admin", "admin"}:
        return True
    if role in {"employee"} and perm.startswith("reviews."):
        return True

    raw = current_user.get("permissions") or []
    if isinstance(raw, str):
        perms: List[str] = [p.strip() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, list):
        perms = [str(p).strip() for p in raw if str(p).strip()]
    else:
        perms = []

    if perm in perms:
        return True
    for p in perms:
        if p.endswith(".*") and perm.startswith(p[:-1]):
            return True
    return False


def require_employee_permissions(required_permissions: Iterable[str]):
    """
    FastAPI dependency for employee-only endpoints.

    Requirements:
    - JWT must represent an employee identity (role in EMPLOYEE_ROLES)
    - Must include employee_id claim (prevents accidentally accepting user tokens)
    - Reads permissions from the token's "permissions" claim (employees.permissions)
    """
    required = [str(p).strip() for p in required_permissions if str(p).strip()]

    async def _dep(
        request: Request = None,
        current_user: Dict[str, Any] = Depends(get_current_user),
    ) -> Dict[str, Any]:
        # NOTE: FastAPI injects Request only when the annotation is exactly Request.
        role = (current_user.get("role") or "").strip().lower()
        membership_type = str(current_user.get("membership_type") or "").strip().lower()
        if membership_type and membership_type != "employee":
            raise HTTPException(status_code=403, detail="EMPLOYEE_REQUIRED")
        if role not in {r.lower() for r in EMPLOYEE_ROLES}:
            raise HTTPException(status_code=403, detail="EMPLOYEE_REQUIRED")

        employee_id = (
            current_user.get("employee_id")
            or current_user.get("employeeId")
        )
        if not employee_id and not membership_type:
            employee_id = current_user.get("user_id") or current_user.get("sub")
        if not employee_id:
            raise HTTPException(status_code=403, detail="EMPLOYEE_ID_REQUIRED")

        missing = [p for p in required if not has_permission(current_user, p)]
        if missing:
            # Best-effort: emit an authz denied metric with low-cardinality endpoint template.
            try:
                if request is None:  # pragma: no cover
                    raise RuntimeError("no_request_context")
                from observability.reviews_metrics import record_employee_authz_denied

                route = request.scope.get("route")
                endpoint = getattr(route, "path", None) or request.url.path
                record_employee_authz_denied(endpoint=endpoint, required_permission=missing[0])
            except Exception:
                pass

            raise HTTPException(
                status_code=403,
                detail={"error": "MISSING_PERMISSIONS", "missing": missing},
            )

        # Populate common log context (safe; no PII).
        try:
            if request is None:  # pragma: no cover
                raise RuntimeError("no_request_context")
            request.state.operation = "employee"
            request.state.actor_employee_id = str(employee_id)
        except Exception:
            pass

        return current_user

    return _dep

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
