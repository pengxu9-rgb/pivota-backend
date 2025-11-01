"""Merchant Dashboard API Routes - Fixed Version (No Demo Data)"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import random
import httpx
import string
import json
from utils.auth import get_current_user
from db.database import database
from models.order_response import format_order_for_response
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

# REMOVED ALL DEMO_MERCHANT_DATA - Use real data only

@router.get("/merchant/profile")
async def get_merchant_profile(current_user: dict = Depends(get_current_user)):
    """Get merchant profile from real database only"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = current_user.get("merchant_id")
    
    if not merchant_id:
        # Try to get merchant_id from merchant_users table
        user_query = """
            SELECT merchant_id FROM merchant_users 
            WHERE user_id = :user_id
        """
        user_result = await database.fetch_one(user_query, {"user_id": current_user["user_id"]})
        if user_result:
            merchant_id = user_result["merchant_id"]
        else:
            raise HTTPException(status_code=404, detail="Merchant not found for this user")
    
    try:
        # Get real merchant data only
        query = """
            SELECT * FROM merchants WHERE merchant_id = :merchant_id
        """
        merchant = await database.fetch_one(query, {"merchant_id": merchant_id})
        
        if not merchant:
            raise HTTPException(status_code=404, detail=f"Merchant {merchant_id} not found")
        
        # Get real statistics
        stats_query = """
            SELECT 
                COUNT(*) as total_orders,
                COALESCE(SUM(total), 0) as total_revenue
            FROM orders
            WHERE merchant_id = :merchant_id
        """
        stats = await database.fetch_one(stats_query, {"merchant_id": merchant_id})
        
        return {
            "status": "success",
            "data": {
                "merchant_id": merchant["merchant_id"],
                "business_name": merchant["business_name"],
                "email": merchant["email"],
                "phone": merchant["phone"],
                "website": merchant["website"],
                "country": merchant["country"],
                "business_type": merchant["business_type"],
                "status": merchant["status"],
                "created_at": merchant["created_at"].isoformat() if merchant["created_at"] else None,
                "total_orders": stats["total_orders"] if stats else 0,
                "total_revenue": float(stats["total_revenue"]) if stats else 0
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching merchant profile: {e}")
        # NO FALLBACK TO DEMO DATA - Show real error
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.get("/merchant/{merchant_id}/integrations")
async def get_merchant_stores(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant stores from database only"""
    stores = []
    try:
        # Real data only
        query = """
            SELECT 
                msi.integration_id as id,
                msi.platform,
                msi.store_name as name,
                msi.status,
                msi.created_at as connected_at,
                msi.credentials,
                COUNT(DISTINCT p.product_id) as product_count
            FROM merchant_store_integrations msi
            LEFT JOIN products_cache p ON p.merchant_id = msi.merchant_id 
                AND p.platform = msi.platform
            WHERE msi.merchant_id = :merchant_id
            GROUP BY msi.integration_id, msi.platform, msi.store_name, 
                     msi.status, msi.created_at, msi.credentials
        """
        rows = await database.fetch_all(query, {"merchant_id": merchant_id})
        
        for row in rows:
            stores.append({
                "id": row["id"],
                "platform": row["platform"],
                "name": row["name"],
                "status": row["status"],
                "connected_at": row["connected_at"].isoformat() if row["connected_at"] else None,
                "product_count": row["product_count"] or 0
            })
    except Exception as e:
        logger.error(f"Database error in get_merchant_stores: {e}")
        # NO FALLBACK - Show real error
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
    return {"status": "success", "data": {"stores": stores}}

@router.get("/merchant/{merchant_id}/psps")
async def get_merchant_psps(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get real PSPs from database only"""
    psps = []
    try:
        # Get actual PSPs from database
        query = """
            SELECT 
                p.psp_id,
                p.psp_name,
                mp.credentials,
                mp.status,
                mp.created_at,
                mp.is_primary,
                COUNT(DISTINCT o.order_id) as transaction_count,
                COALESCE(SUM(o.total), 0) as total_volume
            FROM merchant_psps mp
            JOIN psps p ON mp.psp_id = p.psp_id
            LEFT JOIN orders o ON o.merchant_id = mp.merchant_id 
                AND o.psp_id = mp.psp_id
            WHERE mp.merchant_id = :merchant_id
            GROUP BY p.psp_id, p.psp_name, mp.credentials, 
                     mp.status, mp.created_at, mp.is_primary
        """
        
        rows = await database.fetch_all(query, {"merchant_id": merchant_id})
        
        for row in rows:
            psps.append({
                "id": row["psp_id"],
                "name": row["psp_name"],
                "status": row["status"],
                "is_primary": row["is_primary"],
                "connected_at": row["created_at"].isoformat() if row["created_at"] else None,
                "transaction_count": row["transaction_count"],
                "total_volume": float(row["total_volume"])
            })
    except Exception as e:
        logger.error(f"Database error in get_merchant_psps: {e}")
        # NO FALLBACK - Show real error
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    
    return {"status": "success", "data": {"psps": psps}}

@router.get("/merchant/{merchant_id}/orders")
async def get_merchant_orders(
    merchant_id: str,
    status: Optional[str] = None,
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user)
):
    """Get real orders from database only"""
    try:
        # Build query
        base_query = """
            SELECT 
                order_id,
                customer_email,
                customer_name,
                total,
                currency,
                payment_status as status,
                psp_id,
                created_at,
                updated_at
            FROM orders
            WHERE merchant_id = :merchant_id
        """
        
        # Add status filter if provided
        if status:
            base_query += " AND payment_status = :status"
        
        # Add ordering and pagination
        base_query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        
        # Build parameters
        params = {"merchant_id": merchant_id, "limit": limit, "offset": offset}
        if status:
            params["status"] = status
        
        # Execute query
        rows = await database.fetch_all(base_query, params)
        
        # Count total orders
        count_query = """
            SELECT COUNT(*) as total FROM orders WHERE merchant_id = :merchant_id
        """
        if status:
            count_query += " AND payment_status = :status"
            count_result = await database.fetch_one(count_query, {"merchant_id": merchant_id, "status": status})
        else:
            count_result = await database.fetch_one(count_query, {"merchant_id": merchant_id})
        
        total = count_result["total"] if count_result else 0
        
        # Format orders
        orders = []
        for row in rows:
            orders.append({
                "id": row["order_id"],
                "customer_email": row["customer_email"],
                "customer_name": row["customer_name"],
                "amount": float(row["total"]) if row["total"] else 0,
                "currency": row["currency"] or "USD",
                "status": row["status"],
                "psp_id": row["psp_id"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
            })
        
        return {
            "status": "success",
            "data": {
                "orders": orders,
                "total": total,
                "limit": limit,
                "offset": offset
            }
        }
    except Exception as e:
        logger.error(f"Error fetching orders from DB: {e}")
        # NO FALLBACK TO DEMO DATA
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.get("/merchant/dashboard/stats")
async def get_dashboard_stats(current_user: dict = Depends(get_current_user)):
    """Get real dashboard statistics from database only"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = current_user.get("merchant_id")
    
    if not merchant_id:
        # Try to get merchant_id from merchant_users table
        user_query = """
            SELECT merchant_id FROM merchant_users 
            WHERE user_id = :user_id
        """
        user_result = await database.fetch_one(user_query, {"user_id": current_user["user_id"]})
        if user_result:
            merchant_id = user_result["merchant_id"]
        else:
            raise HTTPException(status_code=404, detail="Merchant not found for this user")
    
    try:
        # Query orders directly from database
        orders_query = """
        SELECT 
            order_id,
            total,
            payment_status as status,
            customer_email,
            items,
            created_at
        FROM orders
        WHERE merchant_id = :merchant_id
        ORDER BY created_at DESC
        LIMIT 1000
        """
        orders = await database.fetch_all(orders_query, {"merchant_id": merchant_id})
        
        # Get PSP count
        psp_query = """
        SELECT COUNT(*) as count
        FROM merchant_psps
        WHERE merchant_id = :merchant_id AND status = 'active'
        """
        psp_result = await database.fetch_one(psp_query, {"merchant_id": merchant_id})
        psp_count = psp_result["count"] if psp_result else 0
        
        # Calculate statistics
        total_orders = len(orders)
        total_revenue = sum(float(order["total"]) for order in orders if order["total"])
        paid_orders = [o for o in orders if o["status"] in ["paid", "completed", "succeeded"]]
        
        # Get unique customers
        customers = set()
        for order in orders:
            if order.get("customer_email"):
                customers.add(order["customer_email"])
        
        # Get recent orders
        recent_orders = []
        for order in orders[:5]:
            recent_orders.append({
                "id": order["order_id"],
                "amount": float(order["total"]) if order["total"] else 0,
                "status": order["status"],
                "customer": order.get("customer_email", "Guest"),
                "date": order["created_at"].isoformat() if order["created_at"] else None
            })
        
        return {
            "status": "success",
            "data": {
                "total_orders": total_orders,
                "total_revenue": round(total_revenue, 2),
                "total_customers": len(customers),
                "total_products": psp_count,  # Using PSP count for now
                "average_order_value": round(total_revenue / total_orders, 2) if total_orders > 0 else 0,
                "conversion_rate": round(len(paid_orders) / total_orders * 100, 2) if total_orders > 0 else 0,
                "recent_orders": recent_orders,
                "psp_count": psp_count
            }
        }
    except Exception as e:
        logger.error(f"❌ Dashboard stats error for merchant {merchant_id}: {e}")
        # NO FALLBACK TO DEMO DATA
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@router.get("/merchant/{merchant_id}/analytics")
async def get_merchant_analytics(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant analytics from real data only"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get analytics from real orders
        analytics_query = """
            SELECT 
                COUNT(*) as total_orders,
                COALESCE(SUM(total), 0) as total_revenue,
                COALESCE(AVG(total), 0) as avg_order_value,
                COUNT(DISTINCT customer_email) as total_customers,
                SUM(CASE WHEN payment_status IN ('paid', 'completed', 'succeeded') THEN 1 ELSE 0 END) as successful_orders,
                SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN 1 ELSE 0 END) as orders_last_30_days,
                SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN total ELSE 0 END) as revenue_last_30_days
            FROM orders
            WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
        """
        
        analytics = await database.fetch_one(analytics_query, {"merchant_id": merchant_id})
        
        # Get recent orders
        recent_orders_query = """
            SELECT order_id, total as amount, payment_status as status, customer_name, created_at
            FROM orders
            WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
            ORDER BY created_at DESC
            LIMIT 5
        """
        recent_orders_rows = await database.fetch_all(recent_orders_query, {"merchant_id": merchant_id})
        
        recent_orders = []
        for row in recent_orders_rows:
            recent_orders.append({
                "id": row["order_id"],
                "amount": float(row["amount"]) if row["amount"] else 0,
                "status": row["status"],
                "customer": row["customer_name"] or "Guest",
                "date": row["created_at"].isoformat() if row["created_at"] else None
            })
        
        return {
            "status": "success",
            "data": {
                "total_orders": analytics["total_orders"] or 0,
                "total_revenue": float(analytics["total_revenue"]) or 0,
                "average_order_value": float(analytics["avg_order_value"]) or 0,
                "total_customers": analytics["total_customers"] or 0,
                "successful_orders": analytics["successful_orders"] or 0,
                "orders_last_30_days": analytics["orders_last_30_days"] or 0,
                "revenue_last_30_days": float(analytics["revenue_last_30_days"]) or 0,
                "recent_orders": recent_orders
            }
        }
        
    except Exception as e:
        logger.error(f"Error in merchant analytics: {e}")
        # NO FALLBACK TO DEMO DATA
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
