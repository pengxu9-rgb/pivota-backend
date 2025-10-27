"""
Temporary debug endpoint for Agent data verification
"""
from fastapi import APIRouter, Depends, Header
from typing import Dict, Any, Optional
from datetime import datetime
from db.database import database
from utils.auth import decode_token

router = APIRouter(prefix="/agent/debug", tags=["Agent Debug"])


@router.get("/usage-logs")
async def check_usage_logs(
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    Check agent_usage_logs table for debugging
    Returns endpoint counts and sample data
    """
    try:
        # Resolve agent_id from JWT
        agent_id = None
        if authorization and authorization.startswith("Bearer "):
            try:
                payload = decode_token(authorization.split(" ")[1])
                agent_id = payload.get("agent_id")
            except:
                pass
        
        if not agent_id:
            return {"error": "Missing or invalid token"}
        
        # Check if table exists
        table_exists = await database.fetch_val(
            """SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'agent_usage_logs'
            )"""
        )
        
        if not table_exists:
            return {
                "error": "agent_usage_logs table does not exist",
                "agent_id": agent_id
            }
        
        # Get total count
        total_count = await database.fetch_val(
            "SELECT COUNT(*) FROM agent_usage_logs WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        ) or 0
        
        # Get endpoint breakdown
        endpoint_counts = await database.fetch_all(
            """SELECT endpoint, COUNT(*) as count, MAX(timestamp) as last_call
               FROM agent_usage_logs 
               WHERE agent_id = :agent_id
               GROUP BY endpoint 
               ORDER BY count DESC""",
            {"agent_id": agent_id}
        )
        
        # Get recent logs
        recent_logs = await database.fetch_all(
            """SELECT endpoint, method, status_code, timestamp, response_time_ms
               FROM agent_usage_logs 
               WHERE agent_id = :agent_id
               ORDER BY timestamp DESC 
               LIMIT 10""",
            {"agent_id": agent_id}
        )
        
        return {
            "agent_id": agent_id,
            "total_logs": total_count,
            "endpoint_counts": [
                {
                    "endpoint": row["endpoint"],
                    "count": row["count"],
                    "last_call": str(row["last_call"])
                }
                for row in endpoint_counts
            ],
            "recent_logs": [
                {
                    "endpoint": row["endpoint"],
                    "method": row["method"],
                    "status_code": row["status_code"],
                    "timestamp": str(row["timestamp"]),
                    "response_time_ms": row["response_time_ms"]
                }
                for row in recent_logs
            ]
        }
        
    except Exception as e:
        return {
            "error": str(e),
            "error_type": type(e).__name__
        }


@router.get("/orders")
async def check_orders(
    authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    Check orders table for this agent
    """
    try:
        # Resolve agent_id from JWT
        agent_id = None
        if authorization and authorization.startswith("Bearer "):
            try:
                payload = decode_token(authorization.split(" ")[1])
                agent_id = payload.get("agent_id")
            except:
                pass
        
        if not agent_id:
            return {"error": "Missing or invalid token"}
        
        # Get order stats
        orders = await database.fetch_all(
            """SELECT order_id, merchant_id, total, payment_status, created_at
               FROM orders
               WHERE agent_id = :agent_id
               ORDER BY created_at DESC""",
            {"agent_id": agent_id}
        )
        
        total_orders = len(orders)
        paid_orders = len([o for o in orders if o["payment_status"] == "paid"])
        total_revenue = sum(float(o["total"]) for o in orders if o["payment_status"] == "paid")
        
        # Get unique merchants
        merchants = await database.fetch_all(
            """SELECT DISTINCT m.merchant_id, m.business_name, m.store_url
               FROM orders o
               JOIN merchant_onboarding m ON o.merchant_id = m.merchant_id
               WHERE o.agent_id = :agent_id""",
            {"agent_id": agent_id}
        )
        
        return {
            "agent_id": agent_id,
            "total_orders": total_orders,
            "paid_orders": paid_orders,
            "pending_orders": total_orders - paid_orders,
            "total_revenue": total_revenue,
            "avg_order_value": total_revenue / paid_orders if paid_orders > 0 else 0,
            "orders": [
                {
                    "order_id": o["order_id"],
                    "merchant_id": o["merchant_id"],
                    "total": float(o["total"]),
                    "status": o["payment_status"],
                    "created_at": str(o["created_at"])
                }
                for o in orders
            ],
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "store_url": m["store_url"]
                }
                for m in merchants
            ]
        }
        
    except Exception as e:
        return {
            "error": str(e),
            "error_type": type(e).__name__
        }
