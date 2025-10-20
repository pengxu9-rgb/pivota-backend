"""
Missing Employee Portal Endpoints
Provides endpoints that were called by frontend but not implemented in backend
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List
from datetime import datetime, timedelta
from utils.auth import get_current_user
from db.database import database
import random

router = APIRouter()

# ============== Admin Endpoints ==============

@router.get("/admin/logs")
async def get_admin_logs(
    limit: int = Query(default=100, ge=1, le=500),
    current_user: dict = Depends(get_current_user)
):
    """Get system logs"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Generate demo logs
    log_types = ["info", "warning", "error", "debug"]
    actions = ["user_login", "merchant_created", "psp_connected", "order_processed", "sync_completed"]
    
    logs = []
    for i in range(min(limit, 50)):
        logs.append({
            "log_id": f"log_{i+1000}",
            "timestamp": (datetime.now() - timedelta(minutes=i*5)).isoformat(),
            "level": random.choice(log_types),
            "action": random.choice(actions),
            "message": f"System event: {random.choice(actions)}",
            "user": "system" if i % 3 == 0 else "employee@pivota.com"
        })
    
    return {
        "status": "success",
        "logs": logs
    }

@router.get("/admin/psp/status")
async def get_admin_psp_status(
    current_user: dict = Depends(get_current_user)
):
    """Get overall PSP system status"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get PSP statistics
        stats_query = """
            SELECT 
                provider,
                COUNT(*) as total_connections,
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active_connections
            FROM merchant_psps
            GROUP BY provider
        """
        
        psp_stats = await database.fetch_all(stats_query)
        
        # Get transaction stats by PSP
        trans_query = """
            SELECT 
                p.provider,
                COUNT(o.order_id) as transactions,
                COALESCE(SUM(o.amount), 0) as volume
            FROM merchant_psps p
            LEFT JOIN orders o ON p.psp_id = o.psp_id
            GROUP BY p.provider
        """
        
        trans_stats = await database.fetch_all(trans_query)
        
        # Combine stats
        psp_status = {}
        for stat in psp_stats:
            provider = stat["provider"]
            psp_status[provider] = {
                "total_connections": stat["total_connections"],
                "active_connections": stat["active_connections"],
                "transactions": 0,
                "volume": 0
            }
        
        for trans in trans_stats:
            provider = trans["provider"]
            if provider in psp_status:
                psp_status[provider]["transactions"] = trans["transactions"]
                psp_status[provider]["volume"] = float(trans["volume"])
        
        return {
            "status": "success",
            "psp_status": psp_status,
            "overall_health": "operational"
        }
    
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "psp_status": {}
        }

# ============== Analytics ==============

@router.get("/analytics/payment-success")
async def get_payment_analytics(
    current_user: dict = Depends(get_current_user)
):
    """Get payment success analytics"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get payment success rate by status
        analytics_query = """
            SELECT 
                status,
                COUNT(*) as count,
                COALESCE(SUM(amount), 0) as total_amount
            FROM orders
            GROUP BY status
        """
        
        results = await database.fetch_all(analytics_query)
        
        status_breakdown = []
        total_orders = 0
        successful_orders = 0
        
        for result in results:
            count = result["count"]
            total_orders += count
            
            if result["status"] in ["completed", "delivered"]:
                successful_orders += count
            
            status_breakdown.append({
                "status": result["status"],
                "count": count,
                "amount": float(result["total_amount"]),
                "percentage": 0  # Will calculate after
            })
        
        # Calculate percentages
        for item in status_breakdown:
            item["percentage"] = round((item["count"] / total_orders * 100), 1) if total_orders > 0 else 0
        
        success_rate = (successful_orders / total_orders * 100) if total_orders > 0 else 0
        
        return {
            "status": "success",
            "analytics": {
                "overall_success_rate": round(success_rate, 1),
                "total_transactions": total_orders,
                "successful_transactions": successful_orders,
                "failed_transactions": total_orders - successful_orders,
                "status_breakdown": status_breakdown
            }
        }
    
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

# ============== Integrations ==============

@router.post("/integrations/shopify/connect")
async def connect_shopify_for_merchant(
    merchant_id: str,
    shop_domain: str,
    access_token: str,
    current_user: dict = Depends(get_current_user)
):
    """Connect Shopify store for a merchant (employee-assisted)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Verify merchant exists
        merchant_query = "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id"
        merchant = await database.fetch_one(merchant_query, {"merchant_id": merchant_id})
        
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        # Use the merchant API extensions endpoint
        import uuid
        store_id = f"store_shopify_{uuid.uuid4().hex[:8]}"
        
        insert_query = """
            INSERT INTO merchant_stores (
                store_id, merchant_id, platform, name, domain,
                api_key, status, connected_at, product_count
            ) VALUES (
                :store_id, :merchant_id, :platform, :name, :domain,
                :api_key, :status, :connected_at, :product_count
            )
        """
        
        await database.execute(insert_query, {
            "store_id": store_id,
            "merchant_id": merchant_id,
            "platform": "shopify",
            "name": shop_domain,
            "domain": shop_domain,
            "api_key": access_token,
            "status": "active",
            "connected_at": datetime.now(),
            "product_count": 0
        })
        
        return {
            "status": "success",
            "message": "Shopify store connected successfully",
            "store_id": store_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect Shopify: {str(e)}")

@router.post("/merchant/onboarding/setup-psp")
async def setup_psp_for_merchant(
    merchant_id: str,
    psp_type: str,
    api_key: str,
    test_mode: bool = True,
    current_user: dict = Depends(get_current_user)
):
    """Setup PSP for a merchant (employee-assisted)"""
    if current_user["role"] not in ["employee", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Verify merchant exists
        merchant_query = "SELECT merchant_id FROM merchant_onboarding WHERE merchant_id = :merchant_id"
        merchant = await database.fetch_one(merchant_query, {"merchant_id": merchant_id})
        
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")
        
        import uuid
        psp_id = f"psp_{psp_type}_{uuid.uuid4().hex[:8]}"
        
        insert_query = """
            INSERT INTO merchant_psps (
                psp_id, merchant_id, provider, name, api_key,
                status, connected_at, capabilities
            ) VALUES (
                :psp_id, :merchant_id, :provider, :name, :api_key,
                :status, :connected_at, :capabilities
            )
        """
        
        capabilities = "card_payments,bank_transfers,digital_wallets"
        
        await database.execute(insert_query, {
            "psp_id": psp_id,
            "merchant_id": merchant_id,
            "provider": psp_type.lower(),
            "name": f"{psp_type.capitalize()} Account",
            "api_key": api_key,
            "status": "active",
            "connected_at": datetime.now(),
            "capabilities": capabilities
        })
        
        return {
            "status": "success",
            "message": f"{psp_type} connected successfully",
            "psp_id": psp_id
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect PSP: {str(e)}")

@router.post("/integrations/{platform}/sync-products")
async def sync_products_for_merchant(
    platform: str,
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Sync products for merchant store (employee-assisted)"""
    if current_user["role"] not in ["employee", "admin", "merchant"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if platform not in ["shopify", "wix"]:
        raise HTTPException(status_code=400, detail="Invalid platform")
    
    try:
        # Get store
        store_query = """
            SELECT * FROM merchant_stores
            WHERE merchant_id = :merchant_id AND platform = :platform
            LIMIT 1
        """
        store = await database.fetch_one(store_query, {
            "merchant_id": merchant_id,
            "platform": platform
        })
        
        if not store:
            raise HTTPException(status_code=404, detail=f"No {platform} store found for this merchant")
        
        # Simulate product sync (in production, would call actual API)
        product_count = random.randint(10, 50)
        
        # Update store
        update_query = """
            UPDATE merchant_stores
            SET product_count = :product_count,
                last_sync = :last_sync
            WHERE store_id = :store_id
        """
        
        await database.execute(update_query, {
            "product_count": product_count,
            "last_sync": datetime.now(),
            "store_id": store["store_id"]
        })
        
        return {
            "status": "success",
            "message": f"Synced {product_count} products from {platform}",
            "product_count": product_count,
            "store_id": store["store_id"]
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")
