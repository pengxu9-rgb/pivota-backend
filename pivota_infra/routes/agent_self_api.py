"""
Agent Self-Service API Routes

This module provides endpoints for agents to manage their own resources,
specifically to retrieve their full API key.
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from datetime import datetime

from db.database import database
from utils.auth import get_current_user

router = APIRouter(prefix="/agent/self", tags=["agent-self"])

@router.get("/api-key")
async def get_own_api_key(
    current_user: dict = Depends(get_current_user)
):
    """
    Get the full API key for the authenticated agent.
    
    This is a self-service endpoint that allows agents to retrieve
    their own API key using JWT authentication.
    """
    try:
        # Get agent_id from token (could be in agent_id field or email field)
        agent_id = current_user.get("agent_id") or current_user.get("email")
        if not agent_id:
            raise HTTPException(status_code=401, detail="Agent ID not found in token")
        
        # Only allow agent role to access this endpoint
        if current_user.get("role") != "agent":
            raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
        
        # Get the full API key from database
        # Support both agent_id and email as identifiers
        result = await database.fetch_one(
            """
            SELECT 
                agent_id,
                email,
                name,
                api_key,
                created_at,
                last_active
            FROM agents
            WHERE agent_id = :agent_id OR email = :agent_id
            """,
            {"agent_id": agent_id}
        )
        
        if not result:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        if not result["api_key"]:
            raise HTTPException(status_code=404, detail="API key not found")
        
        return {
            "status": "success",
            "agent_id": result["agent_id"],
            "email": result["email"],
            "name": result["name"],
            "api_key": result["api_key"],  # Return full key
            "created_at": result["created_at"].isoformat() if result["created_at"] else None,
            "last_active": result["last_active"].isoformat() if result["last_active"] else None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error retrieving API key: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve API key")

@router.get("/profile")
async def get_agent_profile(
    current_user: dict = Depends(get_current_user)
):
    """
    Get the agent's own profile information.
    """
    try:
        # Get agent_id from token
        agent_id = current_user.get("agent_id") or current_user.get("email")
        if not agent_id:
            raise HTTPException(status_code=401, detail="Agent ID not found in token")
        
        # Only allow agent role to access this endpoint
        if current_user.get("role") != "agent":
            raise HTTPException(status_code=403, detail="Only agents can access this endpoint")
        
        # Get agent profile
        # Support both agent_id and email as identifiers
        result = await database.fetch_one(
            """
            SELECT 
                agent_id,
                email,
                name,
                type as agent_type,
                created_at,
                last_active,
                is_active,
                company_name,
                website
            FROM agents
            WHERE agent_id = :agent_id OR email = :agent_id
            """,
            {"agent_id": agent_id}
        )
        
        if not result:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        return {
            "status": "success",
            "agent": {
                "agent_id": result["agent_id"],
                "email": result["email"],
                "name": result["name"],
                "agent_type": result["agent_type"] or "basic",
                "created_at": result["created_at"].isoformat() if result["created_at"] else None,
                "last_active": result["last_active"].isoformat() if result["last_active"] else None,
                "is_active": result["is_active"],
                "company_name": result["company_name"],
                "website": result["website"]
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error retrieving agent profile: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve profile")