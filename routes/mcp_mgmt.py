"""
MCP (Model Context Protocol) Management Routes
Provides endpoints for managing MCP connections and interactions

HONESTY CONTRACT (fabrication-belt fix, 2026-08-11): every value returned here is
either read from the database or measured in this process. This module used to
fabricate operator-facing telemetry — random.randint "response times", hardcoded
"connected"/"healthy"/"99.9% uptime" verdicts, and datetime.now() presented as a
sync time — which made a real outage invisible. None of these endpoints performs
a LIVE platform connectivity probe; they report configuration state and say so.
"""
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from utils.auth import EMPLOYEE_STAFF_ROLES, get_current_user
from db.database import database

SUPPORTED_PLATFORMS = ["shopify", "wix", "woocommerce", "bigcommerce"]

# The API version each platform integration is PINNED to in this codebase —
# configuration facts, not values detected from a live handshake.
API_VERSION_BY_PLATFORM = {
    "shopify": "2025-10",
    "wix": "v1",
    "woocommerce": "wc/v3",
    "bigcommerce": "v3",
}

# No prefix here: main.py mounts this router at the real prefix AND at the
# legacy /mcp alias, so the rename cannot break a caller mid-deploy.
router = APIRouter()

@router.get("/status")
async def get_mcp_status(
    current_user: dict = Depends(get_current_user)
):
    """Configuration-derived MCP status: every field comes from the database."""
    if current_user["role"] not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        # Get total merchants and stores for MCP stats
        merchants_query = "SELECT COUNT(*) as total FROM merchant_onboarding WHERE status = 'active'"
        merchants_count = await database.fetch_one(merchants_query)

        stores_query = "SELECT COUNT(*) as total, platform FROM merchant_stores WHERE status = 'active' GROUP BY platform"
        stores_by_platform = await database.fetch_all(stores_query)

        # The real most-recent sync across all stores — this used to be
        # datetime.now(), i.e. the page-load time dressed up as a sync time.
        last_sync_row = await database.fetch_one(
            "SELECT MAX(last_sync) AS max_sync FROM merchant_stores WHERE status = 'active'"
        )
        last_sync = last_sync_row["max_sync"] if last_sync_row else None

        shopify_stores = next((s["total"] for s in stores_by_platform if s["platform"] == "shopify"), 0)
        wix_stores = next((s["total"] for s in stores_by_platform if s["platform"] == "wix"), 0)
        total_active_stores = sum(s["total"] for s in stores_by_platform)

        return {
            "status": "success",
            "mcp_status": {
                # "connected" = at least one active store is configured. It is a
                # DB fact, not a probe result ("health": "ok" below means only
                # that this endpoint and its DB queries succeeded).
                "connected": total_active_stores > 0,
                "version": "1.0.0",
                "last_sync": last_sync.isoformat() if last_sync else None,
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
                "health": "ok"
            }
        }

    except Exception as e:
        # A failed status read is an ERROR, not a "success" with a shrug —
        # the old shape kept the dashboard green through a DB outage.
        print(f"Error getting MCP status: {e}")
        return {
            "status": "error",
            "mcp_status": {
                "connected": False,
                "version": "1.0.0",
                "health": "unreachable",
                "error": str(e)
            }
        }

@router.post("/test-connection")
async def test_mcp_connection(
    platform: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """
    Store-configuration check — NOT a live connectivity probe.

    This endpoint reports which platforms have active stores configured, and
    measures the database round-trip it actually performs. It never contacts a
    platform API (the old implementation returned random.randint "response
    times" and hardcoded "connected" verdicts, so a real outage looked healthy).
    """
    if current_user["role"] not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        if platform:
            if platform not in SUPPORTED_PLATFORMS:
                raise HTTPException(status_code=400, detail="Invalid platform")

            started = monotonic()
            stores = await database.fetch_one(
                """
                SELECT COUNT(*) as total FROM merchant_stores
                WHERE platform = :platform AND status = 'active'
                """,
                {"platform": platform},
            )
            elapsed_ms = int((monotonic() - started) * 1000)

            active_stores = stores["total"] if stores else 0
            has_connections = active_stores > 0

            return {
                "status": "success",
                "test_result": {
                    "platform": platform,
                    "check": "store_configuration_only",
                    "live_probe": False,
                    "connected": has_connections,
                    # Measured DB round-trip for this check — the only latency
                    # this endpoint actually observes.
                    "response_time": elapsed_ms,
                    "api_version": API_VERSION_BY_PLATFORM.get(platform, "unknown"),
                    "active_stores": active_stores,
                    "message": (
                        f"{active_stores} active {platform} store(s) configured; "
                        "live platform connectivity was not probed"
                        if has_connections
                        else f"No active {platform} stores configured; live platform connectivity was not probed"
                    ),
                }
            }
        else:
            started = monotonic()
            rows = await database.fetch_all(
                """
                SELECT platform, COUNT(*) as total FROM merchant_stores
                WHERE status = 'active'
                GROUP BY platform
                """
            )
            elapsed_ms = int((monotonic() - started) * 1000)

            counts = {r["platform"]: r["total"] for r in rows}
            platforms = {
                p: {
                    "status": "configured" if counts.get(p, 0) > 0 else "no_active_stores",
                    "active_stores": counts.get(p, 0),
                }
                for p in SUPPORTED_PLATFORMS
            }
            any_configured = any(v["active_stores"] > 0 for v in platforms.values())

            return {
                "status": "success",
                "test_result": {
                    "check": "store_configuration_only",
                    "live_probe": False,
                    "overall": "configured" if any_configured else "no_active_stores",
                    "response_time": elapsed_ms,
                    "platforms": platforms,
                    "message": "Store configuration read from database; live platform connectivity was not probed",
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection test failed: {str(e)}")

@router.get("/merchants")
async def get_mcp_merchants(
    current_user: dict = Depends(get_current_user)
):
    """Get merchants with MCP connections"""
    if current_user["role"] not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get merchants with their MCP connections
        merchants_query = """
            SELECT
                m.merchant_id,
                m.business_name,
                m.contact_email,
                m.status,
                COUNT(s.store_id) as connected_stores,
                STRING_AGG(DISTINCT s.platform, ', ') as platforms,
                MAX(s.last_sync) as last_sync
            FROM merchant_onboarding m
            LEFT JOIN merchant_stores s ON m.merchant_id = s.merchant_id
            WHERE m.status = 'active'
            GROUP BY m.merchant_id, m.business_name, m.contact_email, m.status
            ORDER BY m.business_name
        """

        merchants = await database.fetch_all(merchants_query)

        return {
            "status": "success",
            "merchants": [
                {
                    "merchant_id": m["merchant_id"],
                    "business_name": m["business_name"],
                    "email": m["contact_email"],
                    "mcp_status": "connected" if m["connected_stores"] > 0 else "not_connected",
                    "connected_stores": m["connected_stores"],
                    "platforms": m["platforms"].split(", ") if m["platforms"] else [],
                    # Real most-recent store sync, or null if this merchant has
                    # never synced (was datetime.now() — the page-load time).
                    "last_sync": m["last_sync"].isoformat() if m["last_sync"] else None
                }
                for m in merchants
            ]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get MCP merchants: {str(e)}")

@router.get("/analytics")
async def get_mcp_analytics(
    days: int = 30,
    current_user: dict = Depends(get_current_user)
):
    """Get MCP analytics"""
    if current_user["role"] not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get sync statistics. `days` is typed int so .format() was not
        # injectable, but bind it anyway — every statement in this repo must
        # PREPARE cleanly with bound params. (The repo's SQL-prepare gate
        # collector does not walk function-local strings, so this statement is
        # pinned by tests/test_mcp_mgmt_honest_health.py and was PREPAREd
        # manually against real Postgres 15.)
        sync_query = """
            SELECT
                DATE(last_sync) as date,
                COUNT(*) as syncs,
                SUM(product_count) as products_synced
            FROM merchant_stores
            WHERE last_sync >= CURRENT_DATE - make_interval(days => :days)
              AND last_sync IS NOT NULL
            GROUP BY DATE(last_sync)
            ORDER BY date DESC
        """

        sync_stats = await database.fetch_all(sync_query, {"days": days})
        
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

@router.post("/sync-all")
async def sync_all_stores(
    current_user: dict = Depends(get_current_user)
):
    """
    Trigger real product sync for all active stores (MCP view).

    Uses the same universal sync pipeline as the merchant-facing Shopify/Wix
    sync endpoints, instead of just updating last_sync timestamps.
    """
    if current_user["role"] not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Lazy import to avoid circular imports at module load time
        from routes.product_sync import SyncRequest, sync_products

        # Get all active stores
        stores_query = """
            SELECT store_id, platform, name, merchant_id 
            FROM merchant_stores 
            WHERE status = 'active'
        """
        stores = await database.fetch_all(stores_query)
        
        if not stores:
            return {
                "status": "success",
                "message": "No active stores to sync",
                "synced": 0,
                "results": [],
            }

        results = []
        success_count = 0
        warning_count = 0
        error_count = 0

        for store in stores:
            store_id = store["store_id"]
            platform = store["platform"]
            merchant_id = store["merchant_id"]

            try:
                sync_request = SyncRequest(
                    merchant_id=merchant_id,
                    force_refresh=True,
                    limit=250,
                    platform=platform,
                )

                sync_result = await sync_products(
                    request=sync_request,
                    background_tasks=BackgroundTasks(),
                    current_user=current_user,
                )

                status = sync_result.status
                if status == "success":
                    success_count += 1
                elif status == "warning":
                    warning_count += 1
                else:
                    error_count += 1

                results.append(
                    {
                        "store_id": store_id,
                        "platform": platform,
                        "merchant_id": merchant_id,
                        "status": status,
                        "message": sync_result.message,
                        "products_synced": sync_result.products_synced,
                        "sync_time": sync_result.sync_time,
                    }
                )
            except Exception as e:  # pragma: no cover - defensive
                error_count += 1
                results.append(
                    {
                        "store_id": store_id,
                        "platform": platform,
                        "merchant_id": merchant_id,
                        "status": "error",
                        "message": str(e),
                        "products_synced": 0,
                        "sync_time": datetime.now().isoformat(),
                    }
                )

        return {
            "status": "success",
            "message": (
                f"Sync completed: success={success_count}, "
                f"warnings={warning_count}, errors={error_count}"
            ),
            "synced": success_count,
            "summary": {
                "success": success_count,
                "warnings": warning_count,
                "errors": error_count,
                "total_stores": len(stores),
            },
            "results": results,
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync stores: {str(e)}")

@router.get("/logs")
async def get_mcp_logs(
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    """
    Recent sync EVENTS derived from `merchant_stores.last_sync` — there is no
    operation log table behind this. The old implementation dressed these rows
    up as logs: a fresh random log_id on every request and an unconditional
    "success" status for syncs whose outcome was never recorded.
    """
    if current_user["role"] not in EMPLOYEE_STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
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
            last_sync = store["last_sync"]
            logs.append({
                # Deterministic id: the same event keeps the same id across
                # requests (was a fresh uuid per page load).
                "log_id": f"sync_{store['store_id']}_{int(last_sync.timestamp())}",
                "timestamp": last_sync.isoformat(),
                "operation": "sync_products",
                "platform": store["platform"],
                "store": store["name"],
                "merchant": store["business_name"],
                # A sync was RECORDED at this time; whether it fully succeeded
                # is not stored, so we do not claim it.
                "status": "recorded",
                "details": f"Product sync recorded for {store['platform']} store"
            })

        return {
            "status": "success",
            "source": "derived_from_store_last_sync",
            "logs": logs
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get MCP logs: {str(e)}")



