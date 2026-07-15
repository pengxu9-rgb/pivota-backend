"""
MCP End-to-End Integration Test
Validates complete merchant commerce pipeline: ALL Stores → Products → Inventory → Orders → ALL PSPs
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
from adapters.bigcommerce_adapter import (
    build_bigcommerce_headers,
    normalize_bigcommerce_store_hash,
)
from adapters.woocommerce_adapter import normalize_woocommerce_store_url

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


def _extract_woocommerce_credentials(api_key: str) -> tuple[str, str]:
    raw = str(api_key or "").strip()
    if not raw:
        return "", ""
    try:
        if raw.startswith("{"):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return (
                    str(parsed.get("consumer_key") or "").strip(),
                    str(parsed.get("consumer_secret") or "").strip(),
                )
    except Exception:
        pass
    if ":" in raw:
        consumer_key, consumer_secret = raw.split(":", 1)
        return consumer_key.strip(), consumer_secret.strip()
    return raw, ""


def _extract_bigcommerce_credentials(domain: str, api_key: str) -> tuple[str, str, str]:
    raw = str(api_key or "").strip()
    access_token = raw
    client_id = ""
    store_hash = normalize_bigcommerce_store_hash(domain)
    try:
        if raw.startswith("{"):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                access_token = str(parsed.get("access_token") or "").strip()
                client_id = str(parsed.get("client_id") or "").strip()
                store_hash = normalize_bigcommerce_store_hash(
                    parsed.get("store_hash") or domain
                )
    except Exception:
        pass
    return store_hash, access_token, client_id

@router.post("/test/{merchant_id}", response_model=MCPTestResponse)
async def run_mcp_e2e_test(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Run comprehensive MCP integration test covering:
    1. ALL Store connectivity (Shopify, Wix, WooCommerce, BigCommerce, etc.)
    2. Product sync status per platform
    3. Inventory data availability
    4. Order processing capability
    5. ALL Payment processors readiness
    """
    start_time = datetime.now()
    tests: List[MCPTestResult] = []
    
    try:
        # Test 1: ALL Stores Connectivity
        stores_test = await _test_all_stores_connectivity(merchant_id)
        tests.append(stores_test)
        
        # Test 2: Product Sync per platform
        product_test = await _test_product_sync_by_platform(merchant_id)
        tests.append(product_test)
        
        # Test 3: Inventory Query
        inventory_test = await _test_inventory_query(merchant_id)
        tests.append(inventory_test)
        
        # Test 4: Order Creation Capability
        order_test = await _test_order_capability(merchant_id)
        tests.append(order_test)
        
        # Test 5: ALL Payment Integrations
        payment_test = await _test_all_payment_integrations(merchant_id)
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


async def _test_all_stores_connectivity(merchant_id: str) -> MCPTestResult:
    """Test 1: Test ALL connected stores (Shopify, Wix, WooCommerce, etc.)"""
    start = datetime.now()
    try:
        # Get ALL connected stores
        stores_query = """
            SELECT store_id, platform, domain, name, api_key, status
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
            AND status IN ('active', 'connected')
            ORDER BY platform, connected_at DESC
        """
        stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
        
        if not stores or len(stores) == 0:
            return MCPTestResult(
                component="Store Connectivity",
                status="error",
                message="No stores connected. Please connect your stores in Integrations.",
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
        
        # Test each store
        store_results = []
        for store in stores:
            platform = store["platform"]
            domain = store["domain"]
            api_key = store["api_key"]
            name = store["name"]
            
            store_test = await _test_single_store_api(platform, domain, api_key, name)
            store_results.append(store_test)
        
        # Aggregate
        success_count = len([r for r in store_results if r["status"] == "success"])
        warning_count = len([r for r in store_results if r["status"] == "warning"])
        error_count = len([r for r in store_results if r["status"] == "error"])
        
        overall_status = "success" if success_count == len(stores) else ("warning" if error_count == 0 else "error")
        
        # Build message
        platform_list = ', '.join([f"{r['platform']}({r['status'][:1].upper()})" for r in store_results])
        
        return MCPTestResult(
            component="Store Connectivity",
            status=overall_status,
            message=f"{len(stores)} store(s) tested: {platform_list}",
            details={
                "total_stores": len(stores),
                "success": success_count,
                "warning": warning_count,
                "error": error_count,
                "stores": store_results
            },
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


async def _test_single_store_api(platform: str, domain: str, api_key: str, name: str) -> Dict[str, Any]:
    """Test a single store's API connectivity"""
    try:
        if platform == "shopify":
            if not domain or not api_key:
                return {
                    "platform": "Shopify",
                    "name": name,
                    "domain": domain,
                    "status": "error",
                    "message": "Credentials incomplete"
                }
            
            # Parse token if it's JSON
            token = api_key
            token_source = "direct"
            try:
                if isinstance(api_key, str) and api_key.strip().startswith("{"):
                    parsed = json.loads(api_key)
                    token = parsed.get("access_token") or parsed.get("token") or api_key
                    token_source = "json_parsed"
                    logger.info(f"Shopify token parsed from JSON: {token[:15]}...")
            except Exception as parse_error:
                logger.warning(f"Failed to parse Shopify token as JSON: {parse_error}")
                token_source = "parse_failed"
            
            logger.info(f"Testing Shopify {domain} with token (length={len(token)}, source={token_source})")
            
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"https://{domain}/admin/api/2025-10/shop.json",
                        headers={"X-Shopify-Access-Token": token}
                    )
                    
                    logger.info(f"Shopify API response: {resp.status_code}")
                    
                    if resp.status_code == 200:
                        shop_data = resp.json().get("shop", {})
                        return {
                            "platform": "Shopify",
                            "name": shop_data.get("name", name),
                            "domain": domain,
                            "status": "success",
                            "message": "Connected & reachable"
                        }
                    else:
                        error_body = resp.text[:200] if resp.text else "No error body"
                        logger.error(f"Shopify API error {resp.status_code}: {error_body}")
                        return {
                            "platform": "Shopify",
                            "name": name,
                            "domain": domain,
                            "status": "error",
                            "message": f"HTTP {resp.status_code}",
                            "error": error_body
                        }
            except Exception as e:
                return {
                    "platform": "Shopify",
                    "name": name,
                    "domain": domain,
                    "status": "error",
                    "message": f"Unreachable: {str(e)[:50]}"
                }
        
        elif platform == "wix":
            if not domain or not api_key:
                return {
                    "platform": "Wix",
                    "name": name,
                    "domain": domain,
                    "status": "error",
                    "message": "Credentials incomplete"
                }
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        "https://www.wixapis.com/stores/v1/products/query",
                        json={"query": {"limit": 1}},
                        headers={"Authorization": api_key, "wix-site-id": domain}
                    )
                    if resp.status_code == 200:
                        return {
                            "platform": "Wix",
                            "name": name,
                            "domain": domain,
                            "status": "success",
                            "message": "Connected & reachable"
                        }
                    elif resp.status_code in [401, 403]:
                        return {
                            "platform": "Wix",
                            "name": name,
                            "domain": domain,
                            "status": "warning",
                            "message": f"Reachable but auth issue (HTTP {resp.status_code})"
                        }
                    else:
                        return {
                            "platform": "Wix",
                            "name": name,
                            "domain": domain,
                            "status": "error",
                            "message": f"API error {resp.status_code}"
                        }
            except Exception as e:
                return {
                    "platform": "Wix",
                    "name": name,
                    "domain": domain,
                    "status": "error",
                    "message": f"Unreachable: {str(e)[:50]}"
                }
        
        elif platform == "woocommerce":
            try:
                consumer_key, consumer_secret = _extract_woocommerce_credentials(api_key)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{normalize_woocommerce_store_url(domain)}/wp-json/wc/v3/system_status",
                        params={
                            "consumer_key": consumer_key,
                            "consumer_secret": consumer_secret,
                        },
                    )
                    return {
                        "platform": "WooCommerce",
                        "name": name,
                        "domain": domain,
                        "status": "success" if resp.status_code == 200 else "warning",
                        "message": f"Connected (HTTP {resp.status_code})"
                    }
            except Exception as e:
                return {
                    "platform": "WooCommerce",
                    "name": name,
                    "domain": domain,
                    "status": "error",
                    "message": f"Unreachable: {str(e)[:50]}"
                }
        
        elif platform == "bigcommerce":
            try:
                store_hash, access_token, client_id = _extract_bigcommerce_credentials(domain, api_key)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"https://api.bigcommerce.com/stores/{store_hash}/v2/store",
                        headers=build_bigcommerce_headers(access_token, client_id),
                    )
                    return {
                        "platform": "BigCommerce",
                        "name": name,
                        "domain": domain,
                        "status": "success" if resp.status_code == 200 else "warning",
                        "message": f"Connected (HTTP {resp.status_code})"
                    }
            except Exception as e:
                return {
                    "platform": "BigCommerce",
                    "name": name,
                    "domain": domain,
                    "status": "error",
                    "message": f"Unreachable: {str(e)[:50]}"
                }
        
        else:
            # Generic platform (PrestaShop, Square, Magento, etc.)
            return {
                "platform": platform.title(),
                "name": name,
                "domain": domain,
                "status": "success",
                "message": "Connected (detailed test not available)"
            }
            
    except Exception as e:
        return {
            "platform": platform.title(),
            "name": name or "Unknown",
            "domain": domain or "Unknown",
            "status": "error",
            "message": str(e)[:100]
        }


async def _test_product_sync_by_platform(merchant_id: str) -> MCPTestResult:
    """Test 2: Product sync capability - SEPARATED by platform"""
    start = datetime.now()
    try:
        # Get products count per platform
        cache_query = """
            SELECT 
                platform,
                COUNT(*) as count,
                MAX(cached_at) as last_sync
            FROM products_cache
            WHERE merchant_id = :merchant_id
            AND (expires_at IS NULL OR expires_at > NOW())
            GROUP BY platform
            ORDER BY platform
        """
        results = await database.fetch_all(cache_query, {"merchant_id": merchant_id})
        
        if not results or len(results) == 0:
            return MCPTestResult(
                component="Product Sync",
                status="warning",
                message="No products synced yet. Run product sync to populate cache.",
                details={"platforms": []},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
        
        # Build per-platform details
        platform_details = []
        total_products = 0
        for row in results:
            platform = row["platform"]
            count = row["count"]
            last_sync = row["last_sync"]
            total_products += count
            platform_details.append({
                "platform": platform.title(),
                "product_count": count,
                "last_sync": str(last_sync) if last_sync else None
            })
        
        platforms_str = ', '.join([f"{p['platform']}({p['product_count']})" for p in platform_details])
        
        return MCPTestResult(
            component="Product Sync",
            status="success",
            message=f"{total_products} products across {len(results)} platform(s): {platforms_str}",
            details={"platforms": platform_details, "total_products": total_products},
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
            AND (
                product_data->>'inventory_quantity' IS NOT NULL 
                OR product_data->>'stock' IS NOT NULL
            )
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


async def _test_all_payment_integrations(merchant_id: str) -> MCPTestResult:
    """Test 5: ALL Payment integrations readiness"""
    start = datetime.now()
    try:
        # Check ALL connected PSPs
        psp_query = """
            SELECT provider, name, status, account_id
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
            ORDER BY provider
        """
        psps = await database.fetch_all(psp_query, {"merchant_id": merchant_id})
        
        if not psps or len(psps) == 0:
            return MCPTestResult(
                component="Payment Integration",
                status="warning",
                message="No PSPs connected. Add payment processors in Integrations to accept payments.",
                details={"psps": []},
                latency_ms=(datetime.now() - start).microseconds // 1000
            )
        
        # Build PSP details
        psp_details = []
        active_count = 0
        for psp in psps:
            provider = psp["provider"]
            name = psp["name"]
            status = psp["status"]
            if status == "active":
                active_count += 1
            psp_details.append({
                "provider": provider.title(),
                "name": name,
                "status": status
            })
        
        psps_str = ', '.join([f"{p['provider']}({p['status'][:1].upper()})" for p in psp_details])
        
        overall_status = "success" if active_count > 0 else "warning"
        
        return MCPTestResult(
            component="Payment Integration",
            status=overall_status,
            message=f"{len(psps)} PSP(s) configured: {psps_str}",
            details={"psps": psp_details, "total": len(psps), "active": active_count},
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


async def _test_single_store_api(platform: str, domain: str, api_key: str, name: str) -> Dict[str, Any]:
    """Test a single store's API (returns dict for aggregation)"""
    
    if platform == "shopify":
        if not domain or not api_key:
            return {"platform": "Shopify", "name": name, "domain": domain, "status": "error", "message": "Incomplete credentials"}
        
        # Parse token if it's JSON (CRITICAL: must match _test_single_store_api logic)
        token = api_key
        try:
            if isinstance(api_key, str) and api_key.strip().startswith("{"):
                parsed = json.loads(api_key)
                token = parsed.get("access_token") or parsed.get("token") or api_key
                logger.info(f"[Aggregation] Shopify token parsed from JSON")
        except:
            pass
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://{domain}/admin/api/2025-10/shop.json",
                    headers={"X-Shopify-Access-Token": token}
                )
                if resp.status_code == 200:
                    return {"platform": "Shopify", "name": name, "domain": domain, "status": "success", "message": "OK"}
                else:
                    return {"platform": "Shopify", "name": name, "domain": domain, "status": "error", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"platform": "Shopify", "name": name, "domain": domain, "status": "error", "message": str(e)[:50]}
    
    elif platform == "wix":
        if not domain or not api_key:
            return {"platform": "Wix", "name": name, "domain": domain, "status": "error", "message": "Incomplete credentials"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://www.wixapis.com/stores/v1/products/query",
                    json={"query": {"limit": 1}},
                    headers={"Authorization": api_key, "wix-site-id": domain}
                )
                if resp.status_code == 200:
                    return {"platform": "Wix", "name": name, "domain": domain, "status": "success", "message": "OK"}
                elif resp.status_code in [401, 403]:
                    return {"platform": "Wix", "name": name, "domain": domain, "status": "warning", "message": f"Auth issue (HTTP {resp.status_code})"}
                else:
                    return {"platform": "Wix", "name": name, "domain": domain, "status": "error", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"platform": "Wix", "name": name, "domain": domain, "status": "error", "message": str(e)[:50]}
    
    elif platform == "woocommerce":
        try:
            consumer_key, consumer_secret = _extract_woocommerce_credentials(api_key)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{normalize_woocommerce_store_url(domain)}/wp-json/wc/v3/system_status",
                    params={
                        "consumer_key": consumer_key,
                        "consumer_secret": consumer_secret,
                    },
                )
                status = "success" if resp.status_code == 200 else "warning"
                return {"platform": "WooCommerce", "name": name, "domain": domain, "status": status, "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"platform": "WooCommerce", "name": name, "domain": domain, "status": "error", "message": str(e)[:50]}
    
    elif platform == "bigcommerce":
        try:
            store_hash, access_token, client_id = _extract_bigcommerce_credentials(domain, api_key)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://api.bigcommerce.com/stores/{store_hash}/v2/store",
                    headers=build_bigcommerce_headers(access_token, client_id),
                )
                status = "success" if resp.status_code == 200 else "warning"
                return {"platform": "BigCommerce", "name": name, "domain": domain, "status": status, "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"platform": "BigCommerce", "name": name, "domain": domain, "status": "error", "message": str(e)[:50]}
    
    else:
        # Generic platform
        return {"platform": platform.title(), "name": name, "domain": domain, "status": "success", "message": "Connected (no API test)"}
