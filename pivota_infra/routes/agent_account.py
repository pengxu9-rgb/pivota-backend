"""
Agent Account Management - REBUILT from scratch
Simplified and adapted to actual database schema
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr, validator
from typing import Optional
from datetime import datetime
import secrets
import hashlib

from db.database import database
from utils.auth import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/agent/account", tags=["agent-account"])

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
    
    @validator('password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters long')
        return v

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

class AgentLoginResponse(BaseModel):
    success: bool
    token: str
    agent: AgentInfo
    api_key: str

# ============================================================================
# Endpoints
# ============================================================================

@router.post("/register")
async def register_agent(data: AgentRegisterRequest):
    """
    Register a new AI Agent
    Simplified to work with actual database schema
    """
    try:
        # 1. Check if user already exists
        existing_user = await database.fetch_one(
            "SELECT id FROM users WHERE email = :email",
            {"email": data.email}
        )
        
        if existing_user:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # 2. Create user account
        password_hash = hash_password(data.password)
        
        await database.execute(
            """
            INSERT INTO users (id, email, password_hash, full_name, role, active)
            VALUES (gen_random_uuid(), :email, :password_hash, :full_name, :role, :active)
            """,
            {
                "email": data.email,
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
        
        # 4. Create agent record - using ONLY the columns we know exist
        # Based on error messages: agents table has both 'email' (NOT NULL) and 'name' (NOT NULL)
        await database.execute(
            """
            INSERT INTO agents (agent_id, name, email, api_key)
            VALUES (:agent_id, :name, :email, :api_key)
            """,
            {
                "agent_id": agent_id,
                "name": data.agent_name,
                "email": data.email,
                "api_key": api_key
            }
        )
        
        print(f"✅ Agent created: {agent_id} for {data.email}")
        
        return {
            "success": True,
            "message": "Agent account created successfully",
            "agent_id": agent_id,
            "api_key": api_key,
            "email": data.email,
            "important": "Save your API key now! It won't be shown again."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        # If agent creation failed, clean up user account
        try:
            await database.execute(
                "DELETE FROM users WHERE email = :email AND role = 'agent'",
                {"email": data.email}
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
        # 1. Find user
        user = await database.fetch_one(
            "SELECT id, email, password_hash, full_name, role, active FROM users WHERE email = :email",
            {"email": data.email}
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
            "SELECT agent_id, name, email, api_key FROM agents WHERE email = :email",
            {"email": data.email}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent record not found")
        
        # 4. Update last login
        await database.execute(
            "UPDATE users SET last_login = :last_login WHERE email = :email",
            {"last_login": datetime.utcnow(), "email": data.email}
        )
        
        # 5. Create JWT token
        token = create_access_token({
            "sub": user["email"],
            "user_id": str(user["id"]),
            "role": "agent",
            "agent_id": agent["agent_id"]
        })
        
        # Get agent name safely
        agent_name = "Agent"  # Default
        try:
            if agent["name"]:
                agent_name = agent["name"]
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
                "agent_id": agent["agent_id"],
                "agent_name": agent_name,
                "email": agent["email"],
                "company": "",
                "description": "",
                "status": "active"
            },
            api_key=agent["api_key"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Login failed: {str(e)}"
        )

