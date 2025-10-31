"""
MCP End-to-End Integration Test
Validates complete merchant commerce pipeline: Store → Products → Inventory → Orders → Payment
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from db.database import database
from utils.auth import get_current_user
import httpx
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp", tags=["mcp-e2e-test"])

class MCPTestResult(BaseModel):
    component: str
    status: str  # "success", "warning", "error"
    message: str
    details: Optional[Dict[str, Any]] = None
    latency_ms: Optional[int] = None

class MCPTestResponse(BaseModel):
    overall_status: str
    merchant_id: str
    timestamp: str
    tests: List[MCPTestResult]
    summary: Dict[str, int]

@router.post("/test/{merchant_id}", response_model=MCPTestResponse)
async def run_mcp_e2e_test(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Run comprehensive MCP integration test covering:
    1. Store connectivity (Shopify/Wix/etc)
    2. Product sync capability
    3. Inventory query
    4. Order creation
    5. Payment processing readiness
    """
    start_time = datetime.now()
    tests: List[MCPTestResult] = []
    
    try:
        # Test 1: Store Connectivity
        store_test = await _test_store_connectivity(merchant_id)
        tests.append(store_test)
        
        # Test 2: Product Sync (only if store connected)
        if store_test.status == "success":
            product_test = await _test_product_sync(merchant_id, store_test.details)
            tests.append(product_test)
        else:
            tests.append(MCPTestResult(
                component="Product Sync",
                status="skipped",
                message="Skipped due to store connectivity failure"
            ))
        
        # Test 3: Inventory Query
        inventory_test = await _test_inventory_query(merchant_id)
        tests.append(inventory_test)
        
        # Test 4: Order Creation Capability
        order_test = await _test_order_capability(merchant_id)
        tests.append(order_test)
        
        # Test 5: Payment Integration
        payment_test = await _test_payment_integration(merchant_id)
        tests.append(payment_test)
        
        # Calculate summary
        summary = {
            "total": len(tests),
            "success": len([t for t in tests if t.status == "success"]),
            "warning": len([t for t in tests if t.status == "warning"]),
            "error": len([t for t in tests if t.status == "error"]),
            "skipped": len([t for t in tests if t.status == "skipped"])
        }
        
        # Determine overall status
        if summary["error"] > 0:
            overall_status = "error"
        elif summary["warning"] > 0:
            overall_status = "warning"
        elif summary["success"] == summary["total"]:
            overall_status = "success"
        else:
            overall_status = "partial"
        
        return MCPTestResponse(
            overall_status=overall_status,
            merchant_id=merchant_id,
            timestamp=datetime.now().isoformat(),
            tests=tests,
            summary=summary
        )
        
    except Exception as e:
        logger.error(f"MCP test failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _test_store_connectivity(merchant_id: str) -> MCPTestResult:
    """Test 1: Store API connectivity"""
    start = datetime.now()
    try:
        # Get connected stores
        stores_query = """
            SELECT store_id, platform, domain, api_key, status
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
            AND status IN ('active', 'connected')
            ORDER BY connected_at DESC
            LIMIT 1
        """
        store = await database.fetch_one(stores_query, {"merchant_id": merchant_id})
        
        if not store:
            return MCPTestResult(
                component="Store Connectivity",
                status="error",
                message="No store connected. Please connect your store in Integrations.",
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
        
        platform = store["platform"]
        domain = store["domain"]
        api_key = store["api_key"]
        
        # Platform-specific connectivity test
        if platform == "shopify":
            if not domain or not api_key:
                return MCPTestResult(
                    component="Store Connectivity",
                    status="error",
                    message=f"Shopify credentials incomplete (domain={bool(domain)}, token={bool(api_key)})",
                    latency_ms=(datetime.now() - start).microseconds // 1000
                )
            
            # Test Shopify API
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"https://{domain}/admin/api/2023-10/shop.json",
                        headers={"X-Shopify-Access-Token": api_key}
                    )
                    if resp.status_code == 200:
                        shop_data = resp.json().get("shop", {})
                        return MCPTestResult(
                            component="Store Connectivity",
                            status="success",
                            message=f"Shopify store connected: {shop_data.get('name', domain)}",
                            details={"platform": platform, "domain": domain, "shop_name": shop_data.get("name")},
                            latency_ms=(datetime.now() - start).microseconds // 1000
                        )
                    else:
                        return MCPTestResult(
                            component="Store Connectivity",
                            status="error",
                            message=f"Shopify API returned {resp.status_code}: {resp.text[:200]}",
                            latency_ms=(datetime.now() - start).microseconds // 1000
                        )
            except Exception as e:
                return MCPTestResult(
                    component="Store Connectivity",
                    status="error",
                    message=f"Shopify API unreachable: {str(e)}",
                    latency_ms=(datetime.now() - start).microseconds // 1000
                )
        
        elif platform == "wix":
            # Wix connectivity test
            if not domain or not api_key:
                return MCPTestResult(
                    component="Store Connectivity",
                    status="error",
                    message=f"Wix credentials incomplete (site_id={bool(domain)}, api_key={bool(api_key)})",
                    latency_ms=(datetime.now() - start).microseconds // 1000
                )
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        "https://www.wixapis.com/stores/v1/products/query",
                        json={"query": {"limit": 1}},
                        headers={"Authorization": api_key, "wix-site-id": domain}
                    )
                    if resp.status_code in [200, 401, 403]:  # 401/403 means auth reached server
                        return MCPTestResult(
                            component="Store Connectivity",
                            status="success" if resp.status_code == 200 else "warning",
                            message=f"Wix store reachable: {domain}" + (f" (HTTP {resp.status_code})" if resp.status_code != 200 else ""),
                            details={"platform": platform, "domain": domain},
                            latency_ms=(datetime.now() - start).microseconds // 1000
                        )
                    else:
                        return MCPTestResult(
                            component="Store Connectivity",
                            status="error",
                            message=f"Wix API returned {resp.status_code}",
                            latency_ms=(datetime.now() - start).microseconds // 1000
                        )
            except Exception as e:
                return MCPTestResult(
                    component="Store Connectivity",
                    status="error",
                    message=f"Wix API unreachable: {str(e)}",
                    latency_ms=(datetime.now() - start).microseconds // 1000
                )
        
        elif platform == "woocommerce":
            # WooCommerce connectivity test
            try:
                creds = api_key.split(":", 1) if ":" in api_key else [api_key, ""]
                consumer_key, consumer_secret = (creds[0], creds[1]) if len(creds) == 2 else (api_key, "")
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{domain.rstrip('/')}/wp-json/wc/v3/system_status",
                        auth=(consumer_key, consumer_secret)
                    )
                    if resp.status_code == 200:
                        return MCPTestResult(
                            component="Store Connectivity",
                            status="success",
                            message=f"WooCommerce store connected: {domain}",
                            details={"platform": platform, "domain": domain},
                            latency_ms=(datetime.now() - start).microseconds // 1000
                        )
                    else:
                        return MCPTestResult(
                            component="Store Connectivity",
                            status="warning",
                            message=f"WooCommerce reachable but auth may need review (HTTP {resp.status_code})",
                            details={"platform": platform, "domain": domain},
                            latency_ms=(datetime.now() - start).microseconds // 1000
                        )
            except Exception as e:
                return MCPTestResult(
                    component="Store Connectivity",
                    status="error",
                    message=f"WooCommerce unreachable: {str(e)}",
                    latency_ms=(datetime.now() - start).microseconds // 1000
                )
        
        elif platform == "bigcommerce":
            # BigCommerce connectivity test
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"https://api.bigcommerce.com/stores/{domain}/v3/catalog/summary",
                        headers={"X-Auth-Token": api_key}
                    )
                    if resp.status_code == 200:
                        return MCPTestResult(
                            component="Store Connectivity",
                            status="success",
                            message=f"BigCommerce store connected: {domain}",
                            details={"platform": platform, "domain": domain},
                            latency_ms=(datetime.now() - start).microseconds // 1000
                        )
                    else:
                        return MCPTestResult(
                            component="Store Connectivity",
                            status="warning",
                            message=f"BigCommerce reachable but returned {resp.status_code}",
                            details={"platform": platform, "domain": domain},
                            latency_ms=(datetime.now() - start).microseconds // 1000
                        )
            except Exception as e:
                return MCPTestResult(
                    component="Store Connectivity",
                    status="error",
                    message=f"BigCommerce unreachable: {str(e)}",
                    latency_ms=(datetime.now() - start).microseconds // 1000
                )
        
        elif platform in ["prestashop", "square", "magento"]:
            # Generic platform connectivity (no deep API test, just record presence)
            return MCPTestResult(
                component="Store Connectivity",
                status="success",
                message=f"{platform.title()} store connected: {domain}",
                details={"platform": platform, "domain": domain},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
        
        else:
            return MCPTestResult(
                component="Store Connectivity",
                status="warning",
                message=f"{platform.title()} store connected but detailed test not available",
                details={"platform": platform, "domain": domain},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
            
    except Exception as e:
        logger.error(f"Store connectivity test failed: {e}")
        return MCPTestResult(
            component="Store Connectivity",
            status="error",
            message=str(e),
            latency_ms=(datetime.now() - start).microseconds // 1000
        )


async def _test_product_sync(merchant_id: str, store_details: Optional[Dict]) -> MCPTestResult:
    """Test 2: Product sync capability"""
    start = datetime.now()
    try:
        # Check products cache
        cache_query = """
            SELECT COUNT(*) as count, MAX(cached_at) as last_sync
            FROM products_cache
            WHERE merchant_id = :merchant_id
            AND (expires_at IS NULL OR expires_at > NOW())
        """
        result = await database.fetch_one(cache_query, {"merchant_id": merchant_id})
        
        product_count = result["count"] if result else 0
        last_sync = result["last_sync"] if result else None
        
        if product_count > 0:
            return MCPTestResult(
                component="Product Sync",
                status="success",
                message=f"{product_count} products synced from {store_details.get('platform', 'store') if store_details else 'store'}",
                details={"product_count": product_count, "last_sync": str(last_sync)},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
        else:
            return MCPTestResult(
                component="Product Sync",
                status="warning",
                message="No products synced yet. Run product sync to populate cache.",
                details={"product_count": 0},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
            
    except Exception as e:
        logger.error(f"Product sync test failed: {e}")
        return MCPTestResult(
            component="Product Sync",
            status="error",
            message=str(e),
            latency_ms=(datetime.now() - start).microseconds // 1000
        )


async def _test_inventory_query(merchant_id: str) -> MCPTestResult:
    """Test 3: Inventory query capability"""
    start = datetime.now()
    try:
        # Check if we can query inventory from cache
        inventory_query = """
            SELECT COUNT(*) as products_with_inventory
            FROM products_cache
            WHERE merchant_id = :merchant_id
            AND (expires_at IS NULL OR expires_at > NOW())
            AND product_data->>'inventory_quantity' IS NOT NULL
        """
        result = await database.fetch_one(inventory_query, {"merchant_id": merchant_id})
        
        inventory_count = result["products_with_inventory"] if result else 0
        
        if inventory_count > 0:
            return MCPTestResult(
                component="Inventory Query",
                status="success",
                message=f"{inventory_count} products with inventory data available",
                details={"products_with_inventory": inventory_count},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
        else:
            return MCPTestResult(
                component="Inventory Query",
                status="warning",
                message="No inventory data available. Sync products to populate inventory.",
                details={"products_with_inventory": 0},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
            
    except Exception as e:
        logger.error(f"Inventory query test failed: {e}")
        return MCPTestResult(
            component="Inventory Query",
            status="error",
            message=str(e),
            latency_ms=(datetime.now() - start).microseconds // 1000
        )


async def _test_order_capability(merchant_id: str) -> MCPTestResult:
    """Test 4: Order creation capability"""
    start = datetime.now()
    try:
        # Check order table and recent orders
        orders_query = """
            SELECT COUNT(*) as total_orders,
                   COUNT(CASE WHEN created_at > NOW() - INTERVAL '7 days' THEN 1 END) as recent_orders
            FROM orders
            WHERE merchant_id = :merchant_id
        """
        result = await database.fetch_one(orders_query, {"merchant_id": merchant_id})
        
        total_orders = result["total_orders"] if result else 0
        recent_orders = result["recent_orders"] if result else 0
        
        return MCPTestResult(
            component="Order Processing",
            status="success",
            message=f"Order system ready. {total_orders} total orders, {recent_orders} in last 7 days.",
            details={"total_orders": total_orders, "recent_orders": recent_orders},
            latency_ms=(datetime.now() - start).microseconds // 1000
        )
            
    except Exception as e:
        logger.error(f"Order capability test failed: {e}")
        return MCPTestResult(
            component="Order Processing",
            status="error",
            message=str(e),
            latency_ms=(datetime.now() - start).microseconds // 1000
        )


async def _test_payment_integration(merchant_id: str) -> MCPTestResult:
    """Test 5: Payment integration readiness"""
    start = datetime.now()
    try:
        # Check connected PSPs
        psp_query = """
            SELECT COUNT(*) as psp_count,
                   STRING_AGG(DISTINCT provider, ', ') as providers
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
            AND status = 'active'
        """
        result = await database.fetch_one(psp_query, {"merchant_id": merchant_id})
        
        psp_count = result["psp_count"] if result else 0
        providers = result["providers"] if result else None
        
        if psp_count > 0:
            return MCPTestResult(
                component="Payment Integration",
                status="success",
                message=f"{psp_count} PSP(s) connected: {providers}",
                details={"psp_count": psp_count, "providers": providers},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
        else:
            return MCPTestResult(
                component="Payment Integration",
                status="warning",
                message="No PSPs connected. Add payment processors in Integrations to accept payments.",
                details={"psp_count": 0},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
            
    except Exception as e:
        logger.error(f"Payment integration test failed: {e}")
        return MCPTestResult(
            component="Payment Integration",
            status="error",
            message=str(e),
            latency_ms=(datetime.now() - start).microseconds // 1000
        )

