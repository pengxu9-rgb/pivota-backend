"""
Create test agent account with API key
"""

from fastapi import APIRouter
import secrets
from db.database import database
from utils.auth import hash_password

router = APIRouter(prefix="/admin/create", tags=["admin-create"])

@router.post("/test-agent")
async def create_test_agent_account():
    """
    Create test agent: testuser@pivota.cc / Test123!
    Public endpoint (temporary)
    """
    try:
        email = "testuser@pivota.cc"
        password = "Test123!"
        
        # Generate API key
        api_key = f"pk_live_{secrets.token_urlsafe(32)[:32]}"
        
        # Check if agent exists in agents table
        existing_agent = await database.fetch_one(
            "SELECT agent_id FROM agents WHERE email = :email",
            {"email": email}
        )
        
        if existing_agent:
            # Update API key
            await database.execute(
                """
                UPDATE agents 
                SET api_key = :api_key, name = :name, status = :status
                WHERE email = :email
                """,
                {
                    "api_key": api_key,
                    "name": "Test Agent User",
                    "status": "active",
                    "email": email
                }
            )
            agent_id = existing_agent["agent_id"]
        else:
            # Create agent
            agent_id = email
            await database.execute(
                """
                INSERT INTO agents (agent_id, name, email, company, api_key, status)
                VALUES (:agent_id, :name, :email, :company, :api_key, :status)
                """,
                {
                    "agent_id": agent_id,
                    "name": "Test Agent User",
                    "email": email,
                    "company": "Pivota Test",
                    "api_key": api_key,
                    "status": "active"
                }
            )
        
        return {
            "status": "success",
            "message": "Test agent created in agents table (for API key display)",
            "credentials": {
                "email": email,
                "note": "Use agent@test.com / Agent123! to login (auth is separate)"
            },
            "agent_details": {
                "agent_id": agent_id,
                "api_key": api_key
            },
            "instructions": "Login as agent@test.com, then check Manage API Keys"
        }
        
    except Exception as e:
        import traceback
        return {
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()
        }

