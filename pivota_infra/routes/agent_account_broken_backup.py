"""
Agent Account Management
Registration and login system for AI Agents
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr, validator
from typing import Optional
from datetime import datetime
import secrets
import hashlib

from db.database import database
from utils.auth import hash_password, verify_password, create_access_token

router = APIRouter(prefix="/agent/account", tags=["agent-account"])


# ============================================================================
# Request/Response Models
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
        if not any(c.isupper() for c in v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not any(c.islower() for c in v):
            raise ValueError('Password must contain at least one lowercase letter')
        if not any(c.isdigit() for c in v):
            raise ValueError('Password must contain at least one digit')
        return v

class AgentLoginRequest(BaseModel):
    email: EmailStr
    password: str

class AgentLoginResponse(BaseModel):
    success: bool
    token: str
    agent: dict
    api_key: str


# ============================================================================
# Endpoints
# ============================================================================

@router.post("/register")
async def register_agent(data: AgentRegisterRequest):
    """
    Register a new AI Agent account
    Creates both user account and agent record with API key
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
            INSERT INTO users (email, password_hash, full_name, role, active)
            VALUES (:email, :password_hash, :full_name, :role, :active)
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
        
        # Generate API key: ak_live_<64 hex chars>
        api_key_raw = secrets.token_bytes(32)
        api_key = f"ak_live_{api_key_raw.hex()}"
        
        # Hash the API key for storage (store hash, not plaintext)
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        
        # 4. Create agent record - use absolute minimal approach
        # Based on error: agents table has 'email' column (NOT NULL)
        insert_attempts = [
            # Attempt 1: Using 'email' and 'name' columns
            {
                "sql": "INSERT INTO agents (agent_id, name, email, api_key) VALUES (:agent_id, :name, :email, :api_key)",
                "params": {
                    "agent_id": agent_id,
                    "name": data.agent_name,
                    "email": data.email,
                    "api_key": api_key
                },
                "description": "email + name columns"
            },
            # Attempt 2: Using 'email' and 'agent_name' columns
            {
                "sql": "INSERT INTO agents (agent_id, agent_name, email, api_key) VALUES (:agent_id, :agent_name, :email, :api_key)",
                "params": {
                    "agent_id": agent_id,
                    "agent_name": data.agent_name,
                    "email": data.email,
                    "api_key": api_key
                },
                "description": "email + agent_name columns"
            },
            # Attempt 3: With owner_email instead of email
            {
                "sql": "INSERT INTO agents (agent_id, name, owner_email, api_key) VALUES (:agent_id, :name, :owner_email, :api_key)",
                "params": {
                    "agent_id": agent_id,
                    "name": data.agent_name,
                    "owner_email": data.email,
                    "api_key": api_key
                },
                "description": "owner_email + name columns"
            }
        ]
        
        agent_created = False
        last_error = None
        
        for attempt in insert_attempts:
            try:
                await database.execute(attempt["sql"], attempt["params"])
                print(f"✅ Agent created using: {attempt['description']}")
                agent_created = True
                break
            except Exception as e:
                last_error = str(e)
                print(f"⚠️ Attempt '{attempt['description']}' failed: {e}")
                continue
        
        if not agent_created:
            print(f"❌ All insert attempts failed. Last error: {last_error}")
            raise Exception(f"Failed to create agent record. Database schema mismatch: {last_error}")
        
        return {
            "success": True,
            "message": "Agent account created successfully",
            "agent_id": agent_id,
            "api_key": api_key,  # Return only once - save this!
            "email": data.email,
            "important": "Save your API key now! It won't be shown again."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        # If agent creation failed, clean up the user account to allow retry
        try:
            await database.execute(
                "DELETE FROM users WHERE email = :email AND role = 'agent'",
                {"email": data.email}
            )
            print(f"🧹 Cleaned up user account for {data.email} after agent creation failure")
        except:
            pass
        
        raise HTTPException(
            status_code=500,
            detail=f"Registration failed: {str(e)}"
        )


@router.post("/login", response_model=AgentLoginResponse)
async def login_agent(data: AgentLoginRequest):
    """
    Login for AI Agent
    Returns JWT token and API key
    """
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
        
        # 3. Get agent record - try different column names
        agent = None
        # Try with agent_name first
        try:
            agent = await database.fetch_one(
                "SELECT agent_id, agent_name as name, owner_email, is_active, api_key FROM agents WHERE owner_email = :email",
                {"email": data.email}
            )
        except:
            # Fallback to name column
            try:
                agent = await database.fetch_one(
                    "SELECT agent_id, name, owner_email, is_active, api_key FROM agents WHERE owner_email = :email",
                    {"email": data.email}
                )
            except:
                pass
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent record not found. Please contact support.")
        
        # 4. Use the API key from agents table (already stored there)
        api_key = agent["api_key"]
        
        # 5. Update last login
        await database.execute(
            "UPDATE users SET last_login = :last_login WHERE email = :email",
            {"last_login": datetime.utcnow(), "email": data.email}
        )
        
        # 6. Create JWT token
        token = create_access_token({
            "sub": user["email"],
            "user_id": str(user["id"]),
            "role": "agent",
            "agent_id": agent["agent_id"]
        })
        
        return AgentLoginResponse(
            success=True,
            token=token,
            agent={
                "agent_id": agent["agent_id"],
                "agent_name": agent.get("name") or agent.get("agent_name") or "Agent",
                "email": agent.get("owner_email") or user["email"],
                "company": "",  # Not stored in agent table
                "description": agent.get("description", ""),
                "status": "active" if agent.get("is_active", True) else "inactive"
            },
            api_key=api_key
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Login failed: {str(e)}"
        )


@router.get("/me")
async def get_agent_profile(current_user: dict = Depends(lambda: {})):
    """
    Get current agent profile
    Requires authentication token
    """
    from utils.auth import get_current_user
    user = await get_current_user()
    
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="Not an agent account")
    
    # Get agent details
    agent = await database.fetch_one(
        """
        SELECT agent_id, name, email, company, status, tier, 
               rate_limit_rpm, daily_quota, total_orders, total_gmv
        FROM agents 
        WHERE email = :email
        """,
        {"email": user["email"]}
    )
    
    if not agent:
        raise HTTPException(status_code=404, detail="Agent record not found")
    
    return {
        "success": True,
        "agent": dict(agent)
    }


