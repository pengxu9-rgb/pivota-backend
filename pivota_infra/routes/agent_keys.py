"""
Agent API Key Management Routes
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, List
from datetime import datetime, timedelta
import secrets
import hashlib
from pydantic import BaseModel

from db.database import database
from utils.auth import get_current_user

router = APIRouter(prefix="/agents", tags=["agent-keys"])

class CreateApiKeyRequest(BaseModel):
    name: str
    
class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key: str
    created_at: str
    last_used: Optional[str] = None
    status: str
    usage_count: int

@router.get("/{agent_id}/api-keys")
async def get_agent_api_keys(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get all API keys for an agent"""
    try:
        # Verify the agent owns this resource or is admin
        user_agent_id = current_user.get("agent_id") or current_user.get("email")
        if user_agent_id != agent_id and current_user.get("role") not in ["admin", "employee"]:
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
                SELECT api_key, name, created_at 
                FROM agents 
                WHERE agent_id = :agent_or_email OR email = :agent_or_email
                LIMIT 1
                """,
                {"agent_or_email": agent_id}
            )
            if legacy and legacy["api_key"]:
                masked = f"{legacy['api_key'][:10]}****"
                formatted_keys.append({
                    "id": "legacy",
                    "name": legacy.get("name") or "Primary Key",
                    "key": masked,
                    "created_at": (legacy.get("created_at") or datetime.utcnow()).isoformat(),
                    "last_used": None,
                    "status": "active",
                    "usage_count": 0,
                })

        for key in keys:
            last_used = None
            if key["last_used"]:
                # Calculate relative time
                diff = datetime.utcnow() - key["last_used"]
                if diff.days > 0:
                    last_used = f"{diff.days} days ago"
                elif diff.seconds > 3600:
                    last_used = f"{diff.seconds // 3600} hours ago"
                elif diff.seconds > 60:
                    last_used = f"{diff.seconds // 60} minutes ago"
                else:
                    last_used = "Just now"
            
            formatted_keys.append({
                "id": key["id"],
                "name": key["name"],
                "key": key["key"],
                "created_at": key["created_at"].isoformat() if key["created_at"] else None,
                "last_used": last_used,
                "status": key["status"],
                "usage_count": key["usage_count"]
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
        if user_agent_id != agent_id and current_user.get("role") not in ["admin", "employee"]:
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
        
        # Generate a new API key
        key_prefix = "pk_live_" if "production" in request.name.lower() else "pk_test_"
        random_part = secrets.token_urlsafe(32)[:32]  # 32 chars
        full_key = f"{key_prefix}{random_part}"
        
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
                "key_prefix": full_key[:10]  # Store first 10 chars for display
            }
        )
        
        return {
            "status": "success",
            "key": full_key,  # Return full key only on creation
            "key_id": str(result["id"]),
            "name": request.name,
            "created_at": result["created_at"].isoformat()
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
        if user_agent_id != agent_id and current_user.get("role") not in ["admin", "employee"]:
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
        if user_agent_id != agent_id and current_user.get("role") not in ["admin", "employee"]:
            raise HTTPException(status_code=403, detail="Not authorized to reset this API key")
        
        # Generate new API key
        new_api_key = f"pk_live_{secrets.token_urlsafe(32)[:32]}"
        
        # Update in database
        await database.execute(
            """
            UPDATE agents 
            SET api_key = :api_key
            WHERE agent_id = :agent_id
            """,
            {"api_key": new_api_key, "agent_id": agent_id}
        )
        
        return {
            "status": "success",
            "api_key": new_api_key
        }
        
    except Exception as e:
        print(f"Error resetting API key: {e}")
        raise HTTPException(status_code=500, detail="Failed to reset API key")
