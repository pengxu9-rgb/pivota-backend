"""Merchant Dashboard API Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime
from decimal import Decimal
import logging
import httpx
import string
import json
import asyncio
import hashlib
import secrets
from urllib.parse import urlparse
from pydantic import BaseModel
from config.settings import resolve_public_api_base_url
from utils.auth import get_current_user
from db.database import database
from db.merchant_onboarding import merchant_onboarding
from db.merchant_portal_preferences import (
    DEFAULT_MERCHANT_PORTAL_PREFERENCES,
    DEFAULT_PORTAL_LANGUAGE,
    get_merchant_portal_preferences,
    upsert_merchant_portal_preferences,
)
from adapters.psp_adapter import get_psp_adapter
from models.order_response import format_order_for_response
from services.merchant_webhook_service import (
    get_signing_secret as get_merchant_webhook_signing_secret,
    get_webhook_config as get_merchant_webhook_config,
    list_deliveries as list_merchant_webhook_deliveries,
    list_webhook_events_catalog as list_merchant_webhook_events_catalog,
    rotate_signing_secret as rotate_merchant_webhook_signing_secret,
    send_test_webhook as send_merchant_test_webhook,
    update_webhook_config as update_merchant_webhook_config,
)
from services.merchant_psp_config_service import (
    build_runtime_adapter_kwargs,
    build_provider_connect_record,
    evaluate_psp_readiness,
    parse_capabilities,
)
from services.merchant_psp_telemetry_service import (
    get_merchant_psp_telemetry,
    unavailable_payment_telemetry,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_STRIPE_AFTERCARE_EVENTS = [
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.refunded",
    "refund.created",
    "refund.updated",
    "refund.failed",
    "checkout.session.completed",
]


def _stripe_webhook_target_url(psp_id: str) -> str:
    return f"{resolve_public_api_base_url().rstrip('/')}/webhooks/stripe/{psp_id}"


def _stripe_webhook_psp_id_from_url(url: str) -> Optional[str]:
    """Return the embedded psp_id for a Pivota-managed per-PSP Stripe webhook URL.

    Matches ``<any-host>/webhooks/stripe/psp_stripe_<id>`` and returns the
    ``psp_stripe_...`` segment. Host-agnostic on purpose: a Stripe account may
    still hold orphan endpoints minted under a previous public host. Returns
    ``None`` for the bare ``/webhooks/stripe`` platform endpoint and for any URL
    that isn't one of our per-PSP endpoints, so neither is ever swept.
    """
    try:
        path = urlparse(str(url or "").strip()).path
    except Exception:
        return None
    prefix = "/webhooks/stripe/"
    if not path.startswith(prefix):
        return None
    tail = path[len(prefix):].strip("/")
    if not tail or "/" in tail or not tail.startswith("psp_stripe_"):
        return None
    return tail


async def _live_stripe_psp_ids(merchant_id: Optional[str]) -> set[str]:
    """psp_ids with a live merchant_psps row for this merchant (Stripe only).

    Used to decide which per-PSP webhook endpoints are still owned by a real PSP
    and must never be disabled by the orphan sweep. Best-effort: a lookup failure
    yields an empty set, which only widens what the sweep treats as orphaned, so
    we degrade conservatively by also passing the active psp_id separately.
    """
    if not merchant_id:
        return set()
    try:
        rows = await database.fetch_all(
            "SELECT psp_id FROM merchant_psps WHERE merchant_id = :merchant_id AND provider = 'stripe'",
            {"merchant_id": merchant_id},
        )
    except Exception as exc:
        logger.warning(
            "stripe_live_psp_lookup_failed",
            extra={"merchant_id": merchant_id, "error": str(exc)},
        )
        return set()
    live: set[str] = set()
    for row in rows or []:
        psp_id = str(dict(row).get("psp_id") or "").strip()
        if psp_id:
            live.add(psp_id)
    return live


def _stripe_object_field(obj: Any, field: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(field)
    try:
        getter = getattr(obj, "get", None)
    except Exception:
        getter = None
    if callable(getter):
        try:
            return getter(field)
        except Exception:
            pass
    try:
        return obj[field]  # type: ignore[index]
    except Exception:
        pass
    return getattr(obj, field, None)


def _stripe_list_data(obj: Any) -> List[Any]:
    pager = getattr(obj, "auto_paging_iter", None)
    if callable(pager):
        try:
            return list(pager())
        except Exception:
            pass
    data = _stripe_object_field(obj, "data") or []
    return list(data) if isinstance(data, list) else []


def _stripe_endpoint_is_disabled(endpoint: Any) -> bool:
    status = str(_stripe_object_field(endpoint, "status") or "").strip().lower()
    disabled = _stripe_object_field(endpoint, "disabled")
    return status == "disabled" or disabled is True


async def _disable_duplicate_stripe_webhook_endpoints(
    *,
    stripe_sdk: Any,
    desired_url: str,
    active_endpoint_id: str,
    active_psp_id: str,
    stripe_kwargs: Dict[str, Any],
    live_psp_ids: Optional[set[str]] = None,
) -> int:
    """Disable stale per-PSP Stripe webhook endpoints on this account.

    Two classes are disabled:
      1. Exact-URL duplicates of the active endpoint (legacy behaviour).
      2. Orphans — Pivota per-PSP endpoints (``/webhooks/stripe/psp_stripe_*``)
         whose embedded psp_id is neither the active psp_id nor a live
         merchant_psps row. These are left behind when a PSP is re-provisioned
         under a new psp_id and otherwise linger enabled forever, returning 400
         "Invalid signature" on every delivery (prod incident 2026-06-16).

    Endpoints for OTHER live psp_ids on the same account (e.g. a sibling PSP) are
    preserved via ``live_psp_ids``.
    """
    list_endpoint = getattr(stripe_sdk.WebhookEndpoint, "list", None)
    if not callable(list_endpoint):
        return 0

    try:
        endpoints = _stripe_list_data(list_endpoint(limit=100, **stripe_kwargs))
    except Exception as exc:
        logger.warning(
            "stripe_webhook_duplicate_scan_failed",
            extra={"active_endpoint_id": active_endpoint_id, "error": str(exc)},
        )
        return 0

    keep_psp_ids = set(live_psp_ids or set())
    if active_psp_id:
        keep_psp_ids.add(active_psp_id)

    disabled_count = 0
    for endpoint in endpoints:
        endpoint_id = str(_stripe_object_field(endpoint, "id") or "").strip()
        endpoint_url = str(_stripe_object_field(endpoint, "url") or "").strip()
        if not endpoint_id or endpoint_id == active_endpoint_id:
            continue
        if _stripe_endpoint_is_disabled(endpoint):
            continue

        is_exact_duplicate = endpoint_url == desired_url
        endpoint_psp_id = _stripe_webhook_psp_id_from_url(endpoint_url)
        is_orphan = endpoint_psp_id is not None and endpoint_psp_id not in keep_psp_ids
        if not (is_exact_duplicate or is_orphan):
            continue
        try:
            stripe_sdk.WebhookEndpoint.modify(endpoint_id, disabled=True, **stripe_kwargs)
            disabled_count += 1
        except Exception as exc:
            logger.warning(
                "stripe_webhook_duplicate_disable_failed",
                extra={
                    "active_endpoint_id": active_endpoint_id,
                    "duplicate_endpoint_id": endpoint_id,
                    "error": str(exc),
                },
            )
    if disabled_count:
        logger.info(
            "stripe_webhook_duplicate_endpoints_disabled",
            extra={"active_endpoint_id": active_endpoint_id, "disabled_count": disabled_count},
        )
    return disabled_count


async def _ensure_stripe_webhook_endpoint(
    *,
    psp_id: str,
    api_key: str,
    provider_config: Dict[str, Any],
    account_id: Optional[str],
    environment: str,
    merchant_id: Optional[str] = None,
) -> tuple[Dict[str, Any], bool]:
    import stripe as stripe_sdk

    stripe_sdk.api_key = api_key
    next_config = dict(provider_config or {})
    desired_url = _stripe_webhook_target_url(psp_id)
    desired_events = list(_STRIPE_AFTERCARE_EVENTS)
    stripe_kwargs = {"stripe_account": account_id} if account_id else {}
    live_psp_ids = await _live_stripe_psp_ids(merchant_id)
    existing_endpoint_id = str(next_config.get("webhook_endpoint_id") or "").strip()
    existing_secret = str(next_config.get("webhook_endpoint_secret") or "").strip()

    if existing_endpoint_id and existing_secret:
        try:
            endpoint = stripe_sdk.WebhookEndpoint.retrieve(existing_endpoint_id, **stripe_kwargs)
            endpoint_url = str(_stripe_object_field(endpoint, "url") or "").strip()
            enabled_events = sorted(
                str(item).strip() for item in (_stripe_object_field(endpoint, "enabled_events") or [])
            )
            if endpoint_url != desired_url or enabled_events != sorted(desired_events):
                stripe_sdk.WebhookEndpoint.modify(
                    existing_endpoint_id,
                    url=desired_url,
                    enabled_events=desired_events,
                    **stripe_kwargs,
                )
            next_config["webhook_endpoint_id"] = existing_endpoint_id
            next_config["webhook_endpoint_secret"] = existing_secret
            next_config["webhook_url"] = desired_url
            await _disable_duplicate_stripe_webhook_endpoints(
                stripe_sdk=stripe_sdk,
                desired_url=desired_url,
                active_endpoint_id=existing_endpoint_id,
                active_psp_id=psp_id,
                stripe_kwargs=stripe_kwargs,
                live_psp_ids=live_psp_ids,
            )
            return next_config, False
        except Exception:
            pass

    created = stripe_sdk.WebhookEndpoint.create(
        url=desired_url,
        enabled_events=desired_events,
        description=f"Pivota merchant PSP webhook for {psp_id} ({environment})",
        metadata={"psp_id": psp_id, "environment": environment},
        **stripe_kwargs,
    )
    created_secret = str(_stripe_object_field(created, "secret") or "").strip()
    created_id = str(_stripe_object_field(created, "id") or "").strip()
    if not created_secret or not created_id:
        raise ValueError("Stripe webhook endpoint creation did not return endpoint credentials")

    next_config["webhook_endpoint_id"] = created_id
    next_config["webhook_endpoint_secret"] = created_secret
    next_config["webhook_url"] = desired_url
    await _disable_duplicate_stripe_webhook_endpoints(
        stripe_sdk=stripe_sdk,
        desired_url=desired_url,
        active_endpoint_id=created_id,
        active_psp_id=psp_id,
        stripe_kwargs=stripe_kwargs,
        live_psp_ids=live_psp_ids,
    )
    return next_config, True


async def disable_stripe_webhook_endpoint_for_psp(
    *,
    api_key: str,
    provider_config: Any,
    account_id: Optional[str],
) -> bool:
    """Best-effort disable of the Stripe webhook endpoint bound to a PSP.

    Called on PSP deletion/rotation so the endpoint does not orphan: once the
    merchant_psps row is gone, handle_stripe_webhook can no longer load the
    per-PSP secret and every delivery fails signature verification with 400.
    Never raises — deletion must not be blocked by a Stripe call.
    """
    api_key = str(api_key or "").strip()
    if not api_key:
        return False

    config: Dict[str, Any] = {}
    if isinstance(provider_config, dict):
        config = provider_config
    elif isinstance(provider_config, str):
        try:
            parsed = json.loads(provider_config)
            if isinstance(parsed, dict):
                config = parsed
        except Exception:
            config = {}
    endpoint_id = str(config.get("webhook_endpoint_id") or "").strip()
    if not endpoint_id:
        return False

    try:
        import stripe as stripe_sdk

        stripe_sdk.api_key = api_key
        stripe_kwargs = {"stripe_account": account_id} if account_id else {}
        stripe_sdk.WebhookEndpoint.modify(endpoint_id, disabled=True, **stripe_kwargs)
        logger.info(
            "stripe_webhook_endpoint_disabled_on_psp_removal",
            extra={"endpoint_id": endpoint_id},
        )
        return True
    except Exception as exc:
        logger.warning(
            "stripe_webhook_endpoint_disable_on_psp_removal_failed",
            extra={"endpoint_id": endpoint_id, "error": str(exc)},
        )
        return False


class MerchantPortalPreferencesRequest(BaseModel):
    email_orders: Optional[bool] = None
    email_payments: Optional[bool] = None
    email_inventory: Optional[bool] = None
    email_weekly: Optional[bool] = None
    portal_language: Optional[
        Literal["en", "zh-CN", "ja-JP", "ko-KR", "fr-FR", "de-DE"]
    ] = None
    # W5 P7: consent toggle for audit executor dispatch. true = auto-execute
    # (default), false = opt into per-run approval.
    executor_auto_execute: Optional[bool] = None


class MerchantWebhookConfigRequest(BaseModel):
    url: Optional[str] = None
    events: List[str] = []
    enabled: bool = False


class MerchantWebhookTestRequest(BaseModel):
    event_type: Optional[str] = "order.created"


class MerchantOrderBackedCanaryRequest(BaseModel):
    amount: float
    currency: str
    order_id: Optional[str] = None
    customer_email: Optional[str] = None
    customer_name: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    emit_merchant_webhook: bool = False
    enforce_live_readiness: bool = True
    label: Optional[str] = None
    preferred_provider: Optional[str] = None

# Payment status normalization:
# - "status" is the order lifecycle (pending/completed/fulfilled/etc.)
# - "payment_status" is the payment lifecycle (unpaid/pending/paid/etc.)
# Merchant dashboards should treat revenue as "paid/confirmed" only.
PAID_PAYMENT_STATUSES_SQL = "('paid','completed','succeeded','success','settled','partially_refunded')"

# Production dashboard routes must fail closed when DB reads fail. Demo
# merchant fixtures belong in tests/dev-only fixtures, not runtime fallback.
DEMO_MERCHANT_DATA = {}

# REMOVED: generate_demo_orders() + generate_analytics() — fabricated random
# order/analytics fixtures. Production dashboards fail closed (DEMO_MERCHANT_DATA
# is empty, so the .get() call sites return None); demo data belongs in tests.


async def _resolve_merchant_id(current_user: dict) -> str:
    merchant_id = current_user.get("merchant_id")
    if merchant_id:
        return merchant_id

    email = current_user.get("email")
    if email:
        result = await database.fetch_one(
            """
            SELECT merchant_id
            FROM merchant_onboarding
            WHERE contact_email = :email
            LIMIT 1
            """,
            {"email": email},
        )
        if result:
            row = dict(result)
            if row.get("merchant_id"):
                return str(row["merchant_id"])

    raise HTTPException(status_code=400, detail="Merchant ID not found in token")


def _generate_merchant_api_key() -> tuple[str, str]:
    api_key = f"pk_live_{secrets.token_urlsafe(32)}"
    api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return api_key, api_key_hash

@router.get("/merchant/profile")
async def get_merchant_profile(current_user: dict = Depends(get_current_user)):
    """Get merchant profile from real database"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Get merchant_id from JWT token
        merchant_id = current_user.get("merchant_id")
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Merchant ID not found in token")
        
        # Query merchant data from database
        merchant_query = """
            SELECT 
                merchant_id,
                business_name,
                store_url,
                website,
                region,
                contact_email,
                contact_phone,
                status,
                operating_mode,
                created_at
            FROM merchant_onboarding
            WHERE merchant_id = :merchant_id
        """
        merchant = await database.fetch_one(merchant_query, {"merchant_id": merchant_id})
        
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        # operating_mode tells the portal whether this is a store-less brand
        # (declared merchant mode). Read defensively so a row/record without the
        # column (e.g. pre-migration test fixtures) falls back to 'storefront'.
        merchant_map = dict(merchant)
        operating_mode = merchant_map.get("operating_mode") or "storefront"

        # Get statistics
        stats_query = """
            SELECT 
                COUNT(*) as total_orders,
                COALESCE(SUM(CASE WHEN payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as total_revenue
            FROM orders
            WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
        """
        stats = await database.fetch_one(stats_query, {"merchant_id": merchant_id})
        
        return {
            "status": "success",
            "data": {
                "merchant_id": merchant["merchant_id"],
                "business_name": merchant["business_name"],
                "contact_email": merchant["contact_email"],
                "email": merchant["contact_email"],
                "contact_phone": merchant["contact_phone"],
                "phone": merchant["contact_phone"],
                "website": merchant["website"] or merchant["store_url"],
                "store_url": merchant["store_url"],
                "address": "",
                "country": merchant["region"],
                "region": merchant["region"],
                "business_type": None,
                "status": merchant["status"],
                "operating_mode": operating_mode,
                "created_at": merchant["created_at"].isoformat() if merchant["created_at"] else None,
                "total_orders": stats["total_orders"] if stats else 0,
                "total_revenue": float(stats["total_revenue"]) if stats else 0
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching merchant profile: {e}")
        merchant_id = current_user.get("merchant_id")
        merchant_data = DEMO_MERCHANT_DATA.get(merchant_id)
        if merchant_data:
            return {"status": "success", "data": merchant_data["profile"]}
        raise HTTPException(status_code=500, detail="Failed to fetch profile")


@router.post("/merchant/payment-canary/order-backed", include_in_schema=False)
async def execute_merchant_order_backed_canary(
    payload: MerchantOrderBackedCanaryRequest,
    current_user: dict = Depends(get_current_user),
):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = await _resolve_merchant_id(current_user)

    from routes.payment_execution_routes import (
        InternalOrderBackedCanaryRequest,
        _execute_order_backed_payment_canary,
    )

    # Loaded, not fabricated. This dict used to hardcode status="approved" and
    # hand it to the executor, which creates a REAL order row and runs a REAL
    # PSP payment — so it bypassed both the order-creation gate and
    # _load_canary_merchant's own "Only approved merchants can process
    # payments" check. A rejected merchant took money through here.
    from routes.payment_execution_routes import _load_canary_merchant

    merchant = await _load_canary_merchant(merchant_id)
    requested_order_id = payload.order_id or (
        f"merchant_canary_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    )
    result = await _execute_order_backed_payment_canary(
        merchant=merchant,
        payment_request=InternalOrderBackedCanaryRequest(
            amount=payload.amount,
            currency=payload.currency,
            order_id=requested_order_id,
            customer_email=payload.customer_email or current_user.get("email"),
            customer_name=payload.customer_name,
            description=payload.description,
            metadata=payload.metadata,
            emit_merchant_webhook=payload.emit_merchant_webhook,
            enforce_live_readiness=payload.enforce_live_readiness,
            label=payload.label,
            preferred_provider=payload.preferred_provider,
        ),
        source="merchant_order_backed_canary",
    )
    return jsonable_encoder(result)


@router.get("/merchant/settings/preferences")
async def get_merchant_settings_preferences(current_user: dict = Depends(get_current_user)):
    """Get merchant portal notification preferences and portal language."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID not found in token")

    preferences = await get_merchant_portal_preferences(merchant_id)
    return {
        "status": "success",
        "data": {
            "merchant_id": merchant_id,
            "email_orders": preferences.get("email_orders", DEFAULT_MERCHANT_PORTAL_PREFERENCES["email_orders"]),
            "email_payments": preferences.get("email_payments", DEFAULT_MERCHANT_PORTAL_PREFERENCES["email_payments"]),
            "email_inventory": preferences.get("email_inventory", DEFAULT_MERCHANT_PORTAL_PREFERENCES["email_inventory"]),
            "email_weekly": preferences.get("email_weekly", DEFAULT_MERCHANT_PORTAL_PREFERENCES["email_weekly"]),
            "portal_language": preferences.get("portal_language", DEFAULT_PORTAL_LANGUAGE),
            "executor_auto_execute": preferences.get(
                "executor_auto_execute",
                DEFAULT_MERCHANT_PORTAL_PREFERENCES["executor_auto_execute"],
            ),
            "updated_at": preferences.get("updated_at"),
        },
    }


@router.put("/merchant/settings/preferences")
async def update_merchant_settings_preferences(
    payload: MerchantPortalPreferencesRequest,
    current_user: dict = Depends(get_current_user),
):
    """Persist merchant portal notification preferences and portal language."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID not found in token")

    preferences = await upsert_merchant_portal_preferences(
        merchant_id,
        payload.model_dump(exclude_none=True),
    )
    return {
        "status": "success",
        "data": {
            "merchant_id": merchant_id,
            "email_orders": preferences["email_orders"],
            "email_payments": preferences["email_payments"],
            "email_inventory": preferences["email_inventory"],
            "email_weekly": preferences["email_weekly"],
            "portal_language": preferences.get("portal_language", DEFAULT_PORTAL_LANGUAGE),
            "executor_auto_execute": preferences.get(
                "executor_auto_execute",
                DEFAULT_MERCHANT_PORTAL_PREFERENCES["executor_auto_execute"],
            ),
            "updated_at": preferences.get("updated_at"),
        },
    }


@router.get("/merchant/executor-runs/pending")
async def get_pending_executor_runs(
    audit_run_id: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    """W5 P7: list this merchant's executor runs awaiting approval.

    Only populated for merchants who opted into approval mode
    (executor_auto_execute=false). Optionally filtered to one audit run.
    """
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID not found in token")

    from db.executor_runs import pending_approval_runs_for_merchant

    runs = await pending_approval_runs_for_merchant(
        merchant_id=merchant_id,
        parent_audit_run_id=audit_run_id,
    )
    return {
        "status": "success",
        "data": {
            "merchant_id": merchant_id,
            "audit_run_id": audit_run_id,
            "runs": runs,
            "count": len(runs),
        },
    }


def _executor_consent_http(result: Dict[str, Any]) -> Dict[str, Any]:
    """Map an approve/decline result dict to an HTTP response (raising
    HTTPException for the non-success outcomes)."""
    status = result.get("status")
    if status == "success":
        return {
            "status": "success",
            "data": {
                "run_id": result.get("run_id"),
                "stage": result.get("stage"),
                "noop": bool(result.get("noop")),
            },
        }
    if status in ("not_found", "forbidden"):
        # 404 for both — don't leak whether another merchant owns the id.
        raise HTTPException(status_code=404, detail="Executor run not found")
    if status == "expired":
        raise HTTPException(
            status_code=409,
            detail="Executor run pending-approval window expired",
        )
    # conflict / anything else
    raise HTTPException(
        status_code=409,
        detail=f"Executor run is not in an approvable state (stage={result.get('stage')})",
    )


@router.post("/merchant/executor-runs/{run_id}/approve")
async def approve_pending_executor_run(
    run_id: str,
    current_user: dict = Depends(get_current_user),
):
    """W5 P7: approve a pending executor run → queue it for the worker.
    Idempotent: approving an already-queued/completed run is a no-op
    success."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID not found in token")

    from db.executor_runs import approve_executor_run

    result = await approve_executor_run(run_id=run_id, merchant_id=merchant_id)
    return _executor_consent_http(result)


@router.post("/merchant/executor-runs/{run_id}/decline")
async def decline_pending_executor_run(
    run_id: str,
    current_user: dict = Depends(get_current_user),
):
    """W5 P7: decline a pending executor run → terminal, never runs.
    Idempotent: declining an already-declined run is a no-op success."""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID not found in token")

    from db.executor_runs import decline_executor_run

    result = await decline_executor_run(run_id=run_id, merchant_id=merchant_id)
    return _executor_consent_http(result)


@router.get("/merchant/{merchant_id}/integrations")
async def get_merchant_stores(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant's connected stores from database"""
    if current_user["role"] not in ["merchant", "admin", "super_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    stores = []
    cache_counts_by_platform: Dict[str, int] = {}
    
    # Try to read from database
    try:
        print(f"DEBUG get_merchant_stores: Querying for merchant_id: {merchant_id}")
        query = """
            SELECT 
                store_id, 
                platform, 
                name, 
                domain, 
                status, 
                connected_at, 
                last_sync, 
                product_count,
                is_primary,
                CASE WHEN api_key IS NOT NULL AND api_key != '' THEN true ELSE false END as api_key_present
            FROM merchant_stores
            WHERE merchant_id = :merchant_id
              AND lower(COALESCE(status, '')) IN ('active', 'connected')
            ORDER BY is_primary DESC, connected_at DESC NULLS LAST
        """
        
        try:
            rows = await database.fetch_all(query, {"merchant_id": merchant_id})
        except Exception as e:
            logger.warning("merchant_store_primary_query_failed", extra={"merchant_id": merchant_id, "error": str(e)})
            query = """
                SELECT
                    store_id,
                    platform,
                    name,
                    domain,
                    status,
                    connected_at,
                    last_sync,
                    product_count,
                    false as is_primary,
                    CASE WHEN api_key IS NOT NULL AND api_key != '' THEN true ELSE false END as api_key_present
                FROM merchant_stores
                WHERE merchant_id = :merchant_id
                  AND lower(COALESCE(status, '')) IN ('active', 'connected')
                ORDER BY connected_at DESC NULLS LAST
            """
            rows = await database.fetch_all(query, {"merchant_id": merchant_id})

        # Derive product counts from products_cache so the UI isn't blocked on
        # merchant_stores.product_count being updated by background import workers.
        try:
            cache_rows = await database.fetch_all(
                """
                SELECT platform, COUNT(*) AS active_cached
                FROM products_cache
                WHERE merchant_id = :merchant_id
                  AND (expires_at IS NULL OR expires_at > NOW())
                GROUP BY platform
                """,
                {"merchant_id": merchant_id},
            )
            for r in cache_rows or []:
                rr = dict(r)
                plat = (rr.get("platform") or "").strip().lower()
                if plat:
                    cache_counts_by_platform[plat] = int(rr.get("active_cached") or 0)
        except Exception:
            cache_counts_by_platform = {}

        print(f"DEBUG get_merchant_stores: Found {len(rows)} stores")
        active_statuses = {"active", "connected"}
        for row in rows:
            is_active = (row["status"] or "").lower() in active_statuses
            has_api_key = row["api_key_present"]
            is_connected = is_active and has_api_key
            platform = (row["platform"] or "").strip().lower()
            cached_count = cache_counts_by_platform.get(platform, 0)
            display_count = cached_count if cached_count > 0 else (row["product_count"] or 0)
            
            stores.append({
                "id": row["store_id"],
                "platform": row["platform"],
                "name": row["name"],
                "domain": row["domain"],
                "status": row["status"],
                "is_active": is_active,
                "is_primary": bool(row["is_primary"]),
                "is_connected": is_connected,
                "api_key_present": has_api_key,
                "shop_domain": row["domain"],  # Alias for compatibility
                "connected_at": row["connected_at"],
                "last_sync": row["last_sync"],
                "product_count": display_count,
                "product_count_source": "products_cache" if cached_count > 0 else "merchant_stores"
            })
    except Exception as e:
        print(f"Database error: {e}")
        # Fallback: return demo data if database fails
        merchant_data = DEMO_MERCHANT_DATA.get(merchant_id)
        if merchant_data:
            stores = merchant_data.get("stores", [])
    
    return {"status": "success", "data": {"stores": stores}}

@router.get("/merchant/{merchant_id}/psps")
async def get_merchant_psps(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant's connected PSPs."""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    psps = []
    
    # Try to read from database
    try:
        query = """
            SELECT psp_id, provider, name, account_id, status, connected_at, capabilities, api_key,
                   environment, provider_config, validation_status, validation_error, last_validated_at
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
            ORDER BY connected_at DESC
        """
        
        rows = await database.fetch_all(query, {"merchant_id": merchant_id})
        print(f"DEBUG: Found {len(rows)} PSPs in database for merchant {merchant_id}")

        psp_telemetry = await get_merchant_psp_telemetry(merchant_id)

        for row in rows:
            row_dict = dict(row)
            capabilities = parse_capabilities(row_dict.get("capabilities"))

            psp_id = row_dict["psp_id"]
            provider = row_dict["provider"]
            api_key = row_dict["api_key"]
            configured = bool(api_key and str(api_key).strip() and api_key != "pending_setup")
            effective_status = row_dict["status"]
            if not configured and (effective_status or "").lower() == "active":
                effective_status = "pending"

            psps.append({
                "id": psp_id,
                "provider": row_dict["provider"],
                "name": row_dict["name"],
                "account_id": row_dict["account_id"],
                "status": effective_status,
                "connected_at": row_dict["connected_at"],
                "capabilities": capabilities,
                "api_key_last4": api_key[-4:] if api_key and len(api_key) >= 4 else "****",
                **(psp_telemetry.get(psp_id) or unavailable_payment_telemetry()),
                "is_active": (effective_status or "").lower() == "active",
                **evaluate_psp_readiness(
                    provider,
                    status=effective_status,
                    api_key=api_key,
                    account_id=row_dict.get("account_id"),
                    provider_config=row_dict.get("provider_config"),
                    environment=row_dict.get("environment"),
                    validation_status=row_dict.get("validation_status"),
                    validation_error=row_dict.get("validation_error"),
                ),
                "last_validated_at": row_dict.get("last_validated_at").isoformat() if row_dict.get("last_validated_at") else None,
            })
            print(f"DEBUG: PSP {psp_id} - payment telemetry not reported")
    except Exception as e:
        print(f"Database error in get_merchant_psps: {e}")
        import traceback
        traceback.print_exc()
        # Fallback: return demo data if database fails
        merchant_data = DEMO_MERCHANT_DATA.get(merchant_id)
        if merchant_data:
            psps = merchant_data.get("psps", [])
    
    return {"status": "success", "data": {"psps": psps}}

@router.get("/merchant/{merchant_id}/orders")
async def get_merchant_orders(
    merchant_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant's orders from real database"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        # Build query with optional status filter
        where_clause = "WHERE merchant_id = :merchant_id"
        params = {"merchant_id": merchant_id}
        
        if status:
            where_clause += " AND status = :status"
            params["status"] = status
        
        # Get total count
        count_query = f"SELECT COUNT(*) as total FROM orders {where_clause}"
        count_result = await database.fetch_one(count_query, params)
        total = count_result["total"] if count_result else 0
        
        # Get paginated orders
        orders_query = f"""
            SELECT 
                order_id, merchant_id, store_id, psp_id,
                total,
                currency, status, payment_status, payment_method,
                customer_name, customer_email,
                created_at, updated_at
            FROM orders
            {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """
        
        params["limit"] = limit
        params["offset"] = offset
        
        rows = await database.fetch_all(orders_query, params)
        
        # Format orders using standardized format
        orders = []
        for row in rows:
            # Convert row to dict
            order_dict = dict(row)
            # Use standardized formatting
            formatted_order = format_order_for_response(order_dict)
            # Add additional fields needed by frontend
            formatted_order.update({
                "id": row["order_id"],
                "order_number": row["order_id"],
                "merchant_id": row["merchant_id"],
                "customer_name": row["customer_name"],
                "customer": {
                    "name": row["customer_name"],
                    "email": row["customer_email"]
                },
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
            })
            orders.append(formatted_order)
        
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
        print(f"Error fetching orders from DB: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch orders")

@router.get("/merchant/dashboard/stats")
async def get_dashboard_stats(current_user: dict = Depends(get_current_user)):
    """Get dashboard stats for current merchant"""
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")
    
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=400, detail="Merchant ID not found")
    
    # Call the existing analytics endpoint
    return await get_merchant_analytics(merchant_id, current_user)

@router.get("/merchant/{merchant_id}/analytics")
async def get_merchant_analytics(
    merchant_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get merchant analytics from real data"""
    try:
        if current_user["role"] not in ["merchant", "admin"]:
            raise HTTPException(status_code=403, detail="Not authorized")

        # Get analytics from real orders
        analytics_query = """
            SELECT 
                COUNT(*) as total_orders_all_time,
                COALESCE(SUM(total), 0) as gmv_all_time,
                COALESCE(SUM(CASE WHEN payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as confirmed_revenue_all_time,
                SUM(CASE WHEN payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN 1 ELSE 0 END) as paid_orders_all_time,
                COALESCE(AVG(CASE WHEN payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE NULL END), 0) as avg_order_value_all_time,
                COUNT(DISTINCT customer_email) as total_customers_all_time,
                COUNT(DISTINCT CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN customer_email END) as total_customers_last_30_days,
                SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN 1 ELSE 0 END) as orders_last_30_days,
                COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' THEN total ELSE 0 END), 0) as gmv_last_30_days,
                SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' AND payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN 1 ELSE 0 END) as paid_orders_last_30_days,
                COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '30 days' AND payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as confirmed_revenue_last_30_days
            FROM orders
            WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
        """
        
        analytics = await database.fetch_one(analytics_query, {"merchant_id": merchant_id})
        
        # Get recent orders
        recent_orders_query = """
            SELECT order_id, total as amount, status, customer_name, created_at
            FROM orders
            WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
            ORDER BY created_at DESC
            LIMIT 5
        """
        recent_orders_rows = await database.fetch_all(recent_orders_query, {"merchant_id": merchant_id})
        
        recent_orders = []
        for row in recent_orders_rows:
            recent_orders.append({
                "order_id": row["order_id"],
                "amount": float(row["amount"] or 0),
                "status": row["status"],
                "customer_name": row["customer_name"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None
            })
        
        # Calculate growth rates (simplified - comparing to previous 30 days)
        growth_query = """
            SELECT 
                COUNT(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days' 
                          AND created_at < CURRENT_DATE - INTERVAL '30 days' THEN 1 END) as orders_prev_30,
                COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days' 
                        AND created_at < CURRENT_DATE - INTERVAL '30 days' THEN total ELSE 0 END), 0) as gmv_prev_30,
                SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days'
                        AND created_at < CURRENT_DATE - INTERVAL '30 days'
                        AND payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN 1 ELSE 0 END) as paid_orders_prev_30,
                COALESCE(SUM(CASE WHEN created_at >= CURRENT_DATE - INTERVAL '60 days' 
                        AND created_at < CURRENT_DATE - INTERVAL '30 days'
                        AND payment_status IN """ + PAID_PAYMENT_STATUSES_SQL + """ THEN total ELSE 0 END), 0) as confirmed_revenue_prev_30
            FROM orders
            WHERE merchant_id = :merchant_id AND (is_deleted IS NULL OR is_deleted = FALSE)
        """
        growth = await database.fetch_one(growth_query, {"merchant_id": merchant_id})
        
        def _to_int(value: Any) -> int:
            try:
                if value is None:
                    return 0
                return int(value)
            except (TypeError, ValueError):
                return 0

        order_growth = 0.0
        revenue_growth = 0.0
        gmv_growth = 0.0
        
        if growth and analytics:
            orders_prev_30 = _to_int(growth["orders_prev_30"])
            orders_last_30 = _to_int(analytics["orders_last_30_days"])
            confirmed_revenue_prev_30 = float(growth["confirmed_revenue_prev_30"] or 0)
            confirmed_revenue_last_30 = float(analytics["confirmed_revenue_last_30_days"] or 0)
            gmv_prev_30 = float(growth["gmv_prev_30"] or 0)
            gmv_last_30 = float(analytics["gmv_last_30_days"] or 0)

            if orders_prev_30 > 0:
                order_growth = ((orders_last_30 - orders_prev_30) / orders_prev_30) * 100
            if confirmed_revenue_prev_30 > 0:
                revenue_growth = ((confirmed_revenue_last_30 - confirmed_revenue_prev_30) / confirmed_revenue_prev_30) * 100
            if gmv_prev_30 > 0:
                gmv_growth = ((gmv_last_30 - gmv_prev_30) / gmv_prev_30) * 100
        
        # Calculate Analytics rates
        total_orders_all_time = _to_int(analytics["total_orders_all_time"]) if analytics else 0
        paid_orders_all_time = _to_int(analytics["paid_orders_all_time"]) if analytics else 0
        gmv_all_time = float(analytics["gmv_all_time"] or 0) if analytics else 0.0
        confirmed_revenue_all_time = float(analytics["confirmed_revenue_all_time"] or 0) if analytics else 0.0

        total_orders = _to_int(analytics["orders_last_30_days"]) if analytics else 0
        paid_orders = _to_int(analytics["paid_orders_last_30_days"]) if analytics else 0
        total_customers = _to_int(analytics["total_customers_last_30_days"]) if analytics else 0
        total_customers_all_time = _to_int(analytics["total_customers_all_time"]) if analytics else 0
        gmv = float(analytics["gmv_last_30_days"] or 0) if analytics else 0.0
        confirmed_revenue = float(analytics["confirmed_revenue_last_30_days"] or 0) if analytics else 0.0
        
        # Order Generation Rate: (orders created / total attempts) * 100
        # For now, assume total_orders = attempts
        order_generation_rate = 100.0 if total_orders > 0 else 0.0
        
        # Order Placement Rate: same as generation (simplified)
        order_placement_rate = 100.0 if total_orders > 0 else 0.0
        
        # Payment Success Rate: (paid orders / total orders) * 100
        payment_success_rate = round((paid_orders / total_orders * 100), 1) if total_orders > 0 else 0.0

        average_order_value = round((confirmed_revenue / paid_orders), 2) if paid_orders > 0 else 0.0
        
        # Format response
        data = {
            "total_orders": total_orders,
            "total_revenue": confirmed_revenue,
            "total_customers": total_customers,
            "customer_breakdown": {
                "last_30_days": total_customers,
                "all_time": total_customers_all_time,
            },
            "all_time_customers": total_customers_all_time,
            "average_order_value": average_order_value,
            "order_growth": round(order_growth, 1),
            "revenue_growth": round(revenue_growth, 1),
            "gmv_growth": round(gmv_growth, 1),
            "recent_orders": recent_orders,
            "conversion_rate": payment_success_rate,
            # Analytics page specific fields
            "order_generation_rate": order_generation_rate,
            "total_order_attempts": total_orders,
            "order_placement_rate": order_placement_rate,
            "total_orders_placed": total_orders,
            "payment_success_rate": payment_success_rate,
            "total_payments_succeeded": paid_orders,
            # Explicit breakdowns (avoid ambiguous "total_revenue" semantics)
            "order_breakdown": {
                "total": total_orders,
                "paid": paid_orders,
                "all_time_total": total_orders_all_time,
                "all_time_paid": paid_orders_all_time,
            },
            "revenue_breakdown": {
                "confirmed": confirmed_revenue,
                "gmv": gmv,
                "all_time_confirmed": confirmed_revenue_all_time,
                "all_time_gmv": gmv_all_time,
            },
            "confirmed_revenue": confirmed_revenue,
            "gmv": gmv,
        }
        
        # Get actual product count from products_cache (only non-expired)
        products_query = """
            SELECT COUNT(*) as count 
            FROM products_cache 
            WHERE merchant_id = :merchant_id 
            AND (expires_at IS NULL OR expires_at > NOW())
        """
        products_count = await database.fetch_one(products_query, {"merchant_id": merchant_id})
        data["total_products"] = products_count["count"] if products_count else 0
        
        return {
            "status": "success",
            "data": data
        }
        
    except asyncio.CancelledError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except HTTPException:
        raise
    except BaseException as e:
        logger.error(f"Error fetching analytics for {merchant_id}: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        # Try to at least get product count even if analytics query failed
        try:
            products_query = """
                SELECT COUNT(*) as count 
                FROM products_cache 
                WHERE merchant_id = :merchant_id 
                AND (expires_at IS NULL OR expires_at > NOW())
            """
            products_count = await database.fetch_one(products_query, {"merchant_id": merchant_id})
            total_products = products_count["count"] if products_count else 0
        except:
            total_products = 0
        
        # Return empty/zero stats instead of random data
        return {
            "status": "success",
            "data": {
                "total_orders": 0,
                "total_revenue": 0.0,
                "total_customers": 0,
                "total_products": total_products,  # At least get this right
                "average_order_value": 0.0,
                "order_growth": 0,
                "revenue_growth": 0,
                "recent_orders": [],
                "conversion_rate": 0,
                "error": str(e)  # Include error for debugging
            }
        }

@router.get("/merchant/api-credentials")
async def get_merchant_api_credentials(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = await _resolve_merchant_id(current_user)
    merchant = await database.fetch_one(
        """
        SELECT merchant_id, api_key, api_key_hash, updated_at, created_at
        FROM merchant_onboarding
        WHERE merchant_id = :merchant_id
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    merchant_row = dict(merchant)
    api_key = str(merchant_row.get("api_key") or "").strip()
    issued_at = merchant_row.get("updated_at") or merchant_row.get("created_at")
    return {
        "status": "success",
        "data": {
            "merchant_id": merchant_id,
            "issued": bool(api_key),
            "api_key": api_key or None,
            "api_key_last4": api_key[-4:] if api_key else None,
            "header_name": "X-Merchant-API-Key",
            "sample_endpoint": "/payment/execute",
            "issued_at": issued_at.isoformat() if hasattr(issued_at, "isoformat") else None,
        },
    }


@router.post("/merchant/api-credentials/rotate")
async def rotate_merchant_api_credentials(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = await _resolve_merchant_id(current_user)
    merchant = await database.fetch_one(
        """
        SELECT merchant_id
        FROM merchant_onboarding
        WHERE merchant_id = :merchant_id
        LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    if not merchant:
        raise HTTPException(status_code=404, detail="Merchant not found")

    api_key, api_key_hash = _generate_merchant_api_key()
    issued_at = datetime.utcnow()
    await database.execute(
        """
        UPDATE merchant_onboarding
        SET api_key = :api_key,
            api_key_hash = :api_key_hash,
            updated_at = :updated_at
        WHERE merchant_id = :merchant_id
        """,
        {
            "merchant_id": merchant_id,
            "api_key": api_key,
            "api_key_hash": api_key_hash,
            "updated_at": issued_at,
        },
    )
    return {
        "status": "success",
        "data": {
            "merchant_id": merchant_id,
            "issued": True,
            "api_key": api_key,
            "api_key_last4": api_key[-4:],
            "header_name": "X-Merchant-API-Key",
            "sample_endpoint": "/payment/execute",
            "issued_at": issued_at.isoformat() + "Z",
        },
    }


@router.get("/merchant/webhooks/events/catalog")
async def get_merchant_webhook_events_catalog(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")
    return list_merchant_webhook_events_catalog()


@router.get("/merchant/webhooks/config")
async def get_webhook_config(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = await _resolve_merchant_id(current_user)
    try:
        config = await get_merchant_webhook_config(merchant_id)
        return {
            "status": "success",
            "data": config,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch webhook config: {exc}")


@router.put("/merchant/webhooks/config")
async def put_webhook_config(
    payload: MerchantWebhookConfigRequest,
    current_user: dict = Depends(get_current_user),
):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = await _resolve_merchant_id(current_user)
    try:
        config = await update_merchant_webhook_config(
            merchant_id,
            enabled=payload.enabled,
            destination_url=payload.url,
            subscribed_events=payload.events,
        )
        return {
            "status": "success",
            "data": config,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save webhook config: {exc}")


@router.get("/merchant/webhooks/secret")
async def get_webhook_secret(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = await _resolve_merchant_id(current_user)
    try:
        return await get_merchant_webhook_signing_secret(merchant_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load webhook secret: {exc}")


@router.post("/merchant/webhooks/secret/rotate")
async def rotate_webhook_secret(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = await _resolve_merchant_id(current_user)
    try:
        return await rotate_merchant_webhook_signing_secret(merchant_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to rotate webhook secret: {exc}")


@router.get("/merchant/webhooks/deliveries")
async def get_webhook_deliveries(
    limit: int = Query(default=20, ge=1, le=100),
    status: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = await _resolve_merchant_id(current_user)
    try:
        deliveries = await list_merchant_webhook_deliveries(merchant_id, limit=limit, status=status)
        return {
            "status": "success",
            "data": deliveries,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load webhook deliveries: {exc}")


@router.post("/merchant/webhooks/test")
async def test_webhook(
    payload: MerchantWebhookTestRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    if current_user["role"] != "merchant":
        raise HTTPException(status_code=403, detail="Merchant access only")

    merchant_id = await _resolve_merchant_id(current_user)
    try:
        delivery = await send_merchant_test_webhook(
            merchant_id,
            event_type=payload.event_type or "order.created",
            request_id=request.headers.get("x-request-id"),
        )
        return {
            "status": "success",
            "data": delivery,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to send test webhook: {exc}")

@router.post("/merchant/psp/{psp_id}/test")
async def test_psp_connection(
    psp_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Test PSP connection with real API call"""
    if current_user["role"] not in ["merchant", "admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Get PSP details from database
    psp_query = """
        SELECT provider, api_key, secret_key, account_id, merchant_id, status,
               environment, provider_config, validation_status, validation_error
        FROM merchant_psps 
        WHERE psp_id = :psp_id
    """
    psp = await database.fetch_one(psp_query, {"psp_id": psp_id})
    
    if not psp:
        raise HTTPException(status_code=404, detail="PSP not found")
    
    psp_row = dict(psp)
    provider = psp_row["provider"]
    api_key = psp_row["api_key"]
    readiness_before = evaluate_psp_readiness(
        provider,
        status=psp_row.get("status"),
        api_key=api_key,
        account_id=psp_row.get("account_id"),
        provider_config=psp_row.get("provider_config"),
        environment=psp_row.get("environment"),
        validation_status=psp_row.get("validation_status"),
        validation_error=psp_row.get("validation_error"),
    )
    provider_summary = readiness_before["provider_summary"]
    
    # Check if API key is configured
    if not api_key or api_key == "pending_setup":
        return {
            "status": "error",
            "message": f"PSP not configured yet. Please add API credentials for {provider}.",
            "data": {
                "provider": provider,
                "configured": False,
                "live_charge_ready": False,
                "readiness_blockers": readiness_before["readiness_blockers"],
            }
        }
    
    # Test actual PSP connection
    try:
        success = False
        validation_message = ""
        provider_config_for_persist = psp_row.get("provider_config")

        if provider == "stripe":
            import stripe as stripe_sdk

            stripe_mode = str(provider_summary.get("mode") or "payment_intent").strip().lower()
            if stripe_mode == "payment_intent" and not provider_summary.get("public_key_present"):
                raise ValueError("Stripe public key is missing")
            stripe_sdk.api_key = api_key
            account_id = provider_summary.get("account_id")
            if account_id:
                stripe_sdk.Balance.retrieve(stripe_account=account_id)
            else:
                stripe_sdk.Balance.retrieve()
            if provider_summary.get("environment") == "live":
                raw_provider_config = psp_row.get("provider_config")
                stripe_provider_config: Dict[str, Any] = {}
                if isinstance(raw_provider_config, dict):
                    stripe_provider_config = dict(raw_provider_config)
                elif isinstance(raw_provider_config, str):
                    try:
                        parsed_provider_config = json.loads(raw_provider_config)
                        if isinstance(parsed_provider_config, dict):
                            stripe_provider_config = dict(parsed_provider_config)
                    except Exception:
                        stripe_provider_config = {}
                provider_config_for_persist, created_endpoint = await _ensure_stripe_webhook_endpoint(
                    psp_id=psp_id,
                    api_key=api_key,
                    provider_config=stripe_provider_config,
                    account_id=account_id,
                    environment=provider_summary.get("environment") or "live",
                    merchant_id=psp_row.get("merchant_id"),
                )
                validation_message = (
                    "Stripe credentials verified and webhook endpoint provisioned"
                    if created_endpoint
                    else "Stripe credentials verified and webhook endpoint confirmed"
                )
            else:
                validation_message = "Stripe credentials verified"
            success = True

        elif provider == "adyen":
            merchant_account = provider_summary.get("merchant_account")
            if not merchant_account:
                raise ValueError("Adyen merchant account is missing")
            if not provider_summary.get("client_key_present"):
                raise ValueError("Adyen client key is missing")
            test_url = (
                "https://checkout-live.adyen.com/v70/paymentMethods"
                if provider_summary.get("environment") == "live"
                else "https://checkout-test.adyen.com/v70/paymentMethods"
            )
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    test_url,
                    headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                    json={"merchantAccount": merchant_account},
                    timeout=10.0,
                )
            if response.status_code != 200:
                raise ValueError(f"Adyen validation failed: {response.status_code} {response.text[:240]}")
            success = True
            validation_message = "Adyen credentials verified"

        elif provider == "checkout":
            processing_channel = provider_summary.get("processing_channel_id")
            if not processing_channel:
                raise ValueError("Checkout.com processing channel ID is missing")
            if not provider_summary.get("public_key_present"):
                raise ValueError("Checkout.com public key is missing")
            checkout_adapter = get_psp_adapter(
                provider,
                api_key,
                **build_runtime_adapter_kwargs(
                    provider,
                    api_key=api_key,
                    account_id=psp_row.get("account_id"),
                    provider_config=psp_row.get("provider_config"),
                    environment=provider_summary.get("environment"),
                    secret_key=psp_row.get("secret_key"),
                ),
            )
            checkout_success, checkout_intent, checkout_error = await checkout_adapter.create_payment_intent(
                amount=Decimal("0.01"),
                currency="USD",
                metadata={
                    "order_id": f"checkout_validation_{psp_id}",
                    "source": "merchant_psp_validation",
                    "merchant_id": psp_row.get("merchant_id"),
                    "customer_email": current_user.get("email"),
                },
            )
            if not checkout_success or not checkout_intent:
                raise ValueError(checkout_error or "Checkout.com validation failed")
            success = True
            validation_message = "Checkout.com credentials verified"

        else:
            return {
                "status": "warning",
                "message": f"{provider.capitalize()} is not in the wave-1 PSP validation scope.",
                "data": {
                    "psp_id": psp_id,
                    "provider": provider,
                    "configured": bool(api_key and api_key != "pending_setup"),
                    "tested_at": datetime.now().isoformat() + "Z",
                },
            }

        persisted_record = build_provider_connect_record(
            provider,
            api_key=api_key,
            account_id=psp_row.get("account_id"),
            provider_config=provider_config_for_persist,
            environment=provider_summary.get("environment"),
            validation_status="valid" if success else "invalid",
            validation_error=None,
        )
        await database.execute(
            """
            UPDATE merchant_psps
            SET environment = :environment,
                provider_config = CAST(:provider_config AS JSONB),
                validation_status = :validation_status,
                validation_error = NULL,
                last_validated_at = NOW()
            WHERE psp_id = :psp_id
            """,
            {
                "psp_id": psp_id,
                "environment": persisted_record["environment"],
                "provider_config": json.dumps(persisted_record["provider_config"]),
                "validation_status": persisted_record["validation_status"],
            },
        )

        readiness = evaluate_psp_readiness(
            provider,
            status=psp_row.get("status"),
            api_key=api_key,
            account_id=psp_row.get("account_id"),
            provider_config=persisted_record["provider_config"],
            environment=persisted_record["environment"],
            validation_status=persisted_record["validation_status"],
            validation_error=persisted_record["validation_error"],
        )

        return {
            "status": "success",
            "message": (
                validation_message
                if readiness["live_charge_ready"]
                else f"{validation_message}, but the processor is still blocked for live charge"
            ),
            "data": {
                "psp_id": psp_id,
                "provider": provider,
                "tested_at": datetime.now().isoformat() + "Z",
                "configured": True,
                "environment": readiness["environment"],
                "validation_status": readiness["validation_status"],
                "live_charge_ready": readiness["live_charge_ready"],
                "readiness_blockers": readiness["readiness_blockers"],
            }
        }
        
    except Exception as e:
        print(f"❌ PSP test error: {e}")
        error_text = str(e)[:500]
        persisted_record = build_provider_connect_record(
            provider,
            api_key=api_key,
            account_id=psp_row.get("account_id"),
            provider_config=provider_config_for_persist,
            environment=provider_summary.get("environment"),
            validation_status="invalid",
            validation_error=error_text,
        )
        await database.execute(
            """
            UPDATE merchant_psps
            SET environment = :environment,
                provider_config = CAST(:provider_config AS JSONB),
                validation_status = :validation_status,
                validation_error = :validation_error,
                last_validated_at = NOW()
            WHERE psp_id = :psp_id
            """,
            {
                "psp_id": psp_id,
                "environment": persisted_record["environment"],
                "provider_config": json.dumps(persisted_record["provider_config"]),
                "validation_status": persisted_record["validation_status"],
                "validation_error": error_text,
            },
        )
        readiness = evaluate_psp_readiness(
            provider,
            status=psp_row.get("status"),
            api_key=api_key,
            account_id=psp_row.get("account_id"),
            provider_config=persisted_record["provider_config"],
            environment=persisted_record["environment"],
            validation_status=persisted_record["validation_status"],
            validation_error=persisted_record["validation_error"],
        )
        return {
            "status": "error",
            "message": f"Failed to test {provider}: {error_text}",
            "data": {
                "psp_id": psp_id,
                "provider": provider,
                "error": error_text,
                "live_charge_ready": readiness["live_charge_ready"],
                "readiness_blockers": readiness["readiness_blockers"],
            }
        }
