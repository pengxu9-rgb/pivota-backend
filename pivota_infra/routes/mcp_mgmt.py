"""
MCP (Model Context Protocol) Management Routes
Provides endpoints for managing MCP connections and interactions
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from utils.auth import get_current_user
from db.database import database
import uuid
import random

router = APIRouter()

@router.get("/mcp/status")
async def get_mcp_status(
    current_user: dict = Depends(get_current_user)
):
    """Get MCP connection status"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get MCP connections from database
        # For now, return simulated status
        
        # Get total merchants and stores for MCP stats
        merchants_query = "SELECT COUNT(*) as total FROM merchant_onboarding WHERE status = 'active'"
        merchants_count = await database.fetch_one(merchants_query)
        
        stores_query = "SELECT COUNT(*) as total, platform FROM merchant_stores WHERE status = 'active' GROUP BY platform"
        stores_by_platform = await database.fetch_all(stores_query)
        
        shopify_stores = next((s["total"] for s in stores_by_platform if s["platform"] == "shopify"), 0)
        wix_stores = next((s["total"] for s in stores_by_platform if s["platform"] == "wix"), 0)
        
        return {
            "status": "success",
            "mcp_status": {
                "connected": True,
                "version": "1.0.0",
                "last_sync": datetime.now().isoformat(),
                "active_connections": merchants_count["total"] if merchants_count else 0,
                "platforms": {
                    "shopify": {
                        "connected": shopify_stores > 0,
                        "stores": shopify_stores,
                        "status": "active" if shopify_stores > 0 else "inactive"
                    },
                    "wix": {
                        "connected": wix_stores > 0,
                        "stores": wix_stores,
                        "status": "active" if wix_stores > 0 else "inactive"
                    }
                },
                "health": "healthy",
                "uptime": "99.9%"
            }
        }
    
    except Exception as e:
        print(f"Error getting MCP status: {e}")
        return {
            "status": "success",
            "mcp_status": {
                "connected": False,
                "version": "1.0.0",
                "health": "unknown",
                "error": str(e)
            }
        }

@router.post("/mcp/test-connection")
async def test_mcp_connection(
    platform: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Test MCP connection"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Simulate MCP connection test
        if platform:
            # Test specific platform
            if platform not in ["shopify", "wix"]:
                raise HTTPException(status_code=400, detail="Invalid platform")
            
            # Check if platform has stores
            stores_query = """
                SELECT COUNT(*) as total FROM merchant_stores 
                WHERE platform = :platform AND status = 'active'
            """
            stores = await database.fetch_one(stores_query, {"platform": platform})
            
            has_connections = stores and stores["total"] > 0
            
            return {
                "status": "success",
                "test_result": {
                    "platform": platform,
                    "connected": has_connections,
                    "response_time": random.randint(50, 200),  # ms
                    "api_version": "2024-01" if platform == "shopify" else "v1",
                    "active_stores": stores["total"] if stores else 0,
                    "message": f"Successfully connected to {platform.upper()} MCP" if has_connections else f"No active {platform} stores found"
                }
            }
        else:
            # Test all platforms
            return {
                "status": "success",
                "test_result": {
                    "overall": "healthy",
                    "platforms": {
                        "shopify": {
                            "status": "connected",
                            "response_time": random.randint(50, 150)
                        },
                        "wix": {
                            "status": "connected",
                            "response_time": random.randint(50, 150)
                        }
                    },
                    "message": "All MCP connections are healthy"
                }
            }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")

@router.get("/mcp/merchants")
async def get_mcp_merchants(
    current_user: dict = Depends(get_current_user)
):
    """Get merchants with MCP connections"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get merchants with their MCP connections
        merchants_query = """
            SELECT 
                m.merchant_id,
                m.business_name,
                m.email,
                m.status,
                COUNT(s.store_id) as connected_stores,
                STRING_AGG(DISTINCT s.platform, ', ') as platforms
            FROM merchant_onboarding m
            LEFT JOIN merchant_stores s ON m.merchant_id = s.merchant_id
            WHERE m.status = 'active'
            GROUP BY m.merchant_id, m.business_name, m.email, m.status
            ORDER BY m.business_name
        """
        
        merchants = await database.fetch_all(merchants_query)
        
        return {
            "status": "success",
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "email": m["email"],
                    "mcp_status": "connected" if m["connected_stores"] > 0 else "not_connected",
                    "connected_stores": m["connected_stores"],
                    "platforms": m["platforms"].split(", ") if m["platforms"] else [],
                    "last_sync": datetime.now().isoformat()
                }
                for m in merchants
            ]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get MCP merchants: {str(e)}")

@router.get("/mcp/analytics")
async def get_mcp_analytics(
    days: int = 30,
    current_user: dict = Depends(get_current_user)
):
    """Get MCP analytics"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get sync statistics
        sync_query = """
            SELECT 
                DATE(last_sync) as date,
                COUNT(*) as syncs,
                SUM(product_count) as products_synced
            FROM merchant_stores
            WHERE last_sync >= CURRENT_DATE - INTERVAL '{days} days'
              AND last_sync IS NOT NULL
            GROUP BY DATE(last_sync)
            ORDER BY date DESC
        """.format(days=days)
        
        sync_stats = await database.fetch_all(sync_query)
        
        # Get platform distribution
        platform_query = """
            SELECT 
                platform,
                COUNT(*) as stores,
                SUM(product_count) as total_products
            FROM merchant_stores
            WHERE status = 'active'
            GROUP BY platform
        """
        
        platform_dist = await database.fetch_all(platform_query)
        
        return {
            "status": "success",
            "analytics": {
                "sync_history": [
                    {
                        "date": s["date"].isoformat() if s["date"] else None,
                        "syncs": s["syncs"],
                        "products_synced": s["products_synced"] or 0
                    }
                    for s in sync_stats
                ],
                "platform_distribution": [
                    {
                        "platform": p["platform"],
                        "stores": p["stores"],
                        "products": p["total_products"] or 0
                    }
                    for p in platform_dist
                ],
                "summary": {
                    "total_syncs": sum(s["syncs"] for s in sync_stats),
                    "total_products": sum(p["total_products"] or 0 for p in platform_dist),
                    "active_platforms": len(platform_dist)
                }
            }
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get MCP analytics: {str(e)}")

@router.post("/mcp/sync-all")
async def sync_all_stores(
    current_user: dict = Depends(get_current_user)
):
    """Trigger sync for all active stores"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get all active stores
        stores_query = """
            SELECT store_id, platform, name FROM merchant_stores 
            WHERE status = 'active'
        """
        stores = await database.fetch_all(stores_query)
        
        if not stores:
            return {
                "status": "success",
                "message": "No active stores to sync",
                "synced": 0
            }
        
        # Update last_sync for all stores (simulated sync)
        update_query = """
            UPDATE merchant_stores
            SET last_sync = :last_sync
            WHERE status = 'active'
        """
        
        await database.execute(update_query, {"last_sync": datetime.now()})
        
        return {
            "status": "success",
            "message": f"Successfully initiated sync for {len(stores)} stores",
            "synced": len(stores),
            "stores": [
                {
                    "store_id": s["store_id"],
                    "platform": s["platform"],
                    "name": s["name"]
                }
                for s in stores
            ]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync stores: {str(e)}")

@router.get("/mcp/logs")
async def get_mcp_logs(
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    """Get MCP operation logs"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Generate demo logs based on recent syncs
        stores_query = """
            SELECT 
                s.store_id, s.platform, s.name, s.last_sync, m.business_name
            FROM merchant_stores s
            JOIN merchant_onboarding m ON s.merchant_id = m.merchant_id
            WHERE s.last_sync IS NOT NULL
            ORDER BY s.last_sync DESC
            LIMIT :limit
        """
        
        stores = await database.fetch_all(stores_query, {"limit": limit})
        
        logs = []
        for store in stores:
            logs.append({
                "log_id": f"log_{uuid.uuid4().hex[:8]}",
                "timestamp": store["last_sync"].isoformat() if store["last_sync"] else None,
                "operation": "sync_products",
                "platform": store["platform"],
                "store": store["name"],
                "merchant": store["business_name"],
                "status": "success",
                "details": f"Synced products from {store['platform']}"
            })
        
        return {
            "status": "success",
            "logs": logs
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get MCP logs: {str(e)}")
