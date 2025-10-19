"""Merchant Dashboard API Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import random
import httpx
import string
from utils.auth import get_current_user
try:
    # Import in-memory data from extensions if available
    from .merchant_api_extensions import merchant_stores_db, merchant_psps_db
except Exception:
    merchant_stores_db, merchant_psps_db = {}, {}

router = APIRouter()

# Demo data for merchant dashboard
DEMO_MERCHANT_DATA = {
    "merch_208139f7600dbf42": {
        "id": "merch_208139f7600dbf42",
        "business_name": "ChydanTest Store",
        "email": "merchant@test.com",
        "status": "active",
        "created_at": "2025-01-01T00:00:00Z",
        "profile": {
            "business_name": "ChydanTest Store",
            "contact_name": "Test Merchant",
            "email": "merchant@test.com",
            "phone": "+1234567890",
            "address": "123 Test Street",
            "city": "New York",
            "country": "US",
            "postal_code": "10001"
        },
        "stores": [{
            "id": "store_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=12)),
            "platform": "shopify",
            "name": "chydantest.myshopify.com",
            "status": "connected",
            "connected_at": "2025-01-15T10:00:00Z",
            "domain": "chydantest.myshopify.com",
            "api_key": "shpat_xxxxxxxxxxxxx",
            "last_sync": "2025-10-19T10:00:00Z"
        }],
        "psps": [{
            "id": "psp_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=12)),
            "provider": "stripe",
            "name": "Stripe Account",
            "status": "active",
            "connected_at": "2025-01-15T11:00:00Z",
            "account_id": "acct_1234567890",
            "capabilities": ["card", "bank_transfer", "alipay", "wechat_pay"],
            "fees": {
                "card": 2.9,
                "bank_transfer": 1.5,
                "alipay": 2.5,
                "wechat_pay": 2.5
            }
        }],
        "webhooks": {
            "endpoint": "https://chydantest.myshopify.com/webhooks/pivota",
            "secret": "whsec_" + ''.join(random.choices(string.ascii_letters + string.digits, k=32)),
            "events": ["order.created", "order.updated", "payment.completed", "payment.failed"],
            "created_at": "2025-01-15T12:00:00Z",
            "status": "active"
        }
    }
}

def generate_demo_orders(merchant_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Generate demo orders for merchant"""
    orders = []
    statuses = ["completed", "pending", "processing", "failed", "refunded"]
    
    for i in range(limit):
        order_date = datetime.now() - timedelta(days=random.randint(0, 30), hours=random.randint(0, 23))
        orders.append({
            "order_id": f"ORD{str(1000 + i).zfill(8)}",
            "merchant_id": merchant_id,
            "amount": round(random.uniform(10, 500), 2),
            "currency": "USD",
            "status": random.choice(statuses),
            "payment_method": random.choice(["card", "bank_transfer", "alipay", "wechat_pay"]),
            "customer": {
                "name": f"Customer {i+1}",
                "email": f"customer{i+1}@example.com"
            },
            "created_at": order_date.isoformat() + "Z",
            "updated_at": order_date.isoformat() + "Z"
        })
    
    return sorted(orders, key=lambda x: x["created_at"], reverse=True)

def generate_analytics(merchant_id: str) -> Dict[str, Any]:
    """Generate analytics data for merchant"""
    return {
        "total_revenue": round(random.uniform(10000, 50000), 2),
        "total_orders": random.randint(100, 500),
        "average_order_value": round(random.uniform(50, 150), 2),
        "conversion_rate": round(random.uniform(1.5, 3.5), 2),
        "top_products": [
            {"name": f"Product {i+1}", "sales": random.randint(10, 100)} 
            for i in range(5)
        ],
        "revenue_by_month": [
            {"month": f"2025-{str(i).zfill(2)}", "revenue": round(random.uniform(1000, 5000), 2)}
            for i in range(1, 11)
        ]
    }

@router.get("/merchant/profile")
async def get_merchant_profile(current_user: dict = Depends(get_current_user)):
    """Get merchant profile"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = "merch_208139f7600dbf42"  # Fixed merchant ID for demo
    merchant_data = DEMO_MERCHANT_DATA.get(merchant_id)
    
    if not merchant_data:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    return {
        "status": "success",
        "data": merchant_data["profile"]
    }

@router.get("/merchant/{merchant_id}/integrations")
async def get_merchant_stores(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant's connected stores"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_data = DEMO_MERCHANT_DATA.get(merchant_id)
    demo_stores = merchant_data["stores"] if merchant_data else []
    runtime_stores = merchant_stores_db.get(merchant_id, [])
    stores = [*runtime_stores, *demo_stores]

    # Enrich with product_count by querying products endpoint
    try:
        # Get token from current user context
        token = None
        if hasattr(current_user, 'get'):
            token = current_user.get('token')
        
        headers = {}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://localhost:8000/products/{merchant_id}",
                headers=headers
            )
            if resp.status_code == 200:
                payload = resp.json()
                products = payload.get("products", [])
                # Count by platform
                platform_to_count = {}
                for p in products:
                    plat = (p.get("platform") or "").lower()
                    platform_to_count[plat] = platform_to_count.get(plat, 0) + 1

                # Attach counts; default 0
                for s in stores:
                    plat = (s.get("platform") or "").lower()
                    s["product_count"] = platform_to_count.get(plat, s.get("product_count", 0))
            else:
                # If products API not available, ensure field exists
                for s in stores:
                    s.setdefault("product_count", s.get("product_count", 0))
    except Exception:
        for s in stores:
            s.setdefault("product_count", s.get("product_count", 0))

    return {"status": "success", "data": {"stores": stores}}

@router.get("/merchant/{merchant_id}/psps")
async def get_merchant_psps(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant's connected PSPs"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_data = DEMO_MERCHANT_DATA.get(merchant_id)
    demo_psps = merchant_data["psps"] if merchant_data else []
    runtime_psps = merchant_psps_db.get(merchant_id, [])
    psps = [*runtime_psps, *demo_psps]

    return {"status": "success", "data": {"psps": psps}}

@router.get("/merchant/{merchant_id}/orders")
async def get_merchant_orders(
    merchant_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant's orders"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    orders = generate_demo_orders(merchant_id, limit=50)
    
    # Filter by status if provided
    if status:
        orders = [o for o in orders if o["status"] == status]
    
    # Apply pagination
    total = len(orders)
    orders = orders[offset:offset + limit]
    
    return {
        "status": "success",
        "data": {
            "orders": orders,
            "total": total,
            "limit": limit,
            "offset": offset
        }
    }

@router.get("/merchant/{merchant_id}/analytics")
async def get_merchant_analytics(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant analytics"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return {
        "status": "success",
        "data": generate_analytics(merchant_id)
    }

@router.get("/merchant/webhooks/config")
async def get_webhook_config(current_user: dict = Depends(get_current_user)):
    """Get webhook configuration"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = "merch_208139f7600dbf42"
    merchant_data = DEMO_MERCHANT_DATA.get(merchant_id)
    
    if not merchant_data:
        raise HTTPException(status_code=404, detail="Merchant not found")
    
    return {
        "status": "success",
        "data": merchant_data["webhooks"]
    }

@router.get("/merchant/webhooks/deliveries")
async def get_webhook_deliveries(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user)
):
    """Get webhook delivery logs"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Generate demo delivery logs
    deliveries = []
    statuses = ["success", "success", "success", "failed", "pending"]
    
    for i in range(limit):
        delivery_time = datetime.now() - timedelta(minutes=random.randint(0, 1440))
        deliveries.append({
            "id": f"del_{i+1000}",
            "event": random.choice(["order.created", "payment.completed", "order.updated"]),
            "status": random.choice(statuses),
            "attempts": 1 if i % 5 != 0 else random.randint(1, 3),
            "response_code": 200 if i % 5 != 0 else random.choice([200, 400, 500]),
            "created_at": delivery_time.isoformat() + "Z",
            "completed_at": (delivery_time + timedelta(seconds=random.uniform(0.1, 2))).isoformat() + "Z"
        })
    
    return {
        "status": "success",
        "data": {
            "deliveries": sorted(deliveries, key=lambda x: x["created_at"], reverse=True)
        }
    }

@router.post("/merchant/webhooks/test")
async def test_webhook(
    event: str,
    current_user: dict = Depends(get_current_user)
):
    """Send test webhook"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return {
        "status": "success",
        "message": f"Test webhook for event '{event}' sent successfully",
        "data": {
            "event": event,
            "delivery_id": f"del_test_{random.randint(1000, 9999)}",
            "sent_at": datetime.now().isoformat() + "Z"
        }
    }

@router.post("/merchant/psp/{psp_id}/test")
async def test_psp_connection(
    psp_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Test PSP connection"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Simulate test result
    success = random.choice([True, True, True, False])  # 75% success rate
    
    return {
        "status": "success" if success else "error",
        "message": "PSP connection test successful" if success else "Failed to connect to PSP",
        "data": {
            "psp_id": psp_id,
            "tested_at": datetime.now().isoformat() + "Z",
            "response_time": round(random.uniform(0.1, 1.5), 3),
            "capabilities_tested": ["card", "bank_transfer"] if success else []
        }
    }
