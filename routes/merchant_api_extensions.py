"""Extended Merchant API Routes for Dashboard Features"""
from fastapi import APIRouter, Depends, HTTPException, Body, BackgroundTasks, Query, Response
from typing import Dict, Any, Optional, List
from utils.auth import get_current_user
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from db.database import database
from db.orders import get_order, mark_order_shipped
from db.products import log_order_event
from services.refund_service import refund_service
from services.merchant_webhook_service import emit_merchant_webhook_event
from pydantic import BaseModel
from utils.logger import logger
from config.feature_flags import is_feature_enabled
from config.settings import settings
import httpx
import os
import random
import string
import time
import json
import hashlib
import csv
import io

from services.payment_routing_service import PaymentRoutingService
from services.merchant_psp_config_service import (
    SUPPORTED_CANONICAL_PSPS,
    persist_canonical_merchant_psp,
)
from services.commerce_index_source_service import register_commerce_index_source
from services.merchant_store_service import get_primary_store
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from routes.after_sales_cases import _ensure_after_sales_cases_table, _serialize_case
from readiness.remediation import (
    ActionNotExecutableError,
    ActionNotFoundError,
    JobNotFoundError,
    PlanSupersededError,
    delete_source_data_decision_state,
    get_execution_job,
    get_product_blocker_detail,
    get_source_data_triage,
    preview_remediation_action,
    run_remediation_action,
    upsert_source_data_decision_state,
)
from readiness.summary import (
    build_readiness_optimization,
    build_readiness_summary,
    schedule_readiness_optimization_warmup,
)

router = APIRouter()

# Request models
class RefundRequest(BaseModel):
    """Refund request with required fields"""
    amount: float  # Required
    reason: str  # Required
    source: str = "pivota_merchant"


class ReadinessRefreshRequest(BaseModel):
    scope: str = "merchant"
    reason: str = "manual"
    queue_mode: str = "full"
    page: int = 1
    page_size: int = 50
    search: Optional[str] = None
    issue_bucket: Optional[str] = None
    push_status: str = "all"
    blocked_only: bool = False
    low_quality_only: bool = False
    sort_by: str = "default"
    segment: str = "all"


class ReadinessActionPreviewRequest(BaseModel):
    plan_id: str
    action_id: Optional[str] = None
    action_type: Optional[str] = None
    targets: List[Dict[str, Any]] = []
    dry_run: bool = True


class ReadinessActionRunRequest(BaseModel):
    plan_id: str
    action_id: Optional[str] = None
    action_type: Optional[str] = None
    targets: List[Dict[str, Any]] = []
    idempotency_key: Optional[str] = None
    execution_mode: str = "sync"


class SourceDataDecisionRequest(BaseModel):
    decision_state: str

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")


def _render_source_data_triage_csv(payload: Dict[str, Any]) -> str:
    buffer = io.StringIO()
    fieldnames = [
        "plan_id",
        "snapshot_id",
        "reason_code",
        "reason_label",
        "scope",
        "platform",
        "platform_product_id",
        "platform_admin_url",
        "product_id",
        "product_title",
        "variant_id",
        "variant_title",
        "sku",
        "price_value",
        "price_currency",
        "inventory_quantity",
        "blocked_variant_count",
        "excluded_variant_count",
        "readiness_blocker_codes",
        "readiness_warning_codes",
        "agent_push_status",
        "agent_push_reason_codes",
        "recommended_action_type",
        "fix_surface",
        "decision_state",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()

    plan_id = str(payload.get("plan_id") or "").strip()
    snapshot_id = str(payload.get("snapshot_id") or "").strip()

    for row in payload.get("rows") or []:
        writer.writerow(
            {
                "plan_id": plan_id,
                "snapshot_id": snapshot_id,
                "reason_code": row.get("reason_code"),
                "reason_label": row.get("reason_label"),
                "scope": row.get("scope"),
                "platform": row.get("platform"),
                "platform_product_id": row.get("platform_product_id"),
                "platform_admin_url": row.get("platform_admin_url"),
                "product_id": row.get("product_id"),
                "product_title": row.get("product_title"),
                "variant_id": row.get("variant_id"),
                "variant_title": row.get("variant_title"),
                "sku": row.get("sku"),
                "price_value": row.get("price_value"),
                "price_currency": row.get("price_currency"),
                "inventory_quantity": row.get("inventory_quantity"),
                "blocked_variant_count": row.get("blocked_variant_count"),
                "excluded_variant_count": row.get("excluded_variant_count"),
                "readiness_blocker_codes": "|".join(
                    str(code) for code in (row.get("readiness_blocker_codes") or [])
                ),
                "readiness_warning_codes": "|".join(
                    str(code) for code in (row.get("readiness_warning_codes") or [])
                ),
                "agent_push_status": row.get("agent_push_status"),
                "agent_push_reason_codes": "|".join(
                    str(code) for code in (row.get("agent_push_reason_codes") or [])
                ),
                "recommended_action_type": row.get("recommended_action_type"),
                "fix_surface": row.get("fix_surface"),
                "decision_state": row.get("decision_state"),
            }
        )

    return buffer.getvalue()


async def _emit_merchant_refund_webhook_best_effort(
    *,
    merchant_id: str,
    order_id: str,
    amount: float,
    refund_id: Optional[str],
    order: Optional[Dict[str, Any]] = None,
) -> None:
    resolved_order = order or await get_order(order_id) or {}
    currency = str(resolved_order.get("currency") or "USD")
    payment_status = str(
        resolved_order.get("payment_status")
        or resolved_order.get("status")
        or "refund_processed"
    )

    try:
        await emit_merchant_webhook_event(
            str(merchant_id),
            event_type="refund.processed",
            payload={
                "order_id": str(order_id),
                "merchant_id": str(merchant_id),
                "refund_id": str(refund_id or ""),
                "amount": float(amount),
                "currency": currency,
                "is_partial": payment_status == "partially_refunded",
                "status": payment_status,
            },
        )
    except Exception as exc:
        logger.warning(
            "Failed to emit merchant refund.processed webhook for %s: %s",
            merchant_id,
            exc,
        )


async def _ensure_refund_tables_best_effort() -> None:
    """
    Ensure refund tables/columns exist.
    Best-effort: do not raise on failures (keeps merchant UI stable during partial deploys).
    """
    try:
        await database.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_refunded DECIMAL(10,2) DEFAULT 0")
    except Exception:
        return

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
                processed_at TIMESTAMP
            )
            """
        )
        await database.execute("CREATE INDEX IF NOT EXISTS idx_order_refunds ON refund_records (order_id, created_at DESC)")
        await database.execute("CREATE INDEX IF NOT EXISTS idx_merchant_refunds ON refund_records (merchant_id, created_at DESC)")
    except Exception:
        return


def _normalize_shopify_domain(domain: str) -> str:
    d = (domain or "").strip()
    if not d:
        return d
    d = d.replace("https://", "").replace("http://", "").strip().rstrip("/")
    if d.endswith(".myshopify.com"):
        return d
    return f"{d}.myshopify.com"


async def _get_shopify_credentials_for_merchant(merchant_id: str) -> Optional[dict]:
    try:
        store = await get_primary_store(merchant_id)
    except Exception:
        store = None
    if not store or str(store.get("platform", "")).lower() != "shopify":
        return None
    shop_domain = _normalize_shopify_domain(store.get("domain") or "")
    access_token, _ = await resolve_shopify_admin_access_token(
        shop_domain=shop_domain,
        api_key_raw=store.get("api_key_raw") or store.get("api_key"),
        store_id=str(store.get("store_id") or "").strip() or None,
    )
    if not shop_domain or not access_token:
        return None
    return {"shop_domain": shop_domain, "access_token": access_token}


async def _shopify_get_order(shop_domain: str, access_token: str, shopify_order_id: str) -> Optional[dict]:
    url = f"https://{shop_domain}/admin/api/2025-10/orders/{shopify_order_id}.json"
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        try:
            return resp.json().get("order")
        except Exception:
            return None


async def _shopify_get_transactions(shop_domain: str, access_token: str, shopify_order_id: str) -> list[dict]:
    url = f"https://{shop_domain}/admin/api/2025-10/orders/{shopify_order_id}/transactions.json"
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return []
        try:
            return resp.json().get("transactions") or []
        except Exception:
            return []


async def _shopify_create_transaction(
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    *,
    kind: str,
    amount: str,
    gateway: str = "manual",
    status: str = "success",
    parent_id: Optional[int] = None,
) -> Optional[dict]:
    url = f"https://{shop_domain}/admin/api/2025-10/orders/{shopify_order_id}/transactions.json"
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
    tx: dict[str, Any] = {"kind": kind, "status": status, "amount": amount, "gateway": gateway}
    if parent_id is not None:
        tx["parent_id"] = parent_id
    payload = {"transaction": tx}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            return None
        try:
            return resp.json().get("transaction")
        except Exception:
            return None


async def _shopify_ensure_manual_sale_transaction_best_effort(
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
) -> Optional[int]:
    try:
        order = await _shopify_get_order(shop_domain, access_token, shopify_order_id)
        if not order:
            return None

        txs = await _shopify_get_transactions(shop_domain, access_token, shopify_order_id)
        for tx in txs:
            try:
                if str(tx.get("status")) == "success" and str(tx.get("kind")) in ("sale", "capture"):
                    return int(tx["id"])
            except Exception:
                continue

        total_price = str(order.get("total_price") or "0")
        created = await _shopify_create_transaction(
            shop_domain,
            access_token,
            shopify_order_id,
            kind="sale",
            amount=total_price,
            gateway="manual",
            status="success",
        )
        if created and created.get("id") is not None:
            return int(created["id"])
    except Exception:
        return None
    return None


async def _shopify_create_manual_refund_best_effort(
    *,
    merchant_id: str,
    shopify_order_id: str,
    refund_id: str,
    amount: float,
) -> None:
    creds = await _get_shopify_credentials_for_merchant(merchant_id)
    if not creds:
        return
    shop_domain = creds["shop_domain"]
    access_token = creds["access_token"]

    try:
        await _ensure_refund_tables_best_effort()
        parent_tx_id = await _shopify_ensure_manual_sale_transaction_best_effort(shop_domain, access_token, shopify_order_id)
        refund_amount = str(Decimal(str(amount)).quantize(Decimal("0.01")))

        url = f"https://{shop_domain}/admin/api/2025-10/orders/{shopify_order_id}/refunds.json"
        headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
        refund_tx: dict[str, Any] = {
            "kind": "refund",
            "gateway": "manual",
            "status": "success",
            "amount": refund_amount,
        }
        if parent_tx_id is not None:
            refund_tx["parent_id"] = parent_tx_id
        payload = {
            "refund": {
                "notify": False,
                "note": f"Pivota refund {refund_id}",
                "refund_line_items": [],
                "transactions": [refund_tx],
            }
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code in (200, 201):
                data = resp.json() if resp.content else {}
                shopify_refund_id = None
                try:
                    shopify_refund_id = data.get("refund", {}).get("id")
                except Exception:
                    shopify_refund_id = None

                await database.execute(
                    """
                    UPDATE refund_records
                    SET
                        platform_type = 'shopify',
                        platform_refund_id = COALESCE(:platform_refund_id, platform_refund_id),
                        platform_sync_status = 'synced'
                    WHERE refund_id = :refund_id
                    """,
                    {
                        "refund_id": refund_id,
                        "platform_refund_id": str(shopify_refund_id) if shopify_refund_id is not None else None,
                    },
                )
                return

        # Fallback: create a manual "refund" transaction (shows up in Payments/transactions).
        created_tx = await _shopify_create_transaction(
            shop_domain,
            access_token,
            shopify_order_id,
            kind="refund",
            amount=refund_amount,
            gateway="manual",
            status="success",
            parent_id=parent_tx_id,
        )

        if created_tx and created_tx.get("id") is not None:
            await database.execute(
                """
                UPDATE refund_records
                SET
                    platform_type = 'shopify',
                    platform_refund_id = COALESCE(:platform_refund_id, platform_refund_id),
                    platform_sync_status = 'synced'
                WHERE refund_id = :refund_id
                """,
                {"refund_id": refund_id, "platform_refund_id": str(created_tx["id"])},
            )
        else:
            await database.execute(
                """
                UPDATE refund_records
                SET platform_type = 'shopify', platform_sync_status = 'failed'
                WHERE refund_id = :refund_id
                """,
                {"refund_id": refund_id},
            )
    except Exception:
        try:
            await database.execute(
                """
                UPDATE refund_records
                SET platform_type = 'shopify', platform_sync_status = 'failed'
                WHERE refund_id = :refund_id
                """,
                {"refund_id": refund_id},
            )
        except Exception:
            pass

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


async def _get_active_merchant_psps(merchant_id: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT psp_id, provider, name, status, connected_at
        FROM merchant_psps
        WHERE merchant_id = :merchant_id
          AND status = 'active'
        ORDER BY connected_at DESC NULLS LAST, psp_id ASC
        """,
        {"merchant_id": merchant_id},
    )
    return [dict(row) for row in rows or []]


def _normalize_merchant_route_priority(
    requested_priority: Any,
    active_psps: List[Dict[str, Any]],
    *,
    strict: bool,
    append_unlisted_active: bool = True,
) -> List[Dict[str, Any]]:
    active_order = []
    active_providers: Dict[str, Dict[str, Any]] = {}
    for row in active_psps:
        provider = str(row.get("provider") or "").strip().lower()
        if provider and provider not in active_providers:
            active_providers[provider] = row
            active_order.append(provider)

    if not active_order:
        raise HTTPException(status_code=400, detail="No active PSPs available for routing")

    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    requested_list = requested_priority if isinstance(requested_priority, list) else []
    for entry in requested_list:
        provider = str((entry or {}).get("psp") or "").strip().lower()
        if not provider:
            continue
        if provider not in active_providers:
            if strict:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid or inactive PSP in routing config: {provider}",
                )
            continue
        if provider in seen:
            continue
        seen.add(provider)
        normalized.append({"psp": provider, "priority": len(normalized) + 1})

    if append_unlisted_active:
        for provider in active_order:
            if provider in seen:
                continue
            seen.add(provider)
            normalized.append({"psp": provider, "priority": len(normalized) + 1})

    return normalized


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
        access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=store.get("api_key_raw") or store.get("api_key"),
            store_id=str(store.get("store_id") or "").strip() or None,
        )
        access_token = str(access_token or "").strip()
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
                    f"https://{shop_domain}/admin/api/2025-10/orders/{shopify_order_id}.json"
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
            refunds_url = f"https://{shop_domain}/admin/api/2025-10/orders/{shopify_order_id}/refunds.json"
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

@router.get("/merchant/dashboard/readiness")
async def get_dashboard_readiness(current_user: dict = Depends(get_current_user)):
    """Get readiness summary independently from heavier dashboard analytics."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    try:
        summary = await build_readiness_summary(merchant_id)
        schedule_readiness_optimization_warmup(merchant_id)
        return {
            "status": "success",
            "data": summary.model_dump(),
        }
    except Exception as e:
        logger.error(f"❌ Dashboard readiness error for merchant {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Dashboard readiness failed: {str(e)}")


@router.get("/merchant/readiness/optimization")
async def get_readiness_optimization(
    queue_mode: str = Query("full"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    search: Optional[str] = Query(None),
    issue_bucket: Optional[str] = Query(None),
    push_status: str = Query("all"),
    blocked_only: bool = Query(False),
    low_quality_only: bool = Query(False),
    sort_by: str = Query("default"),
    segment: str = Query("all"),
    current_user: dict = Depends(get_current_user),
):
    """Get merchant-safe readiness optimization payload for the product optimization workspace."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    try:
        started_at = time.perf_counter()
        payload = await build_readiness_optimization(
            merchant_id,
            queue_mode=queue_mode,
            page=page,
            page_size=page_size,
            search=search,
            issue_bucket=issue_bucket,
            push_status=push_status,
            blocked_only=blocked_only,
            low_quality_only=low_quality_only,
            sort_by=sort_by,
            segment=segment,
        )
        serialization_started = time.perf_counter()
        payload_data = payload.model_dump()
        serialization_ms = round((time.perf_counter() - serialization_started) * 1000.0, 2)
        logger.info(
            "merchant_readiness_optimization_route merchant=%s queue_mode=%s page=%s page_size=%s build_and_page_ms=%.2f serialization_ms=%.2f returned_items=%s total_items=%s",
            merchant_id,
            queue_mode,
            page,
            page_size,
            round((serialization_started - started_at) * 1000.0, 2),
            serialization_ms,
            len(payload_data.get("product_queue") or []),
            ((payload_data.get("product_queue_page") or {}).get("total_items") if isinstance(payload_data, dict) else None),
        )
        return {
            "status": "success",
            "data": payload_data,
        }
    except Exception as e:
        logger.error(f"❌ Readiness optimization error for merchant {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Readiness optimization failed: {str(e)}")


@router.post("/merchant/readiness/actions/refresh")
async def refresh_readiness_optimization(
    body: ReadinessRefreshRequest = Body(default=ReadinessRefreshRequest()),
    current_user: dict = Depends(get_current_user),
):
    """Refresh the merchant optimization plan and return the latest workspace payload."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    try:
        payload = await build_readiness_optimization(
            merchant_id,
            force_refresh=True,
            queue_mode=body.queue_mode,
            page=body.page,
            page_size=body.page_size,
            search=body.search,
            issue_bucket=body.issue_bucket,
            push_status=body.push_status,
            blocked_only=body.blocked_only,
            low_quality_only=body.low_quality_only,
            sort_by=body.sort_by,
            segment=body.segment,
        )
        return {
            "status": "success",
            "data": payload.model_dump(),
            "meta": {
                "scope": body.scope,
                "reason": body.reason,
                "queue_mode": body.queue_mode,
                "page": body.page,
                "page_size": body.page_size,
                "refresh_state": payload.plan.refresh_state,
                "plan_id": payload.plan.plan_id,
                "snapshot_id": payload.plan.snapshot_id,
            },
        }
    except Exception as e:
        logger.error(f"❌ Readiness optimization refresh error for merchant {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Readiness optimization refresh failed: {str(e)}")


@router.post("/merchant/readiness/actions/preview")
async def preview_readiness_action(
    body: ReadinessActionPreviewRequest = Body(...),
    current_user: dict = Depends(get_current_user),
):
    """Preview a remediation action against the latest optimization plan."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    try:
        preview = await preview_remediation_action(
            merchant_id,
            plan_id=body.plan_id,
            action_id=body.action_id,
            action_type=body.action_type,
            targets=body.targets,
        )
        return {
            "status": "success",
            "data": preview,
            "meta": {
                "dry_run": body.dry_run,
                "plan_id": body.plan_id,
            },
        }
    except PlanSupersededError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "OPTIMIZATION_PLAN_SUPERSEDED",
                "current_plan_id": exc.current_plan_id,
                "current_snapshot_id": exc.current_snapshot_id,
            },
        )
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "OPTIMIZATION_ACTION_NOT_FOUND", "message": str(exc)})
    except Exception as e:
        logger.error(f"❌ Readiness action preview error for merchant {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Readiness action preview failed: {str(e)}")


@router.post("/merchant/readiness/actions/run")
async def run_readiness_action(
    body: ReadinessActionRunRequest = Body(...),
    current_user: dict = Depends(get_current_user),
):
    """Execute a remediation action against the latest optimization plan."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    if body.execution_mode != "sync":
        raise HTTPException(
            status_code=400,
            detail={"code": "UNSUPPORTED_EXECUTION_MODE", "message": "Only sync execution is currently supported."},
        )

    try:
        result = await run_remediation_action(
            merchant_id,
            plan_id=body.plan_id,
            action_id=body.action_id,
            action_type=body.action_type,
            targets=body.targets,
            idempotency_key=body.idempotency_key,
        )
        return {
            "status": "success",
            "data": result,
        }
    except PlanSupersededError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "OPTIMIZATION_PLAN_SUPERSEDED",
                "current_plan_id": exc.current_plan_id,
                "current_snapshot_id": exc.current_snapshot_id,
            },
        )
    except ActionNotExecutableError as exc:
        raise HTTPException(status_code=409, detail={"code": "OPTIMIZATION_ACTION_NOT_EXECUTABLE", "message": str(exc)})
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "OPTIMIZATION_ACTION_NOT_FOUND", "message": str(exc)})
    except Exception as e:
        logger.error(f"❌ Readiness action run error for merchant {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Readiness action run failed: {str(e)}")


@router.get("/merchant/readiness/optimization/products/{platform}/{platform_product_id}/blockers")
async def get_readiness_product_blockers(
    platform: str,
    platform_product_id: str,
    plan_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Return merchant-safe blocker and exclusion details for one selected product."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    try:
        detail = await get_product_blocker_detail(
            merchant_id,
            plan_id=plan_id,
            platform=platform,
            platform_product_id=platform_product_id,
        )
        return {
            "status": "success",
            "data": detail,
        }
    except PlanSupersededError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "OPTIMIZATION_PLAN_SUPERSEDED",
                "current_plan_id": exc.current_plan_id,
                "current_snapshot_id": exc.current_snapshot_id,
            },
        )
    except ActionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "OPTIMIZATION_PRODUCT_BLOCKERS_NOT_FOUND",
                "message": str(exc),
            },
        )
    except Exception as e:
        logger.error(
            "❌ Readiness blocker detail error for merchant %s product %s/%s: %s",
            merchant_id,
            platform,
            platform_product_id,
            e,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Readiness blocker detail failed: {str(e)}",
        )


@router.get("/merchant/readiness/optimization/source-data-triage/export.csv")
async def export_readiness_source_data_triage_csv(
    plan_id: str,
    reason_code: Optional[str] = None,
    limit: int = 5000,
    current_user: dict = Depends(get_current_user),
):
    """Export the source-data triage queue as CSV for the selected plan/lane."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)
    normalized_limit = max(1, min(int(limit or 5000), 5000))

    try:
        triage = await get_source_data_triage(
            merchant_id,
            plan_id=plan_id,
            reason_code=reason_code,
            limit=normalized_limit,
        )
        lane_code = str(reason_code or "all").strip() or "all"
        filename = f"catalog-health-source-data-triage-{lane_code}-{plan_id}.csv"
        return Response(
            content=_render_source_data_triage_csv(triage),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except PlanSupersededError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "OPTIMIZATION_PLAN_SUPERSEDED",
                "current_plan_id": exc.current_plan_id,
                "current_snapshot_id": exc.current_snapshot_id,
            },
        )
    except Exception as e:
        logger.error(
            "❌ Readiness source-data triage export failed for merchant %s plan %s reason %s: %s",
            merchant_id,
            plan_id,
            reason_code,
            e,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Readiness source-data triage export failed: {str(e)}",
        )


@router.get("/merchant/readiness/optimization/source-data-triage")
async def get_readiness_source_data_triage(
    plan_id: str,
    reason_code: Optional[str] = None,
    limit: int = 500,
    current_user: dict = Depends(get_current_user),
):
    """Return the source-data triage queue for the selected optimization plan."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)
    normalized_limit = max(1, min(int(limit or 500), 5000))

    try:
        triage = await get_source_data_triage(
            merchant_id,
            plan_id=plan_id,
            reason_code=reason_code,
            limit=normalized_limit,
        )
        return {
            "status": "success",
            "data": triage,
        }
    except PlanSupersededError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "OPTIMIZATION_PLAN_SUPERSEDED",
                "current_plan_id": exc.current_plan_id,
                "current_snapshot_id": exc.current_snapshot_id,
            },
        )
    except Exception as e:
        logger.error(
            "❌ Readiness source-data triage failed for merchant %s plan %s reason %s: %s",
            merchant_id,
            plan_id,
            reason_code,
            e,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Readiness source-data triage failed: {str(e)}",
        )


@router.put("/merchant/readiness/source-data-decisions/{reason_code}/{platform}/{platform_product_id}")
async def put_readiness_source_data_decision(
    reason_code: str,
    platform: str,
    platform_product_id: str,
    body: SourceDataDecisionRequest = Body(...),
    current_user: dict = Depends(get_current_user),
):
    """Persist the merchant's source-data decision for one product queue target."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    try:
        decision = await upsert_source_data_decision_state(
            merchant_id,
            reason_code=reason_code,
            platform=platform,
            platform_product_id=platform_product_id,
            decision_state=body.decision_state,
        )
        return {
            "status": "success",
            "data": decision,
        }
    except ActionNotExecutableError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SOURCE_DATA_DECISION_UNSUPPORTED",
                "message": str(exc),
            },
        )
    except ActionNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SOURCE_DATA_DECISION_INVALID",
                "message": str(exc),
            },
        )
    except Exception as e:
        logger.error(
            "❌ Source-data decision save failed for merchant %s %s/%s (%s): %s",
            merchant_id,
            platform,
            platform_product_id,
            reason_code,
            e,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Source-data decision save failed: {str(e)}",
        )


@router.delete("/merchant/readiness/source-data-decisions/{reason_code}/{platform}/{platform_product_id}")
async def delete_readiness_source_data_decision(
    reason_code: str,
    platform: str,
    platform_product_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Clear a previously saved merchant source-data decision."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    try:
        decision = await delete_source_data_decision_state(
            merchant_id,
            reason_code=reason_code,
            platform=platform,
            platform_product_id=platform_product_id,
        )
        return {
            "status": "success",
            "data": decision,
        }
    except ActionNotExecutableError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SOURCE_DATA_DECISION_UNSUPPORTED",
                "message": str(exc),
            },
        )
    except Exception as e:
        logger.error(
            "❌ Source-data decision delete failed for merchant %s %s/%s (%s): %s",
            merchant_id,
            platform,
            platform_product_id,
            reason_code,
            e,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Source-data decision delete failed: {str(e)}",
        )


@router.get("/merchant/readiness/jobs/{job_id}")
async def get_readiness_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Return the latest known state for a remediation execution job."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        job = get_execution_job(job_id)
        return {
            "status": "success",
            "data": job.model_dump(),
        }
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail={"code": "OPTIMIZATION_JOB_NOT_FOUND", "message": "Job not found."})

@router.put("/merchant/profile")
async def update_merchant_profile(
    profile_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Update merchant profile.

    Contact fields persist to merchant_onboarding. Changing contact_email is a
    login-email change: it must move users.email, auth_identities, and
    merchant_onboarding.contact_email together, or the merchant can no longer
    log in with the address the portal shows them.
    """
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    merchant_id = await get_merchant_id_from_user(current_user)

    merchant = await database.fetch_one(
        """
        SELECT merchant_id, business_name, contact_email, contact_phone, website
        FROM merchant_onboarding
        WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    from db.auth_identity import normalize_email, record_identity_event

    # The address this session authenticated with — the row we are allowed to move.
    login_email = normalize_email(current_user.get("email") or "")
    current_contact = normalize_email(merchant["contact_email"] or "")

    new_email_raw = (profile_data.get("contact_email") or "").strip()
    new_email = normalize_email(new_email_raw)
    email_changed = bool(new_email) and new_email not in (login_email, current_contact)

    if email_changed:
        try:
            from email_validator import validate_email

            validate_email(new_email_raw, check_deliverability=False)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid email address")

        existing_user = await database.fetch_one(
            "SELECT id FROM users WHERE LOWER(email) = :email LIMIT 1",
            {"email": new_email},
        )
        existing_identity = await database.fetch_one(
            "SELECT identity_id FROM auth_identities WHERE email_normalized = :email LIMIT 1",
            {"email": new_email},
        )
        if existing_user or existing_identity:
            raise HTTPException(
                status_code=409,
                detail="An account with this email already exists",
            )

    def _field(key: str) -> Optional[str]:
        # A key absent from the payload keeps its current value; an explicit
        # empty string clears the (nullable) column.
        if key not in profile_data:
            return merchant[key]
        value = profile_data.get(key)
        return str(value).strip() if value is not None else None

    updates = {
        "business_name": _field("business_name") or merchant["business_name"],
        "contact_phone": _field("contact_phone"),
        "website": _field("website"),
        "contact_email": new_email_raw if email_changed else (merchant["contact_email"] or login_email),
        "merchant_id": merchant_id,
    }

    async with database.transaction():
        await database.execute(
            """
            UPDATE merchant_onboarding
            SET business_name = :business_name,
                contact_email = :contact_email,
                contact_phone = :contact_phone,
                website = :website,
                updated_at = CURRENT_TIMESTAMP
            WHERE merchant_id = :merchant_id
            """,
            updates,
        )
        if email_changed:
            old_email = login_email or current_contact
            await database.execute(
                "UPDATE users SET email = :new_email WHERE LOWER(email) = :old_email",
                {"new_email": new_email, "old_email": old_email},
            )
            await database.execute(
                """
                UPDATE auth_identities
                SET email = :new_email,
                    email_normalized = :new_email,
                    updated_at = CURRENT_TIMESTAMP
                WHERE email_normalized = :old_email
                """,
                {"new_email": new_email, "old_email": old_email},
            )

    if email_changed:
        try:
            await record_identity_event(
                event_type="login_email_changed",
                email=new_email,
                identity_id=current_user.get("identity_id") or current_user.get("sub"),
                details={
                    "merchant_id": merchant_id,
                    "old_email": login_email or current_contact,
                    "source": "merchant_portal_settings",
                },
            )
        except Exception as exc:
            logger.warning("[MerchantProfile] Failed to record identity event: %s", exc)
        logger.info(
            "[MerchantProfile] Login email changed for merchant %s: %s -> %s",
            merchant_id,
            login_email or current_contact,
            new_email,
        )

    return {
        "status": "success",
        "message": (
            "Profile updated. Your login email has changed — please sign in again."
            if email_changed
            else "Profile updated successfully"
        ),
        "login_email_changed": email_changed,
        "data": {
            "merchant_id": merchant_id,
            "business_name": updates["business_name"],
            "contact_email": updates["contact_email"],
            "contact_phone": updates["contact_phone"],
            "website": updates["website"],
        },
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
        try:
            store_check_row = await database.fetch_one(
                """
                SELECT store_id, platform, domain, status, product_count
                FROM merchant_stores
                WHERE merchant_id = :merchant_id
                  AND platform = 'shopify'
                  AND status IN ('active', 'connected')
                ORDER BY is_primary DESC, connected_at DESC NULLS LAST
                LIMIT 1
                """,
                {"merchant_id": merchant_id}
            )
        except Exception:
            store_check_row = await database.fetch_one(
                """
                SELECT store_id, platform, domain, status, product_count
                FROM merchant_stores
                WHERE merchant_id = :merchant_id
                  AND platform = 'shopify'
                  AND status IN ('active', 'connected')
                ORDER BY connected_at DESC NULLS LAST
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
        
        if (store_check.get("status") or "").lower() not in ("active", "connected"):
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
    active_psps = await _get_active_merchant_psps(merchant_id)

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
        psp_priority = _normalize_merchant_route_priority(
            default_config["psp_priority"],
            active_psps,
            strict=False,
            append_unlisted_active=True,
        )
        routing_strategy = "priority"
        max_retries = max(0, len(psp_priority) - 1)
        timeout_ms = 30000
        route_id = default_config["route_id"]
        metadata_dict: Dict[str, Any] = {}
        created_at = datetime.now(timezone.utc)
        updated_at = created_at
    else:
        # databases.fetch_one returns a Record; normalize to dict for .get access
        route_dict = dict(route)

        raw_psp_priority = route_dict["psp_priority"]
        if isinstance(raw_psp_priority, str):
            raw_psp_priority = json.loads(raw_psp_priority)
        psp_priority = _normalize_merchant_route_priority(
            raw_psp_priority,
            active_psps,
            strict=False,
            append_unlisted_active=False,
        )
        if not psp_priority:
            psp_priority = _normalize_merchant_route_priority(
                [],
                active_psps,
                strict=False,
                append_unlisted_active=True,
            )
        routing_strategy = "priority"
        max_retries = max(0, len(psp_priority) - 1)
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

        if (
            route_dict.get("routing_strategy") != "priority"
            or route_dict.get("max_retries") != max_retries
            or psp_priority != raw_psp_priority
        ):
            await database.execute(
                """
                UPDATE payment_routes
                SET psp_priority = :psp_priority,
                    routing_strategy = 'priority',
                    max_retries = :max_retries,
                    updated_at = NOW()
                WHERE route_id = :route_id
                """,
                {
                    "route_id": route_id,
                    "psp_priority": json.dumps(psp_priority),
                    "max_retries": max_retries,
                },
            )

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
    active_psps = await _get_active_merchant_psps(merchant_id)
    normalized_priority = _normalize_merchant_route_priority(
        update.psp_priority,
        active_psps,
        strict=True,
        append_unlisted_active=False,
    )
    if not normalized_priority:
        raise HTTPException(
            status_code=400,
            detail="Routing config must include at least one active PSP",
        )
    max_retries = max(0, len(normalized_priority) - 1)
    timeout_ms = update.timeout_ms if update.timeout_ms > 0 else 30000

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
                "psp_priority": json.dumps(normalized_priority),
                "routing_strategy": "priority",
                "max_retries": max_retries,
                "timeout_ms": timeout_ms,
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
                "agent_id": None,
                "merchant_id": merchant_id,
                "psp_priority": json.dumps(normalized_priority),
                "routing_strategy": "priority",
                "max_retries": max_retries,
                "timeout_ms": timeout_ms,
            },
        )

    return {
        "status": "success",
        "data": {
            "route_id": route_id,
            "merchant_id": merchant_id,
            "psp_priority": normalized_priority,
            "routing_strategy": "priority",
            "max_retries": max_retries,
            "timeout_ms": timeout_ms,
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
                connected_at,
                is_primary
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND lower(COALESCE(status, '')) IN ('active', 'connected')
            ORDER BY is_primary DESC, connected_at DESC NULLS LAST
        """

        try:
            stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})
        except Exception:
            stores_query = """
                SELECT
                    store_id,
                    platform,
                    name,
                    domain,
                    status,
                    product_count,
                    last_sync,
                    connected_at,
                    false as is_primary
                FROM merchant_stores
                WHERE merchant_id = :merchant_id
                  AND lower(COALESCE(status, '')) IN ('active', 'connected')
                ORDER BY connected_at DESC NULLS LAST
            """
            stores = await database.fetch_all(stores_query, {"merchant_id": merchant_id})

        store_list = [dict(store) for store in stores]
        total_stores = len(store_list)
        active_statuses = {"active", "connected"}
        active_stores = sum(1 for s in store_list if (s.get("status") or "").lower() in active_statuses)

        # Aggregate product data
        product_cache_stats = None
        product_cache_by_platform = None
        try:
            cache_query = """
                SELECT 
                    COUNT(*) AS total_cached,
                    COUNT(CASE WHEN expires_at IS NULL OR expires_at > NOW() THEN 1 END) AS active_cached
                FROM products_cache
                WHERE merchant_id = :merchant_id
            """
            product_cache_stats = await database.fetch_one(cache_query, {"merchant_id": merchant_id})

            by_platform_query = """
                SELECT platform,
                       COUNT(*) AS total_cached,
                       COUNT(CASE WHEN expires_at IS NULL OR expires_at > NOW() THEN 1 END) AS active_cached
                FROM products_cache
                WHERE merchant_id = :merchant_id
                GROUP BY platform
            """
            product_cache_by_platform = await database.fetch_all(by_platform_query, {"merchant_id": merchant_id})
        except Exception:
            product_cache_stats = None
            product_cache_by_platform = None

        total_cached = product_cache_stats["total_cached"] if product_cache_stats and product_cache_stats["total_cached"] else 0
        active_cached = product_cache_stats["active_cached"] if product_cache_stats and product_cache_stats["active_cached"] else 0
        cache_counts_by_platform = {}
        if product_cache_by_platform:
            for row in product_cache_by_platform:
                r = dict(row)
                plat = (r.get("platform") or "").strip().lower()
                if not plat:
                    continue
                cache_counts_by_platform[plat] = {
                    "total": int(r.get("total_cached") or 0),
                    "active": int(r.get("active_cached") or 0),
                }

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
                "product_count": (
                    (cache_counts_by_platform.get((store.get("platform") or "").strip().lower(), {}) or {}).get("active") or 0
                )
                or (store.get("product_count") or 0),
                "product_count_source": "products_cache"
                if ((cache_counts_by_platform.get((store.get("platform") or "").strip().lower(), {}) or {}).get("active") or 0) > 0
                else "merchant_stores",
                "domain": store.get("domain"),
                "is_primary": bool(store.get("is_primary")),
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
                "product_cache_counts_by_platform": cache_counts_by_platform,
                "nodes": nodes
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Failed to load MCP summary for merchant {merchant_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load MCP summary: {str(e)}")

@router.post("/merchant/integrations/commerce-index/sources")
async def register_merchant_commerce_index_source(
    source_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user),
):
    """Register a merchant-authorized source without accepting connector secrets.

    Credentials continue to use each connector's existing secure onboarding
    path.  This endpoint stores only the audit contract that governs catalogue
    facts and downstream Commerce Index publication.
    """
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    merchant_id = await get_merchant_id_from_user(current_user)
    try:
        source = await register_commerce_index_source(
            merchant_id=merchant_id,
            provider=str(source_data.get("provider") or ""),
            status=str(source_data.get("status") or "pending"),
            consent_ref=source_data.get("consent_ref"),
            source_metadata=source_data.get("source_metadata") or {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "success", "data": source}


@router.post("/merchant/integrations/psp/connect")
async def connect_psp(
    psp_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """Connect a PSP provider"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")

    # A rejected merchant must not connect a payment provider. The gate in
    # merchant_onboarding_routes' PSP setup route is NOT the only door, despite
    # a comment there that used to say so: this route takes the merchant_id
    # from the caller's own session and had no onboarding-status check of any
    # kind, so a rejected merchant — who can still log in — connected a PSP
    # here exactly as before.
    from db.merchant_onboarding import get_merchant_onboarding
    from services.store_lifecycle_service import SUPPRESSED_ONBOARDING_STATUSES

    _merchant_id = current_user.get("merchant_id")
    _onboarding = await get_merchant_onboarding(_merchant_id) if _merchant_id else None
    if _onboarding and str(_onboarding.get("status") or "").strip().lower() in (
        SUPPRESSED_ONBOARDING_STATUSES
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Merchant account is {_onboarding.get('status')}. "
                "Only approved merchants can connect a payment provider."
            ),
        )
    
    merchant_id = await get_merchant_id_from_user(current_user)
    provider = str(psp_data.get("provider", "")).strip().lower()
    if provider not in SUPPORTED_CANONICAL_PSPS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported PSP provider for this phase: {provider or 'unknown'}",
        )
    
    # Validate API key format
    api_key = psp_data.get("api_key", "")
    if not api_key or len(api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key")
    
    secret_key = str(psp_data.get("secret_key") or "").strip() or None
    account_id = str(psp_data.get("account_id") or "").strip() or None
    environment = str(psp_data.get("environment") or "").strip().lower() or None
    provider_config: Optional[Dict[str, Any]] = None

    if provider == "adyen":
        merchant_account = str(psp_data.get("merchant_account") or account_id or "").strip()
        client_key = str(psp_data.get("client_key") or "").strip()
        if not merchant_account:
            merchant_account = getattr(settings, "adyen_merchant_account", "").strip()
        if not merchant_account:
            raise HTTPException(status_code=400, detail="Adyen requires merchant_account")
        if not client_key:
            raise HTTPException(status_code=400, detail="Adyen requires client_key")
        account_id = merchant_account
        provider_config = {
            "merchant_account": merchant_account,
            "client_key": client_key,
        }
    elif provider == "stripe":
        public_key = str(
            psp_data.get("public_key")
            or psp_data.get("publicKey")
            or psp_data.get("publishable_key")
            or psp_data.get("publishableKey")
            or ""
        ).strip()
        if public_key:
            provider_config = {
                "public_key": public_key,
            }
    elif provider == "checkout":
        processing_channel_id = str(psp_data.get("processing_channel_id") or account_id or "").strip()
        public_key = str(psp_data.get("public_key") or "").strip()
        if not processing_channel_id:
            raise HTTPException(status_code=400, detail="Checkout.com requires processing_channel_id")
        if not public_key:
            raise HTTPException(status_code=400, detail="Checkout.com requires public_key")
        account_id = processing_channel_id
        provider_config = {
            "processing_channel_id": processing_channel_id,
            "public_key": public_key,
        }
    elif provider == "antom":
        merchant_account = str(psp_data.get("merchant_id") or account_id or "").strip()
        client_id = str(psp_data.get("client_id") or "").strip()
        if not merchant_account:
            raise HTTPException(status_code=400, detail="Antom requires merchant_id")
        if not client_id:
            raise HTTPException(status_code=400, detail="Antom requires client_id")
        account_id = merchant_account
        provider_config = {
            "merchant_id": merchant_account,
            "client_id": client_id,
        }
    capabilities = ["card", "bank_transfer"] if provider in ["stripe", "adyen", "checkout", "antom"] else ["card"]
    # Antom credentials are stored for onboarding and signed-contract setup, but
    # must not enter generic payment routing before its dedicated adapter and
    # verified webhook flow are enabled.
    connection_status = "inactive" if provider == "antom" else "active"
    
    try:
        # Use transaction to ensure data is committed
        async with database.transaction():
            persisted = await persist_canonical_merchant_psp(
                merchant_id=merchant_id,
                provider=provider,
                api_key=api_key,
                account_id=account_id,
                secret_key=secret_key,
                environment=environment,
                provider_config=provider_config,
                name=f"{provider.capitalize()} Account",
                capabilities=capabilities,
                status=connection_status,
                stripe_mode="payment_intent",
            )
            print(f"✅ PSP saved to DB: {persisted['psp_id']} for merchant {merchant_id}")

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
    
    connected_at_value = persisted["connected_at"]
    if isinstance(connected_at_value, datetime):
        connected_at_display = connected_at_value.isoformat()
        if connected_at_value.tzinfo is None:
            connected_at_display += "Z"
    else:
        connected_at_display = str(connected_at_value)

    new_psp = {
        "id": persisted["psp_id"],
        "provider": provider,
        "name": persisted["name"],
        "status": persisted["status"],
        "connected_at": connected_at_display,
        "account_id": account_id,
        "capabilities": persisted["capabilities"],
        "api_key_last4": api_key[-4:] if len(api_key) >= 4 else "****",
        "environment": persisted["record"]["environment"],
        "provider_summary": persisted["record"]["provider_summary"],
        "validation_status": persisted["record"]["validation_status"],
        "validation_error": persisted["record"]["validation_error"],
        "reused_existing": persisted["reused_existing"],
    }

    return {
        "status": "success",
        "message": (
            "Antom settings saved; payment execution is pending signed-contract enablement"
            if provider == "antom"
            else
            f"{provider.capitalize()} settings updated successfully"
            if persisted["reused_existing"]
            else f"{provider.capitalize()} connected successfully"
        ),
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

    # Backward compatible safety:
    # This generic endpoint historically accepted raw tokens and stored them without validation,
    # which can accidentally overwrite a working Shopify connection with an invalid token and
    # cause persistent 401s downstream (primary store selection is recency-based).
    if platform == "shopify":
        from routes.merchant_store_connections import ConnectShopifyRequest, merchant_connect_shopify

        domain = store_url.replace("https://", "").replace("http://", "").strip("/").strip()
        req = ConnectShopifyRequest(merchant_id=merchant_id, shop_domain=domain, access_token=api_key)
        return await merchant_connect_shopify(request=req, current_user=current_user)

    if platform == "wix":
        raise HTTPException(
            status_code=400,
            detail="Please connect Wix via /integrations/wix/connect (requires wix_site_id + api_key).",
        )
    
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
            try:
                await database.execute(
                    """
                    UPDATE merchant_stores
                    SET is_primary = TRUE
                    WHERE store_id = :store_id
                      AND merchant_id = :merchant_id
                      AND NOT EXISTS (
                        SELECT 1
                        FROM merchant_stores
                        WHERE merchant_id = :merchant_id
                          AND store_id != :store_id
                          AND is_primary = TRUE
                          AND lower(COALESCE(status, '')) IN ('active', 'connected')
                      )
                    """,
                    {
                        "store_id": store_id,
                        "merchant_id": merchant_id,
                    },
                )
            except Exception:
                pass
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
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """Get order details"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    merchant_id = await get_merchant_id_from_user(current_user)
    
    try:
        await _ensure_refund_tables_best_effort()

        # Query directly from database
        query = """
            SELECT 
                order_id, merchant_id, store_id, psp_id,
                total, currency, status, payment_status, payment_method,
                customer_name, customer_email, shipping_address,
                items, subtotal, shipping_fee, tax,
                COALESCE(total_refunded, 0) as total_refunded,
                shopify_order_id,
                created_at, updated_at
            FROM orders
            WHERE order_id = :order_id AND merchant_id = :merchant_id
        """
        
        order = await database.fetch_one(query, {"order_id": order_id, "merchant_id": merchant_id})
        
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        order = dict(order)

        # Prefer refund_records sum for historical correctness; fall back to orders.total_refunded.
        try:
            order_total_refunded = float(order.get("total_refunded") or 0)
        except Exception:
            order_total_refunded = 0.0

        try:
            refunded_row = await database.fetch_one(
                """
                SELECT COALESCE(SUM(amount), 0) AS total_refunded
                FROM refund_records
                WHERE order_id = :order_id AND status = 'completed'
                """,
                {"order_id": order_id},
            )
            computed_total_refunded = float(refunded_row["total_refunded"]) if refunded_row else 0.0
        except Exception:
            computed_total_refunded = 0.0
        total_refunded = max(order_total_refunded, computed_total_refunded)

        # Best-effort: if Shopify shows "Paid 0", create a manual sale transaction once the order is viewed.
        try:
            if order.get("shopify_order_id"):
                creds = await _get_shopify_credentials_for_merchant(merchant_id)
                if creds:
                    background_tasks.add_task(
                        _shopify_ensure_manual_sale_transaction_best_effort,
                        creds["shop_domain"],
                        creds["access_token"],
                        str(order["shopify_order_id"]),
                    )
        except Exception:
            pass
        
        return {
            "status": "success",
            "data": {
                "order_id": order["order_id"],
                "amount": float(order["total"]) if order["total"] else 0,
                "total": float(order["total"]) if order["total"] else 0,
                "subtotal": float(order["subtotal"]) if order["subtotal"] else 0,
                "shipping_fee": float(order["shipping_fee"]) if order["shipping_fee"] else 0,
                "tax": float(order["tax"]) if order["tax"] else 0,
                "total_refunded": float(max(float(order["total_refunded"] or 0), computed_total_refunded)),
                "currency": order["currency"],
                "status": order["status"],
                "payment_status": order["payment_status"],
                "payment_method": order["payment_method"],
                "total_refunded": total_refunded,
                "refundable_amount": max(0.0, float(order["total"] or 0) - total_refunded),
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
    background_tasks: BackgroundTasks,
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
            raise HTTPException(status_code=409, detail=f"Refund already processed (refund_id={result.get('refund_id')})")
        elif result["status"] == "failed":
            # Still return success as it's queued for retry
            return {
                "status": "processing",
                "message": "Refund is being processed. You'll be notified when complete.",
                "refund_id": result["refund_id"],
                "amount": refund_request.amount
            }
        else:
            # Best-effort: sync to Shopify so merchants see a "manual refund" record in Shopify.
            try:
                order_row = await database.fetch_one(
                    "SELECT shopify_order_id FROM orders WHERE order_id = :order_id",
                    {"order_id": order_id},
                )
                if order_row and order_row.get("shopify_order_id") and result.get("refund_id"):
                    background_tasks.add_task(
                        _shopify_create_manual_refund_best_effort,
                        merchant_id=merchant_id,
                        shopify_order_id=str(order_row["shopify_order_id"]),
                        refund_id=str(result.get("refund_id")),
                        amount=float(refund_request.amount),
                    )
            except Exception:
                pass

            latest_order = None
            try:
                latest_order = await get_order(order_id)
            except Exception:
                latest_order = None

            await _emit_merchant_refund_webhook_best_effort(
                merchant_id=str(merchant_id),
                order_id=str(order_id),
                amount=float(refund_request.amount),
                refund_id=result.get("refund_id"),
                order=latest_order,
            )

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
                latest_order = None
                try:
                    latest_order = await get_order(order_id)
                except Exception:
                    latest_order = None

                await _emit_merchant_refund_webhook_best_effort(
                    merchant_id=str(merchant_id),
                    order_id=str(order_id),
                    amount=float(refund_request.amount),
                    refund_id=result.get("refund_id"),
                    order=latest_order,
                )
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
    background_tasks: BackgroundTasks,
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

    # Best-effort backfill: sync completed refunds that haven't been synced to Shopify yet.
    try:
        order_row = await database.fetch_one(
            "SELECT shopify_order_id FROM orders WHERE order_id = :order_id",
            {"order_id": order_id},
        )
        shopify_order_id = str(order_row["shopify_order_id"]) if order_row and order_row.get("shopify_order_id") else None
        if shopify_order_id:
            for r in refunds[:5]:
                if r.get("status") != "completed":
                    continue
                if r.get("platform_type") == "shopify" and r.get("platform_sync_status") == "synced":
                    continue
                if r.get("platform_refund_id"):
                    continue
                if not r.get("refund_id"):
                    continue
                background_tasks.add_task(
                    _shopify_create_manual_refund_best_effort,
                    merchant_id=merchant_id,
                    shopify_order_id=shopify_order_id,
                    refund_id=str(r.get("refund_id")),
                    amount=float(r.get("amount") or 0),
                )
    except Exception:
        pass

    # Get order details for context
    order_query = """
    SELECT total as total_amount, COALESCE(total_refunded, 0) as total_refunded, payment_status, currency
    FROM orders 
    WHERE order_id = :order_id
    """
    try:
        order_data = await database.fetch_one(order_query, {"order_id": order_id})
        order_data = dict(order_data) if order_data else None
    except Exception:
        order_data = None
    if not order_data:
        order_data = {"total_amount": 0, "total_refunded": 0, "payment_status": "unknown", "currency": "USD"}

    # Calculate refund summary
    completed_sum = sum(float(r.get("amount") or 0) for r in refunds if r.get("status") == "completed")
    pending_sum = sum(float(r.get("amount") or 0) for r in refunds if r.get("status") == "pending")

    # Use the larger of (orders.total_refunded) and (refund_records sum) for correctness.
    try:
        order_total_refunded = float(order_data["total_refunded"]) if order_data and order_data.get("total_refunded") is not None else 0.0
    except Exception:
        order_total_refunded = 0.0
    effective_total_refunded = max(order_total_refunded, float(completed_sum or 0))

    try:
        total_amount = float(order_data["total_amount"]) if order_data and order_data.get("total_amount") is not None else 0.0
    except Exception:
        total_amount = 0.0

    return {
        "status": "success",
        "order_summary": {
            "order_id": order_id,
            "total_amount": total_amount,
            "total_refunded": effective_total_refunded,
            "payment_status": order_data.get("payment_status", "unknown"),
            "currency": order_data.get("currency") or "USD",
            "refundable_amount": max(0.0, total_amount - effective_total_refunded),
        },
        "refund_summary": {
            "total_refunds": len(refunds),
            "completed_amount": completed_sum,
            "pending_amount": pending_sum,
            "failed_count": len([r for r in refunds if r.get("status") == "failed"]),
        },
        "refunds": refunds,
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
    try:
        await _emit_merchant_refund_webhook_best_effort(
            merchant_id=str(merchant_id),
            order_id=str(order_id),
            amount=float(amount),
            refund_id=refund_result.get("refund_id") if isinstance(refund_result, dict) else None,
            order=order,
        )
    except Exception:
        pass

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
