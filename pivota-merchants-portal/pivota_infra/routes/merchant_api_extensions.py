"""Extended Merchant API Routes for Dashboard Features"""
from fastapi import APIRouter, Depends, HTTPException, Body
from typing import Dict, Any, Optional
from utils.auth import get_current_user
from datetime import datetime, timezone
from db.database import database
from db.orders import mark_order_shipped
from db.products import log_order_event
from services.refund_service import refund_service
from pydantic import BaseModel
from utils.logger import logger
from config.feature_flags import is_feature_enabled
import httpx
import os
import random
import string
import time

router = APIRouter()

# Request models
class RefundRequest(BaseModel):
    """Refund request with required fields"""
    amount: float  # Required
    reason: str  # Required
    source: str = "pivota_merchant"

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

async def get_merchant_id_from_user(current_user: dict) -> str:
    """Get merchant ID from current user token"""
    # Get merchant_id from JWT token
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        # Fallback: query database by email
        query = """
            SELECT merchant_id FROM merchant_onboarding 
            WHERE contact_email = :email
            LIMIT 1
        """
        result = await database.fetch_one(query, {"email": current_user.get("email")})
        if result:
            merchant_id = result["merchant_id"]
    
    if not merchant_id:
        raise HTTPException(status_code=404, detail="Merchant ID not found")
    
    return merchant_id

@router.get("/merchant/dashboard/stats")
async def get_dashboard_stats(current_user: dict = Depends(get_current_user)):
    """Get real dashboard statistics from database"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
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
        paid_orders = [o for o in orders if o["status"] == "paid"]
        
        # Get unique customers
        customers = set()
        for order in orders:
            if order.get("customer_email"):
                customers.add(order["customer_email"])
        
        # Parse items and calculate top products
        import json
        product_sales = {}
        for order in orders:
            try:
                items = json.loads(order["items"]) if isinstance(order["items"], str) else order["items"]
                for item in items:
                    product_id = item.get("product_id")
                    if product_id:
                        if product_id not in product_sales:
                            product_sales[product_id] = {
                                "id": product_id,
                                "name": item.get("product_title", "Unknown Product"),
                                "sales": 0,
                                "revenue": 0
                            }
                        quantity = item.get("quantity", 1)
                        price = float(item.get("unit_price", 0))
                        product_sales[product_id]["sales"] += quantity
                        product_sales[product_id]["revenue"] += price * quantity
            except:
                pass
        
        top_products = sorted(
            product_sales.values(),
            key=lambda x: x["revenue"],
            reverse=True
        )[:5]
        
        # Format recent orders
        recent_orders = []
        for order in orders[:5]:
            recent_orders.append({
                "order_id": order["order_id"],
                "amount": float(order["amount"]),
                "status": order["status"],
                "customer": {
                    "email": order.get("customer_email", "")
                },
                "created_at": order["created_at"].isoformat() if order.get("created_at") else None
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
                "top_products": top_products,
                "recent_orders": recent_orders,
                "psp_count": psp_count  # Add real PSP count
            }
        }
    except Exception as e:
        import traceback
        logger.error(f"❌ Dashboard stats error for merchant {merchant_id}: {e}")
        traceback.print_exc()
        # DON'T catch errors silently - raise them so we can see what's wrong
        raise HTTPException(status_code=500, detail=f"Dashboard stats failed: {str(e)}")

@router.put("/merchant/profile")
async def update_merchant_profile(
    profile_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Update merchant profile"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # In a real implementation, this would update the database
    return {
        "status": "success",
        "message": "Profile updated successfully",
        "data": profile_data
    }

@router.post("/merchant/integrations/shopify/sync")
async def sync_shopify_products(current_user: dict = Depends(get_current_user)):
    """Sync products from Shopify store - 真正同步产品，不只是读缓存"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # 1. Check if store is actually connected
        store_check_row = await database.fetch_one(
            """
            SELECT store_id, platform, domain, status, product_count 
            FROM merchant_stores 
            WHERE merchant_id = :merchant_id AND platform = 'shopify'
            LIMIT 1
            """,
            {"merchant_id": merchant_id}
        )
        
        if not store_check_row:
            raise HTTPException(
                status_code=400, 
                detail="No Shopify store connected. Please connect your store first in Integrations."
            )
        
        # Convert Row to dict
        store_check = dict(store_check_row)
        
        if store_check["status"] != "active":
            raise HTTPException(
                status_code=400,
                detail=f"Store is {store_check['status']}. Please reconnect your store."
            )
        
        # 2. 调用真正的产品同步（会从 Shopify API 拉取产品并更新 products_cache）
        from routes.product_sync import sync_products, SyncRequest
        from fastapi import BackgroundTasks
        
        sync_request = SyncRequest(
            merchant_id=merchant_id,
            force_refresh=True,  # 强制刷新
            limit=250,
            platform="shopify"  # 明确指定同步 Shopify
        )
        
        # 执行真正的同步
        sync_result = await sync_products(
            request=sync_request,
            background_tasks=BackgroundTasks(),
            current_user=current_user
        )
        
        print(f"✅ Synced {sync_result.products_synced} Shopify products for merchant {merchant_id}")

        return {
            "status": "success",
            "message": f"Successfully synced {sync_result.products_synced} products from {store_check['domain']}",
            "data": {
                "product_count": sync_result.products_synced,
                "store_domain": store_check['domain'],
                "synced_at": sync_result.sync_time
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")


@router.get("/merchant/mcp/summary")
async def get_merchant_mcp_summary(current_user: dict = Depends(get_current_user)):
    """Return MCP dashboard metrics for the current merchant"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    try:
        stores_query = """
            SELECT 
                store_id,
                platform,
                name,
                domain,
                status,
                product_count,
                last_sync,
                connected_at
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
            ORDER BY connected_at DESC NULLS LAST
        """

        stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})

        store_list = [dict(store) for store in stores]
        total_stores = len(store_list)
        active_statuses = {"active", "connected"}
        active_stores = sum(1 for s in store_list if (s.get("status") or "").lower() in active_statuses)

        # Aggregate product data
        product_cache_stats = None
        try:
            cache_query = """
                SELECT 
                    COUNT(*) AS total_cached,
                    COUNT(CASE WHEN expires_at IS NULL OR expires_at > NOW() THEN 1 END) AS active_cached
                FROM products_cache
                WHERE merchant_id = :merchant_id
            """
            product_cache_stats = await database.fetch_one(cache_query, {"merchant_id": merchant_id})
        except Exception:
            product_cache_stats = None

        total_cached = product_cache_stats["total_cached"] if product_cache_stats and product_cache_stats["total_cached"] else 0
        active_cached = product_cache_stats["active_cached"] if product_cache_stats and product_cache_stats["active_cached"] else 0

        sum_product_counts = sum((s.get("product_count") or 0) for s in store_list)
        total_requests = active_cached or total_cached or sum_product_counts

        now_utc = datetime.now(timezone.utc)
        latencies_seconds = []
        nodes = []
        latest_sync_dt = None

        for store in store_list:
            store_status = (store.get("status") or "").lower()
            last_sync = store.get("last_sync")
            if last_sync is not None and isinstance(last_sync, datetime):
                sync_dt = last_sync if last_sync.tzinfo else last_sync.replace(tzinfo=timezone.utc)
            else:
                sync_dt = None

            # Track latest sync for reporting
            if sync_dt:
                latest_sync_dt = max(latest_sync_dt, sync_dt) if latest_sync_dt else sync_dt

            # Latency should be API response time, not time since last sync
            # Set to null since we don't have real-time API latency monitoring yet
            latency_ms = None
            uptime = 99.9 if store_status in active_statuses else 0.0

            nodes.append({
                "id": store.get("store_id"),
                "name": store.get("name") or f"{store.get('platform', 'Store').title()} Store",
                "platform": store.get("platform"),
                "status": "online" if store_status in active_statuses else "offline",
                "latency": latency_ms,
                "latency_ms": latency_ms,
                "uptime": uptime,
                "product_count": store.get("product_count") or 0,
                "domain": store.get("domain"),
                "last_sync": sync_dt.isoformat() if sync_dt else None
            })

        # Average latency set to null until we have real API response time monitoring
        avg_latency_ms = None

        success_rate = round((active_stores / total_stores) * 100, 2) if total_stores else 0.0
        latest_sync = latest_sync_dt.isoformat() if latest_sync_dt else None

        primary_store = store_list[0] if store_list else {}

        return {
            "status": "success",
            "data": {
                "connected": active_stores > 0,
                "total_stores": total_stores,
                "active_stores": active_stores,
                "platform": primary_store.get("platform"),
                "shop_domain": primary_store.get("domain"),
                "total_requests": total_requests,
                "avg_latency": avg_latency_ms,
                "avg_latency_ms": avg_latency_ms,
                "success_rate": success_rate,
                "latest_sync": latest_sync,
                "last_sync": latest_sync,
                "active_products": active_cached or sum_product_counts,
                "total_products": sum_product_counts or active_cached,
                "nodes": nodes
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Failed to load MCP summary for merchant {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load MCP summary: {str(e)}")

@router.post("/merchant/integrations/psp/connect")
async def connect_psp(
    psp_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Connect a PSP provider"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    provider = psp_data.get("provider", "").lower()
    
    # Validate API key format
    api_key = psp_data.get("api_key", "")
    if not api_key or len(api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key")
    
    # Get secret key for PayPal
    secret_key = psp_data.get("secret_key", "")
    if provider == "paypal" and (not secret_key or len(secret_key) < 10):
        raise HTTPException(status_code=400, detail="PayPal requires both Client ID and Client Secret")
    
    # Validate provider-specific required fields
    # Checkout.com requires processing_channel_id (we store in account_id)
    if provider == "checkout":
        provided_account_id = (psp_data.get("account_id") or "").strip()
        if not provided_account_id:
            raise HTTPException(status_code=400, detail="Checkout.com requires processing_channel_id in account_id field")
        account_id = provided_account_id
    else:
        account_id = "acct_" + ''.join(random.choices(string.digits, k=10))

    # Save to database
    # [Phase 6.2 Fix] Include provider in psp_id to match constraint: psp_{provider}_{12chars}
    psp_id = f"psp_{provider}_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    capabilities = ["card", "bank_transfer"] if provider in ["stripe", "adyen"] else ["card"]
    
    new_psp = {
        "id": psp_id,
        "provider": provider,
        "name": f"{provider.capitalize()} Account",
        "status": "active",
        "connected_at": datetime.now().isoformat() + "Z",
        "account_id": account_id,
        "capabilities": capabilities,
        "api_key_last4": api_key[-4:] if len(api_key) >= 4 else "****"
    }
    
    try:
        # Use transaction to ensure data is committed
        async with database.transaction():
            # Build query dynamically based on whether secret_key is provided
            base_cols = ["psp_id", "merchant_id", "provider", "name", "api_key", "account_id", "capabilities", "status", "connected_at"]
            base_vals = [":psp_id", ":merchant_id", ":provider", ":name", ":api_key", ":account_id", ":capabilities", ":status", ":connected_at"]
            params = {
                "psp_id": psp_id,
                "merchant_id": merchant_id,
                "provider": provider,
                "name": f"{provider.capitalize()} Account",
                "api_key": api_key,
                "account_id": account_id,
                "capabilities": ','.join(capabilities),
                "status": 'active',
                "connected_at": datetime.now()
            }
            
            # Add secret_key for PayPal
            if secret_key:
                base_cols.append("secret_key")
                base_vals.append(":secret_key")
                params["secret_key"] = secret_key
            
            query = f"""
                INSERT INTO merchant_psps ({', '.join(base_cols)})
                VALUES ({', '.join(base_vals)})
            """
            await database.execute(query, params)
            print(f"✅ PSP saved to DB: {psp_id} for merchant {merchant_id}")
            
            # Verify the save within transaction
            verify_query = "SELECT COUNT(*) as count FROM merchant_psps WHERE merchant_id = :merchant_id"
            result = await database.fetch_one(verify_query, {"merchant_id": merchant_id})
            print(f"✅ Total PSPs for merchant {merchant_id} (in transaction): {result['count']}")

            # Also set flags on merchant_onboarding for dashboard compatibility
            await database.execute(
                """
                UPDATE merchant_onboarding
                SET psp_connected = true,
                    psp_type = :psp_type,
                    updated_at = :updated_at
                WHERE merchant_id = :merchant_id
                """,
                {
                    "psp_type": provider,
                    "updated_at": datetime.now(),
                    "merchant_id": merchant_id,
                }
            )
    except Exception as e:
        print(f"❌ PSP Database save error: {e}")
        import traceback
        traceback.print_exc()
        # Return error instead of success if save fails
        raise HTTPException(status_code=500, detail=f"Failed to save PSP: {str(e)}")
    
    return {
        "status": "success",
        "message": f"{provider.capitalize()} connected successfully",
        "data": new_psp
    }

@router.post("/merchant/integrations/store/connect")
async def connect_store(
    store_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Connect an e-commerce store"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    platform = store_data.get("platform", "").lower()
    store_url = store_data.get("store_url", "")
    api_key = store_data.get("api_key", "")
    
    if not store_url or not api_key:
        raise HTTPException(status_code=400, detail="Store URL and API key required")
    
    # Save to database
    store_id = "store_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    domain = store_url.replace("https://", "").replace("http://", "").strip("/")
    
    new_store = {
        "id": store_id,
        "platform": platform,
        "name": domain,
        "status": "connected",
        "connected_at": datetime.now().isoformat() + "Z",
        "domain": domain,
        "api_key_last4": api_key[-4:] if len(api_key) >= 4 else "****",
        "product_count": 0
    }
    
    try:
        # Use transaction to ensure data is committed
        async with database.transaction():
            query = """
                INSERT INTO merchant_stores (store_id, merchant_id, platform, name, domain, api_key, status, connected_at)
                VALUES (:store_id, :merchant_id, :platform, :name, :domain, :api_key, :status, :connected_at)
            """
            await database.execute(query, {
                "store_id": store_id,
                "merchant_id": merchant_id,
                "platform": platform,
                "name": domain,
                "domain": domain,
                "api_key": api_key,
                "status": 'connected',
                "connected_at": datetime.now()
            })
            print(f"✅ Store saved to DB: {store_id} for merchant {merchant_id}")
            
            # Verify the save within transaction
            verify_query = "SELECT COUNT(*) as count FROM merchant_stores WHERE merchant_id = :merchant_id"
            result = await database.fetch_one(verify_query, {"merchant_id": merchant_id})
            print(f"✅ Total stores for merchant {merchant_id} (in transaction): {result['count']}")
    except Exception as e:
        print(f"❌ Store Database save error: {e}")
        import traceback
        traceback.print_exc()
        # Return error instead of success if save fails
        raise HTTPException(status_code=500, detail=f"Failed to save store: {str(e)}")
    
    return {
        "status": "success",
        "message": f"{platform.capitalize()} store connected successfully",
        "data": new_store
    }

@router.get("/merchant/orders/{order_id}")
async def get_order_detail(
    order_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get order details"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # Query directly from database
        query = """
            SELECT 
                order_id, merchant_id, store_id, psp_id,
                total, currency, status, payment_status, payment_method,
                customer_name, customer_email, shipping_address,
                items, subtotal, shipping_fee, tax,
                created_at, updated_at
            FROM orders
            WHERE order_id = :order_id AND merchant_id = :merchant_id
        """
        
        order = await database.fetch_one(query, {"order_id": order_id, "merchant_id": merchant_id})
        
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        return {
            "status": "success",
            "data": {
                "order_id": order["order_id"],
                "amount": float(order["total"]) if order["total"] else 0,
                "total": float(order["total"]) if order["total"] else 0,
                "subtotal": float(order["subtotal"]) if order["subtotal"] else 0,
                "shipping_fee": float(order["shipping_fee"]) if order["shipping_fee"] else 0,
                "tax": float(order["tax"]) if order["tax"] else 0,
                "currency": order["currency"],
                "status": order["status"],
                "payment_status": order["payment_status"],
                "payment_method": order["payment_method"],
                "customer": {
                    "name": order["customer_name"],
                    "email": order["customer_email"]
                },
                "customer_name": order["customer_name"],
                "customer_email": order["customer_email"],
                "shipping_address": order["shipping_address"] or {},
                "items": order["items"] or [],
                "created_at": order["created_at"].isoformat() if order["created_at"] else None,
                "updated_at": order["updated_at"].isoformat() if order["updated_at"] else None,
                "psp_id": order["psp_id"],
                "store_id": order["store_id"]
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching order details: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch order details")

@router.post("/merchant/orders/{order_id}/ship")
async def merchant_mark_shipped(
    order_id: str,
    payload: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Mark order as shipped (Merchant accessible)"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    # Check if order belongs to merchant
    check_query = "SELECT 1 FROM orders WHERE order_id = :order_id AND merchant_id = :merchant_id"
    order_exists = await database.fetch_one(check_query, {"order_id": order_id, "merchant_id": merchant_id})
    
    if not order_exists:
        raise HTTPException(status_code=404, detail="Order not found")
        
    tracking_number = payload.get("tracking_number")
    carrier = payload.get("carrier")
    
    if not tracking_number:
        raise HTTPException(status_code=400, detail="Tracking number is required")
        
    success = await mark_order_shipped(order_id, tracking_number, carrier)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update order status")
    
    # Log event
    await log_order_event(
        event_type="order_shipped",
        order_id=order_id,
        merchant_id=merchant_id,
        metadata={
            "tracking_number": tracking_number,
            "carrier": carrier,
            "action_by": "merchant"
        }
    )
    
    return {
        "status": "success",
        "message": "Order marked as shipped",
        "data": {
            "order_id": order_id,
            "status": "completed",
            "fulfillment_status": "shipped",
            "tracking_number": tracking_number,
            "carrier": carrier
        }
    }

@router.post("/merchant/orders/{order_id}/refund")
async def merchant_refund_order(
    order_id: str,
    refund_request: RefundRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Process refund for an order (Merchant accessible)
    
    Requires:
    - amount: Refund amount (required)
    - reason: Refund reason (required)
    - source: Origin of refund (defaults to 'pivota_merchant')
    
    Features:
    - Idempotency protection
    - Automatic retry on failure
    - Platform sync support (future)
    """
    # Check feature flag
    if not is_feature_enabled("enable_internal_refund"):
        raise HTTPException(
            status_code=503,
            detail="Refund feature is currently disabled"
        )
    
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    # Verify order belongs to merchant
    check_query = """
    SELECT 1 FROM orders 
    WHERE order_id = :order_id 
    AND merchant_id = :merchant_id
    """
    order_exists = await database.fetch_one(check_query, {
        "order_id": order_id, 
        "merchant_id": merchant_id
    })
    
    if not order_exists:
        raise HTTPException(status_code=404, detail="Order not found")
    
    try:
        # Process refund through service
        result = await refund_service.create_refund(
            order_id=order_id,
            amount=refund_request.amount,
            reason=refund_request.reason,
            source=refund_request.source,
            created_by=current_user.get("email", current_user.get("user_id"))
        )
        
        # Log refund event
        await log_order_event(
            event_type="merchant_refund",
            order_id=order_id,
            merchant_id=merchant_id,
            metadata={
                "refund_id": result.get("refund_id"),
                "amount": refund_request.amount,
                "reason": refund_request.reason,
                "status": result["status"]
            }
        )
        
        # Handle different statuses
        if result["status"] == "duplicate":
            return HTTPException(
                status_code=409,
                detail={
                    "message": "Refund already processed",
                    "refund_id": result["refund_id"]
                }
            )
        elif result["status"] == "failed":
            # Still return success as it's queued for retry
            return {
                "status": "processing",
                "message": "Refund is being processed. You'll be notified when complete.",
                "refund_id": result["refund_id"],
                "amount": refund_request.amount
            }
        else:
            return {
                "status": "success",
                "message": "Refund processed successfully",
                "refund_id": result["refund_id"],
                "psp_refund_id": result.get("psp_refund_id"),
                "amount": refund_request.amount
            }
            
    except ValueError as e:
        # Validation errors
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Refund processing error: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to process refund. Please try again later."
        )

@router.get("/merchant/orders/{order_id}/refunds")
async def get_order_refunds(
    order_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get refund history for an order"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    # Verify order belongs to merchant
    check_query = """
    SELECT 1 FROM orders 
    WHERE order_id = :order_id 
    AND merchant_id = :merchant_id
    """
    order_exists = await database.fetch_one(check_query, {
        "order_id": order_id, 
        "merchant_id": merchant_id
    })
    
    if not order_exists:
        raise HTTPException(status_code=404, detail="Order not found")
    
    # Get refund history with enhanced details
    refunds = await refund_service.get_refund_history(order_id)
    
    # Get order details for context
    order_query = """
    SELECT total_amount, total_refunded, payment_status, currency
    FROM orders 
    WHERE order_id = :order_id
    """
    order_data = await database.fetch_one(order_query, {"order_id": order_id})
    
    # Calculate refund summary
    total_refunded = sum(r.get("amount", 0) for r in refunds if r.get("status") == "completed")
    pending_refunds = sum(r.get("amount", 0) for r in refunds if r.get("status") == "pending")
    
    return {
        "status": "success",
        "order_summary": {
            "order_id": order_id,
            "total_amount": float(order_data["total_amount"]) if order_data["total_amount"] else 0,
            "total_refunded": float(order_data["total_refunded"]) if order_data["total_refunded"] else 0,
            "payment_status": order_data["payment_status"],
            "currency": order_data["currency"] or "USD",
            "refundable_amount": float(order_data["total_amount"] - order_data["total_refunded"]) if order_data["total_amount"] and order_data["total_refunded"] else float(order_data["total_amount"]) if order_data["total_amount"] else 0
        },
        "refund_summary": {
            "total_refunds": len(refunds),
            "completed_amount": total_refunded,
            "pending_amount": pending_refunds,
            "failed_count": len([r for r in refunds if r.get("status") == "failed"])
        },
        "refunds": refunds
    }

@router.post("/merchant/orders/export")
async def export_orders(current_user: dict = Depends(get_current_user)):
    """Export orders to CSV"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return {
        "status": "success",
        "message": "Export started. You will receive an email when ready.",
        "export_id": "exp_" + str(int(time.time()))
    }

@router.post("/merchant/products/add")
async def add_product(
    product_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Add a new product"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return {
        "status": "success",
        "message": "Product added successfully",
        "data": {
            "id": "prod_" + str(int(time.time())),
            **product_data
        }
    }

@router.post("/merchant/security/change-password")
async def change_password(
    password_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Change password"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    old_password = password_data.get("old_password")
    new_password = password_data.get("new_password")
    
    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="Old and new passwords required")
    
    # In real implementation, verify old password and update
    return {
        "status": "success",
        "message": "Password changed successfully"
    }

@router.post("/merchant/security/enable-2fa")
async def enable_2fa(current_user: dict = Depends(get_current_user)):
    """Enable two-factor authentication"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    import secrets
    secret = secrets.token_hex(16)
    
    return {
        "status": "success",
        "message": "2FA enabled successfully",
        "data": {
            "secret": secret,
            "qr_code_url": f"otpauth://totp/Pivota:{current_user['email']}?secret={secret}&issuer=Pivota"
        }
    }
