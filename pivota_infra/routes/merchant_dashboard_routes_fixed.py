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
        merchant_row = await database.fetch_one(query, {"merchant_id": merchant_id})
        
        if not merchant_row:
            raise HTTPException(status_code=404, detail=f"Merchant {merchant_id} not found")
        
        merchant = dict(merchant_row)
        
        # Get real statistics
        stats_query = """
            SELECT 
                COUNT(*) as total_orders,
                COALESCE(SUM(total), 0) as total_revenue
            FROM orders
            WHERE merchant_id = :merchant_id
        """
        stats_row = await database.fetch_one(stats_query, {"merchant_id": merchant_id})
        stats = dict(stats_row) if stats_row else {}
        
        return {
            "status": "success",
            "data": {
                "merchant_id": merchant.get("merchant_id"),
                "business_name": merchant.get("business_name"),
                "email": merchant.get("email"),
                "phone": merchant.get("phone"),
                "website": merchant.get("website"),
                "country": merchant.get("country"),
                "business_type": merchant.get("business_type"),
                "status": merchant.get("status"),
                "created_at": merchant.get("created_at").isoformat() if merchant.get("created_at") else None,
                "total_orders": stats.get("total_orders") or 0,
                "total_revenue": float(stats.get("total_revenue") or 0)
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
            # Convert Record to dict
            r = dict(row)
            stores.append({
                "id": r.get("id"),
                "platform": r.get("platform"),
                "name": r.get("name"),
                "status": r.get("status"),
                "connected_at": r.get("connected_at").isoformat() if r.get("connected_at") else None,
                "product_count": r.get("product_count") or 0
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
            # Convert Record to dict
            r = dict(row)
            psps.append({
                "id": r.get("psp_id"),
                "name": r.get("psp_name"),
                "status": r.get("status"),
                "is_primary": r.get("is_primary"),
                "connected_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "transaction_count": r.get("transaction_count") or 0,
                "total_volume": float(r.get("total_volume") or 0)
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
        
        total = dict(count_result).get("total", 0) if count_result else 0
        
        # Format orders
        orders = []
        for row in rows:
            # Convert Record to dict
            r = dict(row)
            orders.append({
                "id": r.get("order_id"),
                "customer_email": r.get("customer_email"),
                "customer_name": r.get("customer_name"),
                "amount": float(r.get("total") or 0),
                "currency": r.get("currency") or "USD",
                "status": r.get("status"),
                "psp_id": r.get("psp_id"),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "updated_at": r.get("updated_at").isoformat() if r.get("updated_at") else None
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
        psp_count = dict(psp_result).get("count", 0) if psp_result else 0
        
        # Calculate statistics - convert Records to dicts
        total_orders = len(orders)
        total_revenue = sum(float(dict(order).get("total") or 0) for order in orders)
        paid_orders = [o for o in orders if dict(o).get("status") in ["paid", "completed", "succeeded"]]
        
        # Get unique customers
        customers = set()
        for order_row in orders:
            order = dict(order_row)
            if order.get("customer_email"):
                customers.add(order.get("customer_email"))
        
        # Get recent orders
        recent_orders = []
        for order_row in orders[:5]:
            # Convert Record to dict
            order = dict(order_row)
            recent_orders.append({
                "id": order.get("order_id"),
                "amount": float(order.get("total") or 0),
                "status": order.get("status"),
                "customer": order.get("customer_email") or "Guest",
                "date": order.get("created_at").isoformat() if order.get("created_at") else None
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
        
        analytics_row = await database.fetch_one(analytics_query, {"merchant_id": merchant_id})
        analytics = dict(analytics_row) if analytics_row else {}
        
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
            # Convert Record to dict
            r = dict(row)
            recent_orders.append({
                "id": r.get("order_id"),
                "amount": float(r.get("amount") or 0),
                "status": r.get("status"),
                "customer": r.get("customer_name") or "Guest",
                "date": r.get("created_at").isoformat() if r.get("created_at") else None
            })
        
        return {
            "status": "success",
            "data": {
                "total_orders": analytics.get("total_orders") or 0,
                "total_revenue": float(analytics.get("total_revenue") or 0),
                "average_order_value": float(analytics.get("avg_order_value") or 0),
                "total_customers": analytics.get("total_customers") or 0,
                "successful_orders": analytics.get("successful_orders") or 0,
                "orders_last_30_days": analytics.get("orders_last_30_days") or 0,
                "revenue_last_30_days": float(analytics.get("revenue_last_30_days") or 0),
                "recent_orders": recent_orders
            }
        }
        
    except Exception as e:
        logger.error(f"Error in merchant analytics: {e}")
        # NO FALLBACK TO DEMO DATA
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
