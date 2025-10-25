"""
Agents Management Routes
Provides endpoints for managing agents in the system
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, List
from datetime import datetime, timedelta
from utils.auth import get_current_user
from db.database import database
import uuid
import secrets

router = APIRouter()

@router.post("/agents/create")
async def create_agent(
    name: str,
    email: str,
    phone: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Create a new agent"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Create agents table if not exists
        create_table_query = """
            CREATE TABLE IF NOT EXISTS agents (
                agent_id VARCHAR(50) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(100) UNIQUE NOT NULL,
                phone VARCHAR(20),
                api_key VARCHAR(100) UNIQUE,
                status VARCHAR(20) DEFAULT 'active',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP WITH TIME ZONE,
                total_merchants INT DEFAULT 0,
                total_transactions INT DEFAULT 0
            )
        """
        await database.execute(create_table_query)
        
        # Check if agent exists
        check_query = "SELECT agent_id FROM agents WHERE email = :email"
        existing = await database.fetch_one(check_query, {"email": email})
        
        if existing:
            raise HTTPException(status_code=400, detail="Agent with this email already exists")
        
        # Generate agent ID and API key
        agent_id = f"agent_{uuid.uuid4().hex[:12]}"
        api_key = f"ak_{secrets.token_urlsafe(32)}"
        
        # Insert agent
        insert_query = """
            INSERT INTO agents (
                agent_id, name, email, phone, api_key, status, created_at, last_active
            ) VALUES (
                :agent_id, :name, :email, :phone, :api_key, :status, :created_at, :last_active
            )
        """
        
        await database.execute(insert_query, {
            "agent_id": agent_id,
            "name": name,
            "email": email,
            "phone": phone,
            "api_key": api_key,
            "status": "active",
            "created_at": datetime.now(),
            "last_active": datetime.now()
        })
        
        return {
            "status": "success",
            "message": "Agent created successfully",
            "agent": {
                "agent_id": agent_id,
                "name": name,
                "email": email,
                "api_key": api_key
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating agent: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {str(e)}")

@router.get("/agents/{agent_id}")
async def get_agent_details(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get detailed agent information"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        agent_query = "SELECT * FROM agents WHERE agent_id = :agent_id"
        agent = await database.fetch_one(agent_query, {"agent_id": agent_id})
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Get agent's merchants
        merchants_query = """
            SELECT COUNT(*) as total_merchants,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active_merchants
            FROM merchant_onboarding
            WHERE agent_id = :agent_id
        """
        merchants_stats = await database.fetch_one(merchants_query, {"agent_id": agent_id})
        
        # Get transaction stats
        trans_query = """
            SELECT COUNT(*) as total_transactions,
                   COALESCE(SUM(amount), 0) as total_volume
            FROM orders o
            JOIN merchant_onboarding m ON o.merchant_id = m.merchant_id
            WHERE m.agent_id = :agent_id
        """
        trans_stats = await database.fetch_one(trans_query, {"agent_id": agent_id})
        
        return {
            "status": "success",
            "agent": {
                "agent_id": agent["agent_id"],
                "name": agent["name"],
                "email": agent["email"],
                "phone": agent["phone"],
                "status": agent["status"],
                "created_at": agent["created_at"].isoformat() if agent["created_at"] else None,
                "last_active": agent["last_active"].isoformat() if agent["last_active"] else None,
                "stats": {
                    "total_merchants": merchants_stats["total_merchants"] if merchants_stats else 0,
                    "active_merchants": merchants_stats["active_merchants"] if merchants_stats else 0,
                    "total_transactions": trans_stats["total_transactions"] if trans_stats else 0,
                    "total_volume": float(trans_stats["total_volume"]) if trans_stats else 0
                }
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get agent details: {str(e)}")

@router.get("/agents/{agent_id}/analytics")
async def get_agent_analytics(
    agent_id: str,
    days: int = 30,
    current_user: dict = Depends(get_current_user)
):
    """Get agent analytics"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get transactions over time
        analytics_query = """
            SELECT 
                DATE(o.created_at) as date,
                COUNT(*) as transactions,
                COALESCE(SUM(o.amount), 0) as revenue
            FROM orders o
            JOIN merchant_onboarding m ON o.merchant_id = m.merchant_id
            WHERE m.agent_id = :agent_id
              AND o.created_at >= CURRENT_DATE - INTERVAL '{days} days'
            GROUP BY DATE(o.created_at)
            ORDER BY date DESC
        """.format(days=days)
        
        trends = await database.fetch_all(analytics_query, {"agent_id": agent_id})
        
        return {
            "status": "success",
            "analytics": [
                {
                    "date": t["date"].isoformat() if t["date"] else None,
                    "transactions": t["transactions"],
                    "revenue": float(t["revenue"])
                }
                for t in trends
            ]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get analytics: {str(e)}")

@router.post("/agents/{agent_id}/reset-api-key")
async def reset_agent_api_key(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Reset agent's API key"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Generate new API key
        new_api_key = f"ak_{secrets.token_urlsafe(32)}"
        
        update_query = """
            UPDATE agents
            SET api_key = :api_key
            WHERE agent_id = :agent_id
        """
        
        await database.execute(update_query, {
            "api_key": new_api_key,
            "agent_id": agent_id
        })
        
        return {
            "status": "success",
            "message": "API key reset successfully",
            "api_key": new_api_key
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset API key: {str(e)}")

@router.delete("/agents/{agent_id}")
async def deactivate_agent(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Deactivate an agent"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        update_query = """
            UPDATE agents
            SET status = 'inactive'
            WHERE agent_id = :agent_id
        """
        
        await database.execute(update_query, {"agent_id": agent_id})
        
        return {
            "status": "success",
            "message": "Agent deactivated successfully"
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to deactivate agent: {str(e)}")

@router.put("/agents/{agent_id}")
async def update_agent(
    agent_id: str,
    name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Update agent information"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Build update query dynamically
        updates = []
        params = {"agent_id": agent_id}
        
        if name:
            updates.append("name = :name")
            params["name"] = name
        if email:
            updates.append("email = :email")
            params["email"] = email
        if phone:
            updates.append("phone = :phone")
            params["phone"] = phone
        if status:
            updates.append("status = :status")
            params["status"] = status
        
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        update_query = f"""
            UPDATE agents
            SET {', '.join(updates)}
            WHERE agent_id = :agent_id
        """
        
        await database.execute(update_query, params)
        
        return {
            "status": "success",
            "message": "Agent updated successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update agent: {str(e)}")





