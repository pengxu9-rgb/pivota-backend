"""
Initialize API key for agent@test.com
Temporary admin endpoint
"""

from fastapi import APIRouter
import secrets
from db.database import database

router = APIRouter(prefix="/admin/init", tags=["admin-init"])

@router.post("/agent-test-key")
async def initialize_agent_test_key():
    """
    Initialize API key for agent@test.com
    Public endpoint (temporary)
    """
    try:
        # Generate API key (ak_live_ format with 64 hex chars)
        api_key = f"ak_live_{secrets.token_hex(32)}"
        
        # Check if agent exists
        agent = await database.fetch_one(
            "SELECT agent_id, email FROM agents WHERE email = :email",
            {"email": "agent@test.com"}
        )
        
        if agent:
            # Update existing agent
            await database.execute(
                """
                UPDATE agents 
                SET api_key = :api_key
                WHERE email = :email
                """,
                {"api_key": api_key, "email": "agent@test.com"}
            )
            
            return {
                "status": "success",
                "message": "API key updated for existing agent",
                "agent_id": agent["agent_id"],
                "email": "agent@test.com",
                "api_key": api_key,
                "note": "Please save this key - it won't be shown again"
            }
        else:
            # Create new agent
            await database.execute(
                """
                INSERT INTO agents (agent_id, name, email, company, api_key, status)
                VALUES (:agent_id, :name, :email, :company, :api_key, :status)
                """,
                {
                    "agent_id": "agent@test.com",
                    "name": "Test Agent",
                    "email": "agent@test.com",
                    "company": "Test Company",
                    "api_key": api_key,
                    "status": "active"
                }
            )
            
            return {
                "status": "success",
                "message": "New agent created with API key",
                "agent_id": "agent@test.com",
                "email": "agent@test.com",
                "api_key": api_key,
                "note": "Please save this key - it won't be shown again"
            }
            
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

