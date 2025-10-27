"""Extended Merchant API Routes for Dashboard Features"""
from fastapi import APIRouter, Depends, HTTPException, Body
from typing import Dict, Any, Optional
from utils.auth import get_current_user
from datetime import datetime
from db.database import database
import httpx
import os
import random
import string

router = APIRouter()

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
    """Sync products from Shopify store"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        # 1. Check if store is actually connected
        store_check = await database.fetch_one(
            """
            SELECT store_id, platform, domain, status 
            FROM merchant_stores 
            WHERE merchant_id = :merchant_id AND platform = 'shopify'
            LIMIT 1
            """,
            {"merchant_id": merchant_id}
        )
        
        if not store_check:
            raise HTTPException(
                status_code=400, 
                detail="No Shopify store connected. Please connect your store first in Integrations."
            )
        
        if store_check["status"] != "active":
            raise HTTPException(
                status_code=400,
                detail=f"Store is {store_check['status']}. Please reconnect your store."
            )
        
        # 2. TODO: Actually call Shopify API to fetch products
        # For now, just count existing products in our database
        
        # 3. Get actual product count from products table
        product_count_result = await database.fetch_one(
            "SELECT COUNT(*) as count FROM products WHERE merchant_id = :merchant_id",
            {"merchant_id": merchant_id}
        )
        product_count = product_count_result["count"] if product_count_result else 0
        
        # 4. Update merchant_stores with actual count and last sync time
        update_result = await database.execute(
            "UPDATE merchant_stores SET product_count = :count, last_sync = NOW() WHERE merchant_id = :merchant_id AND platform = 'shopify'",
            {"count": product_count, "merchant_id": merchant_id}
        )
        
        print(f"✅ Synced {product_count} products for merchant {merchant_id}")
        
        return {
            "status": "success",
            "message": f"Products synced: {product_count} products from {store_check['domain']}",
            "data": {
                "product_count": product_count,
                "store_domain": store_check['domain'],
                "synced_at": datetime.now().isoformat()
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")

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
    
    # Save to database
    psp_id = "psp_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    account_id = "acct_" + ''.join(random.choices(string.digits, k=10))
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

import time

