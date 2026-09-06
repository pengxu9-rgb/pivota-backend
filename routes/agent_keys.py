"""
Agent API Key Management Routes
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from datetime import datetime
import secrets
import hashlib
from pydantic import BaseModel

from db.database import database
from db.agents import is_redacted_agent_api_key
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user

router = APIRouter(prefix="/agents", tags=["agent-keys"])

class CreateApiKeyRequest(BaseModel):
    name: str
    environment: Optional[str] = "live"
    
class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key: str
    created_at: str
    last_used: Optional[str] = None
    status: str
    usage_count: int
    environment: Optional[str] = None
    masked: bool = True


def _normalize_key_environment(raw_environment: Optional[str]) -> str:
    value = str(raw_environment or "live").strip().lower()
    if value in {"production", "prod", "live"}:
        return "live"
    if value in {"test", "sandbox", "staging"}:
        return "test"
    return "live"


def _prefix_for_environment(environment: str) -> str:
    return "ak_live_" if environment == "live" else "ak_test_"


def _infer_environment_from_value(value: Optional[str]) -> str:
    probe = str(value or "").lower()
    if "ak_test_" in probe:
        return "test"
    if "ak_live_" in probe:
        return "live"
    return "unknown"


def _format_last_used(last_used_at):
    if not last_used_at:
        return None

    diff = datetime.utcnow() - last_used_at
    if diff.days > 0:
        return f"{diff.days} days ago"
    if diff.seconds > 3600:
        return f"{diff.seconds // 3600} hours ago"
    if diff.seconds > 60:
        return f"{diff.seconds // 60} minutes ago"
    return "Just now"

@router.get("/{agent_id}/api-keys")
async def get_agent_api_keys(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get all API keys for an agent"""
    try:
        # Verify the agent owns this resource or is admin
        user_agent_id = current_user.get("agent_id") or current_user.get("email")
        if user_agent_id != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
            raise HTTPException(status_code=403, detail="Not authorized to view these API keys")
        
        # Check if api_keys table exists, if not create it
        await database.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(255) NOT NULL,
                name VARCHAR(255) NOT NULL,
                key_hash VARCHAR(255) NOT NULL,
                key_prefix VARCHAR(20) NOT NULL,
                status VARCHAR(50) DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP,
                usage_count INTEGER DEFAULT 0,
                UNIQUE(key_hash)
            )
        """)
        
        # Get API keys for this agent
        keys = await database.fetch_all(
            """
            SELECT 
                id::text,
                name,
                key_prefix || '****' as key,
                created_at,
                last_used,
                status,
                usage_count
            FROM api_keys
            WHERE agent_id = :agent_id
            ORDER BY created_at DESC
            """,
            {"agent_id": agent_id}
        )
        
        # Legacy fallback: if no api_keys rows, surface agents.api_key (masked)
        formatted_keys = []
        if not keys:
            legacy = await database.fetch_one(
                """
                SELECT agent_id, agent_name, owner_email, api_key, api_key_hash, created_at
                FROM agents 
                WHERE agent_id = :agent_or_email OR owner_email = :agent_or_email
                LIMIT 1
                """,
                {"agent_or_email": agent_id}
            )
            # A redacted marker is not a key: never display it and NEVER hash it into api_keys (that
            # would mint `redacted:<agent_id>` — a public string — as a live credential).
            if legacy and legacy["api_key"] and not is_redacted_agent_api_key(legacy["api_key"]):
                legacy_dict = dict(legacy)
                legacy_hash = legacy_dict.get("api_key_hash") or hashlib.sha256(legacy_dict["api_key"].encode("utf-8")).hexdigest()
                try:
                    await database.execute(
                        """
                        INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status)
                        VALUES (:agent_id, :name, :key_hash, :key_prefix, 'active')
                        ON CONFLICT (key_hash) DO NOTHING
                        """,
                        {
                            "agent_id": legacy_dict.get("agent_id") or agent_id,
                            "name": legacy_dict.get("agent_name") or "Primary Key",
                            "key_hash": legacy_hash,
                            "key_prefix": legacy_dict["api_key"][:10],
                        },
                    )
                except Exception:
                    pass

                masked = f"{legacy['api_key'][:10]}****"
                formatted_keys.append({
                    "id": "legacy",
                    "name": legacy_dict.get("agent_name") or "Primary Key",
                    "key": masked,
                    "created_at": (legacy_dict.get("created_at") or datetime.utcnow()).isoformat(),
                    "last_used": None,
                    "status": "active",
                    "usage_count": 0,
                    "environment": _infer_environment_from_value(legacy_dict["api_key"]),
                    "masked": True,
                })

        for key in keys:
            masked_key = key["key"]
            formatted_keys.append({
                "id": key["id"],
                "name": key["name"],
                "key": masked_key,
                "created_at": key["created_at"].isoformat() if key["created_at"] else None,
                "last_used": _format_last_used(key["last_used"]),
                "status": key["status"],
                "usage_count": key["usage_count"],
                "environment": _infer_environment_from_value(masked_key),
                "masked": True,
            })
        
        return {
            "status": "success",
            "keys": formatted_keys
        }
        
    except Exception as e:
        print(f"Error getting API keys: {e}")
        # Return empty list instead of error to prevent frontend crash
        return {
            "status": "success",
            "keys": []
        }

@router.post("/{agent_id}/api-keys")
async def create_agent_api_key(
    agent_id: str,
    request: CreateApiKeyRequest,
    current_user: dict = Depends(get_current_user)
):
    """Create a new API key for an agent"""
    try:
        # Verify the agent owns this resource or is admin
        user_agent_id = current_user.get("agent_id") or current_user.get("email")
        if user_agent_id != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
            raise HTTPException(status_code=403, detail="Not authorized to create API keys for this agent")
        
        # Ensure table exists
        await database.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(255) NOT NULL,
                name VARCHAR(255) NOT NULL,
                key_hash VARCHAR(255) NOT NULL,
                key_prefix VARCHAR(20) NOT NULL,
                status VARCHAR(50) DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP,
                usage_count INTEGER DEFAULT 0,
                UNIQUE(key_hash)
            )
        """)
        
        environment = _normalize_key_environment(request.environment)
        full_key = f"{_prefix_for_environment(environment)}{secrets.token_hex(32)}"
        
        # Hash the key for storage
        key_hash = hashlib.sha256(full_key.encode()).hexdigest()
        
        # Store the key
        result = await database.fetch_one(
            """
            INSERT INTO api_keys (agent_id, name, key_hash, key_prefix)
            VALUES (:agent_id, :name, :key_hash, :key_prefix)
            RETURNING id, created_at
            """,
            {
                "agent_id": agent_id,
                "name": request.name,
                "key_hash": key_hash,
                "key_prefix": full_key[:10]
            }
        )
        
        return {
            "status": "success",
            "key": full_key,
            "key_id": str(result["id"]),
            "name": request.name,
            "created_at": result["created_at"].isoformat(),
            "environment": environment,
            "masked": False,
        }
        
    except Exception as e:
        print(f"Error creating API key: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create API key: {str(e)}")

@router.delete("/{agent_id}/api-keys/{key_id}")
async def revoke_agent_api_key(
    agent_id: str,
    key_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Revoke an API key"""
    try:
        # Verify the agent owns this resource or is admin
        user_agent_id = current_user.get("agent_id") or current_user.get("email")
        if user_agent_id != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
            raise HTTPException(status_code=403, detail="Not authorized to revoke this API key")
        
        # Update key status
        result = await database.execute(
            """
            UPDATE api_keys 
            SET status = 'revoked'
            WHERE id = :key_id AND agent_id = :agent_id
            """,
            {"key_id": int(key_id), "agent_id": agent_id}
        )
        
        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="API key not found")
        
        return {
            "status": "success",
            "message": "API key revoked successfully"
        }
        
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid key ID")
    except Exception as e:
        print(f"Error revoking API key: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to revoke API key: {str(e)}")

@router.post("/{agent_id}/reset-api-key")
async def reset_agent_api_key(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Reset the main API key for an agent (legacy endpoint)"""
    try:
        # Verify the agent owns this resource or is admin
        user_agent_id = current_user.get("agent_id") or current_user.get("email")
        if user_agent_id != agent_id and current_user.get("role") not in EMPLOYEE_STAFF_ROLES:
            raise HTTPException(status_code=403, detail="Not authorized to reset this API key")

        # Generate a standards-compliant external key:
        #   ak_live_ + 64 lowercase hex chars.
        new_api_key = f"ak_live_{secrets.token_hex(32)}"
        new_key_hash = hashlib.sha256(new_api_key.encode("utf-8")).hexdigest()

        # Keep legacy agents table in sync.
        try:
            await database.execute(
                """
                UPDATE agents
                SET api_key = :api_key, api_key_hash = :api_key_hash
                WHERE agent_id = :agent_id
                """,
                {"api_key": new_api_key, "api_key_hash": new_key_hash, "agent_id": agent_id},
            )
        except Exception:
            # Backward compatibility for schemas without api_key_hash.
            await database.execute(
                """
                UPDATE agents
                SET api_key = :api_key
                WHERE agent_id = :agent_id
                """,
                {"api_key": new_api_key, "agent_id": agent_id},
            )

        # Sync with hash-key auth tables so get_agent_by_key keeps working.
        key_sync_source = "legacy"
        try:
            table_info = await database.fetch_one(
                """
                SELECT
                  to_regclass('public.api_keys') AS api_keys_table,
                  to_regclass('public.agent_api_keys') AS agent_api_keys_table
                """
            )
            table_dict = dict(table_info or {})
            has_api_keys = bool(table_dict.get("api_keys_table"))
            has_agent_api_keys = bool(table_dict.get("agent_api_keys_table"))
        except Exception:
            has_api_keys = False
            has_agent_api_keys = False

        if has_api_keys:
            await database.execute(
                """
                UPDATE api_keys
                SET status = 'revoked'
                WHERE agent_id = :agent_id AND status = 'active'
                """,
                {"agent_id": agent_id},
            )
            await database.execute(
                """
                INSERT INTO api_keys (agent_id, name, key_hash, key_prefix, status)
                VALUES (:agent_id, :name, :key_hash, :key_prefix, 'active')
                """,
                {
                    "agent_id": agent_id,
                    "name": "Primary Key (Rotated)",
                    "key_hash": new_key_hash,
                    "key_prefix": new_api_key[:10],
                },
            )
            key_sync_source = "api_keys"
        elif has_agent_api_keys:
            await database.execute(
                """
                UPDATE agent_api_keys
                SET is_active = FALSE, last_rotated_at = NOW()
                WHERE agent_id = :agent_id AND COALESCE(is_active, TRUE) = TRUE
                """,
                {"agent_id": agent_id},
            )
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
                    "key_hash": new_key_hash,
                    "key_prefix": new_api_key[:12] + "...",
                    "created_by": "self_reset",
                },
            )
            key_sync_source = "agent_api_keys"

        return {
            "status": "success",
            "api_key": new_api_key,
            "new_api_key": new_api_key,
            "key_sync_source": key_sync_source,
            "environment": "live",
            "masked": False,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error resetting API key: {e}")
        raise HTTPException(status_code=500, detail="Failed to reset API key")
