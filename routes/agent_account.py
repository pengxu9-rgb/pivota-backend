"""
Agent Account Management - REBUILT from scratch
Simplified and adapted to actual database schema
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, EmailStr, field_validator
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta
import os
import secrets
import hashlib
import time

from db.auth_identity import upsert_membership
from db.database import database
from utils.auth import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/agent/account", tags=["agent-account"])


# ── Self-serve registration guardrails ────────────────────────────────────────
# POST /agent/account/register mints a live ak_live_* API key with no approval
# gate and no throttle (2026-08-08 agent-readability audit). Each key gets its
# own 100 rpm / 10k-daily quota, so unthrottled registration is quota
# multiplication: N registrations/minute = N*100 rpm of unmetered capacity.
# Two guardrails, both env-tunable so ops can act without a deploy:
#   * AGENT_SELF_SERVE_REGISTRATION_ENABLED (default ON — preserves today's
#     onboarding behavior; flipping it off is the kill switch).
#   * AGENT_REGISTRATION_PER_IP_HOURLY (default 5; 0 disables the throttle).
# In-memory per-instance store, same precedent as the review-media IP limiter.
_REGISTRATION_IP_LIMIT_STORE: Dict[str, Tuple[int, int]] = {}
_REGISTRATION_IP_LIMIT_MAX_KEYS = 10_000


def _self_serve_registration_enabled() -> bool:
    raw = (os.getenv("AGENT_SELF_SERVE_REGISTRATION_ENABLED") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _registration_hourly_limit() -> int:
    try:
        return max(0, int(os.getenv("AGENT_REGISTRATION_PER_IP_HOURLY") or "5"))
    except ValueError:
        return 5


def _registration_client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return (request.client.host if request.client else None) or "unknown"


def _check_registration_rate_limit(ip: str) -> bool:
    limit = _registration_hourly_limit()
    if limit == 0:
        return True
    window = int(time.time() // 3600)
    if len(_REGISTRATION_IP_LIMIT_STORE) > _REGISTRATION_IP_LIMIT_MAX_KEYS:
        # Bound memory against IP-cycling: drop entries from past windows.
        stale = [k for k, v in _REGISTRATION_IP_LIMIT_STORE.items() if v[0] != window]
        for k in stale:
            _REGISTRATION_IP_LIMIT_STORE.pop(k, None)
    prev = _REGISTRATION_IP_LIMIT_STORE.get(ip)
    if prev and prev[0] == window:
        if prev[1] >= limit:
            return False
        _REGISTRATION_IP_LIMIT_STORE[ip] = (window, prev[1] + 1)
        return True
    _REGISTRATION_IP_LIMIT_STORE[ip] = (window, 1)
    return True


def _validate_agent_password(value: str) -> str:
    if len(value) < 8:
        raise ValueError('Password must be at least 8 characters long')
    return value


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


async def _sync_agent_auth_membership(
    *,
    email: str,
    agent_id: str,
    agent_name: Optional[str],
    password_hash: Optional[str] = None,
) -> Optional[dict]:
    try:
        return await upsert_membership(
            email=_normalize_email(email),
            membership_type="agent",
            role="agent",
            entity_id=str(agent_id),
            status="active",
            full_name=agent_name,
            password_hash=password_hash,
            credential_source="agent_account_password" if password_hash else None,
            source="agent_account_sync",
        )
    except Exception:
        return None


def _identity_id_from_membership(membership: Optional[dict], email: str) -> str:
    if membership:
        identity_id = membership.get("identity_id")
        if identity_id:
            return str(identity_id)
        identity = membership.get("identity")
        if isinstance(identity, dict) and identity.get("identity_id"):
            return str(identity["identity_id"])
    return f"legacy:{_normalize_email(email)}"


async def _resolve_agent_key_table() -> Optional[str]:
    """
    Resolve which key table is used by auth lookup priority.
    Priority follows db.agents._resolve_auth_key_table: api_keys > agent_api_keys > legacy.
    """
    try:
        row = await database.fetch_one(
            """
            SELECT
              to_regclass('public.api_keys') AS api_keys_table,
              to_regclass('public.agent_api_keys') AS agent_api_keys_table
            """
        )
        row_dict = dict(row or {})
        if row_dict.get("api_keys_table"):
            return "api_keys"
        if row_dict.get("agent_api_keys_table"):
            return "agent_api_keys"
    except Exception:
        # Non-postgres or no visibility to regclass: keep legacy-only behavior.
        return None
    return None


async def _sync_new_agent_api_key(
    *,
    agent_id: str,
    api_key: str,
    api_key_hash: str,
) -> str:
    """
    Ensure newly registered agent key is available on the active auth path.
    Returns one of: api_keys / agent_api_keys / legacy.
    """
    key_table = await _resolve_agent_key_table()

    if key_table == "api_keys":
        await database.execute(
            """
            INSERT INTO api_keys (agent_id, name, key_hash, key_prefix)
            VALUES (:agent_id, :name, :key_hash, :key_prefix)
            """,
            {
                "agent_id": agent_id,
                "name": "Primary Key",
                "key_hash": api_key_hash,
                "key_prefix": api_key[:10],
            },
        )
        return "api_keys"

    if key_table == "agent_api_keys":
        await database.execute(
            """
            INSERT INTO agent_api_keys (
                key_id,
                agent_id,
                key_hash,
                key_prefix,
                is_active,
                created_by,
                created_at
            )
            VALUES (
                :key_id,
                :agent_id,
                :key_hash,
                :key_prefix,
                TRUE,
                :created_by,
                NOW()
            )
            """,
            {
                "key_id": f"key_{secrets.token_hex(8)}",
                "agent_id": agent_id,
                "key_hash": api_key_hash,
                "key_prefix": api_key[:12] + "...",
                "created_by": "agent_signup",
            },
        )
        return "agent_api_keys"

    return "legacy"


async def _ensure_agent_api_key_on_auth_path(
    *,
    agent_id: str,
    api_key: str,
    api_key_hash: str,
) -> str:
    """
    Ensure a login-returned key is present on the current auth lookup path.
    This keeps `/agent/account/login` aligned with `/agent/v1/orders*` auth.
    """
    key_table = await _resolve_agent_key_table()

    if key_table == "api_keys":
        existing = await database.fetch_one(
            """
            SELECT id, status
            FROM api_keys
            WHERE key_hash = :key_hash
            LIMIT 1
            """,
            {"key_hash": api_key_hash},
        )

        if existing:
            existing_dict = dict(existing)
            if existing_dict.get("status") != "active":
                await database.execute(
                    """
                    UPDATE api_keys
                    SET agent_id = :agent_id,
                        name = :name,
                        key_prefix = :key_prefix,
                        status = 'active'
                    WHERE id = :id
                    """,
                    {
                        "id": existing_dict["id"],
                        "agent_id": agent_id,
                        "name": "Primary Key",
                        "key_prefix": api_key[:10],
                    },
                )
            return "api_keys"

        await database.execute(
            """
            INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status)
            VALUES (:agent_id, :name, :key_hash, :key_prefix, 'active')
            """,
            {
                "agent_id": agent_id,
                "name": "Primary Key",
                "key_hash": api_key_hash,
                "key_prefix": api_key[:10],
            },
        )
        return "api_keys"

    if key_table == "agent_api_keys":
        api_key_hash_md5 = hashlib.md5(api_key.encode("utf-8")).hexdigest()
        existing = await database.fetch_one(
            """
            SELECT key_id, COALESCE(is_active, TRUE) AS is_active
            FROM agent_api_keys
            WHERE key_hash = :key_hash_sha256 OR key_hash = :key_hash_md5
            ORDER BY CASE WHEN key_hash = :key_hash_sha256 THEN 0 ELSE 1 END
            LIMIT 1
            """,
            {
                "key_hash_sha256": api_key_hash,
                "key_hash_md5": api_key_hash_md5,
            },
        )

        if existing:
            existing_dict = dict(existing)
            if not bool(existing_dict.get("is_active", True)):
                await database.execute(
                    """
                    UPDATE agent_api_keys
                    SET agent_id = :agent_id,
                        key_prefix = :key_prefix,
                        is_active = TRUE
                    WHERE key_id = :key_id
                    """,
                    {
                        "key_id": existing_dict["key_id"],
                        "agent_id": agent_id,
                        "key_prefix": api_key[:12] + "...",
                    },
                )
            return "agent_api_keys"

        await database.execute(
            """
            INSERT INTO agent_api_keys (
                key_id,
                agent_id,
                key_hash,
                key_prefix,
                is_active,
                created_by,
                created_at
            )
            VALUES (
                :key_id,
                :agent_id,
                :key_hash,
                :key_prefix,
                TRUE,
                :created_by,
                NOW()
            )
            """,
            {
                "key_id": f"key_{secrets.token_hex(8)}",
                "agent_id": agent_id,
                "key_hash": api_key_hash,
                "key_prefix": api_key[:12] + "...",
                "created_by": "agent_login_sync",
            },
        )
        return "agent_api_keys"

    return "legacy"


# ============================================================================
# Models
# ============================================================================

class AgentRegisterRequest(BaseModel):
    email: EmailStr
    password: str
    agent_name: str
    company: Optional[str] = None
    description: Optional[str] = None
    phone: Optional[str] = None
    
    @field_validator('password')
    @classmethod
    def validate_password(cls, value: str) -> str:
        return _validate_agent_password(value)

class AgentLoginRequest(BaseModel):
    email: EmailStr
    password: str

class AgentInfo(BaseModel):
    agent_id: str
    agent_name: str
    email: str
    company: str
    description: str
    status: str
    # [Phase 6.2] Agent tier (basic/premium)
    agent_type: str = "basic"

class AgentLoginResponse(BaseModel):
    success: bool
    token: str
    agent: AgentInfo
    api_key: str

# ============================================================================
# Endpoints
# ============================================================================

@router.post("/register")
async def register_agent(data: AgentRegisterRequest, http_request: Request):
    """
    Register a new AI Agent
    Simplified to work with actual database schema
    """
    if not _self_serve_registration_enabled():
        raise HTTPException(
            status_code=403,
            detail="Self-serve agent registration is currently disabled. Contact support@pivota.cc.",
        )
    client_ip = _registration_client_ip(http_request)
    if not _check_registration_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many registrations from this address; try again later.",
            headers={"Retry-After": "3600"},
        )

    agent_id: Optional[str] = None
    email = _normalize_email(data.email)

    try:
        # 1. Check if user already exists
        existing_user = await database.fetch_one(
            "SELECT id FROM users WHERE LOWER(email) = LOWER(:email)",
            {"email": email}
        )
        
        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # 2. Create user account
        password_hash = hash_password(data.password)
        
        await database.execute(
            """
            INSERT INTO users (email, password_hash, full_name, role, active)
            VALUES (:email, :password_hash, :full_name, :role, :active)
            """,
            {
                "email": email,
                "password_hash": password_hash,
                "full_name": data.agent_name,
                "role": "agent",
                "active": True
            }
        )
        
        # 3. Generate agent_id and API key
        agent_id = f"agent_{secrets.token_hex(8)}"
        api_key_raw = secrets.token_bytes(32)
        api_key = f"ak_live_{api_key_raw.hex()}"
        api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        
        # 4. Create agent record - aligned with current agents table schema
        await database.execute(
            """
            INSERT INTO agents (
                agent_id,
                agent_name,
                owner_email,
                description,
                api_key,
                api_key_hash,
                is_active,
                created_at,
                updated_at
            )
            VALUES (
                :agent_id,
                :agent_name,
                :owner_email,
                :description,
                :api_key,
                :api_key_hash,
                TRUE,
                NOW(),
                NOW()
            )
            """,
            {
                "agent_id": agent_id,
                "agent_name": data.agent_name,
                "owner_email": email,
                "description": data.description or "",
                "api_key": api_key,
                "api_key_hash": api_key_hash,
            }
        )

        # 5. Persist key in the auth lookup table when hash-key auth is enabled.
        key_sync_source = await _sync_new_agent_api_key(
            agent_id=agent_id,
            api_key=api_key,
            api_key_hash=api_key_hash,
        )
        await _sync_agent_auth_membership(
            email=email,
            agent_id=agent_id,
            agent_name=data.agent_name,
            password_hash=password_hash,
        )

        print(f"✅ Agent created: {agent_id} for {email} (key sync: {key_sync_source})")
        
        return {
            "success": True,
            "message": "Agent account created successfully",
            "agent_id": agent_id,
            "api_key": api_key,
            "email": email,
            "important": "Save your API key now! It won't be shown again."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        # If any step failed after partial writes, clean up created rows.
        if agent_id:
            try:
                await database.execute(
                    "DELETE FROM agents WHERE agent_id = :agent_id",
                    {"agent_id": agent_id},
                )
                print(f"🧹 Cleaned up agent record after failure: {agent_id}")
            except Exception:
                pass

        try:
            await database.execute(
                "DELETE FROM users WHERE email = :email AND role = 'agent'",
                {"email": email}
            )
            print(f"🧹 Cleaned up user account after failure")
        except:
            pass
        
        import traceback
        print(f"❌ Agent registration error: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Registration failed: {str(e)}"
        )


@router.post("/login", response_model=AgentLoginResponse)
async def login_agent(data: AgentLoginRequest):
    """Agent login"""
    try:
        email = _normalize_email(data.email)
        # 1. Find user
        user = await database.fetch_one(
            "SELECT id, email, password_hash, full_name, role, active FROM users WHERE LOWER(email) = LOWER(:email)",
            {"email": email}
        )
        
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        if user["role"] != "agent":
            raise HTTPException(status_code=403, detail="This login is for agents only")
        
        if not user["active"]:
            raise HTTPException(status_code=403, detail="Account is deactivated")
        
        # 2. Verify password
        if not verify_password(data.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        # 3. Get agent record
        agent = await database.fetch_one(
            """
            SELECT agent_id, agent_name, owner_email, api_key, agent_type
            FROM agents
            WHERE LOWER(owner_email) = LOWER(:email)
            """,
            {"email": email}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent record not found")
        
        # databases.Record → plain dict for safe .get() usage
        agent_dict = dict(agent)

        if not agent_dict.get("api_key"):
            raise HTTPException(status_code=403, detail="Agent API key is unavailable")

        api_key_hash = hashlib.sha256(agent_dict["api_key"].encode("utf-8")).hexdigest()
        await _ensure_agent_api_key_on_auth_path(
            agent_id=agent_dict["agent_id"],
            api_key=agent_dict["api_key"],
            api_key_hash=api_key_hash,
        )

        # 4. Update last login
        await database.execute(
            "UPDATE users SET last_login = :last_login WHERE email = :email",
            {"last_login": datetime.utcnow(), "email": user["email"]}
        )
        agent_membership = await _sync_agent_auth_membership(
            email=user["email"],
            agent_id=agent_dict["agent_id"],
            agent_name=agent_dict.get("agent_name") or user["full_name"],
            password_hash=user["password_hash"],
        )
        identity_id = _identity_id_from_membership(agent_membership, user["email"])
        
        # 5. Create JWT token
        # Extend agent portal session to reduce unexpected logouts.
        # Other roles keep the default (24h) expiry from utils.auth.
        token = create_access_token(
            {
                "sub": identity_id,
                "identity_id": identity_id,
                "email": user["email"],  # Required by get_current_user
                "user_id": str(user["id"]),
                "role": "agent",
                "agent_id": agent["agent_id"],
                "membership_type": "agent",
                "membership_id": agent_membership.get("membership_id") if agent_membership else None,
                "aud": "agent-portal",
                "scope": "agent",
            },
            # Agent portal sessions: ~7 days
            expires_delta=timedelta(days=7),
        )
        
        # Get agent name safely
        agent_name = "Agent"  # Default
        try:
            if agent["agent_name"]:
                agent_name = agent["agent_name"]
        except (KeyError, TypeError):
            # Fallback to user's full name if available
            try:
                if user["full_name"]:
                    agent_name = user["full_name"]
            except (KeyError, TypeError):
                pass
        
        return AgentLoginResponse(
            success=True,
            token=token,
            agent={
                "agent_id": agent_dict["agent_id"],
                "agent_name": agent_name,
                "email": agent_dict["owner_email"],
                "company": "",
                "description": "",
                "status": "active",
                "agent_type": agent_dict.get("agent_type") or "basic",
            },
            api_key=agent_dict["api_key"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Login failed: {str(e)}"
        )
