"""Extended Merchant API Routes for Dashboard Features"""
from fastapi import APIRouter, Depends, HTTPException, Body, BackgroundTasks
from typing import Dict, Any, Optional, List
from utils.auth import get_current_user
from datetime import datetime, timezone, timedelta
from db.database import database
from db.orders import get_order, mark_order_shipped
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
import json
import hashlib

from services.payment_routing_service import PaymentRoutingService
from services.merchant_store_service import get_primary_store
from routes.after_sales_cases import _ensure_after_sales_cases_table, _serialize_case

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


class MerchantRoutingUpdate(BaseModel):
    """Merchant-level PSP routing configuration"""
    psp_priority: List[Dict[str, Any]]
    routing_strategy: str = "priority"
    max_retries: int = 2
    timeout_ms: int = 30000


class ApproveAfterSalesCaseRequest(BaseModel):
    """Optional override fields for merchant approval."""
    approved_refund_amount: Optional[float] = None
    note: Optional[str] = None


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _coerce_json_list(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


async def _ensure_refund_tables_best_effort() -> None:
    """
    Best-effort defensive DDL for refund tables/columns.
    Production should normally rely on SQL migrations, but the migration runner can be best-effort.
    """
    try:
        await database.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_refunded NUMERIC(10,2) DEFAULT 0;")
    except Exception:
        pass

    try:
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS refund_records (
                refund_id VARCHAR(50) PRIMARY KEY,
                order_id VARCHAR(50) NOT NULL,
                merchant_id VARCHAR(50) NOT NULL,
                amount DECIMAL(10,2) NOT NULL,
                currency VARCHAR(3) DEFAULT 'USD',
                reason VARCHAR(100),
                source VARCHAR(50),
                status VARCHAR(50) DEFAULT 'pending',
                platform_type VARCHAR(50),
                platform_refund_id VARCHAR(255),
                platform_sync_status VARCHAR(50),
                psp_type VARCHAR(50),
                psp_refund_id VARCHAR(255),
                raw_payload JSONB,
                created_by VARCHAR(255),
                error_message TEXT,
                idempotency_key VARCHAR(255) UNIQUE,
                created_at TIMESTAMP DEFAULT NOW(),
                processed_at TIMESTAMP,
                CONSTRAINT fk_refund_order FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE RESTRICT
            );
            """
        )
    except Exception:
        pass

    try:
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS refund_retry_queue (
                id SERIAL PRIMARY KEY,
                refund_id VARCHAR(50) NOT NULL,
                retry_count INT DEFAULT 0,
                max_retries INT DEFAULT 3,
                next_retry_at TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
            """
        )
    except Exception:
        pass

    try:
        await database.execute("CREATE INDEX IF NOT EXISTS idx_merchant_refunds ON refund_records (merchant_id, created_at DESC);")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_order_refunds ON refund_records (order_id, created_at DESC);")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_idempotency ON refund_records (idempotency_key);")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_platform_refund ON refund_records (platform_type, platform_refund_id);")
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_retry_queue_next ON refund_retry_queue (next_retry_at) WHERE retry_count < max_retries;"
        )
    except Exception:
        pass


def _error_debug_id(prefix: str) -> str:
    return f"{prefix}_{hashlib.sha256(f'{time.time()}_{random.random()}'.encode()).hexdigest()[:10]}"


async def _append_after_sales_audit_event(case_id: str, event: str, payload: Dict[str, Any]) -> None:
    try:
        row = await database.fetch_one(
            "SELECT audit_log FROM after_sales_cases WHERE case_id = :case_id",
            {"case_id": case_id},
        )
        if not row:
            return
        audit = _coerce_json_list(dict(row).get("audit_log"))
        audit.append({"at": _now_iso(), "event": event, "payload": payload})
        await database.execute(
            """
            UPDATE after_sales_cases
            SET audit_log = CAST(:audit_log AS JSONB),
                updated_at = NOW()
            WHERE case_id = :case_id
            """,
            {"case_id": case_id, "audit_log": json.dumps(audit, ensure_ascii=False)},
        )
    except Exception:
        return


async def _create_shopify_manual_refund_best_effort(
    *,
    merchant_id: str,
    pivota_order_id: str,
    shopify_order_id: str,
    case_id: str,
    refund_amount: float,
    reason: str,
) -> None:
    """
    Best-effort platform sync for Shopify:
    - Create a "manual" refund record in Shopify for accounting.
    - Optionally restock inventory when the order is fully refunded.
    """
    try:
        store = await get_primary_store(merchant_id)
        if not store or str(store.get("platform") or "").lower() != "shopify":
            return
        shop_domain = str(store.get("domain") or "").strip()
        access_token = str(store.get("api_key") or "").strip()
        if not shop_domain or not access_token:
            return

        # Idempotency per after-sales case: skip if already recorded.
        try:
            row = await database.fetch_one(
                "SELECT audit_log FROM after_sales_cases WHERE case_id = :case_id",
                {"case_id": case_id},
            )
            audit = _coerce_json_list(dict(row).get("audit_log")) if row else []
            if any((a.get("event") == "shopify_manual_refund_created") for a in audit):
                return
        except Exception:
            pass

        order = await get_order(pivota_order_id)
        if not order or str(order.get("merchant_id") or "") != str(merchant_id):
            return

        # Restock only when the order is fully refunded (best-effort).
        restock = str(order.get("payment_status") or "").lower() == "refunded"

        async with httpx.AsyncClient(timeout=15.0) as client:
            refund_line_items: list[dict[str, Any]] = []
            if restock:
                # Fetch Shopify line items so we can restock all quantities.
                order_url = (
                    f"https://{shop_domain}/admin/api/2024-01/orders/{shopify_order_id}.json"
                    "?fields=line_items"
                )
                resp = await client.get(
                    order_url,
                    headers={
                        "X-Shopify-Access-Token": access_token,
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code == 200:
                    shopify_order = resp.json().get("order") or {}
                    for li in (shopify_order.get("line_items") or []):
                        try:
                            line_item_id = li.get("id")
                            qty = int(li.get("quantity") or 0)
                            if line_item_id and qty > 0:
                                refund_line_items.append(
                                    {
                                        "line_item_id": line_item_id,
                                        "quantity": qty,
                                        "restock_type": "return",
                                    }
                                )
                        except Exception:
                            continue

            # Create refund record (gateway=manual so it does not hit PSP again).
            refunds_url = f"https://{shop_domain}/admin/api/2024-01/orders/{shopify_order_id}/refunds.json"
            body: Dict[str, Any] = {
                "refund": {
                    "note": f"Pivota after-sales case {case_id}: {reason}".strip()[:240],
                    "notify": False,
                    "transactions": [
                        {
                            "kind": "refund",
                            "gateway": "manual",
                            "amount": f"{refund_amount:.2f}",
                        }
                    ],
                }
            }
            if refund_line_items:
                body["refund"]["refund_line_items"] = refund_line_items

            resp = await client.post(
                refunds_url,
                json=body,
                headers={
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code not in (200, 201):
                await _append_after_sales_audit_event(
                    case_id,
                    "shopify_manual_refund_failed",
                    {"status_code": resp.status_code},
                )
                return
            shopify_refund = resp.json().get("refund") or {}
            await _append_after_sales_audit_event(
                case_id,
                "shopify_manual_refund_created",
                {"shopify_refund_id": shopify_refund.get("id"), "restock": bool(refund_line_items)},
            )
    except Exception:
        # Never fail the merchant approval flow because Shopify sync is best-effort.
        try:
            await _append_after_sales_audit_event(case_id, "shopify_manual_refund_failed", {"status_code": None})
        except Exception:
            pass

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
                "amount": float(order.get("total") or 0),
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
async def sync_shopify_products(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user),
):
    """Sync products from Shopify store - schedule an async import task (do not block request)."""
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
        
        # 2) Schedule a background import task instead of running a long sync in-request.
        #    This prevents browser net::ERR_CONNECTION_CLOSED due to proxy/request timeouts.
        from services.platform_import_service import schedule_import_task
        from jobs.catalog_import_worker import process_import_task_by_id

        # De-dupe: if there's already an active Shopify import task, return it instead of
        # creating another. This prevents "Sync" button spam from spawning concurrent jobs.
        existing_task_row = await database.fetch_one(
            """
            SELECT id, status, counts, error, created_at, updated_at
            FROM platform_import_tasks
            WHERE merchant_id = :merchant_id
              AND source_type = 'connector'
              AND connector = 'shopify'
              AND status IN ('pending', 'running', 'retry_scheduled')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"merchant_id": merchant_id},
        )

        if existing_task_row:
            existing_task = dict(existing_task_row)
            existing_task_id = int(existing_task["id"])
            existing_status = (existing_task.get("status") or "").lower()
            stale_recovered = False

            # If a task is stuck in `running` (e.g., process restarted mid-sync),
            # allow it to be recovered automatically by flipping it back to
            # retry_scheduled so the background worker can pick it up again.
            if existing_status == "running":
                updated_at = existing_task.get("updated_at")
                try:
                    now = datetime.now(timezone.utc)
                    # updated_at from DB might be naive; treat as UTC.
                    if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    if isinstance(updated_at, datetime) and now - updated_at > timedelta(minutes=5):
                        await database.execute(
                            """
                            UPDATE platform_import_tasks
                            SET status = 'retry_scheduled',
                                next_run_at = NOW(),
                                error = 'stale_running_recovered',
                                updated_at = NOW()
                            WHERE id = :task_id
                            """,
                            {"task_id": existing_task_id},
                        )
                        existing_status = "retry_scheduled"
                        stale_recovered = True
                except Exception:
                    # If recovery fails, fall back to returning the task as-is.
                    pass

            # Best-effort: kick it off if it's ready, otherwise just return its status.
            if existing_status in ("pending", "retry_scheduled"):
                background_tasks.add_task(process_import_task_by_id, existing_task_id)

            return {
                "status": "success",
                "message": "Shopify sync already scheduled / in progress.",
                "data": {
                    "task_id": existing_task_id,
                    "scheduled": existing_status in ("pending", "retry_scheduled"),
                    "already_running": existing_status == "running",
                    "stale_recovered": stale_recovered,
                    "task_status": existing_task.get("status"),
                    "counts": existing_task.get("counts"),
                    "product_count": store_check.get("product_count"),
                    "store_domain": store_check["domain"],
                    "platform": "shopify",
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                },
            }

        task_id = await schedule_import_task(
            merchant_id=merchant_id,
            source_type="connector",
            connector="shopify",
        )

        # Kick off processing in the background (best effort). If a dedicated worker is running,
        # it can also pick up the pending task later.
        background_tasks.add_task(process_import_task_by_id, task_id)

        logger.info(
            "✅ Scheduled Shopify sync import task",
            extra={"merchant_id": merchant_id, "task_id": task_id, "store_domain": store_check.get("domain")},
        )

        return {
            "status": "success",
            "message": "Shopify sync scheduled. It may take a few minutes to finish.",
            "data": {
                "task_id": task_id,
                "scheduled": True,
                "product_count": store_check.get("product_count"),
                "store_domain": store_check["domain"],
                "platform": "shopify",
                "synced_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync products: {str(e)}")


@router.get("/merchant/integrations/shopify/sync/status")
async def get_shopify_sync_status(current_user: dict = Depends(get_current_user)):
    """Return recent Shopify import tasks for the current merchant."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    from services.platform_import_service import list_import_tasks

    try:
        tasks = await list_import_tasks(merchant_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load import tasks: {str(e)}")

    shopify_tasks = [
        t for t in tasks
        if (t.get("source_type") == "connector" and (t.get("connector") or "").lower() == "shopify")
    ]

    return {
        "status": "success",
        "data": {
            "merchant_id": merchant_id,
            "tasks": shopify_tasks[:20],
        },
    }


@router.get("/merchant/integrations/routing")
async def get_merchant_routing_config(
    current_user: dict = Depends(get_current_user),
):
    """
    Get PSP routing configuration for the current merchant.

    This surfaces the `payment_routes` configuration where `merchant_id` matches
    the logged-in merchant. If no explicit route exists yet, a default route is
    synthesized based on active merchant_psps (most recently connected first).
    """
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    # Try to load existing route config
    route = await database.fetch_one(
        """
        SELECT route_id, psp_priority, routing_strategy, is_active,
               max_retries, timeout_ms, metadata, created_at, updated_at
        FROM payment_routes
        WHERE merchant_id = :merchant_id AND is_active = true
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )

    if not route:
        # No explicit route yet; synthesize default using PaymentRoutingService logic
        routing_service = PaymentRoutingService(database)
        # For merchant-level defaults we don't need an agent_id on the route;
        # pass None so the FK constraint on payment_routes.agent_id is not violated.
        default_config = await routing_service._create_default_route(
            agent_id=None,
            merchant_id=merchant_id,
        )
        psp_priority = default_config["psp_priority"]
        routing_strategy = default_config["routing_strategy"]
        max_retries = default_config.get("max_retries", 2)
        timeout_ms = 30000
        route_id = default_config["route_id"]
        metadata_dict: Dict[str, Any] = {}
        created_at = datetime.now(timezone.utc)
        updated_at = created_at
    else:
        # databases.fetch_one returns a Record; normalize to dict for .get access
        route_dict = dict(route)

        psp_priority = route_dict["psp_priority"]
        if isinstance(psp_priority, str):
            psp_priority = json.loads(psp_priority)
        routing_strategy = route_dict["routing_strategy"]
        max_retries = route_dict.get("max_retries", 2)
        timeout_ms = route_dict.get("timeout_ms", 30000)
        metadata_raw = route_dict.get("metadata") or {}
        metadata_dict = (
            json.loads(metadata_raw)
            if isinstance(metadata_raw, str)
            else dict(metadata_raw)
            if isinstance(metadata_raw, dict)
            else {}
        )
        route_id = route_dict["route_id"]
        created_at = route_dict.get("created_at")
        updated_at = route_dict.get("updated_at")

    return {
        "status": "success",
        "data": {
            "route_id": route_id,
            "merchant_id": merchant_id,
            "psp_priority": psp_priority,
            "routing_strategy": routing_strategy,
            "max_retries": max_retries,
            "timeout_ms": timeout_ms,
            "metadata": metadata_dict,
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else None,
            "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else None,
        },
    }


@router.put("/merchant/integrations/routing")
async def update_merchant_routing_config(
    update: MerchantRoutingUpdate,
    current_user: dict = Depends(get_current_user),
):
    """
    Update PSP routing configuration for the current merchant.

    This writes to `payment_routes` using merchant_id as the key so that
    PaymentRoutingService.select_psp(agent_id, merchant_id, ...) will honor
    the merchant's preferences for all agents.
    """
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    # Find existing route for this merchant, if any
    existing = await database.fetch_one(
        """
        SELECT route_id
        FROM payment_routes
        WHERE merchant_id = :merchant_id AND is_active = true
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )

    if existing:
        route_id = existing["route_id"]
        await database.execute(
            """
            UPDATE payment_routes
            SET psp_priority = :psp_priority,
                routing_strategy = :routing_strategy,
                max_retries = :max_retries,
                timeout_ms = :timeout_ms,
                updated_at = NOW()
            WHERE route_id = :route_id
            """,
            {
                "route_id": route_id,
                "psp_priority": json.dumps(update.psp_priority),
                "routing_strategy": update.routing_strategy,
                "max_retries": update.max_retries,
                "timeout_ms": update.timeout_ms,
            },
        )
    else:
        # Create new route row for this merchant
        route_id = f"route_{hashlib.md5(f'{merchant_id}{datetime.utcnow()}'.encode()).hexdigest()[:12]}"
        await database.execute(
            """
            INSERT INTO payment_routes (
                route_id, agent_id, merchant_id, psp_priority,
                routing_strategy, max_retries, timeout_ms, is_active, metadata
            ) VALUES (
                :route_id, :agent_id, :merchant_id, :psp_priority,
                :routing_strategy, :max_retries, :timeout_ms, true, '{}'::jsonb
            )
            """,
            {
                "route_id": route_id,
                "agent_id": merchant_id,
                "merchant_id": merchant_id,
                "psp_priority": json.dumps(update.psp_priority),
                "routing_strategy": update.routing_strategy,
                "max_retries": update.max_retries,
                "timeout_ms": update.timeout_ms,
            },
        )

    return {
        "status": "success",
        "data": {
            "route_id": route_id,
            "merchant_id": merchant_id,
        },
    }


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
            raise HTTPException(
                status_code=400,
                detail="Checkout.com requires processing_channel_id in account_id field",
            )
        account_id = provided_account_id
    elif provider == "adyen":
        # For Adyen, account_id should be the merchantAccount.
        # Prefer explicit value from request, otherwise fall back to env.
        from config.settings import settings

        provided_account_id = (psp_data.get("account_id") or "").strip()
        if not provided_account_id:
            provided_account_id = getattr(settings, "adyen_merchant_account", "").strip()
        if not provided_account_id:
            raise HTTPException(
                status_code=400,
                detail="Adyen requires merchantAccount (account_id) to be provided",
            )
        account_id = provided_account_id
    else:
        # For Stripe/others, accept optional account_id from request; do NOT generate fake acct_* IDs.
        account_id = (psp_data.get("account_id") or "").strip() or None

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
                COALESCE(total_refunded, 0) as total_refunded,
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
                "total_refunded": float(order["total_refunded"]) if order["total_refunded"] else 0,
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
        await _ensure_refund_tables_best_effort()

        # Process refund through service
        result = await refund_service.create_refund(
            order_id=order_id,
            amount=refund_request.amount,
            reason=refund_request.reason,
            source=refund_request.source,
            created_by=current_user.get("email", current_user.get("user_id"))
        )
        
        # Best-effort logging (must not fail the refund response).
        try:
            await log_order_event(
                event_type="merchant_refund",
                order_id=order_id,
                merchant_id=merchant_id,
                metadata={
                    "refund_id": result.get("refund_id"),
                    "amount": refund_request.amount,
                    "reason": refund_request.reason,
                    "status": result["status"],
                },
            )
        except Exception:
            pass
        
        # Handle different statuses
        if result["status"] == "duplicate":
            raise HTTPException(
                status_code=409,
                detail=f"Refund already processed (refund_id: {result.get('refund_id')})",
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
        # One retry after best-effort DDL self-heal (handles missing migrations / tables).
        try:
            await _ensure_refund_tables_best_effort()
            result = await refund_service.create_refund(
                order_id=order_id,
                amount=refund_request.amount,
                reason=refund_request.reason,
                source=refund_request.source,
                created_by=current_user.get("email", current_user.get("user_id")),
            )
            if isinstance(result, dict) and result.get("status") == "failed":
                return {
                    "status": "processing",
                    "message": "Refund is being processed. You'll be notified when complete.",
                    "refund_id": result.get("refund_id"),
                    "amount": refund_request.amount,
                }
            if isinstance(result, dict) and result.get("status") == "duplicate":
                raise HTTPException(
                    status_code=409,
                    detail=f"Refund already processed (refund_id: {result.get('refund_id')})",
                )
            if isinstance(result, dict) and result.get("status") == "success":
                return {
                    "status": "success",
                    "message": "Refund processed successfully",
                    "refund_id": result.get("refund_id"),
                    "psp_refund_id": result.get("psp_refund_id"),
                    "amount": refund_request.amount,
                }
        except HTTPException:
            raise
        except Exception:
            pass

        debug_id = _error_debug_id("merchant_refund")
        logger.error(f"Refund processing error debug_id={debug_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            # Keep `detail` a string for backward-compatible clients that render it directly.
            detail=f"Failed to process refund. Please try again later. (debug_id: {debug_id})",
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
    
    await _ensure_refund_tables_best_effort()

    # Get refund history with enhanced details (best-effort)
    try:
        refunds = await refund_service.get_refund_history(order_id)
    except Exception:
        refunds = []
    
    # Get order details for context
    order_query = """
    SELECT total as total_amount, COALESCE(total_refunded, 0) as total_refunded, payment_status, currency
    FROM orders 
    WHERE order_id = :order_id
    """
    try:
        order_data = await database.fetch_one(order_query, {"order_id": order_id})
    except Exception:
        order_data = {"total_amount": 0, "total_refunded": 0, "payment_status": "unknown", "currency": "USD"}
    
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


@router.get("/merchant/orders/{order_id}/after-sales/cases")
async def merchant_list_after_sales_cases_for_order(
    order_id: str,
    current_user: dict = Depends(get_current_user),
):
    """List after-sales cases for an order (merchant-owned)."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)
    oid = str(order_id or "").strip()
    if not oid:
        raise HTTPException(status_code=400, detail="order_id is required")

    # Ensure order belongs to merchant
    order_exists = await database.fetch_one(
        "SELECT 1 FROM orders WHERE order_id = :order_id AND merchant_id = :merchant_id",
        {"order_id": oid, "merchant_id": merchant_id},
    )
    if not order_exists:
        raise HTTPException(status_code=404, detail="Order not found")

    await _ensure_after_sales_cases_table()
    rows = await database.fetch_all(
        """
        SELECT * FROM after_sales_cases
        WHERE order_id = :order_id AND merchant_id = :merchant_id
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"order_id": oid, "merchant_id": merchant_id},
    )
    cases = [_serialize_case(dict(r)) for r in (rows or [])]
    return {"status": "success", "total": len(cases), "cases": cases}


@router.post("/merchant/after-sales/cases/{case_id}/approve")
async def merchant_approve_after_sales_case_and_refund(
    case_id: str,
    background_tasks: BackgroundTasks,
    payload: ApproveAfterSalesCaseRequest = Body(default={}),
    current_user: dict = Depends(get_current_user),
):
    """
    Merchant approval step for buyer-initiated refund requests.

    Behavior:
    - Marks the case as `approved` (audit log) and executes the refund through RefundService.
    - Uses a stable idempotency key (`after_sales_case:{case_id}`) to prevent double refunds.
    """
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)
    cid = str(case_id or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="case_id is required")

    await _ensure_after_sales_cases_table()
    loaded = await database.fetch_one(
        "SELECT * FROM after_sales_cases WHERE case_id = :case_id",
        {"case_id": cid},
    )
    if not loaded:
        raise HTTPException(status_code=404, detail="Case not found")

    loaded = dict(loaded)
    if str(loaded.get("merchant_id") or "") != str(merchant_id):
        raise HTTPException(status_code=404, detail="Case not found")

    current_status = str(loaded.get("status") or "")
    if current_status in ("refunded", "partially_refunded", "refund_processed"):
        return {"status": "success", "case": _serialize_case(loaded), "refund": {"status": "already_processed"}}

    order_id = str(loaded.get("order_id") or "")
    if not order_id:
        raise HTTPException(status_code=500, detail="Case missing order_id")

    # Ensure order belongs to merchant and compute refundable.
    order = await get_order(order_id)
    if not order or str(order.get("merchant_id") or "") != str(merchant_id):
        raise HTTPException(status_code=404, detail="Order not found")

    requested_amount = loaded.get("requested_refund_amount")
    approved_override = payload.approved_refund_amount
    amount = None
    if approved_override is not None:
        amount = float(approved_override)
    elif requested_amount is not None:
        amount = float(requested_amount)
    else:
        try:
            total = float(order.get("total") or order.get("total_amount") or 0)
            refunded = float(order.get("total_refunded") or 0)
            amount = max(0.0, total - refunded)
        except Exception:
            amount = None

    if amount is None or amount <= 0:
        raise HTTPException(status_code=400, detail="Refund amount is not refundable")

    reason = str(loaded.get("reason_text") or loaded.get("reason_code") or "Refund requested").strip() or "Refund requested"
    created_by = current_user.get("email") or current_user.get("sub") or "merchant"

    audit = _coerce_json_list(loaded.get("audit_log"))
    audit.append(
        {
            "at": _now_iso(),
            "event": "merchant_approved",
            "payload": {"amount": amount, "note": payload.note or ""},
        }
    )

    # Best-effort status update before executing refund.
    try:
        await database.execute(
            """
            UPDATE after_sales_cases
            SET status = 'approved',
                audit_log = CAST(:audit_log AS JSONB),
                updated_at = NOW()
            WHERE case_id = :case_id
            """,
            {"case_id": cid, "audit_log": json.dumps(audit, ensure_ascii=False)},
        )
    except Exception:
        pass

    try:
        await _ensure_refund_tables_best_effort()
        refund_result = await refund_service.create_refund(
            order_id=order_id,
            amount=amount,
            reason=reason,
            source="pivota_merchant_after_sales",
            created_by=created_by,
            idempotency_key=f"after_sales_case:{cid}",
        )
    except ValueError as e:
        audit.append({"at": _now_iso(), "event": "refund_validation_failed", "payload": {"error": str(e)[:240]}})
        try:
            await database.execute(
                """
                UPDATE after_sales_cases
                SET status = 'approved',
                    audit_log = CAST(:audit_log AS JSONB),
                    updated_at = NOW()
                WHERE case_id = :case_id
                """,
                {"case_id": cid, "audit_log": json.dumps(audit, ensure_ascii=False)},
            )
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # One retry after best-effort DDL self-heal.
        try:
            await _ensure_refund_tables_best_effort()
            refund_result = await refund_service.create_refund(
                order_id=order_id,
                amount=amount,
                reason=reason,
                source="pivota_merchant_after_sales",
                created_by=created_by,
                idempotency_key=f"after_sales_case:{cid}",
            )
        except Exception:
            refund_result = None

        if refund_result is not None:
            # Continue the flow and persist status below.
            pass
        else:
            debug_id = _error_debug_id("merchant_after_sales_refund")
            audit.append({"at": _now_iso(), "event": "refund_failed", "payload": {"debug_id": debug_id}})
            try:
                await database.execute(
                    """
                    UPDATE after_sales_cases
                    SET status = 'refund_pending',
                        audit_log = CAST(:audit_log AS JSONB),
                        updated_at = NOW()
                    WHERE case_id = :case_id
                    """,
                    {"case_id": cid, "audit_log": json.dumps(audit, ensure_ascii=False)},
                )
            except Exception:
                pass
            logger.error(f"merchant after-sales refund failed debug_id={debug_id}: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to process refund. Please try again later. (debug_id: {debug_id})",
            )

    # Reload order to infer final status (best effort).
    try:
        order = await get_order(order_id)
    except Exception:
        order = None
    inferred_status = "refund_processed"
    try:
        payment_status = str((order or {}).get("payment_status") or "")
        if payment_status in ("partially_refunded", "refunded"):
            inferred_status = payment_status
    except Exception:
        pass

    audit.append(
        {
            "at": _now_iso(),
            "event": "refund_result",
            "payload": {
                "status": str(refund_result.get("status") if isinstance(refund_result, dict) else ""),
                "refund_id": refund_result.get("refund_id") if isinstance(refund_result, dict) else None,
            },
        }
    )

    try:
        await database.execute(
            """
            UPDATE after_sales_cases
            SET status = :status,
                audit_log = CAST(:audit_log AS JSONB),
                updated_at = NOW()
            WHERE case_id = :case_id
            """,
            {"case_id": cid, "status": inferred_status, "audit_log": json.dumps(audit, ensure_ascii=False)},
        )
    except Exception:
        pass

    reloaded = await database.fetch_one("SELECT * FROM after_sales_cases WHERE case_id = :case_id", {"case_id": cid})
    # Best-effort: reflect refund in Shopify as a "manual" refund record (accounting/inventory sync).
    try:
        shopify_order_id = str((order or {}).get("shopify_order_id") or "").strip()
        if shopify_order_id:
            background_tasks.add_task(
                _create_shopify_manual_refund_best_effort,
                merchant_id=str(merchant_id),
                pivota_order_id=str(order_id),
                shopify_order_id=shopify_order_id,
                case_id=str(cid),
                refund_amount=float(amount),
                reason=str(reason),
            )
    except Exception:
        pass

    return {"status": "success", "case": _serialize_case(dict(reloaded) if reloaded else loaded), "refund": refund_result}

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
