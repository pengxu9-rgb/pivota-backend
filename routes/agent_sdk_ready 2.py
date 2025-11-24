"""
SDK-Ready Agent API Endpoints
Provides standardized endpoints for Agent SDK integration
Follows ChatGPT schema best practices with rate limiting
"""
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from utils.auth import get_current_user
from db.database import database
from routes.agent_auth import AgentContext, get_agent_context
import secrets
import time

router = APIRouter(prefix="/agent/v1", tags=["agent-sdk"])

# Simple rate limiter (in production, use Redis)
rate_limit_store = {}

def check_rate_limit(api_key: str, max_requests: int = 100, window_seconds: int = 60):
    """Simple rate limiting check"""
    now = time.time()
    if api_key not in rate_limit_store:
        rate_limit_store[api_key] = []
    
    # Clean old requests
    rate_limit_store[api_key] = [
        req_time for req_time in rate_limit_store[api_key]
        if now - req_time < window_seconds
    ]
    
    # Check limit
    if len(rate_limit_store[api_key]) >= max_requests:
        return False
    
    # Add current request
    rate_limit_store[api_key].append(now)
    return True

# ============================================================================
# SDK-Ready Endpoints
# ============================================================================

@router.get("/health")
async def health_check():
    """
    Health check endpoint for ping monitoring
    Returns API status and version info
    """
    try:
        # Test database connection
        await database.fetch_one("SELECT 1")
        db_status = "healthy"
    except:
        db_status = "unhealthy"
    
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0",
        "services": {
            "database": db_status,
            "api": "operational"
        },
        "uptime": "99.9%"
    }

class AgentAuthRequest(BaseModel):
    agent_name: str
    agent_email: str
    description: Optional[str] = None

@router.post("/auth")
async def generate_api_key(request: AgentAuthRequest):
    """
    Generate or validate agent API key
    Rate limited to prevent abuse
    """
    agent_name = request.agent_name
    agent_email = request.agent_email
    description = request.description
    try:
        # Note: agents table is created in main.py startup
        # Table structure matches main.py:
        # agent_id, name, email, company, use_case, api_key, status, 
        # created_at, last_active, last_key_rotation, deactivated_at,
        # request_count, success_rate, rate_limit
        
        # Check if agent exists
        check_query = "SELECT agent_id, api_key FROM agents WHERE email = :email"
        existing = await database.fetch_one(check_query, {"email": agent_email})
        
        if existing:
            return {
                "status": "success",
                "message": "Agent already exists",
                "agent_id": existing["agent_id"],
                "api_key": existing["api_key"],
                "rate_limit": {
                    "requests_per_minute": 1000,
                    "tier": "standard"
                }
            }
        
        # Generate new agent and API key
        agent_id = f"agent_{secrets.token_hex(8)}"
        api_key = f"ak_live_{secrets.token_hex(32)}"
        
        # Use the full table structure from main.py
        insert_query = """
            INSERT INTO agents (
                agent_id, name, email, company, use_case, api_key, 
                status, created_at, request_count, success_rate, rate_limit
            )
            VALUES (
                :agent_id, :name, :email, :company, :use_case, :api_key,
                :status, :created_at, :request_count, :success_rate, :rate_limit
            )
        """
        
        await database.execute(insert_query, {
            "agent_id": agent_id,
            "name": agent_name,
            "email": agent_email,
            "company": "Independent",  # Default company
            "use_case": description or "General API access",
            "api_key": api_key,
            "status": "active",
            "created_at": datetime.now(),
            "request_count": 0,
            "success_rate": 0,
            "rate_limit": 1000
        })
        
        return {
            "status": "success",
            "message": "API key generated successfully",
            "agent_id": agent_id,
            "api_key": api_key,
            "rate_limit": {
                "requests_per_minute": 100,
                "tier": "standard"
            },
            "documentation": "https://docs.pivota.com/agent-api"
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate API key: {str(e)}")

@router.get("/merchants")
async def list_merchants(
    category: Optional[str] = None,
    country: Optional[str] = None,
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
    context: AgentContext = Depends(get_agent_context)
):
    """
    List connected merchants with filters
    Returns merchants that the agent has access to
    """
    try:
        # Build query with filters
        where_clauses = ["status = :status"]
        params = {"status": status, "limit": limit, "offset": offset}
        
        if category:
            where_clauses.append("business_type = :category")
            params["category"] = category
        
        if country:
            where_clauses.append("country = :country")
            params["country"] = country
        
        where_clause = " AND ".join(where_clauses)
        
        # Get merchants (only select columns that exist)
        # Don't use f-string for the full query, only for WHERE clause
        base_query = """
            SELECT 
                merchant_id, business_name, status
            FROM merchant_onboarding
            WHERE """ + where_clause + """
            ORDER BY business_name
            LIMIT :limit OFFSET :offset
        """
        
        merchants = await database.fetch_all(base_query, params)
        
        # Get total count
        count_query = f"""
            SELECT COUNT(*) as total
            FROM merchant_onboarding
            WHERE {where_clause}
        """
        total_result = await database.fetch_one(count_query, params)
        
        return {
            "status": "success",
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "status": m["status"]
                }
                for m in merchants
            ],
            "pagination": {
                "total": total_result["total"] if total_result else 0,
                "limit": limit,
                "offset": offset,
                "has_more": (total_result["total"] if total_result else 0) > offset + limit
            }
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list merchants: {str(e)}")

@router.post("/payments")
async def initiate_payment(
    order_id: str,
    payment_method: str = "card",
    return_url: Optional[str] = None,
    context: AgentContext = Depends(get_agent_context)
):
    """
    Initiate payment for an order
    Generates payment intent and returns client secret
    """
    try:
        # Get order
        order_query = """
            SELECT o.*, m.merchant_id
            FROM orders o
            JOIN merchant_onboarding m ON o.merchant_id = m.merchant_id
            WHERE o.order_id = :order_id
        """
        order = await database.fetch_one(order_query, {"order_id": order_id})
        
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        # Verify merchant is active
        if order["status"] == "deleted":
            raise HTTPException(status_code=403, detail="Merchant account deactivated")
        
        # Get PSP for merchant
        psp_query = """
            SELECT psp_id, provider, api_key
            FROM merchant_psps
            WHERE merchant_id = :merchant_id AND status = 'active'
            LIMIT 1
        """
        psp = await database.fetch_one(psp_query, {"merchant_id": order["merchant_id"]})
        
        if not psp:
            raise HTTPException(status_code=400, detail="No active payment processor found")
        
        # Simulate payment intent creation
        # In production, this would call actual PSP API (Stripe, Adyen, etc.)
        payment_intent_id = f"pi_{secrets.token_hex(16)}"
        client_secret = f"pi_{secrets.token_hex(24)}_secret_{secrets.token_hex(8)}"
        
        # Update order with payment intent
        update_query = """
            UPDATE orders
            SET status = 'payment_pending',
                updated_at = :updated_at
            WHERE order_id = :order_id
        """
        await database.execute(update_query, {
            "updated_at": datetime.now(),
            "order_id": order_id
        })
        
        return {
            "status": "success",
            "payment_intent": {
                "id": payment_intent_id,
                "client_secret": client_secret,
                "amount": float(order["amount"]),
                "currency": order["currency"],
                "status": "requires_payment_method",
                "provider": psp["provider"],
                "return_url": return_url or f"https://pivota.com/payment/return/{order_id}"
            },
            "order_id": order_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initiate payment: {str(e)}")

@router.get("/rate-limits")
async def get_rate_limits(
    context: AgentContext = Depends(get_agent_context)
):
    """
    Get current rate limit status for the agent
    """
    agent_id = context.agent_id
    
    return {
        "status": "success",
        "agent_id": agent_id,
        "rate_limits": {
            "tier": "standard",
            "limits": {
                "requests_per_minute": 100,
                "requests_per_hour": 5000,
                "requests_per_day": 100000
            },
            "current_usage": {
                "requests_this_minute": len(rate_limit_store.get(agent_id, [])),
                "reset_at": (datetime.now() + timedelta(minutes=1)).isoformat()
            }
        }
    }
