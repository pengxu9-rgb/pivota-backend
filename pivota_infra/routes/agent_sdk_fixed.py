"""
SDK-Ready Agent API Endpoints - COMPREHENSIVE FIX
Properly handles all database schema issues and edge cases
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from db.database import database
from routes.agent_auth import AgentContext, get_agent_context
from utils.logger import logger
import secrets
import json

router = APIRouter(prefix="/agent/v1", tags=["agent-sdk"])

# ============================================================================
# HEALTH CHECK
# ============================================================================

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
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
        }
    }

# ============================================================================
# AUTHENTICATION
# ============================================================================

class AgentAuthRequest(BaseModel):
    agent_name: str
    agent_email: str
    company: Optional[str] = "Independent"
    description: Optional[str] = None

@router.post("/auth")
async def generate_api_key(request: AgentAuthRequest):
    """Generate or retrieve agent API key"""
    try:
        # Check if agent exists
        check_query = "SELECT agent_id, api_key FROM agents WHERE email = :email"
        existing = await database.fetch_one(check_query, {"email": request.agent_email})
        
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
        
        # Generate new agent
        agent_id = f"agent_{secrets.token_hex(8)}"
        api_key = f"ak_live_{secrets.token_hex(32)}"
        
        # Insert with full schema
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
            "name": request.agent_name,
            "email": request.agent_email,
            "company": request.company,
            "use_case": request.description or "General API access",
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
                "requests_per_minute": 1000,
                "tier": "standard"
            }
        }
    
    except Exception as e:
        logger.error(f"Failed to generate API key: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate API key: {str(e)}")

# ============================================================================
# MERCHANTS
# ============================================================================

@router.get("/merchants")
async def list_merchants(
    status: str = "active",
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context)
):
    """List connected merchants - FIXED to only query existing columns"""
    try:
        # Map status values
        if status == "active":
            db_status = "approved"
        else:
            db_status = status
        
        # Query only existing columns
        query = """
            SELECT 
                merchant_id, 
                business_name, 
                status,
                store_url,
                website,
                region,
                contact_email,
                psp_connected,
                psp_type,
                created_at
            FROM merchant_onboarding
            WHERE status = :status
            AND status != 'deleted'
            ORDER BY business_name
            LIMIT :limit OFFSET :offset
        """
        
        merchants = await database.fetch_all(query, {
            "status": db_status,
            "limit": limit,
            "offset": offset
        })
        
        # Get total count
        count_query = """
            SELECT COUNT(*) as total
            FROM merchant_onboarding
            WHERE status = :status
            AND status != 'deleted'
        """
        total_result = await database.fetch_one(count_query, {"status": db_status})
        
        return {
            "status": "success",
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "status": "active" if m["status"] == "approved" else m["status"],
                    "store_url": m["store_url"],
                    "website": m["website"],
                    "region": m["region"],
                    "contact_email": m["contact_email"],
                    "psp_connected": m["psp_connected"],
                    "psp_type": m["psp_type"],
                    "created_at": m["created_at"].isoformat() if m["created_at"] else None
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
        logger.error(f"Failed to list merchants: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list merchants: {str(e)}")

# ============================================================================
# PRODUCTS SEARCH
# ============================================================================

@router.get("/products/search")
async def search_products(
    merchant_id: Optional[str] = None,
    query: Optional[str] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock: Optional[bool] = None,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context)
):
    """Search products - supports cross-merchant search"""
    try:
        # Build WHERE clauses
        where_clauses = []
        params = {"limit": limit, "offset": offset}
        
        if merchant_id:
            # Verify access to merchant
            merchant_check = await database.fetch_one(
                "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :mid AND status != 'deleted'",
                {"mid": merchant_id}
            )
            if not merchant_check:
                raise HTTPException(status_code=404, detail="Merchant not found")
            
            where_clauses.append("merchant_id = :merchant_id")
            params["merchant_id"] = merchant_id
        
        if query:
            # Search in JSON fields using PostgreSQL JSON operators
            where_clauses.append("(LOWER(p.product_data->>'name') LIKE :query OR LOWER(p.product_data->>'description') LIKE :query)")
            params["query"] = f"%{query.lower()}%"
        
        if category:
            where_clauses.append("LOWER(p.product_data->>'category') = :category")
            params["category"] = category.lower()
        
        if min_price is not None:
            where_clauses.append("(p.product_data->>'price')::numeric >= :min_price")
            params["min_price"] = min_price
        
        if max_price is not None:
            where_clauses.append("(p.product_data->>'price')::numeric <= :max_price")
            params["max_price"] = max_price
        
        if in_stock is not None:
            where_clauses.append("(p.product_data->>'in_stock')::boolean = :in_stock")
            params["in_stock"] = in_stock
        
        # Build query
        where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"
        
        # Get products from cache
        query_str = f"""
            SELECT 
                p.id,
                p.merchant_id,
                p.platform,
                p.platform_product_id,
                p.product_data,
                p.cached_at,
                m.business_name as merchant_name
            FROM products_cache p
            JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
            WHERE {where_clause}
            AND m.status != 'deleted'
            AND p.cache_status != 'expired'
            ORDER BY p.cached_at DESC
            LIMIT :limit OFFSET :offset
        """
        
        products = await database.fetch_all(query_str, params)
        
        # Get total count
        count_query = f"""
            SELECT COUNT(*) as total
            FROM products_cache p
            JOIN merchant_onboarding m ON p.merchant_id = m.merchant_id
            WHERE {where_clause}
            AND m.status != 'deleted'
        """
        
        # Remove limit and offset from params for count query
        count_params = {k: v for k, v in params.items() if k not in ['limit', 'offset']}
        total_result = await database.fetch_one(count_query, count_params)
        
        # Extract product data from JSON and calculate relevance scores
        product_list = []
        for p in products:
            try:
                # Convert Row to dict safely
                p_dict = dict(p)
                # Extract product data from JSON column (might be string or dict)
                product_data_raw = p_dict.get("product_data")
                if isinstance(product_data_raw, str):
                    product_info = json.loads(product_data_raw)
                elif isinstance(product_data_raw, dict):
                    product_info = product_data_raw
                else:
                    product_info = {}
                
                # Build response object - merge product_info with metadata
                product_dict = {
                    **product_info,  # Spread all product fields
                    "merchant_id": p_dict["merchant_id"],
                    "merchant_name": p_dict.get("merchant_name"),
                    "platform": p_dict["platform"],
                    "cached_at": p_dict["cached_at"].isoformat() if p_dict.get("cached_at") else None
                }
                
                # Add relevance score
                if query:
                    score = 0
                    name_lower = str(product_dict.get("name", "")).lower()
                    desc_lower = str(product_dict.get("description", "")).lower()
                    query_lower = query.lower()
                    
                    if query_lower in name_lower:
                        score += 10
                    if name_lower.startswith(query_lower):
                        score += 5
                    if query_lower in desc_lower:
                        score += 3
                    
                    product_dict["relevance_score"] = score
                
                product_list.append(product_dict)
            except Exception as e:
                logger.error(f"Error processing product: {e}")
                continue
        
        # Sort by relevance if query provided
        if query:
            product_list.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
        
        return {
            "status": "success",
            "products": product_list,
            "pagination": {
                "total": total_result["total"] if total_result else 0,
                "limit": limit,
                "offset": offset,
                "has_more": (total_result["total"] if total_result else 0) > offset + limit
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to search products: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to search products: {str(e)}")

# ============================================================================
# ORDERS
# ============================================================================

@router.get("/orders")
async def list_orders(
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    context: AgentContext = Depends(get_agent_context)
):
    """List orders"""
    try:
        # Build WHERE clauses
        where_clauses = []
        params = {"limit": limit, "offset": offset}
        
        if merchant_id:
            where_clauses.append("o.merchant_id = :merchant_id")
            params["merchant_id"] = merchant_id
        
        if status:
            where_clauses.append("o.status = :status")
            params["status"] = status
        
        where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"
        
        # Get orders
        query = f"""
            SELECT 
                o.*,
                m.business_name as merchant_name
            FROM orders o
            JOIN merchant_onboarding m ON o.merchant_id = m.merchant_id
            WHERE {where_clause}
            AND m.status != 'deleted'
            ORDER BY o.created_at DESC
            LIMIT :limit OFFSET :offset
        """
        
        orders = await database.fetch_all(query, params)
        
        return {
            "status": "success",
            "orders": [dict(o) for o in orders],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "has_more": len(orders) == limit
            }
        }
    
    except Exception as e:
        logger.error(f"Failed to list orders: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list orders: {str(e)}")

# ============================================================================
# OpenAPI SPEC
# ============================================================================

@router.get("/openapi.json")
async def get_openapi_spec():
    """Return OpenAPI specification for SDK generation"""
    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Pivota Agent API",
            "version": "1.0.0",
            "description": "Production-ready API for agent integrations"
        },
        "servers": [
            {"url": "https://api.pivota.com/agent/v1"}
        ],
        "security": [
            {"ApiKeyAuth": []}
        ],
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key"
                }
            }
        },
        "paths": {
            "/health": {
                "get": {
                    "summary": "Health Check",
                    "responses": {
                        "200": {"description": "API is healthy"}
                    }
                }
            },
            "/auth": {
                "post": {
                    "summary": "Generate API Key",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["agent_name", "agent_email"],
                                    "properties": {
                                        "agent_name": {"type": "string"},
                                        "agent_email": {"type": "string"},
                                        "company": {"type": "string"},
                                        "description": {"type": "string"}
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {"description": "API key generated"}
                    }
                }
            },
            "/merchants": {
                "get": {
                    "summary": "List Merchants",
                    "parameters": [
                        {"name": "status", "in": "query", "schema": {"type": "string"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                        {"name": "offset", "in": "query", "schema": {"type": "integer"}}
                    ],
                    "responses": {
                        "200": {"description": "List of merchants"}
                    }
                }
            },
            "/products/search": {
                "get": {
                    "summary": "Search Products",
                    "parameters": [
                        {"name": "merchant_id", "in": "query", "schema": {"type": "string"}},
                        {"name": "query", "in": "query", "schema": {"type": "string"}},
                        {"name": "category", "in": "query", "schema": {"type": "string"}},
                        {"name": "min_price", "in": "query", "schema": {"type": "number"}},
                        {"name": "max_price", "in": "query", "schema": {"type": "number"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                        {"name": "offset", "in": "query", "schema": {"type": "integer"}}
                    ],
                    "responses": {
                        "200": {"description": "Search results"}
                    }
                }
            },
            "/payments": {
                "post": {
                    "summary": "Create Payment",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["order_id", "payment_method"],
                                    "properties": {
                                        "order_id": {"type": "string"},
                                        "payment_method": {"type": "object"},
                                        "return_url": {"type": "string"},
                                        "idempotency_key": {"type": "string"}
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {"description": "Payment created"}
                    }
                }
            }
        }
    }
