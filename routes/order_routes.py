"""
订单处理 API 路由
Pivota 核心业务流程：Agent 下单 → 支付 → 履约
"""

from services.merchant_store_service import get_merchant_active_stores, get_primary_store, get_store_by_id
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Response, Header, Query, status
from typing import Optional, List, Dict, Any, Tuple
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import asyncio
import time
import hashlib
import httpx
import os
import json
import re
from contextlib import asynccontextmanager
from sqlalchemy import and_, or_, select, text
from config.feature_flags import is_feature_enabled

from models.order import (
    CreateOrderRequest, OrderResponse, PaymentConfirmRequest, 
    OrderListResponse, OrderItem, OrderStatus
)
from db.orders import (
    create_order, get_order, get_orders_by_merchant, get_orders_by_customer,
    update_order_status, update_payment_info, mark_order_paid, 
    update_fulfillment_info, mark_order_shipped, get_order_stats, update_order as update_order_row
)
from db.orders import orders as orders_table
from db.merchant_onboarding import get_merchant_onboarding
from db.products import log_order_event
from db.database import database, IS_POSTGRES
from utils.auth import require_admin, require_admin_or_key, get_current_user
from adapters.psp_adapter import get_psp_adapter
from adapters.multi_psp_orchestrator import create_payment_with_failover
from utils.logger import logger
from services.payment_routing_service import PaymentRoutingService
from services.merchant_payment_initiation_service import build_payment_action
from services.payment_offer_evidence_service import emit_payment_offer_analytics_event, stable_payment_offer_hash
from services.merchant_psp_config_service import (
    build_runtime_adapter_kwargs,
    evaluate_psp_readiness,
    fetch_active_merchant_psps,
    fetch_active_runtime_merchant_psp,
    infer_runtime_provider,
)
from services.merchant_capability_gate import (
    capability_gate_permits_order_create,
)
from services.commerce_attribution_service import (
    PVT_CLICK_ID,
    PVT_PRODUCT_ID,
    PVT_PROMPT_CLUSTER,
    PVT_SURFACE,
    PVT_VARIANT_ID,
    has_attribution_signal,
    materialize_attribution_context,
    upsert_order_attribution_edge,
)
from services.quote_service import (
    QuoteError,
    QuoteSnapshot,
    QuoteService,
    compute_request_fingerprint,
    normalize_discount_codes,
    normalize_items_for_fingerprint,
    normalize_shipping_for_fingerprint,
    parse_decimal_money,
)
from services.commerce_execution_policy import (
    COMMERCE_PATH_PIVOTA_DIRECT_QUOTE_FIRST,
    SURFACE_LEGACY_ADMIN,
    SURFACE_PUBLIC_AGENT_PURCHASE,
    resolve_commerce_execution_policy,
)
from services.shopify_transactions_service import (
    annotate_shopify_order_best_effort,
    extract_shopify_access_token,
    ensure_external_payment_transaction_best_effort,
    list_shopify_order_transactions,
)
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.merchant_webhook_service import emit_merchant_webhook_event
from services.webhook_service import WebhookService
from adapters.woocommerce_adapter import normalize_woocommerce_store_url
from adapters.bigcommerce_adapter import (
    build_bigcommerce_domain,
    build_bigcommerce_headers,
    normalize_bigcommerce_store_hash,
)
from adapters.wix_adapter import (
    create_wix_order as create_wix_order_via_adapter,
)
from services.platform_order_writeback_readiness import (
    is_store_order_writeback_allowed,
    store_order_writeback_context,
)
from services.traffic_taxonomy_service import attach_traffic_taxonomy, build_traffic_taxonomy
from routes.reviews_invitation_issuer import (
    SendInvitationEmailFromOrderRequest,
    send_invitation_email_from_order,
    _internal_key as _reviews_invitation_internal_key,
    _invitation_send_delay_seconds as _reviews_invitation_send_delay_seconds,
    enqueue_invitation_email_send_job_from_order,
)
from services.reviews_invitation_send_jobs_service import (
    enqueue_invitation_send_job_from_order as enqueue_reviews_invitation_send_job_from_order,
)
from utils.database_readiness import (
    DatabaseUnavailableError,
    database_unavailable_http_exception,
    ensure_database_ready,
)


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _checkout_ui_base() -> str:
    return (os.getenv("CHECKOUT_UI_BASE_URL") or "https://agent.pivota.cc").rstrip("/")


def _build_order_payment_return_url(
    order_id: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    metadata_dict = metadata if isinstance(metadata, dict) else {}
    explicit = _clean_text(
        metadata_dict.get("payment_return_url")
        or metadata_dict.get("paymentReturnUrl")
        or metadata_dict.get("return_url")
        or metadata_dict.get("returnUrl")
    )
    if explicit:
        return explicit.replace("{order_id}", str(order_id))

    return f"{_checkout_ui_base()}/order/success?{urlencode({'orderId': str(order_id), 'finalizing': '1'})}"


def _platform_checkout_fallback_enabled() -> bool:
    return str(os.getenv("ORDER_PLATFORM_CHECKOUT_FALLBACK_ENABLED", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _order_commerce_path(metadata: Optional[Dict[str, Any]]) -> str:
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("commerce_path") or "").strip().lower()


def _is_pivota_direct_quote_first_order(metadata: Optional[Dict[str, Any]]) -> bool:
    return _order_commerce_path(metadata) == COMMERCE_PATH_PIVOTA_DIRECT_QUOTE_FIRST


def _order_allows_platform_checkout_fallback(metadata: Optional[Dict[str, Any]]) -> bool:
    return _platform_checkout_fallback_enabled() and not _is_pivota_direct_quote_first_order(metadata)


def _order_defers_payment_surface(metadata: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(metadata, dict):
        return False
    agent_v2 = metadata.get("agent_v2")
    if not isinstance(agent_v2, dict):
        return False
    provider = str(agent_v2.get("checkout_provider") or "").strip().lower()
    hosted_checkout = agent_v2.get("hosted_checkout") is True
    return provider == "pivota_hosted_checkout" or hosted_checkout


async def _log_fallback_pollution_attempt_best_effort(
    *,
    order_id: str,
    merchant_id: str,
    total: Decimal,
    currency: str,
    metadata: Optional[Dict[str, Any]],
    reason: str,
    source: str,
) -> None:
    if not _platform_checkout_fallback_enabled() or not _is_pivota_direct_quote_first_order(metadata):
        return
    try:
        await log_order_event(
            event_type="fallback_pollution_attempt",
            order_id=order_id,
            merchant_id=merchant_id,
            total_amount=float(total),
            currency=currency,
            metadata={
                "commerce_path": _order_commerce_path(metadata),
                "validation_authority": (metadata or {}).get("validation_authority") if isinstance(metadata, dict) else None,
                "execution_policy_version": (metadata or {}).get("execution_policy_version") if isinstance(metadata, dict) else None,
                "reason": reason,
                "source": source,
            },
        )
    except Exception:
        pass


def _order_live_quote_revalidation_enabled() -> bool:
    return str(os.getenv("ORDER_CREATE_LIVE_QUOTE_REVALIDATION_ENABLED", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


FRESH_QUOTE_VALIDATE_SKIP_SECONDS = int(os.getenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS", "30"))


def _fresh_quote_validate_skip_seconds() -> int:
    raw = os.getenv("FRESH_QUOTE_VALIDATE_SKIP_SECONDS")
    if raw is None:
        return max(0, FRESH_QUOTE_VALIDATE_SKIP_SECONDS)
    try:
        return max(0, int(raw))
    except Exception:
        return max(0, FRESH_QUOTE_VALIDATE_SKIP_SECONDS)


def _coerce_utc_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _quote_order_items_unchanged(
    *,
    quote_request_json: Any,
    order_items_for_fingerprint: List[Dict[str, Any]],
) -> bool:
    if not isinstance(quote_request_json, dict):
        return False
    try:
        quote_items = normalize_items_for_fingerprint(quote_request_json.get("items") or [])
        order_items = normalize_items_for_fingerprint(order_items_for_fingerprint)
        return quote_items == order_items
    except Exception:
        return False


async def _bg_log_order_event(*args, **kwargs) -> None:
    try:
        await log_order_event(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("background log_order_event failed: %s", str(exc)[:200])


async def _bg_emit_merchant_webhook_event(*args, **kwargs) -> None:
    try:
        await emit_merchant_webhook_event(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("background emit_merchant_webhook_event failed: %s", str(exc)[:200])


async def _bg_consume_quote_best_effort(
    quote_service: QuoteService,
    quote_id: str,
    *,
    order_id: str,
) -> None:
    try:
        await quote_service.consume_quote_best_effort(quote_id, order_id=order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("background consume_quote_best_effort failed: %s", str(exc)[:200])


router = APIRouter(prefix="/orders", tags=["orders"])
_PG_SHOPIFY_LOCK_SUPPORTED: Optional[bool] = None
_SUPPORTED_ORDER_PROVIDER_HINTS = {"stripe", "adyen", "checkout", "paypal"}


def _shopify_order_lock_key(order_id: str) -> int:
    digest = hashlib.sha256(f"shopify_order:{order_id}".encode("utf-8")).hexdigest()
    # Keep within signed int64 range for pg advisory lock.
    return int(digest[:16], 16) & 0x7FFFFFFFFFFFFFFF


async def _try_acquire_shopify_order_lock(order_id: str) -> Tuple[bool, Optional[int]]:
    global _PG_SHOPIFY_LOCK_SUPPORTED

    if _PG_SHOPIFY_LOCK_SUPPORTED is False:
        return True, None

    lock_key = _shopify_order_lock_key(order_id)
    try:
        row = await database.fetch_one(
            "SELECT pg_try_advisory_lock(:lock_key) AS locked",
            {"lock_key": lock_key},
        )
        _PG_SHOPIFY_LOCK_SUPPORTED = True
        locked = False
        if row is not None:
            try:
                locked = bool(row["locked"])
            except Exception:
                locked = bool(getattr(row, "locked", False))
        return locked, lock_key
    except Exception:
        _PG_SHOPIFY_LOCK_SUPPORTED = False
        return True, None


async def _release_shopify_order_lock(lock_key: Optional[int], *, lock_acquired: bool) -> None:
    if not lock_acquired or lock_key is None or _PG_SHOPIFY_LOCK_SUPPORTED is not True:
        return
    try:
        await database.execute(
            "SELECT pg_advisory_unlock(:lock_key)",
            {"lock_key": lock_key},
        )
    except Exception:
        pass


def _normalize_order_provider_hint(
    selected_psp: Optional[str], preferred_psp: Optional[str]
) -> Optional[str]:
    for candidate in (preferred_psp, selected_psp):
        provider = str(candidate or "").strip().lower()
        if provider in _SUPPORTED_ORDER_PROVIDER_HINTS:
            return provider
    return None


def _decimal_str(value: Decimal) -> str:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).to_eng_string()


def _quote_line_item_key(product_id: Any, variant_id: Any) -> Tuple[str, str]:
    return (str(product_id or "").strip(), str(variant_id or "").strip())


def _build_persisted_order_items(
    order_items: List[OrderItem],
    pricing_quote_meta: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    request_items_payload = [json.loads(item.json()) for item in (order_items or [])]
    if not isinstance(pricing_quote_meta, dict):
        return request_items_payload

    raw_line_items = pricing_quote_meta.get("line_items")
    if not isinstance(raw_line_items, list) or not raw_line_items:
        return request_items_payload

    quote_line_items: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in raw_line_items:
        if not isinstance(row, dict):
            continue
        quote_line_items[_quote_line_item_key(row.get("product_id"), row.get("variant_id"))] = row

    persisted_items: List[Dict[str, Any]] = []
    for item in request_items_payload:
        row = quote_line_items.get(_quote_line_item_key(item.get("product_id"), item.get("variant_id")))
        if not row:
            persisted_items.append(item)
            continue

        try:
            quantity = int(item.get("quantity") or row.get("quantity") or 1)
        except Exception:
            quantity = 1
        quantity = max(1, quantity)

        effective_unit_price = parse_decimal_money(
            row.get("unit_price_effective")
            or row.get("price")
            or row.get("unit_price_original")
            or 0
        )
        line_subtotal = parse_decimal_money(row.get("line_subtotal") or row.get("subtotal") or 0)
        if line_subtotal <= 0 and effective_unit_price > 0:
            line_subtotal = effective_unit_price * Decimal(quantity)

        enriched = dict(item)
        if row.get("title") and not enriched.get("product_title"):
            enriched["product_title"] = str(row.get("title"))
        if row.get("sku") and not enriched.get("sku"):
            enriched["sku"] = str(row.get("sku"))
        if effective_unit_price > 0:
            enriched["unit_price"] = _decimal_str(effective_unit_price)
        if line_subtotal > 0:
            enriched["subtotal"] = _decimal_str(line_subtotal)

        persisted_items.append(enriched)

    return persisted_items


def _build_order_preferred_psps(
    route_config: Optional[Dict[str, Any]],
    preferred_psp: Optional[str],
) -> Optional[List[str]]:
    providers: List[str] = []

    explicit_provider = _normalize_order_provider_hint(None, preferred_psp)
    if explicit_provider:
        # Explicit caller choice is a strict subset, not a soft hint. This prevents
        # silent provider fallbacks that mask PSP-selection bugs and makes quote-first
        # retries deterministic when callers intentionally compare PSP surfaces.
        return [explicit_provider]

    raw_priority = route_config.get("psp_priority") if isinstance(route_config, dict) else []
    if isinstance(raw_priority, str):
        try:
            raw_priority = json.loads(raw_priority)
        except Exception:
            raw_priority = []

    if isinstance(raw_priority, list):
        for entry in sorted(raw_priority, key=lambda item: (item or {}).get("priority", 999)):
            provider = str((entry or {}).get("psp") or "").strip().lower()
            if provider and provider not in providers:
                providers.append(provider)

    return providers or None


async def _ensure_explicit_preferred_psp_available(
    *,
    merchant_id: str,
    preferred_psp: Optional[str],
    enforce_live_readiness: bool,
) -> Optional[str]:
    explicit_provider = _normalize_order_provider_hint(None, preferred_psp)
    if not explicit_provider:
        return None

    try:
        rows = await fetch_active_merchant_psps(
            merchant_id=merchant_id,
            provider=explicit_provider,
        )
    except Exception as exc:
        logger.warning(
            "[OrderRoutes] Failed to load canonical PSP configs for explicit preferred_psp %s: %s",
            explicit_provider,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "PREFERRED_PSP_UNAVAILABLE",
                "message": f"Unable to verify preferred PSP '{explicit_provider}' right now.",
                "preferred_psp": explicit_provider,
            },
        )

    if not rows:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "PREFERRED_PSP_UNAVAILABLE",
                "message": f"Preferred PSP '{explicit_provider}' is not active for this merchant.",
                "preferred_psp": explicit_provider,
            },
        )

    if not enforce_live_readiness:
        return explicit_provider

    readiness = evaluate_psp_readiness(
        explicit_provider,
        status=rows[0].get("status"),
        api_key=rows[0].get("api_key"),
        account_id=rows[0].get("account_id"),
        provider_config=rows[0].get("provider_config"),
        environment=rows[0].get("environment"),
        validation_status=rows[0].get("validation_status"),
        validation_error=rows[0].get("validation_error"),
    )
    if readiness.get("live_charge_ready"):
        return explicit_provider

    raise HTTPException(
        status_code=409,
        detail={
            "error": "PREFERRED_PSP_UNAVAILABLE",
            "message": (
                f"Preferred PSP '{explicit_provider}' is not available under the current live-readiness policy."
            ),
            "preferred_psp": explicit_provider,
            "readiness_blockers": readiness.get("readiness_blockers") or [],
        },
    )


def _finalize_order_psp_used(psp_used: Optional[str], fallback_provider: Optional[str]) -> str:
    value = str(psp_used or fallback_provider or "unknown").strip().lower()
    return value or "unknown"


def _coerce_metadata_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    token = str(value or "").strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _test_psp_probe_enabled() -> bool:
    """Server-side master switch (default OFF) for the scoped test-processor probe override."""
    return str(os.getenv("ALLOW_TEST_PSP_PROBE", "")).strip().lower() in {"1", "true", "yes", "on"}


def _test_psp_probe_merchants() -> set:
    """Allowlist of merchant_ids permitted to bypass live-readiness (test-mode probe). Comma-separated env."""
    raw = os.getenv("TEST_PSP_PROBE_MERCHANTS", "") or ""
    return {m.strip().lower() for m in raw.split(",") if m.strip()}


async def _merchant_active_psp_is_test_mode(merchant_id: Optional[str]) -> bool:
    """True only when EVERY active processor for this merchant is demonstrably TEST-mode.

    Fails CLOSED: no rows, one live-looking row, or any error all return False. This is what makes
    allowlisting a merchant safe — a merchant that somehow has a live processor cannot be granted a
    test-mode bypass even if someone puts it in TEST_PSP_PROBE_MERCHANTS by mistake.
    """
    merchant = str(merchant_id or "").strip()
    if not merchant:
        return False
    try:
        from services.merchant_psp_config_service import fetch_active_merchant_psps

        rows = await fetch_active_merchant_psps(merchant_id=merchant)
    except Exception:
        return False
    if not rows:
        return False
    for row in rows:
        record = row if isinstance(row, dict) else {}
        environment = str(record.get("environment") or "").strip().lower()
        key = str(
            record.get("runtime_secret_key")
            or record.get("secret_key")
            or record.get("api_key")
            or ""
        ).strip()
        # A LIVE-looking key refuses the row outright, whatever the `environment` column claims.
        # environment is a label someone types; the key is what actually charges a card, and a row
        # mislabelled test while holding sk_live_ is exactly the shape this guard exists to stop.
        if key.startswith(("sk_live_", "rk_live_")):
            return False
        looks_test = environment in {"test", "sandbox"} or key.startswith(("sk_test_", "rk_test_"))
        if not looks_test:
            return False
    return True


async def _apply_server_granted_test_psp_stamp(
    metadata: Optional[Dict[str, Any]], merchant_id: Optional[str]
) -> bool:
    """Stamp an allowlisted probe merchant's order server-side, so the bypass no longer depends on
    the CALLER remembering a URL parameter.

    Why: a TEST processor is refused unless the order carries `allow_test_psp_surfaces`, and the
    checkout page only sets that from a URL parameter. A buyer arriving through PDP -> add to bag
    -> cart -> Checkout carries none, so payment always died "All PSPs blocked: stripe: Processor
    is configured for test, not live" on a merchant explicitly allowlisted for the probe (observed
    in production 2026-08-29: ORD_9F4C24E73705231D unstamped failed, ORD_50C00A24BEADFA78 stamped
    paid — same merchant, same env).

    This does not widen containment. The stamp was never a secret: order metadata is caller-supplied
    and forwarded verbatim, so any caller could already set it for any merchant, and the gate has
    always ignored it unless {ALLOW_TEST_PSP_PROBE on} + {merchant allowlisted}. What changes is
    that the server now writes the stamp itself, gated additionally on the merchant's processors
    ACTUALLY being test-mode — a guard the caller-supplied stamp never had.

    Writing the stamp (rather than special-casing the gate) keeps every downstream reader —
    `_resolve_order_live_readiness_requirement`, the payment SDK, and the Stripe webhook livemode
    exemption — working on exactly the semantics they were reviewed under.
    """
    if not isinstance(metadata, dict):
        return False
    # An explicit request to ENFORCE live readiness is the stricter choice and always wins.
    if _coerce_metadata_bool(metadata.get("enforce_live_readiness")) is True:
        return False
    # Already permitted via a caller stamp — nothing to add.
    if _resolve_order_live_readiness_requirement(metadata, merchant_id) is False:
        return False
    if not _test_psp_probe_enabled():
        return False
    if str(merchant_id or "").strip().lower() not in _test_psp_probe_merchants():
        return False
    if not await _merchant_active_psp_is_test_mode(merchant_id):
        return False
    metadata["allow_test_psp_surfaces"] = True
    metadata["test_psp_surfaces_granted_by"] = "server_allowlist"
    return True


def _resolve_order_live_readiness_requirement(
    metadata: Optional[Dict[str, Any]], merchant_id: Optional[str] = None
) -> bool:
    metadata_dict = metadata if isinstance(metadata, dict) else {}
    explicit = _coerce_metadata_bool(metadata_dict.get("enforce_live_readiness"))
    # An explicit request to ENFORCE live readiness (the stricter choice) is always honored.
    if explicit is True:
        return True
    # A request to BYPASS live readiness (run a TEST processor) — via enforce_live_readiness=false or
    # allow_test_psp_surfaces=true — is honored ONLY for an explicitly allowlisted merchant while the
    # server-side probe flag is ON (default OFF). Order metadata is set by EXTERNAL callers (the agent
    # gateway forwards order.metadata verbatim), so an ungated bypass would let ANY order route to a test
    # processor and be marked paid with no real charge (goods shipped unpaid). Scoping the bypass to
    # {ALLOW_TEST_PSP_PROBE on} + {merchant in TEST_PSP_PROBE_MERCHANTS} closes that hole while still
    # enabling a controlled test-mode charge for the probe merchant.
    wants_bypass = explicit is False
    if not wants_bypass:
        allow_test = _coerce_metadata_bool(
            metadata_dict.get("allow_test_psp_surfaces")
            or metadata_dict.get("test_psp_surfaces")
            or metadata_dict.get("allow_test_processors")
        )
        wants_bypass = allow_test is True
    if (
        wants_bypass
        and _test_psp_probe_enabled()
        and str(merchant_id or "").strip().lower() in _test_psp_probe_merchants()
    ):
        return False
    return True


# Sentinel PSP identity for a capability-gated, deferred-payment order that has NO
# merchant_psps row. It NEVER charges: the deferred path (see
# `defer_order_payment_surface`) skips create_payment_with_failover entirely, and
# submit_payment for such an order must route to the protocol/ACP lane (mapped seam,
# see docs/protocol_checkout_capability_canary_runbook.md). Kept as a recognizable,
# non-empty value so the downstream psp_type/psp_id validation passes without ever
# resolving a real PSP adapter.
CAPABILITY_DEFERRED_PSP_PROVIDER = "protocol_deferred"


def _capability_deferred_psp_id(merchant_id: str) -> str:
    return f"{str(merchant_id or '').strip()}:protocol_deferred"


async def _resolve_active_order_psp(
    merchant_id: str,
    provider_hint: Optional[str],
    *,
    defer_payment: bool = False,
) -> Tuple[str, str]:
    psp_row = await fetch_active_runtime_merchant_psp(
        merchant_id=merchant_id,
        provider=provider_hint,
    )

    if not psp_row:
        # Capability-gate bypass (Fix Plan A, option (ii)): a protocol-capable
        # merchant on the deferred-payment lane may create an order with no PSP row.
        # Fail-closed: with AGENT_CHECKOUT_CAPABILITY_GATE off (default) this branch
        # is never taken and the 400 below is byte-identical to today's behavior.
        if defer_payment and await capability_gate_permits_order_create(merchant_id):
            logger.info(
                "[OrderRoutes] Capability-gate: deferred order for protocol-capable "
                "merchant %s created without a merchant_psps row",
                merchant_id,
            )
            return (
                CAPABILITY_DEFERRED_PSP_PROVIDER,
                _capability_deferred_psp_id(merchant_id),
            )
        raise HTTPException(
            status_code=400,
            detail="No active PSP configuration found for this merchant",
        )

    provider = str(psp_row["provider"] or "").strip().lower()
    psp_id = str(psp_row["psp_id"] or "").strip()
    if not provider or not psp_id:
        raise HTTPException(
            status_code=500,
            detail="Active PSP configuration is incomplete for this merchant",
        )
    return provider, psp_id


async def _resolve_order_psp_adapter(order: Dict[str, Any]) -> Tuple[str, Any]:
    merchant_id = str(order.get("merchant_id") or "").strip()
    order_psp_id = str(order.get("psp_id") or "").strip()
    provider_hint = infer_runtime_provider(
        psp_used=order.get("psp_used"),
        psp_id=order_psp_id,
        payment_reference=order.get("payment_intent_id"),
    )
    psp_row = await fetch_active_runtime_merchant_psp(
        merchant_id=merchant_id,
        provider=provider_hint,
        psp_id=order_psp_id,
    )

    if not psp_row:
        raise ValueError("Canonical merchant_psps configuration is missing for this order")

    row_dict = dict(psp_row)
    provider = str(row_dict.get("provider") or "").strip().lower()
    api_key = str(row_dict.get("api_key") or "").strip()
    if not provider or not api_key:
        raise ValueError("Canonical merchant_psps configuration is incomplete for this order")

    adapter = get_psp_adapter(
        provider,
        api_key,
        **build_runtime_adapter_kwargs(
            provider,
            api_key=api_key,
            account_id=row_dict.get("account_id"),
            provider_config=row_dict.get("provider_config"),
            environment=row_dict.get("environment"),
            secret_key=row_dict.get("secret_key"),
        ),
    )
    return provider, adapter


_PSP_SUCCEEDED_STATUSES = {
    "succeeded",
    "paid",
    "completed",
    "success",
    "settled",
    "captured",
}

_PSP_AUTHORIZED_UNCAPTURED_STATUSES = {
    "requires_capture",
    "authorized",
    "authorised",
}


def _csv_env_contains(name: str, value: str) -> bool:
    raw = os.getenv(name, "") or ""
    values = {part.strip() for part in raw.split(",") if part.strip()}
    return "*" in values or value in values


def _authorization_first_feature_enabled_for_merchant(merchant_id: str) -> bool:
    if not is_feature_enabled("enable_authorization_first_orders"):
        return False
    allowlist = os.getenv("FF_AUTH_FIRST_MERCHANT_IDS", "") or ""
    if allowlist.strip():
        return _csv_env_contains("FF_AUTH_FIRST_MERCHANT_IDS", str(merchant_id or "").strip())
    return True


def _order_auth_first_flow_metadata(
    *,
    psp: str,
    store_platform: str,
) -> Dict[str, Any]:
    return {
        "mode": "authorization_first",
        "psp": str(psp or "").strip().lower(),
        "store_platform": str(store_platform or "").strip().lower(),
        "capture_method": "manual",
        "capture_after": "merchant_order_writeback",
        "inventory_strategy": "merchant_platform_order_write_before_capture",
        "void_on_merchant_order_failure": True,
        "enabled_at": datetime.utcnow().isoformat() + "Z",
    }


def _should_use_authorization_first_order_flow(
    *,
    merchant_id: str,
    psp_type: str,
    psp_mode: Optional[str],
    store_info: Optional[Dict[str, Any]],
) -> bool:
    if not _authorization_first_feature_enabled_for_merchant(merchant_id):
        return False
    normalized_psp = str(psp_type or "").strip().lower()
    if normalized_psp == "stripe":
        if not is_feature_enabled("enable_stripe_manual_capture"):
            return False
    elif normalized_psp == "paypal":
        if not is_feature_enabled("enable_paypal_authorization_first"):
            return False
    else:
        return False
    platform = str((store_info or {}).get("platform") or "").strip().lower()
    return platform == "shopify"


def order_uses_authorization_first_payment(order: Optional[Dict[str, Any]]) -> bool:
    metadata = _coerce_order_metadata(order or {})
    payment_flow = metadata.get("payment_flow") if isinstance(metadata.get("payment_flow"), dict) else {}
    return (
        str((payment_flow or {}).get("mode") or "").strip().lower() == "authorization_first"
        and str((payment_flow or {}).get("psp") or (order or {}).get("psp_used") or "").strip().lower()
        in {"stripe", "paypal"}
        and str((payment_flow or {}).get("store_platform") or "").strip().lower() == "shopify"
    )


def _order_payment_allows_merchant_order_write(order: Dict[str, Any], *, platform: str) -> bool:
    payment_status = str((order or {}).get("payment_status") or "").strip().lower()
    if payment_status == "paid":
        return True
    return (
        payment_status in {"authorized", "requires_capture"}
        and str(platform or "").strip().lower() == "shopify"
        and order_uses_authorization_first_payment(order)
    )


def _normalize_psp_status_result(result: Any) -> Tuple[bool, str, Optional[str]]:
    if isinstance(result, tuple):
        ok = bool(result[0]) if len(result) > 0 else False
        status = str(result[1] if len(result) > 1 else "unknown").strip().lower() or "unknown"
        error = result[2] if len(result) > 2 else None
        return ok, status, str(error) if error else None
    if isinstance(result, dict):
        ok = bool(result.get("success", True))
        status = str(result.get("status") or "unknown").strip().lower() or "unknown"
        error = result.get("error")
        return ok, status, str(error) if error else None
    return True, str(result or "unknown").strip().lower() or "unknown", None


def _normalize_psp_amount(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


async def _get_order_payment_status_details(psp_adapter: Any, payment_reference: str) -> Optional[Tuple[bool, Dict[str, Any], Optional[str]]]:
    details_fn = getattr(psp_adapter, "get_payment_status_details", None)
    if not callable(details_fn):
        return None
    result = await details_fn(payment_reference)
    if isinstance(result, tuple):
        ok = bool(result[0]) if len(result) > 0 else False
        details = result[1] if len(result) > 1 and isinstance(result[1], dict) else {}
        error = result[2] if len(result) > 2 else None
        return ok, details, str(error) if error else None
    if isinstance(result, dict):
        return bool(result.get("success", True)), result, str(result.get("error") or "") or None
    return False, {"status": "unknown"}, "Unexpected PSP status detail response"


def _psp_payment_verification_fail_closed() -> bool:
    mode = str(os.getenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "observe") or "").strip().lower()
    return mode == "fail_closed"


async def verify_order_payment_succeeded(order: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    """Verify the PSP payment state before any server-side paid transition."""
    payment_reference = str(order.get("payment_intent_id") or "").strip()
    if not payment_reference:
        return False, "missing_payment_reference", "Order has no PSP payment reference"

    psp_type, psp_adapter = await _resolve_order_psp_adapter(order)
    fail_closed = _psp_payment_verification_fail_closed()

    status_details = await _get_order_payment_status_details(psp_adapter, payment_reference)
    if status_details is not None:
        ok, details, error = status_details
        normalized_status = str(details.get("status") or "").strip().lower() or "unknown"
        if not ok:
            return False, normalized_status, error or f"{psp_type} status lookup failed"
        if normalized_status not in _PSP_SUCCEEDED_STATUSES:
            return False, normalized_status, None

        expected_amount = _normalize_psp_amount(order.get("total"))
        observed_amount = _normalize_psp_amount(details.get("amount"))
        expected_currency = str(order.get("currency") or "").strip().upper()
        observed_currency = str(details.get("currency") or "").strip().upper()
        if fail_closed and expected_amount is None:
            return False, normalized_status, "Order total is unavailable for fail-closed PSP verification"
        if expected_amount is not None and observed_amount is None:
            return False, normalized_status, "PSP payment amount is unavailable"
        if expected_amount is not None and observed_amount != expected_amount:
            return (
                False,
                normalized_status,
                f"PSP payment amount mismatch: expected {expected_amount} {expected_currency or 'UNKNOWN'}, got {observed_amount} {observed_currency or 'UNKNOWN'}",
            )
        if expected_currency and observed_currency and observed_currency != expected_currency:
            return (
                False,
                normalized_status,
                f"PSP payment currency mismatch: expected {expected_currency}, got {observed_currency}",
            )
        if expected_currency and not observed_currency:
            return False, normalized_status, "PSP payment currency is unavailable"
        if fail_closed and not expected_currency:
            return False, normalized_status, "Order currency is unavailable for fail-closed PSP verification"
        return True, normalized_status, None

    if fail_closed:
        return (
            False,
            "details_unavailable",
            f"{psp_type} does not provide PSP amount/currency details required by fail-closed verification",
        )

    ok, normalized_status, error = _normalize_psp_status_result(await psp_adapter.get_payment_status(payment_reference))
    if not ok:
        return False, normalized_status, error or f"{psp_type} status lookup failed"
    if normalized_status not in _PSP_SUCCEEDED_STATUSES:
        return False, normalized_status, None
    return True, normalized_status, None


async def verify_order_payment_succeeded_or_authorized(order: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    """
    Verify payment state for authorization-first flows.

    Captured payments are still accepted as succeeded. Uncaptured authorizations
    are accepted only for orders explicitly marked with `metadata.payment_flow`
    authorization-first, so normal capture-first flows cannot use an authorization
    as a paid transition.
    """
    payment_reference = str(order.get("payment_intent_id") or "").strip()
    if not payment_reference:
        return False, "missing_payment_reference", "Order has no PSP payment reference"

    psp_type, psp_adapter = await _resolve_order_psp_adapter(order)
    fail_closed = _psp_payment_verification_fail_closed()
    accepted_statuses = set(_PSP_SUCCEEDED_STATUSES)
    if order_uses_authorization_first_payment(order):
        accepted_statuses.update(_PSP_AUTHORIZED_UNCAPTURED_STATUSES)

    status_details = await _get_order_payment_status_details(psp_adapter, payment_reference)
    if status_details is not None:
        ok, details, error = status_details
        normalized_status = str(details.get("status") or "").strip().lower() or "unknown"
        if not ok:
            return False, normalized_status, error or f"{psp_type} status lookup failed"
        if normalized_status not in accepted_statuses:
            return False, normalized_status, None

        expected_amount = _normalize_psp_amount(order.get("total"))
        observed_amount = _normalize_psp_amount(details.get("amount"))
        expected_currency = str(order.get("currency") or "").strip().upper()
        observed_currency = str(details.get("currency") or "").strip().upper()
        if fail_closed and expected_amount is None:
            return False, normalized_status, "Order total is unavailable for fail-closed PSP verification"
        if expected_amount is not None and observed_amount is None:
            return False, normalized_status, "PSP payment amount is unavailable"
        if expected_amount is not None and observed_amount != expected_amount:
            return (
                False,
                normalized_status,
                f"PSP payment amount mismatch: expected {expected_amount} {expected_currency or 'UNKNOWN'}, got {observed_amount} {observed_currency or 'UNKNOWN'}",
            )
        if expected_currency and observed_currency and observed_currency != expected_currency:
            return False, normalized_status, f"PSP payment currency mismatch: expected {expected_currency}, got {observed_currency}"
        if expected_currency and not observed_currency:
            return False, normalized_status, "PSP payment currency is unavailable"
        if fail_closed and not expected_currency:
            return False, normalized_status, "Order currency is unavailable for fail-closed PSP verification"
        return True, normalized_status, None

    if fail_closed:
        return (
            False,
            "details_unavailable",
            f"{psp_type} does not provide PSP amount/currency details required by fail-closed verification",
        )

    ok, normalized_status, error = _normalize_psp_status_result(await psp_adapter.get_payment_status(payment_reference))
    if not ok:
        return False, normalized_status, error or f"{psp_type} status lookup failed"
    if normalized_status not in accepted_statuses:
        return False, normalized_status, None
    return True, normalized_status, None


async def finalize_authorized_payment_order(
    order_id: str,
    *,
    order: Optional[Dict[str, Any]] = None,
    source_event: str = "authorization_first_finalize",
) -> Dict[str, Any]:
    """
    For authorization-first orders, write the merchant/platform order before PSP capture.

    The only currently enabled direct path is Stripe PaymentIntent manual capture
    with Shopify order writeback. Other PSP/platform pairs remain capability-gated.
    """
    current_order = await get_order(order_id)
    if not current_order:
        current_order = order
    if not current_order:
        return {"status": "order_missing", "order_id": order_id, "captured": False}
    if str(current_order.get("payment_status") or "").strip().lower() == "paid":
        return {"status": "already_paid", "order_id": order_id, "captured": True}
    if not order_uses_authorization_first_payment(current_order):
        return {"status": "not_authorization_first", "order_id": order_id, "captured": False}

    verified, psp_status, psp_error = await verify_order_payment_succeeded_or_authorized(current_order)
    if not verified:
        return {
            "status": "payment_not_authorized",
            "order_id": order_id,
            "psp_status": psp_status,
            **({"error": psp_error} if psp_error else {}),
            "captured": False,
        }

    if psp_status in _PSP_SUCCEEDED_STATUSES:
        await mark_order_paid(order_id)
        latest_paid_order = await get_order(order_id) or current_order
        linked_order = _get_linked_platform_order(latest_paid_order)
        if not linked_order:
            await sync_order_to_connected_store(order_id)
            latest_paid_order = await get_order(order_id) or latest_paid_order
            linked_order = _get_linked_platform_order(latest_paid_order)
        return {
            "status": "already_captured",
            "order_id": order_id,
            "captured": True,
            "psp_status": psp_status,
            "linked_merchant_order": linked_order,
        }

    metadata = _coerce_order_metadata(current_order)
    payment_flow = metadata.get("payment_flow") if isinstance(metadata.get("payment_flow"), dict) else {}
    payment_flow = {
        **(payment_flow or {}),
        "authorization_status": "authorized",
        "authorized_at": datetime.utcnow().isoformat() + "Z",
        "last_finalize_source": source_event,
    }
    metadata["payment_flow"] = payment_flow
    await update_order_row(
        order_id,
        {
            "status": "authorized",
            "payment_status": "authorized",
            "metadata": metadata,
        },
    )
    await log_order_event(
        event_type="payment_authorized",
        order_id=order_id,
        merchant_id=str(current_order.get("merchant_id") or ""),
        total_amount=float(current_order.get("total") or 0),
        currency=str(current_order.get("currency") or "USD"),
        metadata={
            "source": source_event,
            "psp_status": psp_status,
            "payment_intent_id": current_order.get("payment_intent_id"),
            "capture_after": "merchant_order_writeback",
        },
    )

    latest_order = await get_order(order_id) or current_order
    linked_order = _get_linked_platform_order(latest_order)
    if not linked_order:
        merchant_ok = await sync_order_to_connected_store(order_id)
        latest_order = await get_order(order_id) or latest_order
        linked_order = _get_linked_platform_order(latest_order)
        if not merchant_ok or not linked_order:
            psp_type, psp_adapter = await _resolve_order_psp_adapter(latest_order)
            cancel_ok, cancel_ref, cancel_error = await psp_adapter.cancel_payment_authorization(
                str(latest_order.get("payment_intent_id") or ""),
                reason="requested_by_customer",
                idempotency_key=f"auth_first_void:{order_id}",
            )
            recovery_fields = {
                "status": "authorization_voided" if cancel_ok else "authorization_void_failed",
                "refund_required": False,
                "auto_void_attempted": True,
                "auto_void_succeeded": bool(cancel_ok),
                "void_reference": cancel_ref,
                "void_error": cancel_error,
                "operator_action": "refresh_quote_or_retry_order" if cancel_ok else "manual_psp_void_or_refund_review",
                "reason": "merchant_order_writeback_failed_before_capture",
            }
            await _update_payment_recovery_metadata_best_effort(
                order_id=order_id,
                order=latest_order,
                fields=recovery_fields,
            )
            await update_order_row(
                order_id,
                {
                    "status": "merchant_order_failed",
                    "payment_status": "authorization_voided" if cancel_ok else "authorization_void_failed",
                },
            )
            await log_order_event(
                event_type="payment_authorization_voided" if cancel_ok else "payment_authorization_void_failed",
                order_id=order_id,
                merchant_id=str(latest_order.get("merchant_id") or ""),
                total_amount=float(latest_order.get("total") or 0),
                currency=str(latest_order.get("currency") or "USD"),
                metadata={
                    "source": source_event,
                    "psp": psp_type,
                    "void_reference": cancel_ref,
                    "error": cancel_error,
                },
            )
            return {
                "status": "merchant_order_failed_authorization_voided" if cancel_ok else "merchant_order_failed_authorization_void_failed",
                "order_id": order_id,
                "captured": False,
                "voided": bool(cancel_ok),
                "linked_merchant_order": linked_order,
                **({"error": cancel_error} if cancel_error else {}),
            }

    psp_type, psp_adapter = await _resolve_order_psp_adapter(latest_order)
    capture_ok, capture_ref, capture_error = await psp_adapter.capture_payment(
        str(latest_order.get("payment_intent_id") or ""),
        amount=None,
        idempotency_key=f"auth_first_capture:{order_id}",
    )
    if not capture_ok:
        await _update_payment_recovery_metadata_best_effort(
            order_id=order_id,
            order=latest_order,
            fields={
                "status": "payment_capture_failed",
                "refund_required": False,
                "capture_required": True,
                "operator_action": "retry_capture_or_cancel_merchant_order",
                "capture_error": capture_error,
                "linked_merchant_order": linked_order,
            },
        )
        await update_order_row(order_id, {"status": "payment_capture_failed", "payment_status": "capture_failed"})
        await log_order_event(
            event_type="payment_capture_failed",
            order_id=order_id,
            merchant_id=str(latest_order.get("merchant_id") or ""),
            total_amount=float(latest_order.get("total") or 0),
            currency=str(latest_order.get("currency") or "USD"),
            metadata={
                "source": source_event,
                "psp": psp_type,
                "capture_reference": capture_ref,
                "error": capture_error,
                "linked_merchant_order": linked_order,
            },
        )
        return {
            "status": "payment_capture_failed",
            "order_id": order_id,
            "captured": False,
            "linked_merchant_order": linked_order,
            **({"error": capture_error} if capture_error else {}),
        }

    await mark_order_paid(order_id)
    await _update_payment_recovery_metadata_best_effort(
        order_id=order_id,
        order=latest_order,
        fields={
            "status": "captured_after_merchant_order",
            "refund_required": False,
            "capture_required": False,
            "operator_action": "none",
            "capture_reference": capture_ref,
            "linked_merchant_order": linked_order,
        },
    )
    await log_order_event(
        event_type="payment_captured_after_merchant_order",
        order_id=order_id,
        merchant_id=str(latest_order.get("merchant_id") or ""),
        total_amount=float(latest_order.get("total") or 0),
        currency=str(latest_order.get("currency") or "USD"),
        metadata={
            "source": source_event,
            "psp": psp_type,
            "capture_reference": capture_ref,
            "linked_merchant_order": linked_order,
        },
    )
    return {
        "status": "success",
        "order_id": order_id,
        "captured": True,
        "capture_reference": capture_ref,
        "linked_merchant_order": linked_order,
    }


# ============================================================================
# 促销折扣应用（多件折扣）
# ============================================================================

def _normalize_shopify_domain(domain: str) -> str:
    d = (domain or "").strip()
    if not d:
        return d
    d = d.replace("https://", "").replace("http://", "").strip().rstrip("/")
    if d.endswith(".myshopify.com"):
        return d
    return f"{d}.myshopify.com"


def _normalize_storefront_base_url(domain: str) -> str:
    d = (domain or "").strip()
    if not d:
        return ""
    if not d.startswith(("http://", "https://")):
        d = f"https://{d}"
    return d.rstrip("/")


def _shopify_order_create_lock_key(order_id: str) -> int:
    """
    Stable advisory-lock key for a given order_id.

    Postgres advisory locks accept signed bigint keys; derive one from sha256 to avoid
    collisions across different lock namespaces and order ids.
    """
    raw = f"pivota:shopify_order_create:{order_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


@asynccontextmanager
async def _pg_advisory_lock_best_effort(*, lock_key: int):
    """
    Best-effort Postgres advisory lock.

    - Yields `True` when the lock is acquired (or locking is unavailable).
    - Yields `False` when the lock is available but currently held by someone else.
    """
    if not IS_POSTGRES or not getattr(database, "is_connected", False):
        yield True
        return

    try:
        async with database.connection() as conn:
            acquired = bool(
                await conn.fetch_val(
                    "SELECT pg_try_advisory_lock(:lock_key)",
                    {"lock_key": int(lock_key)},
                )
            )
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock(:lock_key)",
                        {"lock_key": int(lock_key)},
                    )
                except Exception:
                    pass
    except Exception:
        # If advisory locks aren't available for any reason, proceed without blocking order creation.
        yield True


def _build_shopify_cart_permalink_best_effort(
    *,
    shop_domain: str,
    items: List[OrderItem],
    discount_codes: Optional[List[str]] = None,
    prefer_shop_pay: bool = True,
) -> Optional[str]:
    """
    Build a Shopify cart permalink checkout fallback:
      https://{shop}/cart/{variant_id}:{qty},{variant_id}:{qty}?discount=CODE1,CODE2&payment=shop_pay

    With prefer_shop_pay=True (default) we append `payment=shop_pay` so the buyer is
    dropped straight into Shop Pay checkout. This is a plain public URL: it requires no
    app scope and no token, and works under the read-only App Store app. Shop Pay only
    renders if the merchant has Shopify Payments + Shop Pay enabled on their store.

    Note: this does not guarantee pricing match with our quote; the final total
    (incl. tax/shipping) is computed and shown by Shopify checkout.
    """
    domain = _normalize_shopify_domain(shop_domain)
    if not domain:
        return None

    parts: List[str] = []
    for item in items or []:
        variant_id = getattr(item, "variant_id", None) or None
        try:
            qty = int(getattr(item, "quantity", 0) or 0)
        except Exception:
            qty = 0
        if not variant_id or qty <= 0:
            continue
        try:
            variant_numeric = str(int(str(variant_id)))
        except Exception:
            continue
        parts.append(f"{variant_numeric}:{qty}")

    if not parts:
        return None

    base = f"https://{domain}/cart/" + ",".join(parts)

    query: Dict[str, str] = {}
    codes = []
    for c in (discount_codes or []):
        if isinstance(c, str) and c.strip():
            codes.append(c.strip())
    if codes:
        # Shopify supports `discount=CODE` and typically accepts comma-delimited codes.
        query["discount"] = ",".join(codes[:5])
    if prefer_shop_pay:
        # Accelerated handoff into Shop Pay (Shopify-hosted checkout / Shopify Payments).
        query["payment"] = "shop_pay"
    if query:
        return f"{base}?{urlencode(query)}"
    return base


def _build_woocommerce_checkout_permalink_best_effort(
    *,
    store_url: str,
    items: List[OrderItem],
) -> Optional[str]:
    """
    Best-effort WooCommerce hosted checkout fallback.

    We only generate a URL for a single simple product because variable and multi-product
    carts require extra form state that we do not persist in OrderItem today.
    """
    base = _normalize_storefront_base_url(store_url)
    if not base:
        return None

    valid_items: List[OrderItem] = []
    for item in items or []:
        try:
            qty = int(getattr(item, "quantity", 0) or 0)
        except Exception:
            qty = 0
        if qty <= 0:
            continue
        valid_items.append(item)

    if len(valid_items) != 1:
        return None

    item = valid_items[0]
    if getattr(item, "variant_id", None):
        return None

    try:
        product_id = str(int(str(getattr(item, "product_id", "") or "")))
        quantity = int(getattr(item, "quantity", 0) or 0)
    except Exception:
        return None

    if quantity <= 0:
        return None

    query = urlencode({"add-to-cart": product_id, "quantity": quantity})
    return f"{base}/checkout/?{query}"


def _build_bigcommerce_checkout_permalink_best_effort(
    *,
    store_domain: str,
    items: List[OrderItem],
) -> Optional[str]:
    """
    Best-effort BigCommerce hosted checkout fallback.

    BigCommerce's storefront add-to-cart redirect is only reliable here for a single
    product line item without option reconstruction.
    """
    base = _normalize_storefront_base_url(store_domain)
    if not base:
        return None

    valid_items: List[OrderItem] = []
    for item in items or []:
        try:
            qty = int(getattr(item, "quantity", 0) or 0)
        except Exception:
            qty = 0
        if qty <= 0:
            continue
        valid_items.append(item)

    if len(valid_items) != 1:
        return None

    item = valid_items[0]
    if getattr(item, "variant_id", None):
        return None

    try:
        product_id = str(int(str(getattr(item, "product_id", "") or "")))
        quantity = int(getattr(item, "quantity", 0) or 0)
    except Exception:
        return None

    if quantity <= 0:
        return None

    query = urlencode({"action": "buy", "product_id": product_id, "qty": quantity})
    return f"{base}/cart.php?{query}"


def _platform_order_create_lock_key(platform: str, order_id: str) -> int:
    raw = f"pivota:{platform}_order_create:{order_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


def _coerce_order_metadata(order: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = (order or {}).get("metadata")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _get_linked_platform_order(order: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not order:
        return None

    metadata = _coerce_order_metadata(order)
    linked = metadata.get("merchant_order")
    if isinstance(linked, dict):
        platform_order_id = str(linked.get("platform_order_id") or "").strip()
        if platform_order_id:
            linked_copy = dict(linked)
            linked_copy["platform_order_id"] = platform_order_id
            return linked_copy

    shopify_order_id = str((order or {}).get("shopify_order_id") or "").strip()
    if shopify_order_id:
        return {
            "platform": "shopify",
            "platform_order_id": shopify_order_id,
            "platform_order_url": None,
        }
    return None


def _name_parts_from_order(order: Dict[str, Any]) -> Tuple[str, str]:
    shipping_address = order.get("shipping_address") or {}
    raw_name = str(shipping_address.get("name") or order.get("customer_name") or "").strip()
    email = str(order.get("customer_email") or "").strip()
    if not raw_name and email and "@" in email:
        raw_name = email.split("@", 1)[0].strip()
    if not raw_name:
        return "Customer", ""
    parts = [part for part in raw_name.split() if part.strip()]
    if not parts:
        return "Customer", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _build_woocommerce_address(order: Dict[str, Any]) -> Dict[str, Any]:
    shipping_address = order.get("shipping_address") or {}
    first_name, last_name = _name_parts_from_order(order)
    return {
        "first_name": first_name,
        "last_name": last_name,
        "address_1": str(shipping_address.get("address_line1") or "").strip(),
        "address_2": str(shipping_address.get("address_line2") or "").strip(),
        "city": str(shipping_address.get("city") or "").strip(),
        "state": str(shipping_address.get("state") or "").strip(),
        "postcode": str(shipping_address.get("postal_code") or "").strip(),
        "country": str(shipping_address.get("country") or "US").strip(),
        "email": str(order.get("customer_email") or "").strip(),
        "phone": str(shipping_address.get("phone") or "").strip(),
    }


def _build_bigcommerce_address(order: Dict[str, Any]) -> Dict[str, Any]:
    shipping_address = order.get("shipping_address") or {}
    first_name, last_name = _name_parts_from_order(order)
    country = str(shipping_address.get("country") or "US").strip().upper() or "US"
    return {
        "first_name": first_name,
        "last_name": last_name,
        "street_1": str(shipping_address.get("address_line1") or "").strip(),
        "street_2": str(shipping_address.get("address_line2") or "").strip(),
        "city": str(shipping_address.get("city") or "").strip(),
        "state": str(shipping_address.get("state") or "").strip(),
        "zip": str(shipping_address.get("postal_code") or "").strip(),
        "country": country,
        "country_iso2": country,
        "email": str(order.get("customer_email") or "").strip(),
        "phone": str(shipping_address.get("phone") or "").strip(),
    }


def _as_order_items(raw_items: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return items
    for raw_item in raw_items:
        if isinstance(raw_item, dict):
            items.append(dict(raw_item))
    return items


def _merge_linked_platform_order_metadata(
    order: Dict[str, Any],
    *,
    platform: str,
    platform_order_id: str,
    platform_order_name: Optional[str],
    platform_order_url: Optional[str],
    store: Dict[str, Any],
) -> Dict[str, Any]:
    metadata = _coerce_order_metadata(order)
    metadata["merchant_order"] = {
        "status": "merchant_order_created",
        "platform": platform,
        "platform_order_id": platform_order_id,
        "platform_order_name": platform_order_name,
        "platform_order_url": platform_order_url,
        "store_id": str((store or {}).get("store_id") or "").strip() or None,
        "domain": str((store or {}).get("domain") or "").strip() or None,
        "linked_at": datetime.utcnow().isoformat() + "Z",
    }
    return metadata


async def _candidate_platform_stores(
    order: Dict[str, Any],
    *,
    platform: str,
) -> List[Dict[str, Any]]:
    stores = await get_merchant_active_stores(str(order.get("merchant_id") or "").strip())
    platform_stores = [s for s in (stores or []) if str((s or {}).get("platform") or "").strip().lower() == platform]
    if not platform_stores:
        return []

    bound_store_id = str(order.get("store_id") or "").strip() or None
    candidates: List[Dict[str, Any]] = []
    if bound_store_id:
        for store in platform_stores:
            if str((store or {}).get("store_id") or "").strip() == bound_store_id:
                candidates.append(store)
                break
        return candidates

    for store in platform_stores:
        if store not in candidates:
            candidates.append(store)
    return candidates


def _parse_woocommerce_store_credentials(store: Dict[str, Any]) -> Tuple[str, str, str]:
    credentials = dict((store or {}).get("api_credentials") or {})
    raw_api_key = str((store or {}).get("api_key_raw") or (store or {}).get("api_key") or "").strip()
    consumer_key = str(credentials.get("consumer_key") or "").strip()
    consumer_secret = str(credentials.get("consumer_secret") or "").strip()

    if not consumer_key and ":" in raw_api_key:
        consumer_key = raw_api_key.split(":", 1)[0].strip()
    if not consumer_secret and ":" in raw_api_key:
        consumer_secret = raw_api_key.split(":", 1)[1].strip()

    store_url = normalize_woocommerce_store_url((store or {}).get("domain"))
    return store_url, consumer_key, consumer_secret


def _parse_bigcommerce_store_credentials(store: Dict[str, Any]) -> Tuple[str, str, str, str]:
    credentials = dict((store or {}).get("api_credentials") or {})
    store_hash = normalize_bigcommerce_store_hash(
        credentials.get("store_hash") or (store or {}).get("domain")
    )
    access_token = str(credentials.get("access_token") or (store or {}).get("api_key") or "").strip()
    client_id = str(credentials.get("client_id") or "").strip()
    store_domain = str((store or {}).get("domain") or "").strip() or build_bigcommerce_domain(store_hash)
    return store_hash, access_token, client_id, store_domain


async def _resolve_bigcommerce_status_id(
    *,
    client: httpx.AsyncClient,
    store_hash: str,
    headers: Dict[str, str],
) -> Optional[int]:
    try:
        response = await client.get(
            f"https://api.bigcommerce.com/stores/{store_hash}/v2/order_statuses",
            headers=headers,
            timeout=12.0,
        )
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
    except Exception:
        return None

    if not isinstance(payload, list):
        return None

    for row in payload:
        if not isinstance(row, dict):
            continue
        names = {
            str(row.get("name") or "").strip().lower(),
            str(row.get("label") or "").strip().lower(),
            str(row.get("system_label") or "").strip().lower(),
            str(row.get("custom_label") or "").strip().lower(),
        }
        if "awaiting fulfillment" in names:
            try:
                return int(row.get("id"))
            except Exception:
                return None
    return None


def _normalize_bigcommerce_option_value_id(raw_value: Dict[str, Any]) -> Optional[int]:
    for key in ("id", "option_value_id", "value_id"):
        candidate = raw_value.get(key)
        try:
            return int(candidate)
        except Exception:
            continue
    return None


async def _fetch_bigcommerce_variant_product_options(
    *,
    client: httpx.AsyncClient,
    store_hash: str,
    headers: Dict[str, str],
    product_id: int,
    variant_id: int,
) -> List[Dict[str, int]]:
    variant_resp = await client.get(
        f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/variants/{variant_id}",
        headers=headers,
        timeout=12.0,
    )
    if variant_resp.status_code != 200:
        raise ValueError(f"BigCommerce variant lookup failed: HTTP {variant_resp.status_code}")

    variant_payload = variant_resp.json() or {}
    variant = variant_payload.get("data") or {}
    option_values = variant.get("option_values") or []
    assignments: List[Dict[str, int]] = []
    missing_mapping = False

    for option_value in option_values:
        if not isinstance(option_value, dict):
            continue
        option_id = option_value.get("option_id")
        value_id = _normalize_bigcommerce_option_value_id(option_value)
        try:
            option_id_int = int(option_id)
        except Exception:
            option_id_int = None
        if option_id_int is not None and value_id is not None:
            assignments.append({"id": option_id_int, "value": value_id})
        else:
            missing_mapping = True

    if assignments and not missing_mapping:
        return assignments

    options_resp = await client.get(
        f"https://api.bigcommerce.com/stores/{store_hash}/v3/catalog/products/{product_id}/options",
        headers=headers,
        timeout=12.0,
    )
    if options_resp.status_code != 200:
        raise ValueError(f"BigCommerce option lookup failed: HTTP {options_resp.status_code}")

    options_payload = options_resp.json() or {}
    options_rows = options_payload.get("data") or []
    option_map: Dict[Tuple[str, str], Dict[str, int]] = {}
    for row in options_rows:
        if not isinstance(row, dict):
            continue
        try:
            option_id = int(row.get("id"))
        except Exception:
            continue
        display_name = str(row.get("display_name") or row.get("name") or "").strip().lower()
        for value_row in row.get("option_values") or []:
            if not isinstance(value_row, dict):
                continue
            value_id = _normalize_bigcommerce_option_value_id(value_row)
            label = str(value_row.get("label") or value_row.get("name") or "").strip().lower()
            if value_id is None or not display_name or not label:
                continue
            option_map[(display_name, label)] = {"id": option_id, "value": value_id}

    mapped_assignments: List[Dict[str, int]] = []
    for option_value in option_values:
        if not isinstance(option_value, dict):
            continue
        key = (
            str(option_value.get("option_display_name") or option_value.get("display_name") or "").strip().lower(),
            str(option_value.get("label") or option_value.get("option_label") or "").strip().lower(),
        )
        if not key[0] or not key[1]:
            continue
        mapped = option_map.get(key)
        if mapped:
            mapped_assignments.append(mapped)

    if mapped_assignments:
        return mapped_assignments
    raise ValueError("BigCommerce variant option mapping unavailable")


async def _get_platform_checkout_fallback_url_best_effort(
    *,
    merchant_id: str,
    items: List[OrderItem],
    discount_codes: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Platform-hosted checkout fallback when we cannot create an external PSP payment intent.
    Returns {url, platform, method} or None.
    """
    try:
        store = await get_primary_store(merchant_id)
    except Exception:
        store = None

    platform = (store or {}).get("platform")
    domain = (store or {}).get("domain")
    if not platform or not domain:
        return None

    if str(platform).lower() == "shopify":
        url = _build_shopify_cart_permalink_best_effort(
            shop_domain=str(domain),
            items=items,
            discount_codes=discount_codes,
        )
        if url:
            return {"url": url, "platform": "shopify", "method": "cart_permalink"}
    elif str(platform).lower() == "woocommerce":
        url = _build_woocommerce_checkout_permalink_best_effort(
            store_url=str(domain),
            items=items,
        )
        if url:
            return {"url": url, "platform": "woocommerce", "method": "checkout_add_to_cart"}
    elif str(platform).lower() == "bigcommerce":
        url = _build_bigcommerce_checkout_permalink_best_effort(
            store_domain=str(domain),
            items=items,
        )
        if url:
            return {"url": url, "platform": "bigcommerce", "method": "cart_buy_now"}

    return None


# ============================================================================
# 库存检查
# ============================================================================

async def check_inventory_availability(
    merchant_id: str,
    items: List[OrderItem]
) -> Tuple[bool, Dict[str, Any]]:
    """
    检查 Shopify 库存是否充足

    返回: (是否有库存, 库存详情)
    """
    try:
        # 获取主店铺信息（Shopify/Wix/...），用于后续判断
        store_info = await get_primary_store(merchant_id)
        if not store_info:
            return True, {"message": "No store connected, skipping inventory check"}

        if store_info.get("platform") != "shopify":
            # 非 Shopify 平台，暂不检查库存
            return True, {"message": f"Platform {store_info.get('platform')} inventory check not implemented"}
        
        shop_domain = store_info.get("domain")
        access_token, _ = await resolve_shopify_admin_access_token(
            shop_domain=shop_domain,
            api_key_raw=store_info.get("api_key_raw") or store_info.get("api_key"),
            store_id=str(store_info.get("store_id") or "").strip() or None,
        )
        
        if not shop_domain or not access_token:
            return True, {"message": "Shop credentials missing, skipping inventory check"}
        
        # 获取所有产品和变体
        url = f"https://{shop_domain}/admin/api/2025-10/products.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            
            if response.status_code != 200:
                return True, {"message": "Failed to fetch products, allowing order"}
            
            products = response.json().get("products", [])
            
            # 建立 variant_id -> inventory 的映射
            inventory_map = {}
            for product in products:
                for variant in product.get("variants", []):
                    variant_id = str(variant["id"])
                    inventory_map[variant_id] = {
                        "available": variant.get("inventory_quantity", 0),
                        "tracked": variant.get("inventory_management") == "shopify",
                        "sku": variant.get("sku"),
                        "title": f"{product['title']} - {variant.get('title', '')}"
                    }
            
            # 检查每个订单项的库存
            insufficient_items = []
            inventory_details = {}
            
            for item in items:
                if not item.variant_id:
                    # 如果没有 variant_id，跳过检查
                    continue
                
                variant_id = str(item.variant_id)
                if variant_id in inventory_map:
                    inv = inventory_map[variant_id]
                    inventory_details[variant_id] = inv
                    
                    if inv["tracked"] and inv["available"] < item.quantity:
                        insufficient_items.append({
                            "product": item.product_title,
                            "requested": item.quantity,
                            "available": inv["available"]
                        })
            
            if insufficient_items:
                return False, {
                    "message": "Insufficient inventory",
                    "items": insufficient_items
                }
            
            return True, {
                "message": "Inventory check passed",
                "details": inventory_details
            }
            
    except Exception as e:
        # 库存检查失败时，默认允许订单（fail-open）
        logger.error(f"Inventory check failed: {e}")
        return True, {"message": f"Inventory check error: {str(e)}, allowing order"}


def _extract_delivery_option_identifier(selected_delivery_option: Any) -> Optional[str]:
    """
    Best-effort extraction of a stable delivery option identifier for drift diagnostics.

    Do not include full delivery option payload in responses/events (may contain extra data),
    only a stable identifier-like string.
    """
    if not selected_delivery_option:
        return None

    if isinstance(selected_delivery_option, str):
        value = selected_delivery_option.strip()
        return value or None

    if not isinstance(selected_delivery_option, dict):
        return None

    for key in (
        "id",
        "identifier",
        "handle",
        "code",
        "shipping_rate_id",
        "rate_id",
        "title",
        "name",
    ):
        raw = selected_delivery_option.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            return value
    return None


def _build_quote_drift_normalized_request(
    *,
    items: List[Dict[str, Any]],
    discount_codes: List[str],
    shipping_address: Optional[Dict[str, Any]],
    selected_delivery_option: Any,
) -> Dict[str, Any]:
    return {
        "items": normalize_items_for_fingerprint(items),
        "discount_codes": normalize_discount_codes(discount_codes),
        "shipping_geo": normalize_shipping_for_fingerprint(shipping_address),
        "selected_delivery_option": _extract_delivery_option_identifier(selected_delivery_option),
    }


def _coerce_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _pricing_quote_meta_from_order(order: Dict[str, Any]) -> Dict[str, Any]:
    metadata = _coerce_dict((order or {}).get("metadata"))
    pricing_quote = metadata.get("pricing_quote")
    return pricing_quote if isinstance(pricing_quote, dict) else {}


def _shopify_discount_reconciliation_mode() -> str:
    mode = str(os.getenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "observe") or "").strip().lower()
    return mode if mode in {"observe", "fail_closed"} else "observe"


SHOPIFY_WRITE_STRATEGY_REST_SIMPLE = "rest_simple"
SHOPIFY_WRITE_STRATEGY_DRAFT_ORDER_QUOTE = "draft_order_quote"
SHOPIFY_WRITE_STRATEGY_REST_LEGACY_SUPPRESSED = "rest_legacy_suppressed"

SHOPIFY_RECEIPT_POLICY_SEND = "send_shopify_receipt"
SHOPIFY_RECEIPT_POLICY_SUPPRESSED = "shopify_receipt_suppressed"
SHOPIFY_RECEIPT_POLICY_DRAFT_SUPPRESSED = "shopify_receipt_suppressed_pending_draft_canary"


def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def _csv_env_values(name: str) -> List[str]:
    raw = str(os.getenv(name, "") or "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _shopify_draft_order_quote_sync_enabled(*, merchant_id: Optional[str] = None) -> bool:
    if not _env_flag("SHOPIFY_DRAFT_ORDER_QUOTE_SYNC_ENABLED", "0"):
        return False
    allowlist = set(_csv_env_values("SHOPIFY_DRAFT_ORDER_QUOTE_MERCHANT_IDS"))
    if not allowlist:
        return True
    return bool(merchant_id and merchant_id in allowlist)


def _order_amounts_source(order: Dict[str, Any]) -> Optional[str]:
    metadata = _coerce_dict((order or {}).get("metadata"))
    value = metadata.get("amounts_source")
    if value:
        return str(value or "").strip() or None
    pricing_quote = metadata.get("pricing_quote")
    if isinstance(pricing_quote, dict) and pricing_quote:
        return "quote_snapshot"
    return None


def _money2(value: Any) -> Decimal:
    return parse_decimal_money(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _discount_evidence_hash(discount_evidence: Any) -> Optional[str]:
    if not isinstance(discount_evidence, dict) or not discount_evidence:
        return None
    payload = json.dumps(discount_evidence, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _pricing_quote_has_unverified_shipping(pricing_quote_meta: Dict[str, Any]) -> bool:
    if not isinstance(pricing_quote_meta, dict):
        return False
    evidence = pricing_quote_meta.get("discount_evidence")
    if not isinstance(evidence, dict):
        return False
    shipping_evidence = evidence.get("shipping_evidence")
    if not isinstance(shipping_evidence, dict):
        return False
    return str(shipping_evidence.get("status") or "").strip().lower() == "unverified"


def _pricing_quote_discount_total(pricing_quote_meta: Dict[str, Any]) -> Decimal:
    pricing = pricing_quote_meta.get("pricing") if isinstance(pricing_quote_meta, dict) else None
    if isinstance(pricing, dict) and pricing.get("discount_total") is not None:
        # `pricing.discount_total` is the authoritative product/order discount
        # total used for Shopify order reconciliation. Shipping discounts are
        # represented through `shipping_fee` / shipping evidence, and must not
        # be folded into Shopify `total_discounts`.
        return _money2(pricing.get("discount_total"))

    total = Decimal("0.00")
    for collection_key in ("promotion_lines",):
        for line in pricing_quote_meta.get(collection_key) or []:
            if not isinstance(line, dict):
                continue
            total += _money2(line.get("amount")).copy_abs()
    evidence = pricing_quote_meta.get("discount_evidence")
    if isinstance(evidence, dict):
        for app in evidence.get("applications") or []:
            if isinstance(app, dict):
                total += _money2(app.get("amount")).copy_abs()
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pricing_quote_has_line_discounts(pricing_quote_meta: Dict[str, Any]) -> bool:
    if not isinstance(pricing_quote_meta, dict):
        return False
    for line in pricing_quote_meta.get("line_items") or []:
        if not isinstance(line, dict):
            continue
        if _money2(line.get("line_discount_total")).copy_abs() > 0:
            return True
    return False


def _build_shopify_order_discount_codes(pricing_quote_meta: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    REST order creation supports order-level discount_codes. Use fixed amounts only
    because the quote snapshot carries final allocated amounts, not reusable merchant rule math.
    """
    if not isinstance(pricing_quote_meta, dict) or not pricing_quote_meta:
        return []

    evidence = pricing_quote_meta.get("discount_evidence")
    applicable_codes = set()
    if isinstance(evidence, dict):
        for row in evidence.get("codes") or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            if code and row.get("applicable") is True:
                applicable_codes.add(code)

    amounts_by_code: Dict[str, Decimal] = {}
    source_rows = (evidence or {}).get("applications") if isinstance(evidence, dict) else []
    if not source_rows:
        source_rows = pricing_quote_meta.get("promotion_lines") or []
    for row in source_rows or []:
        if not isinstance(row, dict):
            continue
        discount_class = str(row.get("discount_class") or "").strip().lower()
        if discount_class == "product":
            continue
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        amount = _money2(row.get("amount")).copy_abs()
        if amount <= 0:
            continue
        if applicable_codes and code not in applicable_codes:
            continue
        amounts_by_code[code] = amounts_by_code.get(code, Decimal("0.00")) + amount

    discount_total = _pricing_quote_discount_total(pricing_quote_meta)
    if (
        not amounts_by_code
        and len(applicable_codes) == 1
        and discount_total > 0
        and not _pricing_quote_has_line_discounts(pricing_quote_meta)
    ):
        code = next(iter(applicable_codes))
        amounts_by_code[code] = discount_total

    out: List[Dict[str, str]] = []
    for code, amount in amounts_by_code.items():
        if amount <= 0:
            continue
        out.append({"code": code, "amount": str(amount.quantize(Decimal("0.01"))), "type": "fixed_amount"})
    return out[:1]


def _shopify_receipt_representation_blockers(pricing_quote_meta: Dict[str, Any]) -> List[str]:
    """
    Identify quote shapes that Shopify REST order creation cannot faithfully represent today.

    Current REST payload support in this code path is limited to:
    - at most one order-level discount code
    - no code-less automatic discounts
    - no reliable product-level discount recreation

    When these blockers are present, auto-sending a Shopify receipt is unsafe because Shopify
    may generate an email with totals/breakdown that do not match the authoritative quote/charge.
    """
    if not isinstance(pricing_quote_meta, dict) or not pricing_quote_meta:
        return []

    blockers: List[str] = []
    evidence = pricing_quote_meta.get("discount_evidence")
    applications = evidence.get("applications") if isinstance(evidence, dict) else None
    if not isinstance(applications, list):
        applications = pricing_quote_meta.get("promotion_lines") or []

    applicable_codes: List[str] = []
    if isinstance(evidence, dict):
        for row in evidence.get("codes") or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            if code and row.get("applicable") is True:
                applicable_codes.append(code)

    if len(set(applicable_codes)) > 1:
        blockers.append("multiple_applicable_discount_codes")

    for row in applications or []:
        if not isinstance(row, dict):
            continue
        discount_class = str(row.get("discount_class") or "").strip().lower()
        method = str(row.get("method") or "").strip().lower()
        code = str(row.get("code") or "").strip()
        amount = _money2(row.get("amount")).copy_abs()
        if amount <= 0:
            continue
        if discount_class == "product":
            blockers.append("product_level_discount")
        if method == "automatic":
            blockers.append("automatic_discount")
        if discount_class == "shipping" and not code:
            blockers.append("code_less_shipping_discount")

    if _pricing_quote_discount_total(pricing_quote_meta) > 0 and not _build_shopify_order_discount_codes(pricing_quote_meta):
        blockers.append("discount_not_encodable_as_rest_order_discount_code")

    return sorted(set(blockers))


def _select_shopify_write_policy(
    *,
    order: Dict[str, Any],
    pricing_quote_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Decide how a paid Pivota order should be represented in Shopify.

    `quote_snapshot` is the only authoritative amount source for customer-facing
    Shopify receipts. Legacy/non-quote rows may still be written for merchant
    fulfillment, but receipts stay suppressed and those rows are excluded from
    receipt-truth canaries.
    """
    merchant_id = str((order or {}).get("merchant_id") or "").strip() or None
    amounts_source = _order_amounts_source(order)
    blockers = _shopify_receipt_representation_blockers(pricing_quote_meta)

    if amounts_source != "quote_snapshot" or not pricing_quote_meta:
        return {
            "shopify_write_strategy": SHOPIFY_WRITE_STRATEGY_REST_LEGACY_SUPPRESSED,
            "write_path": "rest",
            "receipt_policy": SHOPIFY_RECEIPT_POLICY_SUPPRESSED,
            "representation_status": "legacy_not_authoritative",
            "reconciliation_status": "not_applicable",
            "receipt_blockers": ["non_quote_snapshot_amounts"],
            "draft_order_enabled": False,
        }

    if blockers:
        draft_enabled = _shopify_draft_order_quote_sync_enabled(merchant_id=merchant_id)
        return {
            "shopify_write_strategy": SHOPIFY_WRITE_STRATEGY_DRAFT_ORDER_QUOTE,
            "write_path": "draft_order" if draft_enabled else "rest_suppressed_fallback",
            "receipt_policy": (
                SHOPIFY_RECEIPT_POLICY_DRAFT_SUPPRESSED
                if draft_enabled
                else SHOPIFY_RECEIPT_POLICY_SUPPRESSED
            ),
            "representation_status": (
                "draft_order_quote_enabled"
                if draft_enabled
                else "draft_order_quote_required_rest_suppressed"
            ),
            "reconciliation_status": "pending",
            "receipt_blockers": blockers,
            "draft_order_enabled": draft_enabled,
        }

    return {
        "shopify_write_strategy": SHOPIFY_WRITE_STRATEGY_REST_SIMPLE,
        "write_path": "rest",
        "receipt_policy": SHOPIFY_RECEIPT_POLICY_SEND,
        "representation_status": "rest_simple_representable",
        "reconciliation_status": "pending",
        "receipt_blockers": [],
        "draft_order_enabled": False,
    }


def _shopify_receipt_can_be_auto_sent(
    *,
    customer_email: str,
    pricing_quote_meta: Dict[str, Any],
) -> bool:
    return bool(customer_email) and not _shopify_receipt_representation_blockers(pricing_quote_meta)


_SHOPIFY_ORDER_TAG_MAX_LEN = 40
_SHOPIFY_ORDER_TAG_SAFE_RE = re.compile(r"[^A-Za-z0-9-]+")


def _shopify_order_tag(prefix: str, value: str) -> str:
    """
    Build a Shopify order tag that survives REST order creation.

    Keep full IDs in note_attributes; tags are short, searchable join keys.
    """
    safe_prefix = _SHOPIFY_ORDER_TAG_SAFE_RE.sub("-", str(prefix or "").strip()).strip("-")
    safe_value = _SHOPIFY_ORDER_TAG_SAFE_RE.sub("-", str(value or "").strip()).strip("-")
    safe_prefix = re.sub(r"-+", "-", safe_prefix) or "pivota"
    safe_value = re.sub(r"-+", "-", safe_value)
    tag = f"{safe_prefix}-{safe_value}" if safe_value else safe_prefix
    if len(tag) <= _SHOPIFY_ORDER_TAG_MAX_LEN:
        return tag

    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:8]
    keep = max(1, _SHOPIFY_ORDER_TAG_MAX_LEN - len(digest) - 1)
    return f"{tag[:keep].rstrip('-')}-{digest}"


def _build_shopify_discount_order_annotations(
    *,
    order_id: str,
    pricing_quote_meta: Dict[str, Any],
) -> Tuple[List[str], List[Dict[str, str]]]:
    if not isinstance(pricing_quote_meta, dict) or not pricing_quote_meta:
        return [], []

    tags: List[str] = []
    note_attributes: List[Dict[str, str]] = []
    quote_id = str(pricing_quote_meta.get("quote_id") or "").strip()
    if quote_id:
        tags.append(_shopify_order_tag("pivota-quote-id", quote_id))
        note_attributes.append({"name": "pivota_quote_id", "value": quote_id})

    evidence = pricing_quote_meta.get("discount_evidence")
    evidence_hash = _discount_evidence_hash(evidence)
    if evidence_hash:
        tags.append(_shopify_order_tag("pivota-discount-evidence", evidence_hash))
        note_attributes.append({"name": "pivota_discount_evidence_hash", "value": evidence_hash})

    if isinstance(evidence, dict):
        confidence = str(evidence.get("pricing_confidence") or "").strip()
        if confidence:
            note_attributes.append({"name": "pivota_discount_pricing_confidence", "value": confidence})

    payment_offer_evidence = pricing_quote_meta.get("payment_offer_evidence")
    payment_offer_hash = stable_payment_offer_hash(payment_offer_evidence)
    if payment_offer_hash:
        note_attributes.append({"name": "pivota_payment_offer_evidence_hash", "value": payment_offer_hash})

    # Keep a stable cross-system join key even if the quote id is absent on a legacy row.
    note_attributes.append({"name": "pivota_order_id", "value": str(order_id)})
    return tags, note_attributes


def _extract_shopify_order_reconciliation_totals(
    shopify_order: Optional[Dict[str, Any]],
) -> Dict[str, Optional[Decimal]]:
    if not isinstance(shopify_order, dict):
        return {"total": None, "discount_total": None, "transaction_total": None}

    total_price_set = shopify_order.get("total_price_set")
    total_shop_money = (total_price_set or {}).get("shop_money") if isinstance(total_price_set, dict) else {}
    discount_price_set = shopify_order.get("total_discounts_set")
    discount_shop_money = (discount_price_set or {}).get("shop_money") if isinstance(discount_price_set, dict) else {}
    total = _money2(
        shopify_order.get("current_total_price")
        or shopify_order.get("total_price")
        or (total_shop_money or {}).get("amount")
    )
    discount_total = _money2(
        shopify_order.get("current_total_discounts")
        or shopify_order.get("total_discounts")
        or (discount_shop_money or {}).get("amount")
    )

    transaction_total: Optional[Decimal] = None
    transactions = shopify_order.get("transactions")
    if isinstance(transactions, list):
        transaction_total = Decimal("0.00")
        for txn in transactions:
            if not isinstance(txn, dict):
                continue
            status_value = str(txn.get("status") or "").strip().lower()
            kind = str(txn.get("kind") or "").strip().lower()
            if status_value and status_value not in {"success", "succeeded"}:
                continue
            if kind and kind not in {"sale", "capture"}:
                continue
            transaction_total += _money2(txn.get("amount"))
        transaction_total = transaction_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {
        "total": total,
        "discount_total": discount_total,
        "transaction_total": transaction_total,
    }


def _build_pricing_quote_line_item_map(pricing_quote_meta: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(pricing_quote_meta, dict):
        return out
    for line in pricing_quote_meta.get("line_items") or []:
        if not isinstance(line, dict):
            continue
        variant_id = str(line.get("variant_id") or "").strip()
        product_id = str(line.get("product_id") or "").strip()
        if variant_id:
            out[f"variant:{variant_id}"] = dict(line)
        if product_id:
            out.setdefault(f"product:{product_id}", dict(line))
    return out


def _apply_pricing_quote_line_item_overrides(
    *,
    line_item: Dict[str, Any],
    order_item: Dict[str, Any],
    pricing_quote_meta: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(line_item, dict):
        return line_item

    line_map = _build_pricing_quote_line_item_map(pricing_quote_meta)
    variant_id = str(order_item.get("variant_id") or "").strip()
    product_id = str(order_item.get("product_id") or "").strip()
    quote_line = None
    if variant_id:
        quote_line = line_map.get(f"variant:{variant_id}")
    if not quote_line and product_id:
        quote_line = line_map.get(f"product:{product_id}")
    if not isinstance(quote_line, dict):
        return line_item

    unit_price_original = _money2(quote_line.get("unit_price_original"))
    line_discount_total = _money2(quote_line.get("line_discount_total")).copy_abs()
    if unit_price_original > 0:
        line_item["price"] = str(unit_price_original)
    if line_discount_total > 0:
        line_item["total_discount"] = str(line_discount_total)
    return line_item


def _pricing_quote_line_discount_total(pricing_quote_meta: Dict[str, Any]) -> Decimal:
    if not isinstance(pricing_quote_meta, dict):
        return Decimal("0.00")

    total = Decimal("0.00")
    for line in pricing_quote_meta.get("line_items") or []:
        if not isinstance(line, dict):
            continue
        total += _money2(line.get("line_discount_total")).copy_abs()
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pricing_quote_shipping_fee(pricing_quote_meta: Dict[str, Any], *, fallback: Any = None) -> Decimal:
    pricing = pricing_quote_meta.get("pricing") if isinstance(pricing_quote_meta, dict) else None
    if isinstance(pricing, dict):
        return _money2(pricing.get("shipping_fee"))
    return _money2(fallback)


def _pricing_quote_supports_custom_line_item_rest_encoding(pricing_quote_meta: Dict[str, Any]) -> bool:
    """
    Determine whether we can faithfully encode the authoritative quote into Shopify REST
    using custom line items plus explicit shipping_lines.

    Live verification on production showed Shopify REST `orders.json` still drops
    line-item discount state for this shape, even when the quote discount is fully
    allocated at the line level. Keep this disabled until we move to a surface that
    can authoritatively encode line discounts (for example Draft Orders / GraphQL).
    """
    return False


def _shopify_shipping_line_title(pricing_quote_meta: Dict[str, Any]) -> str:
    evidence = pricing_quote_meta.get("discount_evidence") if isinstance(pricing_quote_meta, dict) else None
    shipping_evidence = evidence.get("shipping_evidence") if isinstance(evidence, dict) else None
    if isinstance(shipping_evidence, dict):
        for key in ("selected_delivery_option_title", "selected_delivery_option_name", "selected_delivery_option"):
            raw = shipping_evidence.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            if isinstance(raw, dict):
                value = str(raw.get("title") or raw.get("name") or "").strip()
                if value:
                    return value
    return "Shipping"


def _build_shopify_shipping_lines(
    *,
    order: Dict[str, Any],
    pricing_quote_meta: Dict[str, Any],
    currency_code: str,
) -> List[Dict[str, Any]]:
    shipping_fee = _pricing_quote_shipping_fee(pricing_quote_meta, fallback=order.get("shipping_fee"))
    if shipping_fee < 0:
        shipping_fee = Decimal("0.00")

    return [
        {
            "title": _shopify_shipping_line_title(pricing_quote_meta),
            "code": "pivota_shipping",
            "price": str(shipping_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            **({"currency": currency_code} if currency_code and len(currency_code) == 3 else {}),
        }
    ]


def _shopify_money_input(amount: Decimal, currency_code: str) -> Dict[str, str]:
    return {
        "amount": str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "currencyCode": str(currency_code or "USD").strip().upper() or "USD",
    }


def _shopify_variant_gid(variant_id: Any) -> Optional[str]:
    raw = str(variant_id or "").strip()
    if not raw:
        return None
    if raw.startswith("gid://shopify/ProductVariant/"):
        return raw
    if raw.isdigit():
        return f"gid://shopify/ProductVariant/{raw}"
    return None


def _shopify_order_gid(order_id: Any) -> Optional[str]:
    raw = str(order_id or "").strip()
    if not raw:
        return None
    if raw.startswith("gid://shopify/Order/"):
        return raw
    if raw.isdigit():
        return f"gid://shopify/Order/{raw}"
    return None


def _shopify_graphql_address_input(shopify_shipping: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(shopify_shipping, dict):
        return {}
    payload = {
        "firstName": shopify_shipping.get("first_name"),
        "lastName": shopify_shipping.get("last_name"),
        "address1": shopify_shipping.get("address1"),
        "address2": shopify_shipping.get("address2"),
        "city": shopify_shipping.get("city"),
        "province": shopify_shipping.get("province"),
        "zip": shopify_shipping.get("zip"),
        "country": shopify_shipping.get("country"),
        "phone": shopify_shipping.get("phone"),
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _shopify_discount_title(pricing_quote_meta: Dict[str, Any], *, fallback: str = "Pivota Quote Discount") -> str:
    evidence = pricing_quote_meta.get("discount_evidence") if isinstance(pricing_quote_meta, dict) else None
    if isinstance(evidence, dict):
        for row in evidence.get("codes") or []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            if code and row.get("applicable") is True:
                return code
    for row in pricing_quote_meta.get("promotion_lines") or [] if isinstance(pricing_quote_meta, dict) else []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if code:
            return code
    return fallback


def _build_shopify_draft_order_input(
    *,
    order_id: str,
    order: Dict[str, Any],
    pricing_quote_meta: Dict[str, Any],
    customer_email: str,
    shopify_shipping: Dict[str, Any],
    currency_code: str,
    shopify_tags: List[str],
    discount_note_attributes: List[Dict[str, str]],
) -> Dict[str, Any]:
    pricing = pricing_quote_meta.get("pricing") if isinstance(pricing_quote_meta, dict) else {}
    shipping_lines = _build_shopify_shipping_lines(
        order=order,
        pricing_quote_meta=pricing_quote_meta,
        currency_code=currency_code,
    )
    shipping_line = shipping_lines[0] if shipping_lines else None
    quote_line_map = _build_pricing_quote_line_item_map(pricing_quote_meta)
    discount_title = _shopify_discount_title(pricing_quote_meta)
    line_items: List[Dict[str, Any]] = []
    allocated_line_discount_total = Decimal("0.00")

    for item in order.get("items") or []:
        if not isinstance(item, dict):
            continue
        variant_gid = _shopify_variant_gid(item.get("variant_id"))
        quote_line = None
        variant_id = str(item.get("variant_id") or "").strip()
        product_id = str(item.get("product_id") or "").strip()
        if variant_id:
            quote_line = quote_line_map.get(f"variant:{variant_id}")
        if not quote_line and product_id:
            quote_line = quote_line_map.get(f"product:{product_id}")

        quantity = int(item.get("quantity") or 0)
        if quantity <= 0:
            continue

        unit_price_original = _money2(
            (quote_line or {}).get("unit_price_original")
            or item.get("unit_price")
        )
        line_discount_total = _money2((quote_line or {}).get("line_discount_total")).copy_abs()
        allocated_line_discount_total += line_discount_total

        draft_line: Dict[str, Any]
        if variant_gid:
            draft_line = {
                "variantId": variant_gid,
                "quantity": quantity,
            }
            if unit_price_original > 0:
                draft_line["priceOverride"] = _shopify_money_input(unit_price_original, currency_code)
        else:
            draft_line = {
                "title": str(item.get("product_title") or "Product"),
                "quantity": quantity,
                "originalUnitPriceWithCurrency": _shopify_money_input(unit_price_original, currency_code),
                "requiresShipping": True,
                "taxable": False,
            }
            sku = str(item.get("sku") or "").strip()
            if sku:
                draft_line["sku"] = sku

        if line_discount_total > 0:
            draft_line["appliedDiscount"] = {
                "title": discount_title,
                "description": "Pivota quote line discount",
                "valueType": "FIXED_AMOUNT",
                "value": float(line_discount_total),
                "amountWithCurrency": _shopify_money_input(line_discount_total, currency_code),
            }
        line_items.append(draft_line)

    pricing_discount_total = _pricing_quote_discount_total(pricing_quote_meta)
    unallocated_discount_total = max(Decimal("0.00"), pricing_discount_total - allocated_line_discount_total)

    custom_attributes = [
        {"key": str(row.get("name")), "value": str(row.get("value"))}
        for row in discount_note_attributes or []
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    ]

    draft_input: Dict[str, Any] = {
        "lineItems": line_items,
        "presentmentCurrencyCode": currency_code,
        "tags": [tag for tag in shopify_tags if isinstance(tag, str) and tag.strip()],
        "customAttributes": custom_attributes,
        "note": f"Pivota Order ID: {order_id}",
        "taxExempt": _money2((pricing or {}).get("tax")) <= 0,
    }
    if customer_email:
        draft_input["email"] = customer_email
    address_input = _shopify_graphql_address_input(shopify_shipping)
    if address_input:
        draft_input["shippingAddress"] = address_input
        draft_input["billingAddress"] = address_input
    if shipping_line:
        draft_input["shippingLine"] = {
            "title": str(shipping_line.get("title") or "Shipping"),
            "price": str(shipping_line.get("price") or "0.00"),
        }
    if unallocated_discount_total > 0:
        draft_input["appliedDiscount"] = {
            "title": discount_title,
            "description": "Pivota quote order discount",
            "valueType": "FIXED_AMOUNT",
            "value": float(unallocated_discount_total),
            "amountWithCurrency": _shopify_money_input(unallocated_discount_total, currency_code),
        }
    return draft_input


def _extract_shopify_legacy_resource_id(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return raw
    match = re.search(r"/(\d+)$", raw)
    return match.group(1) if match else None


def _format_shopify_user_errors(errors: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in errors or []:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "field": row.get("field"),
                "message": str(row.get("message") or "").strip() or "Unknown Shopify user error",
            }
        )
    return out


async def _log_shopify_receipt_suppressed_once(
    *,
    order_id: str,
    merchant_id: str,
    total_amount: float,
    currency: str,
    metadata: Dict[str, Any],
) -> None:
    try:
        existing = await database.fetch_val(
            # Plain string, not text(): databases.Database._build_query calls
            # .values() on a ClauseElement when params are passed, and
            # TextClause has no .values -> AttributeError.
            """
            SELECT 1
            FROM order_events
            WHERE order_id = :order_id
              AND event_type = 'shopify_receipt_suppressed'
            LIMIT 1
            """,
            {"order_id": order_id},
        )
        if existing:
            return
    except Exception:
        pass

    await log_order_event(
        event_type="shopify_receipt_suppressed",
        order_id=order_id,
        merchant_id=merchant_id,
        total_amount=total_amount,
        currency=currency,
        metadata=metadata,
    )


async def _fetch_shopify_order_reconciliation_payload_best_effort(
    *,
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
) -> Optional[Dict[str, Any]]:
    if not shop_domain or not access_token or not shopify_order_id:
        return None

    url = (
        f"https://{shop_domain}/admin/api/2025-10/orders/{shopify_order_id}.json"
        "?status=any&fields=id,current_total_price,total_price,current_total_discounts,total_discounts,"
        "total_price_set,total_discounts_set"
    )
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json",
                },
                timeout=12.0,
            )
        if response.status_code != 200:
            return None
        payload = response.json() if response.content else {}
        order = (payload or {}).get("order") if isinstance(payload, dict) else None
        if not isinstance(order, dict):
            return None
    except Exception:
        return None

    try:
        txns = await list_shopify_order_transactions(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
        )
        if isinstance(txns, list):
            order["transactions"] = txns
    except Exception:
        pass

    return order


async def _create_shopify_draft_order_from_quote(
    *,
    order_id: str,
    order: Dict[str, Any],
    shop_domain: str,
    access_token: str,
    customer_email: str,
    shopify_shipping: Dict[str, Any],
    currency_code: str,
    pricing_quote_meta: Dict[str, Any],
    shopify_tags: List[str],
    discount_note_attributes: List[Dict[str, str]],
) -> Dict[str, Any]:
    from services.shopify_graphql_client import shopify_admin_graphql

    api_version = str(os.getenv("SHOPIFY_DRAFT_ORDER_GRAPHQL_API_VERSION", "2025-10") or "").strip() or "2025-10"
    draft_order_input = _build_shopify_draft_order_input(
        order_id=order_id,
        order=order,
        pricing_quote_meta=pricing_quote_meta,
        customer_email=customer_email,
        shopify_shipping=shopify_shipping,
        currency_code=currency_code,
        shopify_tags=shopify_tags,
        discount_note_attributes=discount_note_attributes,
    )

    create_mutation = """
    mutation PivotaDraftOrderCreate($input: DraftOrderInput!) {
      draftOrderCreate(input: $input) {
        draftOrder {
          id
          legacyResourceId
          name
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    create_data = await shopify_admin_graphql(
        shop_domain=shop_domain,
        access_token=access_token,
        query=create_mutation,
        variables={"input": draft_order_input},
        api_version=api_version,
        timeout_s=20.0,
    )
    create_payload = create_data.get("draftOrderCreate") if isinstance(create_data, dict) else None
    create_user_errors = _format_shopify_user_errors((create_payload or {}).get("userErrors"))
    if create_user_errors:
        raise ValueError(f"draftOrderCreate userErrors={json.dumps(create_user_errors, ensure_ascii=True)}")

    draft_order = (create_payload or {}).get("draftOrder") if isinstance(create_payload, dict) else None
    draft_order_gid = str((draft_order or {}).get("id") or "").strip()
    if not draft_order_gid:
        raise ValueError("draftOrderCreate returned no draft order id")

    complete_mutation = """
    mutation PivotaDraftOrderComplete($id: ID!, $sourceName: String) {
      draftOrderComplete(id: $id, sourceName: $sourceName) {
        draftOrder {
          id
          legacyResourceId
          order {
            id
            legacyResourceId
            name
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    complete_data = await shopify_admin_graphql(
        shop_domain=shop_domain,
        access_token=access_token,
        query=complete_mutation,
        variables={"id": draft_order_gid, "sourceName": "pivota"},
        api_version=api_version,
        timeout_s=20.0,
    )
    complete_payload = complete_data.get("draftOrderComplete") if isinstance(complete_data, dict) else None
    complete_user_errors = _format_shopify_user_errors((complete_payload or {}).get("userErrors"))
    if complete_user_errors:
        raise ValueError(f"draftOrderComplete userErrors={json.dumps(complete_user_errors, ensure_ascii=True)}")

    completed_draft = (complete_payload or {}).get("draftOrder") if isinstance(complete_payload, dict) else None
    completed_order = (completed_draft or {}).get("order") if isinstance(completed_draft, dict) else None
    shopify_order_id = _extract_shopify_legacy_resource_id(
        (completed_order or {}).get("legacyResourceId")
        or (completed_order or {}).get("id")
    )
    if not shopify_order_id:
        raise ValueError("draftOrderComplete returned no Shopify order id")

    return {
        "shopify_order_id": shopify_order_id,
        "draft_order_id": _extract_shopify_legacy_resource_id(
            (completed_draft or {}).get("legacyResourceId")
            or (completed_draft or {}).get("id")
        ),
        "order_payload": {
            "id": shopify_order_id,
            "admin_graphql_api_id": _shopify_order_gid(shopify_order_id),
            "name": (completed_order or {}).get("name"),
        },
    }


def _reconcile_shopify_discount_order(
    *,
    order: Dict[str, Any],
    pricing_quote_meta: Dict[str, Any],
    shopify_order: Optional[Dict[str, Any]],
    transaction_amount: Decimal,
) -> Dict[str, Any]:
    mode = _shopify_discount_reconciliation_mode()
    expected_total = _money2((order or {}).get("total"))
    expected_discount = _pricing_quote_discount_total(pricing_quote_meta)
    observed = _extract_shopify_order_reconciliation_totals(shopify_order)

    mismatches: List[str] = []
    unverified: List[str] = []

    observed_total = observed.get("total")
    if observed_total is None:
        unverified.append("shopify_order_total")
    elif observed_total != expected_total:
        mismatches.append("shopify_order_total")

    observed_discount = observed.get("discount_total")
    if expected_discount > 0:
        if observed_discount is None:
            unverified.append("shopify_discount_total")
        elif observed_discount != expected_discount:
            mismatches.append("shopify_discount_total")

    observed_transaction_total = observed.get("transaction_total")
    if transaction_amount > 0:
        if observed_transaction_total is None:
            unverified.append("shopify_transaction_total")
        elif observed_transaction_total != transaction_amount:
            mismatches.append("shopify_transaction_total")

    status_value = "passed"
    if mismatches:
        status_value = "failed"
    elif unverified:
        status_value = "partial"

    return {
        "status": status_value,
        "mode": mode,
        "passed": not (mode == "fail_closed" and (mismatches or unverified)),
        "mismatches": mismatches,
        "unverified": unverified,
        "expected": {
            "pivota_total": str(expected_total),
            "pivota_discount_total": str(expected_discount),
            "psp_transaction_total": str(transaction_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        },
        "observed": {k: (str(v) if isinstance(v, Decimal) else None) for k, v in observed.items()},
    }


def _is_checkout_ui_order_create(metadata: Optional[Dict[str, Any]]) -> bool:
    meta = metadata if isinstance(metadata, dict) else {}
    ui_source = str(meta.get("ui_source") or "").strip().lower()
    created_via = str(meta.get("created_via") or "").strip().lower()
    commerce_surface = str(meta.get("commerce_surface") or meta.get("surface") or "").strip().lower()
    return (
        ui_source == "checkout_ui"
        or created_via == "checkout_ui"
        or commerce_surface == "checkout_ui"
    )


async def _update_order_shopify_sync_metadata_best_effort(
    *,
    order_id: str,
    order: Dict[str, Any],
    fields: Dict[str, Any],
) -> None:
    latest = await get_order(order_id)
    base_order = latest if isinstance(latest, dict) else order
    metadata = _coerce_dict((base_order or {}).get("metadata"))
    changed = False
    for key, value in (fields or {}).items():
        if value is None and key not in metadata:
            continue
        if metadata.get(key) != value:
            metadata[key] = value
            changed = True
    if not changed:
        return
    try:
        await update_order_row(order_id, {"metadata": metadata})
    except Exception:
        pass


async def _cancel_orphan_shopify_order_without_refund_best_effort(
    *,
    order_id: str,
    order: Dict[str, Any],
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    reason: str,
) -> Dict[str, Any]:
    if not (shop_domain and access_token and shopify_order_id):
        return {"ok": False, "skipped": True, "reason": "missing_shopify_cancel_context"}

    annotation_result: Dict[str, Any] = {}
    try:
        annotation_result = await annotate_shopify_order_best_effort(
            shop_domain=shop_domain,
            access_token=access_token,
            shopify_order_id=shopify_order_id,
            note_attributes={
                "pivota_order_id": str(order_id),
                "pivota_orphan_reason": str(reason or "merchant_order_link_blocked")[:255],
                "pivota_recovery_required": "refund_or_manual_review",
            },
            tags=["pivota-orphan-order", "pivota-order-link-blocked"],
        )
    except Exception as exc:
        annotation_result = {"ok": False, "error": str(exc)[:500]}

    cancel_result: Dict[str, Any] = {
        "ok": False,
        "shopify_order_id": shopify_order_id,
        "annotation": annotation_result,
    }
    try:
        url = f"https://{shop_domain}/admin/api/2025-10/orders/{shopify_order_id}/cancel.json"
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(
                url,
                headers={
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json",
                },
                json={
                    "reason": "other",
                    "email": False,
                    # Funds are handled by Pivota/PSP recovery. Do not trigger a Shopify refund here.
                    "refund": False,
                    "restock": True,
                },
            )
        cancel_result.update(
            {
                "ok": response.status_code in (200, 201),
                "status_code": response.status_code,
                "body": None if response.status_code in (200, 201) else (response.text or "")[:500],
            }
        )
    except Exception as exc:
        cancel_result.update({"ok": False, "error": str(exc)[:500]})

    try:
        await log_order_event(
            event_type=(
                "shopify_orphan_order_cancelled"
                if cancel_result.get("ok")
                else "shopify_orphan_order_cancel_failed"
            ),
            order_id=order_id,
            merchant_id=str((order or {}).get("merchant_id") or ""),
            total_amount=float((order or {}).get("total") or 0),
            currency=str((order or {}).get("currency") or "USD"),
            metadata={
                "shopify_order_id": shopify_order_id,
                "domain": shop_domain,
                "reason": reason,
                "cancel_result": cancel_result,
            },
        )
    except Exception:
        pass

    return cancel_result


async def _mark_merchant_order_sync_failed_best_effort(
    *,
    order_id: str,
    order: Dict[str, Any],
    platform: Optional[str],
    error: str,
    reason: str,
    retryable: bool = True,
) -> None:
    latest = await get_order(order_id)
    base_order = latest if isinstance(latest, dict) else order
    if not isinstance(base_order, dict):
        return
    if str(base_order.get("payment_status") or "").strip().lower() != "paid":
        return
    if _get_linked_platform_order(base_order):
        return

    metadata = _coerce_dict(base_order.get("metadata"))
    merchant_order = metadata.get("merchant_order") if isinstance(metadata.get("merchant_order"), dict) else {}
    merchant_order = dict(merchant_order or {})
    try:
        retry_count = int(merchant_order.get("retry_count") or 0) + 1
    except Exception:
        retry_count = 1
    merchant_order.update(
        {
            "status": "paid_merchant_order_failed",
            "requires_action": "requires_refund_or_retry",
            "platform": str(platform or "unknown").strip().lower() or "unknown",
            "last_error": str(error or "")[:1000],
            "last_failure_reason": reason,
            "last_attempt_at": datetime.utcnow().isoformat() + "Z",
            "retryable": bool(retryable),
            "retry_count": retry_count,
        }
    )
    orphan_order = metadata.get("shopify_orphan_order") if isinstance(metadata.get("shopify_orphan_order"), dict) else {}
    if orphan_order:
        merchant_order["orphan_platform_order"] = orphan_order
        merchant_order["orphan_platform_order_id"] = (
            orphan_order.get("shopify_order_id")
            or orphan_order.get("platform_order_id")
        )
        merchant_order["orphan_platform_order_recovery"] = orphan_order.get("recovery_status")
    metadata["merchant_order"] = merchant_order
    payment_recovery = metadata.get("payment_recovery") if isinstance(metadata.get("payment_recovery"), dict) else {}
    payment_recovery = dict(payment_recovery or {})
    payment_recovery.update(
        {
            "status": "requires_operator_action",
            "refund_required": True,
            "auto_void_attempted": False,
            "auto_refund_attempted": False,
            "operator_action": "retry_merchant_order_or_issue_refund",
            "reason": "payment_captured_or_unknown_before_merchant_order_write_failed",
            "last_updated_at": datetime.utcnow().isoformat() + "Z",
        }
    )
    metadata["payment_recovery"] = payment_recovery

    try:
        await update_order_row(order_id, {"metadata": metadata})
    except Exception:
        pass
    try:
        await log_order_event(
            event_type="merchant_order_sync_failed",
            order_id=order_id,
            merchant_id=str(base_order.get("merchant_id") or ""),
            total_amount=float(base_order.get("total") or 0),
            currency=str(base_order.get("currency") or "USD"),
            metadata={
                "platform": merchant_order.get("platform"),
                "status": merchant_order.get("status"),
                "requires_action": merchant_order.get("requires_action"),
                "retryable": merchant_order.get("retryable"),
                "retry_count": merchant_order.get("retry_count"),
                "reason": reason,
                "error": str(error or "")[:1000],
            },
        )
    except Exception:
        pass


def _row_to_plain_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        try:
            return dict(mapping)
        except Exception:
            pass
    try:
        return dict(row)
    except Exception:
        return {}


def _json_safe_order_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    return value


def _merchant_order_failure_summary(order: Dict[str, Any]) -> Dict[str, Any]:
    metadata = _coerce_order_metadata(order)
    merchant_order = metadata.get("merchant_order") if isinstance(metadata.get("merchant_order"), dict) else {}
    payment_recovery = metadata.get("payment_recovery") if isinstance(metadata.get("payment_recovery"), dict) else {}
    linked_order = _get_linked_platform_order(order)
    return {
        "order_id": order.get("order_id"),
        "merchant_id": order.get("merchant_id"),
        "order_status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "fulfillment_status": order.get("fulfillment_status"),
        "total": _json_safe_order_value(order.get("total")),
        "currency": order.get("currency"),
        "payment_intent_id": order.get("payment_intent_id"),
        "psp_used": order.get("psp_used"),
        "created_at": _json_safe_order_value(order.get("created_at")),
        "paid_at": _json_safe_order_value(order.get("paid_at")),
        "store_id": order.get("store_id"),
        "shopify_order_id": order.get("shopify_order_id"),
        "linked_merchant_order": linked_order,
        "merchant_order": dict(merchant_order or {}),
        "payment_recovery": dict(payment_recovery or {}),
        "operator_action": (
            (payment_recovery or {}).get("operator_action")
            or (merchant_order or {}).get("requires_action")
            or "retry_merchant_order_or_issue_refund"
        ),
    }


async def _fetch_paid_orders_missing_merchant_order(
    *,
    merchant_id: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    conditions = [
        or_(orders_table.c.is_deleted.is_(False), orders_table.c.is_deleted.is_(None)),
        orders_table.c.payment_status == "paid",
        or_(orders_table.c.shopify_order_id.is_(None), orders_table.c.shopify_order_id == ""),
    ]
    if merchant_id:
        conditions.append(orders_table.c.merchant_id == merchant_id)

    try:
        base_query = select(orders_table)
    except Exception:
        base_query = select([orders_table])

    query = (
        base_query.where(and_(*conditions))
        .order_by(orders_table.c.created_at.desc())
        .limit(max(1, min(int(limit), 1000)))
    )
    rows = await database.fetch_all(query)
    return [_row_to_plain_dict(row) for row in (rows or [])]


async def _log_merchant_order_retry_event_best_effort(
    *,
    event_type: str,
    order: Dict[str, Any],
    order_id: str,
    metadata: Dict[str, Any],
) -> None:
    try:
        await log_order_event(
            event_type=event_type,
            order_id=order_id,
            merchant_id=str((order or {}).get("merchant_id") or ""),
            total_amount=float((order or {}).get("total") or 0),
            currency=str((order or {}).get("currency") or "USD"),
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning(
            "Failed to log %s for merchant order retry: order_id=%s error=%s",
            event_type,
            order_id,
            exc,
        )


async def _update_payment_recovery_metadata_best_effort(
    *,
    order_id: str,
    order: Dict[str, Any],
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    latest = await get_order(order_id)
    base_order = latest if isinstance(latest, dict) else order
    metadata = _coerce_order_metadata(base_order)
    payment_recovery = metadata.get("payment_recovery") if isinstance(metadata.get("payment_recovery"), dict) else {}
    payment_recovery = dict(payment_recovery or {})
    payment_recovery.update(fields or {})
    payment_recovery["last_updated_at"] = datetime.utcnow().isoformat() + "Z"
    metadata["payment_recovery"] = payment_recovery
    try:
        await update_order_row(order_id, {"metadata": metadata})
    except Exception as exc:
        logger.warning(
            "Failed to update payment recovery metadata: order_id=%s error=%s",
            order_id,
            exc,
        )
    return payment_recovery


def _remaining_refundable_amount(order: Dict[str, Any]) -> Decimal:
    try:
        total = Decimal(str((order or {}).get("total") or "0"))
    except Exception:
        total = Decimal("0")
    try:
        refunded = Decimal(str((order or {}).get("total_refunded") or "0"))
    except Exception:
        refunded = Decimal("0")
    remaining = (total - refunded).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return max(Decimal("0.00"), remaining)


async def _count_sql_best_effort(sql: str, values: Dict[str, Any]) -> Dict[str, Any]:
    try:
        row = await database.fetch_one(query=sql, values=values)
        count_value = 0
        if row is not None:
            try:
                count_value = row["count"]
            except Exception:
                count_value = getattr(row, "count", 0)
        return {"count": int(count_value or 0), "available": True}
    except Exception as exc:
        return {"count": None, "available": False, "error": str(exc)[:300]}


async def _count_order_events_best_effort(
    *,
    event_type: str,
    merchant_id: Optional[str],
) -> Dict[str, Any]:
    sql = "SELECT COUNT(*) AS count FROM order_events WHERE event_type = :event_type"
    values: Dict[str, Any] = {"event_type": event_type}
    if merchant_id:
        sql += " AND merchant_id = :merchant_id"
        values["merchant_id"] = merchant_id
    return await _count_sql_best_effort(sql, values)


_WEBHOOK_ORDER_IMPACTING_PREDICATE = """
(
    COALESCE(order_id, '') <> ''
    OR LOWER(COALESCE(event_type, '')) LIKE '%payment%'
    OR LOWER(COALESCE(event_type, '')) LIKE '%charge%'
    OR LOWER(COALESCE(event_type, '')) LIKE '%capture%'
    OR LOWER(COALESCE(event_type, '')) LIKE '%authorization%'
    OR LOWER(COALESCE(event_type, '')) LIKE '%refund%'
    OR LOWER(COALESCE(event_type, '')) LIKE '%order%'
)
"""


async def _count_webhook_failed_best_effort(
    *,
    order_impacting: Optional[bool] = None,
) -> Dict[str, Any]:
    sql = "SELECT COUNT(*) AS count FROM webhook_events WHERE status = :status"
    if order_impacting is True:
        sql += f" AND {_WEBHOOK_ORDER_IMPACTING_PREDICATE}"
    elif order_impacting is False:
        sql += f" AND NOT {_WEBHOOK_ORDER_IMPACTING_PREDICATE}"
    return await _count_sql_best_effort(sql, {"status": "failed"})


def _decode_webhook_json_field(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _webhook_event_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = _decode_webhook_json_field(row.get("payload"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    obj = data.get("object") if isinstance(data.get("object"), dict) else {}
    return {
        "id": row.get("id"),
        "event_id": row.get("event_id"),
        "event_type": row.get("event_type"),
        "psp_type": row.get("psp_type"),
        "order_id": row.get("order_id"),
        "reference": row.get("reference"),
        "status": row.get("status"),
        "error_message": row.get("error_message"),
        "retry_count": row.get("retry_count"),
        "received_at": _json_safe_order_value(row.get("received_at")),
        "processed_at": _json_safe_order_value(row.get("processed_at")),
        "last_retry_at": _json_safe_order_value(row.get("last_retry_at")),
        "payload_refs": {
            "object_id": obj.get("id") or payload.get("id"),
            "payment_intent": obj.get("payment_intent"),
            "metadata_order_id": (
                (obj.get("metadata") or {}).get("order_id")
                if isinstance(obj.get("metadata"), dict)
                else None
            ),
        },
    }


async def _fetch_webhook_event_by_event_id(event_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT id, event_id, event_type, psp_type, order_id, reference,
               payload, headers, status, processed_at, error_message,
               retry_count, last_retry_at, received_at, created_at, updated_at
        FROM webhook_events
        WHERE event_id = :event_id
        ORDER BY received_at DESC
        LIMIT 1
        """,
        {"event_id": event_id},
    )
    return _row_to_plain_dict(row) if row else None


async def _count_paid_merchant_order_failed_best_effort(
    *,
    merchant_id: Optional[str],
) -> Dict[str, Any]:
    if IS_POSTGRES:
        sql = """
            SELECT COUNT(*) AS count
            FROM orders
            WHERE COALESCE(is_deleted, false) = false
              AND payment_status = 'paid'
              AND COALESCE(shopify_order_id, '') = ''
              AND metadata -> 'merchant_order' ->> 'status' = 'paid_merchant_order_failed'
        """
        values: Dict[str, Any] = {}
        if merchant_id:
            sql += " AND merchant_id = :merchant_id"
            values["merchant_id"] = merchant_id
        result = await _count_sql_best_effort(sql, values)
        if result.get("available"):
            return result

    try:
        rows = await _fetch_paid_orders_missing_merchant_order(merchant_id=merchant_id, limit=1000)
    except Exception as exc:
        return {"count": None, "available": False, "error": str(exc)[:300]}
    count = 0
    for order in rows:
        metadata = _coerce_order_metadata(order)
        merchant_order = metadata.get("merchant_order") if isinstance(metadata.get("merchant_order"), dict) else {}
        if str((merchant_order or {}).get("status") or "").strip().lower() == "paid_merchant_order_failed":
            count += 1
    return {"count": count, "available": True, "sampled": len(rows) >= 1000}


# ============================================================================
# 订单创建（Agent 调用）
# ============================================================================

@router.post("/create", response_model=OrderResponse)
async def create_new_order(
    order_request: CreateOrderRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin),  # Agent 需要管理员权限
    precomputed_quote_requirement: Optional[Tuple[bool, Optional[Dict[str, Any]]]] = Depends(lambda: None),
    precomputed_loaded_quote: Optional[QuoteSnapshot] = Depends(lambda: None),
    precomputed_store_info: Optional[Dict[str, Any]] = Depends(lambda: None),
):
    """
    **创建新订单（Agent → Pivota）**

    流程：
    1. 验证商户存在且已连接 PSP
    2. 计算订单总价
    3. 创建订单记录
    4. 创建 Stripe Payment Intent
    5. 返回订单详情和支付密钥

    防御性设计：
    - 订单创建后立即记录事件日志
    - 金额使用 Decimal 精确计算
    - 支付信息与订单解耦，失败不影响订单创建
    """
    _create_order_started = time.perf_counter()
    order_id = None
    if not isinstance(precomputed_quote_requirement, tuple):
        precomputed_quote_requirement = None
    if precomputed_loaded_quote is not None and not hasattr(precomputed_loaded_quote, "quote_id"):
        precomputed_loaded_quote = None
    if precomputed_store_info is not None and not isinstance(precomputed_store_info, dict):
        precomputed_store_info = None
    try:
        _t = time.perf_counter()
        await ensure_database_ready()
        logger.info(
            "[OrderRoutes][PERF] step=ensure_database_ready duration_ms=%d order=%s",
            int((time.perf_counter() - _t) * 1000),
            order_id,
        )

        # WS11: parallelize merchant lookup and inventory availability. Quote
        # loading stays below because load_active_quote_or_raise can expire quotes.
        _parallel_started = time.perf_counter()
        merchant, inventory_result = await asyncio.gather(
            get_merchant_onboarding(order_request.merchant_id),
            check_inventory_availability(order_request.merchant_id, order_request.items),
            return_exceptions=True,
        )
        logger.info(
            "[OrderRoutes][PERF] step=parallel_early_reads duration_ms=%d order=%s",
            int((time.perf_counter() - _parallel_started) * 1000),
            order_id,
        )

        if isinstance(merchant, BaseException):
            raise merchant
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        if _is_checkout_ui_order_create(order_request.metadata) and not order_request.quote_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "CHECKOUT_QUOTE_REQUIRED",
                    "message": "quote_id is required for checkout_ui order creation",
                },
            )

        # Quote-first enforcement (PCS v0.2-a): dual guard to prevent bypass.
        _t = time.perf_counter()
        if precomputed_quote_requirement is not None:
            require_quote, require_ctx = precomputed_quote_requirement
            quote_requirement_source = "precomputed"
        else:
            from services.quote_first_enforcement import should_require_quote_for_order_create

            require_quote, require_ctx = await should_require_quote_for_order_create(
                merchant_id=order_request.merchant_id
            )
            quote_requirement_source = "fresh"
        logger.info(
            "[OrderRoutes][PERF] step=should_require_quote_for_order_create duration_ms=%d order=%s source=%s",
            int((time.perf_counter() - _t) * 1000),
            order_id,
            quote_requirement_source,
        )
        if require_quote and not order_request.quote_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "QUOTE_REQUIRED",
                    "message": "quote_id is required",
                    "context": require_ctx,
                },
            )

        # 2. 检查库存（如果商户连接了 Shopify）
        if isinstance(inventory_result, BaseException):
            raise inventory_result
        has_inventory, inventory_info = inventory_result
        if not has_inventory:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Insufficient inventory",
                    "items": inventory_info.get("items", [])
                }
            )

        # 3. 计算订单金额
        # Quote-first path: if quote_id is provided, amounts come from quote snapshot.
        pricing_quote_meta: Optional[Dict[str, Any]] = None
        if order_request.quote_id:
            quote_service = QuoteService()
            live_validation_meta: Optional[Dict[str, Any]] = None
            try:
                _t = time.perf_counter()
                if (
                    precomputed_loaded_quote is not None
                    and str(getattr(precomputed_loaded_quote, "quote_id", "") or "")
                    == str(order_request.quote_id)
                ):
                    quote = precomputed_loaded_quote
                    quote_load_source = "precomputed"
                else:
                    quote = await quote_service.load_active_quote_or_raise(
                        quote_id=order_request.quote_id
                    )
                    quote_load_source = "fresh"
                logger.info(
                    "[OrderRoutes][PERF] "
                    "step=quote_service.load_active_quote_or_raise duration_ms=%d order=%s source=%s",
                    int((time.perf_counter() - _t) * 1000),
                    order_id,
                    quote_load_source,
                )

                order_items_for_fingerprint = [
                    {
                        "product_id": it.product_id,
                        "variant_id": it.variant_id or "",
                        "quantity": it.quantity,
                    }
                    for it in (order_request.items or [])
                ]
                order_discount_codes = normalize_discount_codes(order_request.discount_codes)
                order_shipping_geo = (
                    {
                        "country": order_request.shipping_address.country,
                        "postal_code": order_request.shipping_address.postal_code,
                        "city": order_request.shipping_address.city,
                        "state": order_request.shipping_address.state,
                    }
                    if order_request.shipping_address
                    else None
                )

                order_request_fingerprint = compute_request_fingerprint(
                    merchant_id=order_request.merchant_id,
                    items=order_items_for_fingerprint,
                    discount_codes=order_discount_codes,
                    shipping_address=order_shipping_geo,
                    selected_delivery_option=order_request.selected_delivery_option,
                )

                order_request_normalized = _build_quote_drift_normalized_request(
                    items=order_items_for_fingerprint,
                    discount_codes=order_discount_codes,
                    shipping_address=order_shipping_geo,
                    selected_delivery_option=order_request.selected_delivery_option,
                )

                quote_request_json = quote.request_json if isinstance(quote.request_json, dict) else {}
                quote_request_normalized = _build_quote_drift_normalized_request(
                    items=quote_request_json.get("items") or [],
                    discount_codes=quote_request_json.get("discount_codes") or [],
                    shipping_address=quote_request_json.get("shipping_address"),
                    selected_delivery_option=quote_request_json.get("selected_delivery_option"),
                )

                drift_fields: List[str] = []
                if quote.merchant_id != order_request.merchant_id:
                    drift_fields.append("merchant_id")
                if quote_request_normalized.get("items") != order_request_normalized.get("items"):
                    drift_fields.append("items")
                if quote_request_normalized.get("discount_codes") != order_request_normalized.get(
                    "discount_codes"
                ):
                    drift_fields.append("discount_codes")
                if quote_request_normalized.get("shipping_geo") != order_request_normalized.get("shipping_geo"):
                    drift_fields.append("shipping_geo")
                if quote_request_normalized.get("selected_delivery_option") != order_request_normalized.get(
                    "selected_delivery_option"
                ):
                    drift_fields.append("selected_delivery_option")

                drift_details = {
                    "quote_id": quote.quote_id,
                    "quote_expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
                    "quote_hash_sha256": quote.quote_hash_sha256,
                    "quote_request_fingerprint": quote.request_fingerprint,
                    "order_request_fingerprint": order_request_fingerprint,
                    "drift_fields": (
                        drift_fields
                        if drift_fields
                        else ["selected_delivery_option"]
                        if order_request_fingerprint != quote.request_fingerprint
                        else []
                    ),
                    "quote_request_normalized": quote_request_normalized,
                    "order_request_normalized": order_request_normalized,
                }

                if quote.merchant_id != order_request.merchant_id:
                    raise QuoteError(
                        "QUOTE_MISMATCH",
                        "quote merchant_id mismatch",
                        debug_id=quote.debug_id,
                        details=drift_details,
                    )

                if order_request_fingerprint != quote.request_fingerprint:
                    raise QuoteError(
                        "QUOTE_MISMATCH",
                        "order request does not match quote snapshot",
                        debug_id=quote.debug_id,
                        details=drift_details,
                    )

                if _order_live_quote_revalidation_enabled():
                    skip_seconds = _fresh_quote_validate_skip_seconds()
                    quote_created_at = _coerce_utc_datetime(getattr(quote, "created_at", None))
                    quote_age_seconds = (
                        max(0.0, (datetime.now(timezone.utc) - quote_created_at).total_seconds())
                        if quote_created_at is not None
                        else None
                    )
                    items_unchanged = _quote_order_items_unchanged(
                        quote_request_json=quote.request_json,
                        order_items_for_fingerprint=order_items_for_fingerprint,
                    )
                    if (
                        skip_seconds > 0
                        and quote_age_seconds is not None
                        and quote_age_seconds <= skip_seconds
                        and items_unchanged
                    ):
                        live_validation_meta = {
                            "status": "validated",
                            "validated_via": "skip_fresh_quote",
                            "quote_age_seconds": quote_age_seconds,
                            "items_unchanged": True,
                            "fresh_quote_validate_skip_seconds": skip_seconds,
                        }
                        logger.info(
                            "[OrderRoutes][PERF] "
                            "step=validate_quote_snapshot_live_skipped reason=fresh_quote "
                            "quote_age_seconds=%d order=%s",
                            int(quote_age_seconds),
                            order_id,
                        )
                    else:
                        _t = time.perf_counter()
                        live_validation_meta = await quote_service.validate_quote_snapshot_live(
                            quote,
                            customer_email=order_request.customer_email,
                            create_replacement_quote_on_mismatch=True,
                        )
                        logger.info(
                            "[OrderRoutes][PERF] "
                            "step=quote_service.validate_quote_snapshot_live duration_ms=%d order=%s",
                            int((time.perf_counter() - _t) * 1000),
                            order_id,
                        )

                snap = quote.snapshot_json or {}
                pricing = (snap.get("pricing") or {}) if isinstance(snap, dict) else {}
                quote_currency = None
                try:
                    quote_currency = str(snap.get("currency") or "").strip().upper() if isinstance(snap, dict) else None
                except Exception:
                    quote_currency = None
                if quote_currency:
                    # Quote-first: currency is locked by the quote snapshot, not by the request payload.
                    # This prevents mismatches where amounts are from EUR but currency is defaulted to USD.
                    order_request.currency = quote_currency

                settlement_currency = None
                try:
                    settlement_currency = str(snap.get("settlement_currency") or "").strip().upper() if isinstance(snap, dict) else None
                except Exception:
                    settlement_currency = None

                checkout_url = None
                try:
                    if isinstance(snap, dict):
                        checkout_url = snap.get("checkout_url") or (snap.get("metadata") or {}).get("checkout_url")
                except Exception:
                    checkout_url = None

                subtotal = parse_decimal_money(pricing.get("subtotal"))
                discount_total = parse_decimal_money(pricing.get("discount_total"))
                shipping_fee = parse_decimal_money(pricing.get("shipping_fee"))
                tax = parse_decimal_money(pricing.get("tax"))
                total = parse_decimal_money(pricing.get("total"))

                if total <= 0:
                    fallback_subtotal = Decimal("0")
                    try:
                        raw_line_items = (snap.get("line_items") or []) if isinstance(snap, dict) else []
                        if isinstance(raw_line_items, list) and raw_line_items:
                            for li in raw_line_items:
                                if not isinstance(li, dict):
                                    continue
                                try:
                                    qty = int(li.get("quantity") or 0)
                                except Exception:
                                    qty = 0
                                if qty <= 0:
                                    continue
                                unit = (
                                    li.get("unit_price_effective")
                                    or li.get("unit_price_original")
                                    or li.get("price")
                                    or 0
                                )
                                fallback_subtotal += parse_decimal_money(unit) * Decimal(qty)
                    except Exception:
                        fallback_subtotal = Decimal("0")

                    if fallback_subtotal > 0 and subtotal <= 0:
                        logger.warning(
                            "[QuoteFirst] Quote snapshot pricing subtotal/total is zero; falling back to quote line_items",
                            extra={"merchant_id": order_request.merchant_id, "quote_id": quote.quote_id},
                        )
                        subtotal = fallback_subtotal
                        total = max(Decimal("0"), subtotal - discount_total) + shipping_fee + tax

                pricing_quote_meta = {
                    "quote_id": quote.quote_id,
                    "expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
                    "engine": quote.engine,
                    "engine_ref": quote.engine_ref,
                    "currency": quote_currency,
                    "settlement_currency": settlement_currency,
                    "checkout_url": checkout_url,
                    "request_fingerprint": quote.request_fingerprint,
                    "quote_hash_sha256": quote.quote_hash_sha256,
                    "pricing": pricing,
                    "promotion_lines": snap.get("promotion_lines") or [],
                    "discount_evidence": snap.get("discount_evidence") or {},
                    "payment_offer_evidence": snap.get("payment_offer_evidence") or {},
                    "payment_pricing": snap.get("payment_pricing") or {},
                    "savings_presentation": snap.get("savings_presentation") or {},
                    "line_items": snap.get("line_items") or [],
                }
                if live_validation_meta:
                    pricing_quote_meta["live_validation"] = live_validation_meta
                if (
                    _shopify_discount_reconciliation_mode() == "fail_closed"
                    and _pricing_quote_has_unverified_shipping(pricing_quote_meta)
                ):
                    raise QuoteError(
                        "QUOTE_SHIPPING_UNVERIFIED",
                        "quote shipping fee is not backed by a Shopify delivery option",
                        debug_id=quote.debug_id,
                        details={
                            "quote_id": quote.quote_id,
                            "shipping_evidence": (
                                (pricing_quote_meta.get("discount_evidence") or {}).get("shipping_evidence")
                                if isinstance(pricing_quote_meta.get("discount_evidence"), dict)
                                else None
                            ),
                        },
                    )
            except QuoteError as e:
                detail: Dict[str, Any] = {
                    "error": e.code,
                    "message": e.message,
                    "debug_id": e.debug_id,
                    **({"details": e.details} if getattr(e, "details", None) else {}),
                }
                if e.code == "QUOTE_STALE_REPRICE_REQUIRED":
                    replacement_quote = (
                        (e.details or {}).get("replacement_quote")
                        if isinstance(getattr(e, "details", None), dict)
                        else None
                    )
                    detail.update(
                        {
                            "status": "reprice_required",
                            "action": "review_repriced_quote",
                            "requires_user_confirmation": True,
                            "quote_required_before_purchase": True,
                            "order_created": False,
                            "payment_created": False,
                            "message": (
                                "Quote changed. Review the updated quote and confirm before payment."
                            ),
                        }
                    )
                    if isinstance(replacement_quote, dict) and replacement_quote.get("quote_id"):
                        detail["replacement_quote_id"] = replacement_quote.get("quote_id")
                raise HTTPException(
                    status_code=409,
                    detail=detail,
                )

        else:
            subtotal = sum(item.subtotal for item in order_request.items)

            # Promotions lane deleted (ADR-022): non-quote orders carry no
            # infra-side discount; quote-first orders get discounts from the
            # quote snapshot (Shopify pricing truth).
            discount_total = Decimal("0")

            shipping_fee = Decimal("0")
            tax = Decimal("0")
            total = subtotal + shipping_fee + tax

        # 4. 创建订单
        # Extract agent_id from metadata if present
        agent_id = None
        if order_request.metadata:
            agent_id = order_request.metadata.get("agent_id")

        # Determine PSP using PaymentRoutingService (merchant routing UI),
        # falling back to legacy hints only if routing config is missing.
        routing_service = PaymentRoutingService(database)
        selected_psp = None
        route_config: Dict[str, Any] = {}
        try:
            _t = time.perf_counter()
            selected_psp, route_config = await routing_service.select_psp(
                agent_id=agent_id or "",
                merchant_id=order_request.merchant_id,
                amount=float(total),
                currency=order_request.currency or "USD",
            )
            logger.info(
                "[OrderRoutes][PERF] step=routing_service.select_psp duration_ms=%d order=%s",
                int((time.perf_counter() - _t) * 1000),
                order_id,
            )
            logger.info(
                f"[OrderRoutes] Routing selected PSP '{selected_psp}' for order "
                f"{order_request.merchant_id} via payment_routes config"
            )
        except Exception as e:
            logger.error(f"[OrderRoutes] Routing selection failed, falling back to legacy PSP: {e}")
            selected_psp = None

        # Source of truth is canonical merchant_psps. Route selection and an explicit
        # provider preference can hint which active provider row to choose, but we do
        # not fall back to merchant_onboarding.psp_type for live runtime decisions.
        provider_hint = _normalize_order_provider_hint(
            selected_psp,
            order_request.preferred_psp,
        )

        # Always get psp_id for PSP metrics tracking (even if psp_type is known)
        psp_id_value = None
        # A deferred-payment (hosted/protocol) order does not charge at create time,
        # so a protocol-capable merchant may create one without a merchant_psps row
        # when the capability gate is on (fail-closed; see _resolve_active_order_psp).
        _defers_payment_for_psp = _order_defers_payment_surface(order_request.metadata or {})
        try:
            _t = time.perf_counter()
            psp_type, psp_id_value = await _resolve_active_order_psp(
                order_request.merchant_id,
                provider_hint,
                defer_payment=_defers_payment_for_psp,
            )
            logger.info(
                "[OrderRoutes][PERF] step=_resolve_active_order_psp duration_ms=%d order=%s",
                int((time.perf_counter() - _t) * 1000),
                order_id,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get PSP configuration: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to determine PSP: {str(e)}"
            )
        
        # Ensure psp_type is lowercase for consistency
        if psp_type:
            psp_type = psp_type.lower()
        
        # Validate PSP fields are set
        if not psp_type or not psp_id_value:
            logger.error(f"PSP fields incomplete: psp_type={psp_type}, psp_id={psp_id_value}")
            raise HTTPException(
                status_code=500,
                detail="Failed to determine complete PSP configuration"
            )
        
        logger.info(f"✅ PSP determined: {psp_type} (ID: {psp_id_value})")

        requested_psp_mode = "stripe_checkout" if (order_request.preferred_psp or "").lower() == "stripe_checkout" else None
        
        # 合并订单元数据并记录促销信息（如果有）
        order_metadata: Dict[str, Any] = dict(order_request.metadata or {})
        if getattr(order_request, "idempotency_key", None):
            order_metadata.setdefault("idempotency_key", str(order_request.idempotency_key))
        if getattr(order_request, "selected_payment_offer_id", None):
            order_metadata["selected_payment_offer_id"] = str(order_request.selected_payment_offer_id)
        if isinstance(getattr(order_request, "payment_method_evidence", None), dict):
            order_metadata["payment_method_evidence"] = order_request.payment_method_evidence
        if pricing_quote_meta:
            order_metadata["pricing_quote"] = pricing_quote_meta
            payment_offer_hash = stable_payment_offer_hash(pricing_quote_meta.get("payment_offer_evidence"))
            if payment_offer_hash:
                order_metadata["payment_offer_evidence_hash"] = payment_offer_hash

        order_taxonomy = build_traffic_taxonomy(
            order_metadata,
            authenticated_agent_id=_clean_text(order_metadata.get("agent_id")) if isinstance(order_metadata, dict) else None,
            caller_id=_clean_text(order_metadata.get("caller_id")) if isinstance(order_metadata, dict) else None,
            default_source_channel=_clean_text(order_metadata.get("source_channel") or order_metadata.get("source")),
            default_query_source=_clean_text(order_metadata.get("query_source")),
            default_protocol_name=_clean_text(order_metadata.get("protocol_name") or order_metadata.get("protocol")),
            default_commerce_surface=_clean_text(order_metadata.get("commerce_surface") or order_metadata.get("surface")),
        )
        order_metadata = attach_traffic_taxonomy(order_metadata, order_taxonomy)

        if has_attribution_signal(order_metadata):
            attribution_context = materialize_attribution_context(
                order_metadata,
                default_surface=str(order_metadata.get(PVT_SURFACE) or order_metadata.get("surface") or "merchant_native"),
                merchant_id=order_request.merchant_id,
            )
            for key in (
                PVT_SURFACE,
                PVT_CLICK_ID,
                PVT_PRODUCT_ID,
                PVT_VARIANT_ID,
                PVT_PROMPT_CLUSTER,
            ):
                if attribution_context.get(key):
                    order_metadata[key] = attribution_context[key]

        # Bind order to the current store connection (if any) so downstream Shopify sync
        # does not accidentally use a different store after a merchant connects another store.
        store_id_value: Optional[str] = None
        primary_store: Optional[Dict[str, Any]] = None
        try:
            _t = time.perf_counter()
            if precomputed_store_info is not None:
                primary_store = precomputed_store_info
                store_source = "precomputed"
            else:
                primary_store = await get_primary_store(order_request.merchant_id)
                store_source = "fresh"
            logger.info(
                "[OrderRoutes][PERF] step=get_primary_store duration_ms=%d order=%s source=%s",
                0 if store_source == "precomputed" else int((time.perf_counter() - _t) * 1000),
                order_id,
                store_source,
            )
            if primary_store and primary_store.get("store_id"):
                store_id_value = str(primary_store.get("store_id"))
        except Exception:
            store_id_value = None

        store_platform_for_policy = str((primary_store or {}).get("platform") or "").strip().lower() or None
        if not order_metadata.get("commerce_path"):
            legacy_policy = resolve_commerce_execution_policy(
                platform=store_platform_for_policy,
                surface=SURFACE_LEGACY_ADMIN,
            )
            order_metadata.setdefault("commerce_path", legacy_policy.commerce_path)
            order_metadata.setdefault("execution_policy", legacy_policy.as_dict())
            order_metadata.setdefault("execution_policy_version", legacy_policy.execution_policy_version)
            order_metadata.setdefault("validation_authority", legacy_policy.validation_authority)
            order_metadata.setdefault("legacy_or_fallback", legacy_policy.legacy_or_fallback)
        elif not isinstance(order_metadata.get("execution_policy"), dict):
            policy_surface = (
                SURFACE_LEGACY_ADMIN
                if order_metadata.get("legacy_or_fallback")
                else SURFACE_PUBLIC_AGENT_PURCHASE
            )
            execution_policy = resolve_commerce_execution_policy(
                platform=store_platform_for_policy,
                surface=policy_surface,
            )
            order_metadata.setdefault("execution_policy", execution_policy.as_dict())
            order_metadata.setdefault("execution_policy_version", execution_policy.execution_policy_version)
            order_metadata.setdefault("validation_authority", execution_policy.validation_authority)
            order_metadata.setdefault("legacy_or_fallback", execution_policy.legacy_or_fallback)

        if _should_use_authorization_first_order_flow(
            merchant_id=order_request.merchant_id,
            psp_type=psp_type,
            psp_mode=requested_psp_mode,
            store_info=primary_store,
        ):
            store_platform = str((primary_store or {}).get("platform") or "").strip().lower()
            order_metadata["payment_flow"] = _order_auth_first_flow_metadata(
                psp=psp_type,
                store_platform=store_platform,
            )

        order_metadata["amounts_source"] = "quote_snapshot" if pricing_quote_meta else "legacy_incomplete"
        persisted_order_items = _build_persisted_order_items(order_request.items, pricing_quote_meta)
        await _apply_server_granted_test_psp_stamp(order_metadata, order_request.merchant_id)
        enforce_live_readiness = _resolve_order_live_readiness_requirement(order_metadata, merchant_id=order_request.merchant_id)
        _t = time.perf_counter()
        explicit_preferred_provider = await _ensure_explicit_preferred_psp_available(
            merchant_id=order_request.merchant_id,
            preferred_psp=order_request.preferred_psp,
            enforce_live_readiness=enforce_live_readiness,
        )
        logger.info(
            "[OrderRoutes][PERF] step=_ensure_explicit_preferred_psp_available duration_ms=%d order=%s",
            int((time.perf_counter() - _t) * 1000),
            order_id,
        )

        order_data = {
            "merchant_id": order_request.merchant_id,
            "customer_email": order_request.customer_email,
            "items": persisted_order_items,
            "shipping_address": order_request.shipping_address.model_dump(mode="json"),
            "subtotal": float(subtotal),
            "discount_total": float(discount_total),
            "shipping_fee": float(shipping_fee),
            "tax": float(tax),
            "total": float(total),
            # "amount" field removed - use "total" instead
            "currency": order_request.currency,
            "agent_id": agent_id,  # Extract from metadata
            "agent_session_id": order_request.agent_session_id,
            "metadata": order_metadata,
            # Buyer Vault linkage (internal-only). These are nullable and may be backfilled later.
            "intent_id": str(order_metadata.get("intent_id") or "").strip() or None,
            "agent_user_ref": str(order_metadata.get("agent_user_ref") or order_metadata.get("agentUserRef") or "").strip() or None,
            "buyer_id": str(order_metadata.get("buyer_id") or "").strip() or None,
            "agent_scoped_buyer_ref": str(order_metadata.get("agent_scoped_buyer_ref") or "").strip() or None,
            "psp_used": psp_type,  # Record which PSP provider is used (lowercase)
            # Legacy fields (optional, can be null)
            "store_id": store_id_value,
            "psp_id": psp_id_value,  # Include actual PSP ID for metrics tracking
            "payment_method": None
        }
        _t = time.perf_counter()
        order_id = await create_order(order_data)
        logger.info(
            "[OrderRoutes][PERF] step=create_order duration_ms=%d order=%s",
            int((time.perf_counter() - _t) * 1000),
            order_id,
        )
        try:
            _t = time.perf_counter()
            await upsert_order_attribution_edge(
                order_id=str(order_id),
                merchant_id=order_request.merchant_id,
                metadata=order_metadata,
            )
            logger.info(
                "[OrderRoutes][PERF] step=upsert_order_attribution_edge duration_ms=%d order=%s",
                int((time.perf_counter() - _t) * 1000),
                order_id,
            )
        except Exception as attribution_exc:
            logger.warning(
                "[OrderRoutes] Failed to persist commerce attribution edge for %s: %s",
                order_id,
                attribution_exc,
            )

        payment_offer_evidence_for_events = (
            pricing_quote_meta.get("payment_offer_evidence")
            if isinstance(pricing_quote_meta, dict) and isinstance(pricing_quote_meta.get("payment_offer_evidence"), dict)
            else {}
        )
        selected_payment_offer_id = (
            str(order_request.selected_payment_offer_id)
            if getattr(order_request, "selected_payment_offer_id", None)
            else None
        )
        payment_method_evidence = (
            order_request.payment_method_evidence
            if isinstance(getattr(order_request, "payment_method_evidence", None), dict)
            else None
        )
        if selected_payment_offer_id:
            emit_payment_offer_analytics_event(
                event_type="payment_offer.selected",
                merchant_id=order_request.merchant_id,
                surface="order_create",
                evidence=payment_offer_evidence_for_events,
                selected_payment_offer_id=selected_payment_offer_id,
                payment_method_evidence=payment_method_evidence,
                order_id=str(order_id),
                adapter=psp_type,
                idempotency_key=f"payment_offer.selected:{order_id}:{selected_payment_offer_id}",
            )
        if payment_method_evidence:
            verification_status = str(payment_method_evidence.get("verification_status") or "").strip().lower()
            eligible = payment_method_evidence.get("eligible")
            emit_payment_offer_analytics_event(
                event_type="payment_offer.psp_evidence_received",
                merchant_id=order_request.merchant_id,
                surface="order_create",
                evidence=payment_offer_evidence_for_events,
                selected_payment_offer_id=selected_payment_offer_id,
                payment_method_evidence=payment_method_evidence,
                order_id=str(order_id),
                adapter=psp_type,
                idempotency_key=f"payment_offer.psp_evidence:{order_id}",
            )
            if verification_status in {"psp_verified", "verified"} or eligible is True:
                emit_payment_offer_analytics_event(
                    event_type="payment_offer.psp_verified",
                    merchant_id=order_request.merchant_id,
                    surface="order_create",
                    evidence=payment_offer_evidence_for_events,
                    selected_payment_offer_id=selected_payment_offer_id,
                    payment_method_evidence=payment_method_evidence,
                    order_id=str(order_id),
                    adapter=psp_type,
                    idempotency_key=f"payment_offer.psp_verified:{order_id}:{selected_payment_offer_id or 'none'}",
                )
            elif eligible is False or verification_status in {"rejected", "not_eligible", "unavailable"}:
                emit_payment_offer_analytics_event(
                    event_type="payment_offer.rejected",
                    merchant_id=order_request.merchant_id,
                    surface="order_create",
                    evidence=payment_offer_evidence_for_events,
                    selected_payment_offer_id=selected_payment_offer_id,
                    payment_method_evidence=payment_method_evidence,
                    order_id=str(order_id),
                    adapter=psp_type,
                    idempotency_key=f"payment_offer.rejected:{order_id}:{selected_payment_offer_id or 'none'}",
                )

        try:
            _t = time.perf_counter()
            asyncio.create_task(
                _bg_emit_merchant_webhook_event(
                    order_request.merchant_id,
                    event_type="order.created",
                    payload={
                        "order_id": str(order_id),
                        "merchant_id": str(order_request.merchant_id),
                        "customer_email": order_request.customer_email,
                        "total": float(total),
                        "currency": order_request.currency,
                        "item_count": len(order_request.items or []),
                        "psp_used": psp_type,
                    },
                )
            )
            logger.info(
                "[OrderRoutes][PERF] step=emit_merchant_webhook_event.enqueued duration_ms=%d order=%s",
                int((time.perf_counter() - _t) * 1000),
                order_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to enqueue merchant order.created webhook for %s: %s",
                order_request.merchant_id,
                exc,
            )

        # Consume quote best-effort after order creation succeeds.
        if order_request.quote_id:
            try:
                quote_service = QuoteService()
                _t = time.perf_counter()
                asyncio.create_task(
                    _bg_consume_quote_best_effort(
                        quote_service,
                        order_request.quote_id,
                        order_id=str(order_id),
                    )
                )
                logger.info(
                    "[OrderRoutes][PERF] step=quote_service.consume_quote_best_effort.enqueued duration_ms=%d order=%s",
                    int((time.perf_counter() - _t) * 1000),
                    order_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to enqueue quote consume for %s: %s",
                    order_request.quote_id,
                    str(exc)[:200],
                )

        # 5. 同步创建 Payment Intent（立即返回结果）
        payment_intent_id = None
        client_secret = None
        # For future monitoring: track a single payment_attempt row per order
        # without changing routing or PSP behavior.
        payment_attempt_id = None
        route_id_for_attempt = route_config.get("route_id") if isinstance(route_config, dict) else None
        # Unified payment action for frontends (optional, best-effort)
        payment_action: Dict[str, Any] = {}
        defer_order_payment_surface = _order_defers_payment_surface(order_metadata)
        
        try:
            # Build preferred PSP ordering from routing config (if available)
            try:
                preferred_psps = _build_order_preferred_psps(
                    route_config,
                    order_request.preferred_psp,
                )
            except Exception as pref_err:
                logger.warning(
                    f"[OrderRoutes] Failed to build preferred_psps list from route_config: {pref_err}"
                )
                preferred_psps = None

            # Attempt-level logging is handled inside MultiPSPOrchestrator (best-effort),
            # so we don't create a single aggregated payment_attempt row here.
            payment_attempt_id = None

            # 使用 MultiPSPOrchestrator，按路由配置的优先级（preferred_psps）
            # 自动在 adyen → stripe → checkout 之间切换。
            start_ts = time.monotonic()
            # Agent / 对话场景下，如果前端传了 preferred_psp = "stripe_checkout"，
            # 则通过 metadata.psp_mode 告诉 Stripe 适配器走 Checkout Session 流程，
            # 但 PSP provider 仍然是 "stripe"（由 routing 决定）。
            psp_mode = requested_psp_mode
            await _apply_server_granted_test_psp_stamp(order_metadata, order_request.merchant_id)
            enforce_live_readiness = _resolve_order_live_readiness_requirement(order_metadata, merchant_id=order_request.merchant_id)
            payment_return_url = _build_order_payment_return_url(order_id, order_metadata)
            auth_first_payment_flow = order_metadata.get("payment_flow") if isinstance(order_metadata.get("payment_flow"), dict) else {}
            auth_first_psp = str((auth_first_payment_flow or {}).get("psp") or "").strip().lower()
            auth_first_manual_capture = (
                str((auth_first_payment_flow or {}).get("mode") or "").strip().lower() == "authorization_first"
                and auth_first_psp in {"stripe", "paypal"}
            )
            if auth_first_manual_capture:
                preferred_psps = [auth_first_psp]
            auth_first_payment_metadata = {}
            if auth_first_manual_capture:
                auth_first_payment_metadata = {
                    "payment_flow": "authorization_first",
                    "capture_method": "manual",
                    "payment_capture_method": "manual",
                }
                if auth_first_psp == "stripe":
                    auth_first_payment_metadata["stripe_capture_method"] = "manual"
                elif auth_first_psp == "paypal":
                    auth_first_payment_metadata["paypal_intent"] = "AUTHORIZE"
            _t = time.perf_counter()
            if defer_order_payment_surface:
                success, payment_intent, error, psp_used = True, None, None, psp_type
            else:
                success, payment_intent, error, psp_used = await create_payment_with_failover(
                    merchant_id=order_request.merchant_id,
                    amount=total,
                    currency=order_request.currency,
                    metadata={
                        "order_id": order_id,
                        "merchant_id": order_request.merchant_id,
                        "customer_email": order_request.customer_email,
                        "route_id": route_id_for_attempt,
                        "agent_id": agent_id,
                        **(
                            {"selected_payment_offer_id": str(order_request.selected_payment_offer_id)}
                            if getattr(order_request, "selected_payment_offer_id", None)
                            else {}
                        ),
                        **(
                            {"payment_offer_evidence_hash": str(order_metadata.get("payment_offer_evidence_hash"))}
                            if order_metadata.get("payment_offer_evidence_hash")
                            else {}
                        ),
                        **(
                            {
                                PVT_SURFACE: order_metadata.get(PVT_SURFACE),
                                PVT_CLICK_ID: order_metadata.get(PVT_CLICK_ID),
                                PVT_PRODUCT_ID: order_metadata.get(PVT_PRODUCT_ID),
                                PVT_VARIANT_ID: order_metadata.get(PVT_VARIANT_ID),
                                PVT_PROMPT_CLUSTER: order_metadata.get(PVT_PROMPT_CLUSTER),
                            }
                            if has_attribution_signal(order_metadata)
                            else {}
                        ),
                        **({"psp_mode": psp_mode} if psp_mode else {}),
                        **auth_first_payment_metadata,
                        **({"return_url": payment_return_url} if payment_return_url else {}),
                    },
                    preferred_psps=preferred_psps,
                    restrict_to_preferred_psps=bool(explicit_preferred_provider or auth_first_manual_capture),
                    canonical_psp_required=True,
                    enforce_live_readiness=enforce_live_readiness,
                )
            logger.info(
                "[OrderRoutes][PERF] step=create_payment_with_failover duration_ms=%d order=%s",
                int((time.perf_counter() - _t) * 1000),
                order_id,
            )
            response_ms = int((time.monotonic() - start_ts) * 1000)

            final_psp = _finalize_order_psp_used(psp_used, psp_type)
            logger.info(
                f"[OrderRoutes] Payment intent result via MultiPSPOrchestrator: "
                f"success={success}, psp_used={final_psp}, has_intent={payment_intent is not None}, error={error}"
            )

            if defer_order_payment_surface:
                logger.info(
                    "[OrderRoutes] Payment surface deferred until submit_payment for order %s",
                    order_id,
                )
            elif success and payment_intent:
                payment_intent_id = payment_intent.id
                client_secret = getattr(payment_intent, "client_secret", None)
                psp_type = final_psp
                logger.info(f"✅ Payment intent created via {psp_type}: {payment_intent_id}")

                # Build unified payment_action for frontend / Agent
                try:
                    payment_action = build_payment_action(payment_intent, psp_used=psp_type)
                except Exception as pa_err:
                    logger.warning(
                        f"⚠️ Failed to build payment_action for order {order_id}: {pa_err}"
                    )

                # MultiPSPOrchestrator logs each PSP attempt; no aggregated update here.

                # Log redirect URL when available（Checkout / PayPal / Stripe Checkout）
                redirect_url = getattr(payment_intent, "redirect_url", None)
                if (
                    not redirect_url
                    and psp_type in ["checkout", "paypal"]
                    and client_secret
                    and isinstance(client_secret, str)
                    and client_secret.startswith("http")
                ):
                    redirect_url = client_secret
                if redirect_url:
                    logger.info(f"🔗 {psp_type.capitalize()} redirect URL: {redirect_url}")

                _t = time.perf_counter()
                await update_payment_info(
                    order_id=order_id,
                    payment_intent_id=payment_intent_id,
                    client_secret=client_secret or "",
                    payment_status="awaiting_payment",
                    psp_used=final_psp,
                )
                logger.info(
                    "[OrderRoutes][PERF] step=update_payment_info duration_ms=%d order=%s",
                    int((time.perf_counter() - _t) * 1000),
                    order_id,
                )
                try:
                    _t = time.perf_counter()
                    asyncio.create_task(
                        _bg_log_order_event(
                            event_type="order_created",
                            order_id=order_id,
                            merchant_id=order_request.merchant_id,
                            total_amount=float(total),
                            currency=order_request.currency,
                            payment_method=psp_type,
                            metadata={
                                "total": float(total),
                                "currency": order_request.currency,
                                "items_count": len(order_request.items),
                                "payment_intent_id": payment_intent_id,
                                "psp_type": psp_type,
                            },
                        )
                    )
                    logger.info(
                        "[OrderRoutes][PERF] step=log_order_event.order_created.enqueued duration_ms=%d order=%s",
                        int((time.perf_counter() - _t) * 1000),
                        order_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to enqueue order_created log_order_event for %s: %s",
                        order_id,
                        str(exc)[:200],
                    )
            else:
                logger.error(f"Payment intent creation failed via MultiPSP: {error}")
                if _order_allows_platform_checkout_fallback(order_metadata):
                    fallback_checkout_url = None
                    try:
                        if isinstance(pricing_quote_meta, dict):
                            fallback_checkout_url = pricing_quote_meta.get("checkout_url")
                    except Exception:
                        fallback_checkout_url = None

                    platform_checkout = None
                    if not fallback_checkout_url:
                        _t = time.perf_counter()
                        platform_checkout = await _get_platform_checkout_fallback_url_best_effort(
                            merchant_id=order_request.merchant_id,
                            items=order_request.items,
                            discount_codes=order_request.discount_codes,
                        )
                        logger.info(
                            "[OrderRoutes][PERF] step=_get_platform_checkout_fallback_url_best_effort.payment_failure duration_ms=%d order=%s",
                            int((time.perf_counter() - _t) * 1000),
                            order_id,
                        )

                    if (fallback_checkout_url or platform_checkout) and not payment_action:
                        psp_type = "checkout"
                        client_secret = str(fallback_checkout_url or (platform_checkout or {}).get("url"))
                        payment_action = {
                            "type": "redirect_url",
                            "url": str(fallback_checkout_url or (platform_checkout or {}).get("url")),
                            "raw": {
                                "reason": "psp_unavailable",
                                "error": error,
                                **({"platform": platform_checkout.get("platform"), "method": platform_checkout.get("method")} if platform_checkout else {}),
                            },
                        }
                        _t = time.perf_counter()
                        await log_order_event(
                            event_type="payment_fallback_platform_checkout",
                            order_id=order_id,
                            merchant_id=order_request.merchant_id,
                            total_amount=float(total),
                            currency=order_request.currency,
                            metadata={"checkout_url": str(fallback_checkout_url or (platform_checkout or {}).get("url"))},
                        )
                        logger.info(
                            "[OrderRoutes][PERF] step=log_order_event.payment_fallback_platform_checkout.payment_failure duration_ms=%d order=%s",
                            int((time.perf_counter() - _t) * 1000),
                            order_id,
                        )
                elif _platform_checkout_fallback_enabled():
                    _t = time.perf_counter()
                    await _log_fallback_pollution_attempt_best_effort(
                        order_id=order_id,
                        merchant_id=order_request.merchant_id,
                        total=total,
                        currency=order_request.currency,
                        metadata=order_metadata,
                        reason="psp_unavailable",
                        source="create_new_order.payment_failure",
                    )
                    logger.info(
                        "[OrderRoutes][PERF] step=_log_fallback_pollution_attempt_best_effort.payment_failure duration_ms=%d order=%s",
                        int((time.perf_counter() - _t) * 1000),
                        order_id,
                    )
                else:
                    logger.warning(
                        "[OrderRoutes] platform checkout fallback disabled; keeping PSP-first failure visible for order %s",
                        order_id,
                    )
                # MultiPSPOrchestrator logs each PSP attempt; no aggregated update here.
                _t = time.perf_counter()
                await log_order_event(
                    event_type="payment_intent_failed",
                    order_id=order_id,
                    merchant_id=order_request.merchant_id,
                    total_amount=float(total),
                    currency=order_request.currency,
                    metadata={"error": error, "psp_type": final_psp},
                )
                logger.info(
                    "[OrderRoutes][PERF] step=log_order_event.payment_intent_failed duration_ms=%d order=%s",
                    int((time.perf_counter() - _t) * 1000),
                    order_id,
                )
        except Exception as e:
            logger.error(f"Payment intent creation error: {e}")
            if _order_allows_platform_checkout_fallback(order_metadata):
                fallback_checkout_url = None
                try:
                    if isinstance(pricing_quote_meta, dict):
                        fallback_checkout_url = pricing_quote_meta.get("checkout_url")
                except Exception:
                    fallback_checkout_url = None

                platform_checkout = None
                if not fallback_checkout_url:
                    _t = time.perf_counter()
                    platform_checkout = await _get_platform_checkout_fallback_url_best_effort(
                        merchant_id=order_request.merchant_id,
                        items=order_request.items,
                        discount_codes=order_request.discount_codes,
                    )
                    logger.info(
                        "[OrderRoutes][PERF] step=_get_platform_checkout_fallback_url_best_effort.payment_exception duration_ms=%d order=%s",
                        int((time.perf_counter() - _t) * 1000),
                        order_id,
                    )

                if (
                    not explicit_preferred_provider
                    and (fallback_checkout_url or platform_checkout)
                    and not payment_action
                ):
                    psp_type = "checkout"
                    client_secret = str(fallback_checkout_url or (platform_checkout or {}).get("url"))
                    payment_action = {
                        "type": "redirect_url",
                        "url": str(fallback_checkout_url or (platform_checkout or {}).get("url")),
                        "raw": {
                            "reason": "psp_error",
                            "error": str(e),
                            **({"platform": platform_checkout.get("platform"), "method": platform_checkout.get("method")} if platform_checkout else {}),
                        },
                    }
                    _t = time.perf_counter()
                    await log_order_event(
                        event_type="payment_fallback_platform_checkout",
                        order_id=order_id,
                        merchant_id=order_request.merchant_id,
                        total_amount=float(total),
                        currency=order_request.currency,
                        metadata={"checkout_url": str(fallback_checkout_url or (platform_checkout or {}).get("url"))},
                    )
                    logger.info(
                        "[OrderRoutes][PERF] step=log_order_event.payment_fallback_platform_checkout.payment_exception duration_ms=%d order=%s",
                        int((time.perf_counter() - _t) * 1000),
                        order_id,
                    )
            elif _platform_checkout_fallback_enabled():
                _t = time.perf_counter()
                await _log_fallback_pollution_attempt_best_effort(
                    order_id=order_id,
                    merchant_id=order_request.merchant_id,
                    total=total,
                    currency=order_request.currency,
                    metadata=order_metadata,
                    reason="psp_error",
                    source="create_new_order.payment_exception",
                )
                logger.info(
                    "[OrderRoutes][PERF] step=_log_fallback_pollution_attempt_best_effort.payment_exception duration_ms=%d order=%s",
                    int((time.perf_counter() - _t) * 1000),
                    order_id,
                )
            else:
                logger.warning(
                    "[OrderRoutes] platform checkout fallback disabled after PSP error; keeping failure visible for order %s",
                    order_id,
                )
            # MultiPSPOrchestrator logs each PSP attempt; no aggregated update here.
            # NOTE: `error`/`final_psp` are only bound if create_payment_with_failover
            # returned. This handler also catches exceptions raised BEFORE that call
            # (e.g. an asyncpg-busy error re-raised from load_psp_configs), where
            # those names would be unbound — so log only the exception itself.
            _t = time.perf_counter()
            await log_order_event(
                event_type="payment_intent_error",
                order_id=order_id,
                merchant_id=order_request.merchant_id,
                total_amount=float(total),
                currency=order_request.currency,
                metadata={"error": str(e)},
            )
            logger.info(
                "[OrderRoutes][PERF] step=log_order_event.payment_intent_error duration_ms=%d order=%s",
                int((time.perf_counter() - _t) * 1000),
                order_id,
            )

        # 6. 返回订单信息（支付已同步创建）
        return OrderResponse(
            order_id=order_id,
            merchant_id=order_request.merchant_id,
            customer_email=order_request.customer_email,
            items=[OrderItem(**item) for item in persisted_order_items],
            shipping_address=order_request.shipping_address,
            subtotal=float(subtotal),
            discount_total=float(discount_total),
            shipping_fee=float(shipping_fee),
            tax=float(tax),
            total=float(total),
            currency=order_request.currency,
            status="pending",
            payment_status="awaiting_payment" if payment_intent_id else "pending",
            payment_intent_id=payment_intent_id,
            client_secret=client_secret,
            psp=psp_type,
            payment_action=payment_action or None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            metadata=order_metadata,
            commerce_path=order_metadata.get("commerce_path"),
            execution_policy=(
                order_metadata.get("execution_policy")
                if isinstance(order_metadata.get("execution_policy"), dict)
                else None
            ),
            legacy_or_fallback=(
                bool(order_metadata.get("legacy_or_fallback"))
                if "legacy_or_fallback" in order_metadata
                else None
            ),
            validation_authority=order_metadata.get("validation_authority"),
        )
    except DatabaseUnavailableError:
        raise database_unavailable_http_exception()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Order creation internal error: {e}")
        raise HTTPException(status_code=500, detail=f"Order creation internal error: {str(e)}")
    finally:
        logger.info(
            "[OrderRoutes][PERF] step=create_new_order_total duration_ms=%d order=%s",
            int((time.perf_counter() - _create_order_started) * 1000),
            order_id,
        )


# ============================================================================
# 支付处理
# ============================================================================

@router.post("/payment/confirm")
async def confirm_payment(
    payment_request: PaymentConfirmRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_admin)
):
    """
    **确认支付（Agent 调用）**
    
    流程：
    1. 验证订单存在
    2. 确认 Stripe Payment Intent
    3. 更新订单状态为已支付
    4. 触发履约流程（创建 Shopify 订单）
    """
    
    try:
        await ensure_database_ready()

        order = await get_order(payment_request.order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order["payment_status"] == "paid":
            return {"status": "success", "message": "Order already paid"}

        # 获取商户信息
        merchant = await get_merchant_onboarding(order["merchant_id"])
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        psp_type, psp_adapter = await _resolve_order_psp_adapter(order)
        
        # 确认支付
        success, status, error = await psp_adapter.confirm_payment(
            payment_intent_id=order["payment_intent_id"],
            payment_method_id=payment_request.payment_method_id
        )
        
        if not success:
            raise HTTPException(status_code=400, detail=f"Payment confirmation failed: {error}")

        normalized_confirm_status = str(status or "").strip().lower()
        if (
            normalized_confirm_status in _PSP_AUTHORIZED_UNCAPTURED_STATUSES
            and order_uses_authorization_first_payment(order)
        ):
            auth_result = await finalize_authorized_payment_order(
                payment_request.order_id,
                order=order,
                source_event="admin_confirm_payment",
            )
            if auth_result.get("status") == "success":
                return {
                    "status": "success",
                    "message": "Payment authorized, merchant order created, and payment captured",
                    "order_id": payment_request.order_id,
                    "payment_intent_id": order["payment_intent_id"],
                    "psp_type": psp_type,
                    "authorization_first": True,
                    "linked_merchant_order": auth_result.get("linked_merchant_order"),
                }
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "AUTHORIZATION_FIRST_FINALIZATION_FAILED",
                    "message": "Payment was authorized but merchant-order writeback or capture did not complete.",
                    "order_id": payment_request.order_id,
                    "result": auth_result,
                },
            )
        
        if status == "succeeded":
            # 标记订单已支付
            await mark_order_paid(payment_request.order_id)
            
            # 记录支付成功事件
            await log_order_event(
                event_type="payment_succeeded",
                order_id=payment_request.order_id,
                merchant_id=order["merchant_id"],
                total_amount=float(order["total"]),
                currency=str(order["currency"]),
                payment_method=psp_type,
                status="succeeded",
                metadata={
                    "payment_intent_id": order["payment_intent_id"],
                    "amount": float(order["total"]),
                    "currency": order["currency"],
                    "psp_type": psp_type
                }
            )
            
            # 后台任务：创建 Shopify 订单
            async def create_shopify_order_task():
                """创建 Shopify 订单通知商户发货"""
                try:
                    logger.info(f"Creating Shopify order for {payment_request.order_id}")
                    success = await create_shopify_order(payment_request.order_id)
                    if success:
                        logger.info(f"Shopify order created successfully for {payment_request.order_id}")
                    else:
                        logger.error(f"Failed to create Shopify order for {payment_request.order_id}")
                except Exception as e:
                    logger.error(f"Error in Shopify order creation task: {e}")
            
            background_tasks.add_task(create_shopify_order_task)

            # Legacy Phase 5.5/6 merchant→agent commission system was deprecated
            # 2026-05-23. See docs/monetization/LEGACY_COMMISSION_SYSTEM_AUDIT.md.
            # Pivota v1.3 monetization (T9 stamp → T6 rollup → T7 invoice → T8
            # settlement) is the sole post-payment economic path. Attribution +
            # stamping fire from services.psp_payment_finalizer.finalize_payment_success;
            # nothing to dispatch from this endpoint.

            return {
                "status": "success",
                "message": "Payment confirmed successfully",
                "order_id": payment_request.order_id,
                "payment_intent_id": order["payment_intent_id"],
                "psp_type": psp_type
            }
        else:
            return {
                "status": "pending",
                "message": f"Payment status: {status}",
                "payment_intent_id": order["payment_intent_id"]
            }
            
    except DatabaseUnavailableError:
        raise database_unavailable_http_exception()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payment failed: {str(e)}")


# ============================================================================
# 订单查询
# ============================================================================

@router.get("/ops/transaction-safety/metrics")
async def get_transaction_safety_metrics(
    merchant_id: Optional[str] = Query(None, description="Optional merchant_id filter for order-scoped metrics"),
    _: dict = Depends(require_admin_or_key),
):
    """
    Ops counters for transaction-safety alerting.

    These counters are intentionally read-only and best-effort. Missing optional
    event tables are reported with `available=false` so ops can distinguish zero
    events from missing instrumentation.
    """
    paid_merchant_order_failed_metric = await _count_paid_merchant_order_failed_best_effort(
        merchant_id=merchant_id,
    )
    merchant_order_retry_success_event_metric = await _count_order_events_best_effort(
        event_type="merchant_order_retry_success",
        merchant_id=merchant_id,
    )
    merchant_order_retry_failed_event_metric = await _count_order_events_best_effort(
        event_type="merchant_order_retry_failed",
        merchant_id=merchant_id,
    )

    metrics = {
        # Active unresolved risk: paid Pivota orders that still lack a merchant-platform order.
        "paid_merchant_order_failed_count": paid_merchant_order_failed_metric,
        "paid_merchant_order_failed_active_count": dict(paid_merchant_order_failed_metric),
        # Historical event counters. These do not imply an active unresolved order.
        "merchant_order_retry_success_count": merchant_order_retry_success_event_metric,
        "merchant_order_retry_success_event_count": dict(merchant_order_retry_success_event_metric),
        "merchant_order_retry_failed_count": merchant_order_retry_failed_event_metric,
        "merchant_order_retry_failed_event_count": dict(merchant_order_retry_failed_event_metric),
        "quote_revalidation_failure_count": await _count_order_events_best_effort(
            event_type="quote_revalidation_failed",
            merchant_id=merchant_id,
        ),
        "reconciliation_drift_count": await _count_order_events_best_effort(
            event_type="reconciliation_drift_detected",
            merchant_id=merchant_id,
        ),
        "payment_authorized_count": await _count_order_events_best_effort(
            event_type="payment_authorized",
            merchant_id=merchant_id,
        ),
        "payment_captured_after_merchant_order_count": await _count_order_events_best_effort(
            event_type="payment_captured_after_merchant_order",
            merchant_id=merchant_id,
        ),
        "payment_capture_failed_count": await _count_order_events_best_effort(
            event_type="payment_capture_failed",
            merchant_id=merchant_id,
        ),
        "payment_authorization_void_failed_count": await _count_order_events_best_effort(
            event_type="payment_authorization_void_failed",
            merchant_id=merchant_id,
        ),
        "webhook_duplicate_count": await _count_sql_best_effort(
            "SELECT COUNT(*) AS count FROM webhook_events WHERE status = :status",
            {"status": "duplicate"},
        ),
        "webhook_failed_count": await _count_sql_best_effort(
            "SELECT COUNT(*) AS count FROM webhook_events WHERE status = :status",
            {"status": "failed"},
        ),
        "webhook_failed_order_impacting_count": await _count_webhook_failed_best_effort(
            order_impacting=True,
        ),
        "webhook_failed_non_order_count": await _count_webhook_failed_best_effort(
            order_impacting=False,
        ),
        "fallback_pollution_attempt_count": await _count_order_events_best_effort(
            event_type="fallback_pollution_attempt",
            merchant_id=merchant_id,
        ),
    }
    return {
        "status": "success",
        "merchant_id": merchant_id,
        "metrics": metrics,
        "alert_recommendations": {
            "paid_merchant_order_failed_count": "page_if_greater_than_zero_for_live_merchants",
            "paid_merchant_order_failed_active_count": "same_as_paid_merchant_order_failed_count_current_unresolved_risk",
            "merchant_order_retry_failed_count": "historical_event_count_not_page_by_itself_check_paid_merchant_order_failed_active_count",
            "merchant_order_retry_failed_event_count": "historical_event_count_not_page_by_itself_check_paid_merchant_order_failed_active_count",
            "payment_capture_failed_count": "page_if_greater_than_zero_for_authorization_first_merchants",
            "payment_authorization_void_failed_count": "page_if_greater_than_zero_for_authorization_first_merchants",
            "webhook_failed_count": "investigate_by_event_type_not_page_by_itself",
            "webhook_failed_order_impacting_count": "alert_if_nonzero_for_more_than_one_webhook_retry_interval",
            "reconciliation_drift_count": "alert_if_nonzero_for_more_than_one_reconciliation_interval",
            "fallback_pollution_attempt_count": "page_if_greater_than_zero_direct_purchase_attempted_cache_or_external_checkout_fallback",
        },
        "metric_semantics": {
            "active_unresolved_risk": [
                "paid_merchant_order_failed_count",
                "paid_merchant_order_failed_active_count",
                "payment_capture_failed_count",
                "payment_authorization_void_failed_count",
                "webhook_failed_order_impacting_count",
                "fallback_pollution_attempt_count",
            ],
            "historical_event_counts": [
                "merchant_order_retry_success_count",
                "merchant_order_retry_success_event_count",
                "merchant_order_retry_failed_count",
                "merchant_order_retry_failed_event_count",
                "quote_revalidation_failure_count",
                "reconciliation_drift_count",
                "payment_authorized_count",
                "payment_captured_after_merchant_order_count",
            ],
        },
    }


@router.get("/ops/webhook-failures")
async def list_webhook_failures(
    limit: int = Query(50, ge=1, le=200),
    order_impacting: Optional[bool] = Query(None),
    psp_type: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    _: dict = Depends(require_admin_or_key),
):
    """
    Read-only ops view for webhook rows that still have failed status.

    Operators can use this alongside transaction-safety metrics to inspect
    historical failures before deciding whether a row is still actionable or has
    been superseded by a later successful event/order terminal state.
    """
    sql = """
        SELECT id, event_id, event_type, psp_type, order_id, reference,
               payload, headers, status, processed_at, error_message,
               retry_count, last_retry_at, received_at, created_at, updated_at
        FROM webhook_events
        WHERE status = :status
    """
    values: Dict[str, Any] = {"status": "failed", "limit": int(limit)}
    if order_impacting is True:
        sql += f" AND {_WEBHOOK_ORDER_IMPACTING_PREDICATE}"
    elif order_impacting is False:
        sql += f" AND NOT {_WEBHOOK_ORDER_IMPACTING_PREDICATE}"
    if psp_type:
        sql += " AND LOWER(COALESCE(psp_type, '')) = :psp_type"
        values["psp_type"] = str(psp_type).strip().lower()
    if event_type:
        sql += " AND event_type = :event_type"
        values["event_type"] = event_type
    sql += " ORDER BY received_at DESC LIMIT :limit"

    try:
        rows = await database.fetch_all(query=sql, values=values)
    except Exception as exc:
        return {
            "status": "unavailable",
            "available": False,
            "error": str(exc)[:300],
            "count": None,
            "events": [],
        }

    events = [_webhook_event_summary(_row_to_plain_dict(row)) for row in (rows or [])]
    return {
        "status": "success",
        "available": True,
        "count": len(events),
        "filters": {
            "order_impacting": order_impacting,
            "psp_type": psp_type,
            "event_type": event_type,
            "limit": int(limit),
        },
        "events": events,
    }


@router.post("/ops/webhook-failures/{event_id}/ack")
async def acknowledge_webhook_failure(
    event_id: str,
    reason: str = Query(..., min_length=3, max_length=500),
    _: dict = Depends(require_admin_or_key),
):
    """
    Mark a failed webhook row as `ignored` after operator review.

    This is intentionally explicit and narrow: it does not replay the webhook or
    mutate orders. It only removes a reviewed historical failure from failed
    alert counters while preserving the row and original error in the audit log.
    """
    event_id = str(event_id or "").strip()
    if not event_id:
        raise HTTPException(status_code=400, detail="event_id is required")

    event = await _fetch_webhook_event_by_event_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Webhook event not found")

    current_status = str(event.get("status") or "").strip().lower()
    if current_status != "failed":
        return {
            "status": "already_not_failed",
            "event": _webhook_event_summary(event),
            "message": "Webhook event was not in failed status.",
        }

    ack_message = json.dumps(
        {
            "acknowledged_by": "ops",
            "acknowledged_at": datetime.utcnow().isoformat() + "Z",
            "reason": str(reason or "").strip(),
            "previous_error": event.get("error_message"),
            "previous_status": current_status,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    await WebhookService.update_event_status(event_id, "ignored", ack_message)
    updated = await _fetch_webhook_event_by_event_id(event_id)
    return {
        "status": "acknowledged",
        "event_id": event_id,
        "previous_status": current_status,
        "new_status": "ignored",
        "event": _webhook_event_summary(updated or event),
    }


@router.get("/ops/merchant-order-failures")
async def list_paid_merchant_order_failures(
    merchant_id: Optional[str] = Query(None, description="Optional merchant_id filter"),
    limit: int = Query(50, ge=1, le=200),
    include_all_paid_missing: bool = Query(
        False,
        description="Include paid orders missing a merchant order even if no failure marker was written yet",
    ),
    _: dict = Depends(require_admin_or_key),
):
    """
    Ops view for paid Pivota orders that are missing merchant-platform orders.

    These orders require either an idempotent merchant-order retry or a refund/void
    decision. The endpoint intentionally exposes recovery metadata without mutating
    the order.
    """
    scan_limit = min(max(int(limit) * 5, int(limit)), 1000)
    rows = await _fetch_paid_orders_missing_merchant_order(merchant_id=merchant_id, limit=scan_limit)
    failures: List[Dict[str, Any]] = []
    for order in rows:
        if _get_linked_platform_order(order):
            continue
        metadata = _coerce_order_metadata(order)
        merchant_order = metadata.get("merchant_order") if isinstance(metadata.get("merchant_order"), dict) else {}
        merchant_status = str((merchant_order or {}).get("status") or "").strip().lower()
        if merchant_status == "paid_merchant_order_failed" or include_all_paid_missing:
            failures.append(_merchant_order_failure_summary(order))
        if len(failures) >= int(limit):
            break

    return {
        "status": "success",
        "merchant_id": merchant_id,
        "count": len(failures),
        "include_all_paid_missing": include_all_paid_missing,
        "operator_action": "retry_merchant_order_or_issue_refund",
        "auto_void_supported": False,
        "auto_refund_supported": False,
        "orders": failures,
    }


@router.post("/ops/merchant-order-failures/{order_id}/retry")
async def retry_paid_merchant_order_failure(
    order_id: str,
    _: dict = Depends(require_admin_or_key),
):
    """
    Idempotently retry merchant order writeback for a paid Pivota order.

    Duplicate retries are safe: linked orders return `already_linked`, and Shopify
    writeback still uses the existing advisory lock + Pivota order tag lookup.
    """
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if str(order.get("payment_status") or "").strip().lower() != "paid":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ORDER_NOT_PAID",
                "message": "Merchant order retry is only allowed after payment is paid.",
                "payment_status": order.get("payment_status"),
            },
        )

    linked_order = _get_linked_platform_order(order)
    if linked_order:
        return {
            "status": "already_linked",
            "order_id": order_id,
            "linked_merchant_order": linked_order,
            "message": "Merchant order is already linked; retry skipped.",
        }

    try:
        ok = await sync_order_to_connected_store(order_id)
    except Exception as exc:
        ok = False
        logger.error(
            "Merchant order retry exception: order_id=%s error=%s",
            order_id,
            exc,
            exc_info=True,
        )

    updated_order = await get_order(order_id) or order
    linked_order = _get_linked_platform_order(updated_order)
    summary = _merchant_order_failure_summary(updated_order)

    if ok and linked_order:
        await _log_merchant_order_retry_event_best_effort(
            event_type="merchant_order_retry_success",
            order_id=order_id,
            order=updated_order,
            metadata={
                "source": "ops_retry_endpoint",
                "linked_merchant_order": linked_order,
            },
        )
        return {
            "status": "success",
            "order_id": order_id,
            "linked_merchant_order": linked_order,
            "message": "Merchant order writeback succeeded.",
        }

    if ok:
        await _log_merchant_order_retry_event_best_effort(
            event_type="merchant_order_retry_pending",
            order_id=order_id,
            order=updated_order,
            metadata={
                "source": "ops_retry_endpoint",
                "reason": "retry_returned_true_but_no_linked_merchant_order_observed",
            },
        )
        return {
            "status": "pending",
            "order_id": order_id,
            "message": "Merchant order retry was suppressed or is still in progress; no linked merchant order observed yet.",
            "order": summary,
        }

    await _log_merchant_order_retry_event_best_effort(
        event_type="merchant_order_retry_failed",
        order_id=order_id,
        order=updated_order,
        metadata={
            "source": "ops_retry_endpoint",
            "merchant_order": summary.get("merchant_order"),
            "payment_recovery": summary.get("payment_recovery"),
        },
    )
    return {
        "status": "failed",
        "order_id": order_id,
        "message": "Merchant order writeback still failed; retry or refund decision required.",
        "order": summary,
    }


@router.post("/ops/merchant-order-failures/{order_id}/refund")
async def refund_paid_merchant_order_failure(
    order_id: str,
    _: dict = Depends(require_admin_or_key),
):
    """
    Idempotently refund a paid order that failed merchant order writeback.

    This is the safe captured-payment recovery path until authorization-first
    capture/void is fully wired. It refuses orders that already have a linked
    merchant order.
    """
    if not is_feature_enabled("enable_merchant_order_failure_refund"):
        raise HTTPException(
            status_code=503,
            detail={
                "error": "MERCHANT_ORDER_FAILURE_REFUND_DISABLED",
                "message": "Merchant-order failure refund recovery is disabled.",
            },
        )

    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    linked_order = _get_linked_platform_order(order)
    if linked_order:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "MERCHANT_ORDER_ALREADY_LINKED",
                "message": "Refund recovery is blocked because the merchant order is already linked.",
                "linked_merchant_order": linked_order,
            },
        )

    metadata = _coerce_order_metadata(order)
    merchant_order = metadata.get("merchant_order") if isinstance(metadata.get("merchant_order"), dict) else {}
    merchant_status = str((merchant_order or {}).get("status") or "").strip().lower()
    if merchant_status != "paid_merchant_order_failed":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "MERCHANT_ORDER_FAILURE_REQUIRED",
                "message": "Refund recovery is only allowed for paid_merchant_order_failed orders.",
                "merchant_order_status": merchant_status or None,
            },
        )

    payment_recovery = metadata.get("payment_recovery") if isinstance(metadata.get("payment_recovery"), dict) else {}
    if str((payment_recovery or {}).get("status") or "").strip().lower() == "refund_completed":
        return {
            "status": "already_refunded",
            "order_id": order_id,
            "payment_recovery": payment_recovery,
            "message": "Merchant-order failure refund was already completed.",
        }

    payment_status = str(order.get("payment_status") or "").strip().lower()
    if payment_status not in {"paid", "completed", "partially_refunded"}:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ORDER_NOT_REFUNDABLE",
                "message": "Order payment status is not refundable through merchant-order failure recovery.",
                "payment_status": order.get("payment_status"),
            },
        )

    refundable_amount = _remaining_refundable_amount(order)
    if refundable_amount <= Decimal("0.00"):
        recovery = await _update_payment_recovery_metadata_best_effort(
            order_id=order_id,
            order=order,
            fields={
                "status": "refund_completed",
                "refund_required": False,
                "operator_action": "none",
                "reason": "no_remaining_refundable_amount",
            },
        )
        return {
            "status": "already_refunded",
            "order_id": order_id,
            "payment_recovery": recovery,
            "message": "No remaining refundable amount.",
        }

    from services.refund_service import refund_service

    idempotency_key = f"merchant_order_failure_refund:{order_id}"
    refund_result = await refund_service.create_refund(
        order_id=order_id,
        amount=float(refundable_amount),
        reason="merchant_order_writeback_failed",
        source="pivota_ops_merchant_order_recovery",
        created_by="ops:merchant_order_failure",
        idempotency_key=idempotency_key,
    )
    refund_status = str((refund_result or {}).get("status") or "").strip().lower()
    success = refund_status in {"success", "duplicate"}

    if success:
        recovery = await _update_payment_recovery_metadata_best_effort(
            order_id=order_id,
            order=order,
            fields={
                "status": "refund_completed",
                "refund_required": False,
                "operator_action": "none",
                "reason": "merchant_order_writeback_failed_refunded",
                "refund_id": (refund_result or {}).get("refund_id"),
                "psp_refund_id": (refund_result or {}).get("psp_refund_id")
                or (refund_result or {}).get("platform_refund_id"),
                "refund_amount": str(refundable_amount),
                "refund_idempotency_key": idempotency_key,
                "refund_completed_at": datetime.utcnow().isoformat() + "Z",
            },
        )
        await _log_merchant_order_retry_event_best_effort(
            event_type="merchant_order_failure_refund_succeeded",
            order_id=order_id,
            order=order,
            metadata={
                "source": "ops_refund_endpoint",
                "refund_status": refund_status,
                "refund_id": (refund_result or {}).get("refund_id"),
                "psp_refund_id": (refund_result or {}).get("psp_refund_id")
                or (refund_result or {}).get("platform_refund_id"),
                "amount": str(refundable_amount),
                "idempotency_key": idempotency_key,
            },
        )
        return {
            "status": "success" if refund_status == "success" else "duplicate",
            "order_id": order_id,
            "refund": refund_result,
            "payment_recovery": recovery,
            "message": "Merchant-order failure refund completed.",
        }

    recovery = await _update_payment_recovery_metadata_best_effort(
        order_id=order_id,
        order=order,
        fields={
            "status": "refund_failed",
            "refund_required": True,
            "operator_action": "retry_refund_or_manual_psp_refund",
            "reason": "merchant_order_writeback_failed_refund_failed",
            "last_refund_error": str((refund_result or {}).get("error") or "unknown_refund_error")[:1000],
            "refund_id": (refund_result or {}).get("refund_id"),
            "refund_amount": str(refundable_amount),
            "refund_idempotency_key": idempotency_key,
        },
    )
    await _log_merchant_order_retry_event_best_effort(
        event_type="merchant_order_failure_refund_failed",
        order_id=order_id,
        order=order,
        metadata={
            "source": "ops_refund_endpoint",
            "refund_status": refund_status or "unknown",
            "refund_id": (refund_result or {}).get("refund_id"),
            "error": str((refund_result or {}).get("error") or "unknown_refund_error")[:1000],
            "amount": str(refundable_amount),
            "idempotency_key": idempotency_key,
        },
    )
    return {
        "status": "failed",
        "order_id": order_id,
        "refund": refund_result,
        "payment_recovery": recovery,
        "message": "Merchant-order failure refund failed; manual PSP refund may be required.",
    }


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order_details(
    order_id: str,
    current_user: dict = Depends(require_admin)
):
    """获取订单详情"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    return OrderResponse(
        order_id=order["order_id"],
        merchant_id=order["merchant_id"],
        customer_email=order["customer_email"],
        items=[OrderItem(**item) for item in order["items"]],
        shipping_address=order["shipping_address"],
        subtotal=order["subtotal"],
        shipping_fee=order["shipping_fee"],
        tax=order["tax"],
        total=order["total"],
        currency=order["currency"],
        status=order["status"],
        payment_status=order["payment_status"],
        fulfillment_status=order.get("fulfillment_status"),
        payment_intent_id=order.get("payment_intent_id"),
        shopify_order_id=order.get("shopify_order_id"),
        tracking_number=order.get("tracking_number"),
        created_at=order["created_at"],
        updated_at=order["updated_at"],
        paid_at=order.get("paid_at"),
        shipped_at=order.get("shipped_at"),
        agent_session_id=order.get("agent_session_id"),
        metadata=order.get("metadata")
    )


@router.get("/merchant/{merchant_id}", response_model=OrderListResponse)
async def get_merchant_orders(
    merchant_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(get_current_user)  # Allow authenticated users
):
    """获取商户的订单列表"""
    orders_list = await get_orders_by_merchant(merchant_id, status, limit, offset)
    
    return OrderListResponse(
        status="success",
        total=len(orders_list),
        orders=[
            OrderResponse(
                order_id=o["order_id"],
                merchant_id=o["merchant_id"],
                customer_email=o["customer_email"],
                items=[OrderItem(**item) for item in o["items"]],
                shipping_address=o["shipping_address"],
                subtotal=o["subtotal"],
                shipping_fee=o["shipping_fee"],
                tax=o["tax"],
                total=o["total"],
                currency=o["currency"],
                status=o["status"],
                payment_status=o["payment_status"],
                fulfillment_status=o.get("fulfillment_status"),
                payment_intent_id=o.get("payment_intent_id"),
                shopify_order_id=o.get("shopify_order_id"),
                tracking_number=o.get("tracking_number"),
                created_at=o["created_at"],
                updated_at=o["updated_at"],
                paid_at=o.get("paid_at"),
                shipped_at=o.get("shipped_at"),
                agent_session_id=o.get("agent_session_id"),
                metadata=o.get("metadata")
            ) for o in orders_list
        ]
    )


@router.get("/merchant/{merchant_id}/stats")
async def get_merchant_order_stats(
    merchant_id: str,
    current_user: dict = Depends(require_admin)
):
    """获取商户订单统计"""
    stats = await get_order_stats(merchant_id)
    return stats


# ============================================================================
# Shopify 订单创建（履约集成）
# ============================================================================

async def create_woocommerce_order(order_id: str) -> bool:
    lock_key = _platform_order_create_lock_key("woocommerce", order_id)
    async with _pg_advisory_lock_best_effort(lock_key=lock_key) as lock_acquired:
        if not lock_acquired:
            logger.info("[WooCommerce] Create already in progress; skipping: order_id=%s", order_id)
            return True

        order = await get_order(order_id)
        if not order:
            logger.error("[WooCommerce] Order %s not found", order_id)
            return False
        if _get_linked_platform_order(order):
            return True
        if not _order_payment_allows_merchant_order_write(order, platform="woocommerce"):
            logger.warning(
                "[WooCommerce] Skip create (not paid): order_id=%s payment_status=%s",
                order_id,
                order.get("payment_status"),
            )
            return False

        candidates = await _candidate_platform_stores(order, platform="woocommerce")
        if not candidates:
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={"platform": "woocommerce", "error": "active_woocommerce_store_missing"},
            )
            return False

        order_items = _as_order_items(order.get("items"))
        billing_address = _build_woocommerce_address(order)
        shipping_address = dict(billing_address)
        last_error: Optional[str] = None

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for store in candidates:
                store_url, consumer_key, consumer_secret = _parse_woocommerce_store_credentials(store)
                if not store_url or not consumer_key or not consumer_secret:
                    continue

                line_items: List[Dict[str, Any]] = []
                for item in order_items:
                    try:
                        product_id = int(str(item.get("product_id") or "").strip())
                        quantity = int(item.get("quantity") or 0)
                    except Exception:
                        last_error = "WooCommerce order item is missing a numeric product_id or quantity"
                        line_items = []
                        break
                    if quantity <= 0:
                        last_error = "WooCommerce order item quantity must be > 0"
                        line_items = []
                        break
                    line_item: Dict[str, Any] = {"product_id": product_id, "quantity": quantity}
                    variant_id = str(item.get("variant_id") or "").strip()
                    if variant_id:
                        try:
                            line_item["variation_id"] = int(variant_id)
                        except Exception:
                            last_error = "WooCommerce variation_id must be numeric"
                            line_items = []
                            break
                    unit_price = item.get("unit_price")
                    if unit_price is not None:
                        try:
                            total = Decimal(str(unit_price)) * Decimal(quantity)
                            line_item["subtotal"] = str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                            line_item["total"] = line_item["subtotal"]
                        except Exception:
                            pass
                    line_items.append(line_item)

                if not line_items:
                    continue

                payload = {
                    "status": "processing",
                    "set_paid": True,
                    "payment_method": "pivota_external",
                    "payment_method_title": "Pivota External Payment",
                    "customer_note": f"Pivota Order ID: {order_id}",
                    "billing": billing_address,
                    "shipping": shipping_address,
                    "line_items": line_items,
                }
                response = await client.post(
                    f"{store_url}/wp-json/wc/v3/orders",
                    params={"consumer_key": consumer_key, "consumer_secret": consumer_secret},
                    json=payload,
                )
                if response.status_code not in (200, 201):
                    last_error = f"WooCommerce API error {response.status_code}: {(response.text or '')[:500]}"
                    continue

                data = response.json() or {}
                platform_order_id = str(data.get("id") or "").strip()
                if not platform_order_id:
                    last_error = "WooCommerce response missing order id"
                    continue

                metadata = _merge_linked_platform_order_metadata(
                    order,
                    platform="woocommerce",
                    platform_order_id=platform_order_id,
                    platform_order_name=str(data.get("number") or platform_order_id),
                    platform_order_url=f"{store_url}/wp-admin/post.php?post={platform_order_id}&action=edit",
                    store=store,
                )
                store_id_used = str((store or {}).get("store_id") or "").strip() or None
                await update_fulfillment_info(order_id=order_id, fulfillment_status="processing")
                await update_order_row(
                    order_id,
                    {
                        "metadata": metadata,
                        **({"store_id": store_id_used} if store_id_used else {}),
                    },
                )
                await log_order_event(
                    event_type="merchant_order_created",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={
                        "platform": "woocommerce",
                        "platform_order_id": platform_order_id,
                        "store_id": store_id_used,
                        "domain": str((store or {}).get("domain") or "").strip() or None,
                    },
                )
                logger.info(
                    "[WooCommerce] ✅ Order linked: order_id=%s platform_order_id=%s store_id=%s",
                    order_id,
                    platform_order_id,
                    store_id_used,
                )
                return True

        if last_error:
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={"platform": "woocommerce", "error": last_error},
            )
        return False


async def create_bigcommerce_order(order_id: str) -> bool:
    lock_key = _platform_order_create_lock_key("bigcommerce", order_id)
    async with _pg_advisory_lock_best_effort(lock_key=lock_key) as lock_acquired:
        if not lock_acquired:
            logger.info("[BigCommerce] Create already in progress; skipping: order_id=%s", order_id)
            return True

        order = await get_order(order_id)
        if not order:
            logger.error("[BigCommerce] Order %s not found", order_id)
            return False
        if _get_linked_platform_order(order):
            return True
        if not _order_payment_allows_merchant_order_write(order, platform="bigcommerce"):
            logger.warning(
                "[BigCommerce] Skip create (not paid): order_id=%s payment_status=%s",
                order_id,
                order.get("payment_status"),
            )
            return False

        candidates = await _candidate_platform_stores(order, platform="bigcommerce")
        if not candidates:
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={"platform": "bigcommerce", "error": "active_bigcommerce_store_missing"},
            )
            return False

        order_items = _as_order_items(order.get("items"))
        billing_address = _build_bigcommerce_address(order)
        shipping_address = dict(billing_address)
        last_error: Optional[str] = None

        async with httpx.AsyncClient(timeout=20.0) as client:
            for store in candidates:
                store_hash, access_token, client_id, store_domain = _parse_bigcommerce_store_credentials(store)
                if not store_hash or not access_token:
                    continue

                headers = build_bigcommerce_headers(access_token, client_id)
                status_id = await _resolve_bigcommerce_status_id(
                    client=client,
                    store_hash=store_hash,
                    headers=headers,
                )

                products_payload: List[Dict[str, Any]] = []
                try:
                    for item in order_items:
                        product_id = int(str(item.get("product_id") or "").strip())
                        quantity = int(item.get("quantity") or 0)
                        if quantity <= 0:
                            raise ValueError("BigCommerce order item quantity must be > 0")
                        line_item: Dict[str, Any] = {"product_id": product_id, "quantity": quantity}
                        variant_id = str(item.get("variant_id") or "").strip()
                        if variant_id:
                            product_options = await _fetch_bigcommerce_variant_product_options(
                                client=client,
                                store_hash=store_hash,
                                headers=headers,
                                product_id=product_id,
                                variant_id=int(variant_id),
                            )
                            if product_options:
                                line_item["product_options"] = product_options
                        products_payload.append(line_item)
                except Exception as exc:
                    last_error = f"BigCommerce item mapping failed: {exc}"
                    continue

                payload = {
                    "billing_address": billing_address,
                    "shipping_addresses": [
                        {
                            **shipping_address,
                            "shipping_method": "Pivota External Shipping",
                        }
                    ],
                    "products": products_payload,
                    "customer_message": f"Pivota Order ID: {order_id}",
                    "staff_notes": f"Pivota external payment reference: {order.get('payment_intent_id')}",
                }
                if status_id is not None:
                    payload["status_id"] = status_id

                response = await client.post(
                    f"https://api.bigcommerce.com/stores/{store_hash}/v2/orders",
                    headers=headers,
                    json=payload,
                )
                if response.status_code not in (200, 201):
                    last_error = f"BigCommerce API error {response.status_code}: {(response.text or '')[:500]}"
                    continue

                data = response.json() or {}
                platform_order_id = str(data.get("id") or "").strip()
                if not platform_order_id:
                    last_error = "BigCommerce response missing order id"
                    continue

                metadata = _merge_linked_platform_order_metadata(
                    order,
                    platform="bigcommerce",
                    platform_order_id=platform_order_id,
                    platform_order_name=str(data.get("id") or platform_order_id),
                    platform_order_url=None,
                    store=store,
                )
                store_id_used = str((store or {}).get("store_id") or "").strip() or None
                await update_fulfillment_info(order_id=order_id, fulfillment_status="processing")
                await update_order_row(
                    order_id,
                    {
                        "metadata": metadata,
                        **({"store_id": store_id_used} if store_id_used else {}),
                    },
                )
                await log_order_event(
                    event_type="merchant_order_created",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata={
                        "platform": "bigcommerce",
                        "platform_order_id": platform_order_id,
                        "store_id": store_id_used,
                        "domain": store_domain,
                    },
                )
                logger.info(
                    "[BigCommerce] ✅ Order linked: order_id=%s platform_order_id=%s store_id=%s",
                    order_id,
                    platform_order_id,
                    store_id_used,
                )
                return True

        if last_error:
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={"platform": "bigcommerce", "error": last_error},
            )
        return False


async def create_wix_order(order_id: str) -> bool:
    lock_key = _platform_order_create_lock_key("wix", order_id)
    async with _pg_advisory_lock_best_effort(lock_key=lock_key) as lock_acquired:
        if not lock_acquired:
            logger.info("[Wix] Create already in progress; skipping: order_id=%s", order_id)
            return True

        order = await get_order(order_id)
        if not order:
            logger.error("[Wix] Order %s not found", order_id)
            return False
        if _get_linked_platform_order(order):
            return True
        if not _order_payment_allows_merchant_order_write(order, platform="wix"):
            logger.warning(
                "[Wix] Skip create (not paid): order_id=%s payment_status=%s",
                order_id,
                order.get("payment_status"),
            )
            return False

        candidates = await _candidate_platform_stores(order, platform="wix")
        if not candidates:
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={"platform": "wix", "error": "active_wix_store_missing"},
            )
            return False

        last_error: Optional[str] = None
        last_retryable = True
        last_failure_metadata: Dict[str, Any] = {}
        for store in candidates:
            readiness = store_order_writeback_context(
                store,
                order_id=order_id,
                platform="wix",
            )
            if not readiness.get("allowed"):
                metadata = {
                    "platform": "wix",
                    "error": "wix_order_writeback_not_ready",
                    "retryable": False,
                    "readiness": readiness,
                }
                await log_order_event(
                    event_type="wix_order_writeback_skipped",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    metadata=metadata,
                )
                last_error = "wix_order_writeback_not_ready"
                last_retryable = False
                last_failure_metadata = metadata
                continue

            adapter_order = dict(order)
            adapter_order["store"] = store
            try:
                result = await create_wix_order_via_adapter(
                    str(order.get("merchant_id") or ""),
                    adapter_order,
                )
            except Exception as exc:
                logger.exception("[Wix] Adapter raised unexpectedly")
                result = {
                    "order_id": None,
                    "status": "error",
                    "error": "wix_order_writeback_failed",
                    "raw_response": {"message": str(exc)},
                }

            platform_order_id = str((result or {}).get("order_id") or "").strip()
            if not platform_order_id:
                raw_response = (result or {}).get("raw_response")
                message = None
                if isinstance(raw_response, dict):
                    message = raw_response.get("message") or raw_response.get("error")
                last_error = str((result or {}).get("error") or message or "wix_order_writeback_failed")[:500]
                last_retryable = bool((result or {}).get("retryable", True))
                last_failure_metadata = {"platform": "wix", "error": last_error, "retryable": last_retryable}
                if isinstance(raw_response, dict):
                    for key in (
                        "platform_order_id",
                        "number",
                        "observed_fulfillment_status",
                        "request_physical_line_items",
                        "observed_physical_line_items",
                    ):
                        value = raw_response.get(key)
                        if value is not None:
                            last_failure_metadata[key] = value
                continue

            raw_response = (result or {}).get("raw_response")
            response_dict = raw_response if isinstance(raw_response, dict) else {}
            platform_order_name = str(
                response_dict.get("number")
                or response_dict.get("orderNumber")
                or platform_order_id
            )
            metadata = _merge_linked_platform_order_metadata(
                order,
                platform="wix",
                platform_order_id=platform_order_id,
                platform_order_name=platform_order_name,
                platform_order_url=None,
                store=store,
            )
            store_id_used = str((store or {}).get("store_id") or "").strip() or None
            await update_fulfillment_info(order_id=order_id, fulfillment_status="processing")
            await update_order_row(
                order_id,
                {
                    "metadata": metadata,
                    **({"store_id": store_id_used} if store_id_used else {}),
                },
            )
            await log_order_event(
                event_type="merchant_order_created",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={
                    "platform": "wix",
                    "platform_order_id": platform_order_id,
                    "store_id": store_id_used,
                    "domain": str((store or {}).get("domain") or "").strip() or None,
                },
            )
            await log_order_event(
                event_type="wix_order_created",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata={
                    "platform": "wix",
                    "platform_order_id": platform_order_id,
                    "store_id": store_id_used,
                },
            )
            logger.info(
                "[Wix] Order linked: order_id=%s platform_order_id=%s store_id=%s",
                order_id,
                platform_order_id,
                store_id_used,
            )
            return True

        if last_error:
            failure_metadata = last_failure_metadata or {
                "platform": "wix",
                "error": last_error,
                "retryable": last_retryable,
            }
            await log_order_event(
                event_type="merchant_order_failed",
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata=failure_metadata,
            )
            await log_order_event(
                event_type=(
                    "wix_order_writeback_not_ready"
                    if last_error == "wix_order_writeback_not_ready"
                    else "wix_order_writeback_failed"
                ),
                order_id=order_id,
                merchant_id=order["merchant_id"],
                metadata=failure_metadata,
            )
        return False


async def sync_order_to_connected_store(order_id: str) -> bool:
    order = await get_order(order_id)
    if not order:
        return False
    if _get_linked_platform_order(order):
        return True

    bound_store_id = str(order.get("store_id") or "").strip() or None
    store_info = None
    if bound_store_id:
        store_info = await get_store_by_id(bound_store_id, merchant_id=str(order.get("merchant_id") or "").strip())
        if not store_info:
            logger.info(
                "[MerchantSync] Bound store missing or inactive; refusing primary-store fallback: order_id=%s store_id=%s",
                order_id,
                bound_store_id,
            )
            await _mark_merchant_order_sync_failed_best_effort(
                order_id=order_id,
                order=order,
                platform=None,
                reason="bound_store_missing_or_inactive",
                error="bound merchant store is missing or inactive",
                retryable=False,
            )
            return False
    if not store_info:
        store_info = await get_primary_store(str(order.get("merchant_id") or "").strip())
    platform = str((store_info or {}).get("platform") or "").strip().lower()
    if platform == "shopify":
        success = await _create_shopify_order_impl(order_id)
    elif platform == "woocommerce":
        success = await create_woocommerce_order(order_id)
    elif platform == "bigcommerce":
        success = await create_bigcommerce_order(order_id)
    elif platform == "wix":
        success = await create_wix_order(order_id)
    else:
        logger.info("[MerchantSync] No supported store connected for order_id=%s platform=%s", order_id, platform or None)
        success = False

    if success:
        return True
    failure_error = "merchant order creation failed or no supported store connection was available"
    failure_reason = "merchant_order_create_returned_false"
    retryable = True
    if platform == "wix" and not is_store_order_writeback_allowed(
        store_info,
        order_id=order_id,
        platform="wix",
    ):
        failure_error = "wix_order_writeback_not_ready"
        failure_reason = "wix_order_writeback_not_ready"
        retryable = False
    await _mark_merchant_order_sync_failed_best_effort(
        order_id=order_id,
        order=order,
        platform=platform or None,
        reason=failure_reason,
        error=failure_error,
        retryable=retryable,
    )
    return False


async def create_shopify_order(order_id: str) -> bool:
    """
    Legacy entrypoint retained for webhook/payment callers.

    This now dispatches to the connected merchant platform instead of assuming Shopify-only.
    """
    return await sync_order_to_connected_store(order_id)


async def _create_shopify_order_impl(order_id: str) -> bool:
    """
    在 Shopify 中创建订单（通知商户发货）
    
    防御性设计：
    - 失败不影响 Pivota 订单状态
    - 记录事件日志用于后续重试
    """
    lock_key: Optional[int] = None
    lock_acquired = True
    try:
        lock_acquired, lock_key = await _try_acquire_shopify_order_lock(order_id)
        if not lock_acquired:
            logger.info(
                "[Shopify] Duplicate create suppressed by advisory lock: order_id=%s",
                order_id,
            )
            return True

        logger.info("[Shopify] Starting order creation for %s", order_id)

        order = await get_order(order_id)
        if not order:
            logger.error("[Shopify] Order %s not found", order_id)
            return False

        existing_shopify_order_id = str(order.get("shopify_order_id") or "").strip()
        if existing_shopify_order_id:
            logger.info(
                "[Shopify] Order already linked: order_id=%s shopify_order_id=%s",
                order_id,
                existing_shopify_order_id,
            )
            return True

        if not _order_payment_allows_merchant_order_write(order, platform="shopify"):
            logger.warning(
                "[Shopify] Skip create (not paid): order_id=%s payment_status=%s",
                order_id,
                order.get("payment_status"),
            )
            return False
        logger.info(
            "[Shopify] Order data: merchant_id=%s items_count=%s has_email=%s",
            order.get("merchant_id"),
            len(order.get("items", []) or []),
            bool(str(order.get("customer_email") or "").strip()),
        )

        from services.shopify_graphql_client import shopify_admin_graphql
        from db.orders import update_order as update_order_row

        pricing_quote_meta = _pricing_quote_meta_from_order(order)
        pivota_tag = _shopify_order_tag("pivota-order-id", order_id)
        shopify_write_policy = _select_shopify_write_policy(
            order=order,
            pricing_quote_meta=pricing_quote_meta,
        )
        await _update_order_shopify_sync_metadata_best_effort(
            order_id=order_id,
            order=order,
            fields={
                "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                "receipt_policy": shopify_write_policy.get("receipt_policy"),
                "representation_status": shopify_write_policy.get("representation_status"),
                "reconciliation_status": shopify_write_policy.get("reconciliation_status"),
            },
        )

        def _token_fingerprint(token: Optional[str]) -> Optional[str]:
            if not token:
                return None
            return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

        async def _find_existing_order_id_best_effort(
            *, shop_domain: str, access_token: str
        ) -> Optional[str]:
            query = """
            query($query: String!) {
              orders(first: 5, query: $query) {
                edges {
                  node {
                    legacyResourceId
                    cancelledAt
                  }
                }
              }
            }
            """
            try:
                data = await shopify_admin_graphql(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    query=query,
                    variables={"query": f"tag:{pivota_tag}"},
                    api_version="2025-10",
                    timeout_s=10.0,
                )
                orders_node = data.get("orders") if isinstance(data, dict) else None
                edges = orders_node.get("edges") if isinstance(orders_node, dict) else None
                if isinstance(edges, list) and edges:
                    for edge in edges:
                        node = (edge or {}).get("node") or {}
                        if node.get("cancelledAt"):
                            continue
                        legacy = node.get("legacyResourceId")
                        legacy_str = str(legacy).strip() if legacy is not None else ""
                        if legacy_str:
                            return legacy_str
            except Exception:
                return None
            return None

        # Choose candidate stores:
        # - Prefer the order.store_id if it points at a Shopify store row
        # - Fall back to any active Shopify store for the merchant
        stores = await get_merchant_active_stores(order["merchant_id"])
        shopify_stores = [s for s in (stores or []) if (s or {}).get("platform") == "shopify"]
        if not shopify_stores:
            logger.error("[Shopify] No active Shopify store for merchant %s", order["merchant_id"])
            return False

        bound_store_id = str(order.get("store_id") or "").strip() or None
        bound_store = None
        if bound_store_id:
            for s in shopify_stores:
                if str((s or {}).get("store_id") or "") == bound_store_id:
                    bound_store = s
                    break

        candidates: List[Dict[str, Any]] = []
        if bound_store:
            candidates.append(bound_store)

        bound_domain = (bound_store or {}).get("domain") if bound_store else None
        for s in shopify_stores:
            if s in candidates:
                continue
            if bound_domain and (s or {}).get("domain") == bound_domain:
                candidates.append(s)
        for s in shopify_stores:
            if s in candidates:
                continue
            candidates.append(s)

        receipt_blockers = list(shopify_write_policy.get("receipt_blockers") or [])

        # REST-safe path: prefer variant_id for real Shopify products and only apply
        # quote snapshot price/discount overrides when the strategy explicitly stays on REST.
        line_items = []
        for item in order["items"]:
            has_variant = False
            if item.get("variant_id"):
                try:
                    variant_id = int(item["variant_id"])
                    line_item = {
                        "variant_id": variant_id,
                        "quantity": item["quantity"]
                    }
                    if pricing_quote_meta and str(shopify_write_policy.get("write_path") or "").startswith("rest"):
                        line_item = _apply_pricing_quote_line_item_overrides(
                            line_item=line_item,
                            order_item=item,
                            pricing_quote_meta=pricing_quote_meta,
                        )
                    line_items.append(line_item)
                    has_variant = True
                    logger.info(f"Using variant_id {variant_id} for {item.get('product_title')}")
                except (ValueError, TypeError):
                    logger.warning(f"Invalid variant_id: {item.get('variant_id')}")
            
            if not has_variant:
                line_item = {
                    "title": item.get("product_title", "Product"),
                    "quantity": item["quantity"],
                    "price": str(item["unit_price"]),
                    "taxable": False  # Custom items, tax already calculated
                }
                sku = str(item.get("sku") or "").strip()
                if sku:
                    line_item["sku"] = sku
                if pricing_quote_meta and str(shopify_write_policy.get("write_path") or "").startswith("rest"):
                    line_item = _apply_pricing_quote_line_item_overrides(
                        line_item=line_item,
                        order_item=item,
                        pricing_quote_meta=pricing_quote_meta,
                    )
                line_items.append(line_item)
                logger.info(f"Using custom line item for {item.get('product_title')}")
        
        # 转换地址格式：Pivota → Shopify
        customer_email = str(order.get("customer_email") or "").strip()
        shipping_addr = order.get("shipping_address") or {}
        raw_name = str(shipping_addr.get("name") or "").strip()
        fallback_name = str(order.get("customer_name") or "").strip()
        email_name = ""
        if customer_email and "@" in customer_email:
            email_name = customer_email.split("@", 1)[0].strip()
        full_name = raw_name or fallback_name or email_name or "Customer"
        # Shopify staff notification subjects are often customized to render the buyer identity using
        # `customer.*` and/or `billing_address.*`. If a last name is blank, Liquid templates may apply
        # a fallback like `{{ last_name | default: first_name }}` which can render duplicated names
        # (e.g. "peng peng"). We avoid this by using an invisible last name placeholder for single-token names.
        # NOTE: \u200b (ZWSP) may still be treated as "blank" by some template filters; use
        # a non-whitespace invisible character to prevent Liquid `default`/`blank` fallbacks.
        INVISIBLE_LAST_NAME = "\u2060"  # word joiner
        parts = full_name.split()
        normalized_parts = [p for p in parts if p.strip()]
        # If the name is duplicated (e.g. "peng peng"), treat it as a single-token input.
        all_same = bool(normalized_parts) and len({p.lower() for p in normalized_parts}) == 1
        include_name_field = False
        if not normalized_parts:
            first_name, last_name = "Customer", INVISIBLE_LAST_NAME
        elif len(normalized_parts) == 1 or all_same:
            first_name, last_name = normalized_parts[0], INVISIBLE_LAST_NAME
            full_name = normalized_parts[0]
        else:
            first_name, last_name = normalized_parts[0], " ".join(normalized_parts[1:])
            include_name_field = True
        shopify_shipping = {
            "first_name": first_name,
            "last_name": last_name,
            # Some templates use `billing_address.name`/`shipping_address.name` directly.
            # Avoid sending `name` for single-token inputs, as Shopify may re-parse it and
            # backfill last_name=first_name, resulting in duplicated names.
            **({"name": full_name} if include_name_field else {}),
            "address1": shipping_addr.get("address_line1", ""),
            "address2": shipping_addr.get("address_line2"),
            "city": shipping_addr.get("city", ""),
            "province": shipping_addr.get("state", ""),
            "zip": shipping_addr.get("postal_code", ""),
            "country": shipping_addr.get("country", "US"),
            "phone": shipping_addr.get("phone")
        }
        
        logger.info(
            "[Shopify] Converted address: has_name=%s country=%s",
            bool(full_name and full_name != "Customer"),
            shopify_shipping.get("country"),
        )

        # Merchant PSP payments (e.g. Stripe) complete outside Shopify. Shopify's customer order
        # confirmation email can show "Paid 0" unless a successful transaction exists at the time
        # the email is generated. We embed a best-effort external transaction during order creation
        # to keep the email accurate. We still run a post-create reconciliation (transactions API)
        # afterwards for idempotency / late-binding payment refs.
        psp_used_for_txn = infer_runtime_provider(
            psp_used=order.get("psp_used"),
            psp_id=order.get("psp_id"),
            payment_reference=order.get("payment_intent_id"),
        )
        external_payment_ref = str(order.get("payment_intent_id") or "").strip() or None
        currency_code = str(order.get("currency") or "").strip().upper() or "USD"
        try:
            order_total = Decimal(str(order.get("total") or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception:
            order_total = Decimal("0.00")

        transactions_payload: List[Dict[str, Any]] = []
        if order_total > 0:
            txn: Dict[str, Any] = {
                "kind": "sale",
                "status": "success",
                "amount": str(order_total),
                "source_name": "external",
                # Use "manual" to avoid Shopify rejecting unknown gateway names when the merchant
                # pays via an external PSP (Stripe/Adyen/etc) outside of Shopify Payments.
                "gateway": "manual",
            }
            if currency_code and len(currency_code) == 3:
                txn["currency"] = currency_code
            if external_payment_ref:
                txn["authorization"] = external_payment_ref
            transactions_payload = [txn]

        discount_tags, discount_note_attributes = _build_shopify_discount_order_annotations(
            order_id=order_id,
            pricing_quote_meta=pricing_quote_meta,
        )
        shopify_discount_codes = _build_shopify_order_discount_codes(pricing_quote_meta)
        if shopify_write_policy.get("shopify_write_strategy") != SHOPIFY_WRITE_STRATEGY_REST_SIMPLE:
            shopify_discount_codes = []
        send_receipt = bool(customer_email) and shopify_write_policy.get("receipt_policy") == SHOPIFY_RECEIPT_POLICY_SEND
        shopify_tags = ["pivota", "agent-order", pivota_tag, *discount_tags]
        shopify_shipping_lines = _build_shopify_shipping_lines(
            order=order,
            pricing_quote_meta=pricing_quote_meta,
            currency_code=currency_code,
        )

        shopify_order_data = {
            "order": {
                # Email is required for receipts; keep optional in payload in case a legacy order row is missing it.
                **({"email": customer_email} if customer_email else {}),
                # Ensure staff notification subjects that use `{{ customer.name }}` don't render empty.
                "customer": {
                    "first_name": first_name,
                    "last_name": last_name,
                    **({"email": customer_email} if customer_email else {}),
                },
                **({"transactions": transactions_payload} if transactions_payload else {}),
                "financial_status": "paid",
                "send_receipt": send_receipt,
                "send_fulfillment_receipt": send_receipt,
                "line_items": line_items,
                "shipping_lines": shopify_shipping_lines,
                "shipping_address": shopify_shipping,
                # Many templates reference billing_address.* for the buyer identity.
                "billing_address": shopify_shipping,
                **({"discount_codes": shopify_discount_codes} if shopify_discount_codes else {}),
                **({"note_attributes": discount_note_attributes} if discount_note_attributes else {}),
                "note": f"Pivota Order ID: {order_id}",
                "tags": ",".join(shopify_tags),
            }
        }
        if shopify_write_policy.get("receipt_policy") != SHOPIFY_RECEIPT_POLICY_SEND:
            try:
                await _log_shopify_receipt_suppressed_once(
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    total_amount=float(order_total),
                    currency=currency_code,
                    metadata={
                        "reason": (
                            "shopify_rest_order_cannot_faithfully_render_authoritative_quote"
                            if shopify_write_policy.get("shopify_write_strategy") != SHOPIFY_WRITE_STRATEGY_REST_LEGACY_SUPPRESSED
                            else "non_quote_snapshot_order_receipts_are_not_authoritative"
                        ),
                        "blockers": receipt_blockers,
                        "quote_id": str(pricing_quote_meta.get("quote_id") or "").strip() or None,
                        "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                        "receipt_policy": shopify_write_policy.get("receipt_policy"),
                        "representation_status": shopify_write_policy.get("representation_status"),
                    },
                )
            except Exception:
                pass

        async def _finalize_success(
            *,
            shopify_order_id: str,
            store_used: Dict[str, Any],
            shop_domain: str,
            access_token: str,
            event_type: str,
            shopify_order_payload: Optional[Dict[str, Any]] = None,
        ) -> bool:
            transaction_sync_result: Optional[Dict[str, Any]] = None

            # Reconciliation must use the post-create authoritative Shopify order state.
            # The immediate create response can be sparse, and existing-order reuse has no
            # embedded payload at all. Sync external PSP transactions first, then refetch.
            try:
                psp_used = infer_runtime_provider(
                    psp_used=order.get("psp_used"),
                    psp_id=order.get("psp_id"),
                    payment_reference=order.get("payment_intent_id"),
                )
                payment_ref = order.get("payment_intent_id") or None
                transaction_sync_result = await ensure_external_payment_transaction_best_effort(
                    shop_domain=shop_domain,
                    access_token=access_token,
                    shopify_order_id=shopify_order_id,
                    psp_used=psp_used,
                    external_payment_ref=payment_ref,
                    amount=float(order.get("total") or 0),
                    currency=str(order.get("currency") or "USD"),
                    pivota_order_id=order_id,
                )
            except Exception as e:
                logger.warning(
                    "[Shopify] Payment transaction sync failed order_id=%s shopify_order_id=%s err=%s",
                    order_id,
                    shopify_order_id,
                    str(e),
                )

            authoritative_shopify_order = shopify_order_payload
            fetched_shopify_order = await _fetch_shopify_order_reconciliation_payload_best_effort(
                shop_domain=shop_domain,
                access_token=access_token,
                shopify_order_id=shopify_order_id,
            )
            if isinstance(fetched_shopify_order, dict) and fetched_shopify_order:
                authoritative_shopify_order = fetched_shopify_order

            if pricing_quote_meta:
                reconciliation = _reconcile_shopify_discount_order(
                    order=order,
                    pricing_quote_meta=pricing_quote_meta,
                    shopify_order=authoritative_shopify_order,
                    transaction_amount=order_total,
                )
                await log_order_event(
                    event_type="shopify_discount_reconciliation",
                    order_id=order_id,
                    merchant_id=order["merchant_id"],
                    total_amount=float(order.get("total") or 0),
                    currency=str(order.get("currency") or "USD"),
                    metadata={
                        **reconciliation,
                        "shopify_order_id": shopify_order_id,
                        "store_id": str((store_used or {}).get("store_id") or "").strip() or None,
                        "domain": shop_domain,
                        "transaction_sync": transaction_sync_result,
                        "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                        "receipt_policy": shopify_write_policy.get("receipt_policy"),
                        "representation_status": shopify_write_policy.get("representation_status"),
                    },
                )
                await _update_order_shopify_sync_metadata_best_effort(
                    order_id=order_id,
                    order=order,
                    fields={
                        "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                        "receipt_policy": shopify_write_policy.get("receipt_policy"),
                        "representation_status": shopify_write_policy.get("representation_status"),
                        "reconciliation_status": "passed" if reconciliation.get("passed") else "failed",
                    },
                )
                if not reconciliation.get("passed"):
                    if shopify_write_policy.get("receipt_policy") == SHOPIFY_RECEIPT_POLICY_SEND:
                        try:
                            await _log_shopify_receipt_suppressed_once(
                                order_id=order_id,
                                merchant_id=order["merchant_id"],
                                total_amount=float(order_total),
                                currency=currency_code,
                                metadata={
                                    "reason": "shopify_reconciliation_failed_after_write",
                                    "blockers": reconciliation.get("mismatches") or [],
                                    "quote_id": str(pricing_quote_meta.get("quote_id") or "").strip() or None,
                                    "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                                    "receipt_policy": SHOPIFY_RECEIPT_POLICY_SUPPRESSED,
                                    "representation_status": shopify_write_policy.get("representation_status"),
                                },
                            )
                        except Exception:
                            pass
                    logger.error(
                        "[Shopify] Discount reconciliation blocked order link: order_id=%s shopify_order_id=%s status=%s mismatches=%s unverified=%s",
                        order_id,
                        shopify_order_id,
                        reconciliation.get("status"),
                        reconciliation.get("mismatches"),
                        reconciliation.get("unverified"),
                    )
                    cancel_result = await _cancel_orphan_shopify_order_without_refund_best_effort(
                        order_id=order_id,
                        order=order,
                        shop_domain=shop_domain,
                        access_token=access_token,
                        shopify_order_id=shopify_order_id,
                        reason="shopify_reconciliation_failed_after_write",
                    )
                    await _update_order_shopify_sync_metadata_best_effort(
                        order_id=order_id,
                        order=order,
                        fields={
                            "shopify_orphan_order": {
                                "platform": "shopify",
                                "shopify_order_id": shopify_order_id,
                                "store_id": str((store_used or {}).get("store_id") or "").strip() or None,
                                "domain": shop_domain,
                                "reason": "shopify_reconciliation_failed_after_write",
                                "reconciliation": reconciliation,
                                "recovery_status": (
                                    "cancelled_without_shopify_refund"
                                    if cancel_result.get("ok")
                                    else "requires_operator_cancel"
                                ),
                                "cancel_result": cancel_result,
                                "recorded_at": datetime.utcnow().isoformat() + "Z",
                            }
                        },
                    )
                    return False

            await update_fulfillment_info(
                order_id=order_id,
                shopify_order_id=shopify_order_id,
                fulfillment_status="processing",
            )
            store_id_used = str((store_used or {}).get("store_id") or "").strip() or None
            if store_id_used and store_id_used != bound_store_id:
                try:
                    await update_order_row(order_id, {"store_id": store_id_used})
                except Exception:
                    pass

            await log_order_event(
                event_type=event_type,
                order_id=order_id,
                merchant_id=order["merchant_id"],
                total_amount=float(order.get("total") or 0),
                currency=str(order.get("currency") or "USD"),
                metadata={
                    "shopify_order_id": shopify_order_id,
                    "store_id": store_id_used,
                    "domain": shop_domain,
                    "api_key_fp": _token_fingerprint(access_token),
                    "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                    "receipt_policy": shopify_write_policy.get("receipt_policy"),
                    "representation_status": shopify_write_policy.get("representation_status"),
                },
            )

            logger.info(
                "[Shopify] ✅ Shopify order linked: order_id=%s shopify_order_id=%s store_id=%s domain=%s",
                order_id,
                shopify_order_id,
                store_id_used,
                shop_domain,
            )
            return True

        # Concurrency guard: confirm-payment + Stripe webhook can race and both try to create.
        # Use a Postgres advisory lock when available, otherwise proceed best-effort.
        lock_key = _shopify_order_create_lock_key(order_id)
        async with _pg_advisory_lock_best_effort(lock_key=lock_key) as lock_acquired:
            if not lock_acquired:
                # Another worker is creating the Shopify order. Wait briefly for it to finish
                # and return the observed outcome (avoid duplicate creation).
                for _ in range(60):
                    await asyncio.sleep(0.2)
                    latest = await get_order(order_id)
                    if latest and str(latest.get("shopify_order_id") or "").strip():
                        return True
                logger.info("[Shopify] Create already in progress; skipping: order_id=%s", order_id)
                return False

            # Re-check after acquiring the lock in case another path linked the order just before us.
            latest = await get_order(order_id)
            latest_shopify_order_id = str((latest or {}).get("shopify_order_id") or "").strip()
            if latest_shopify_order_id:
                logger.info(
                    "[Shopify] Order linked while waiting for lock: order_id=%s shopify_order_id=%s",
                    order_id,
                    latest_shopify_order_id,
                )
                return True

            # NOTE: Shopify REST Admin API is on a legacy track; keep as-is for v0.1,
            # but plan migration to GraphQL Admin Orders API if you intend to ship as a public app.
            async with httpx.AsyncClient() as client:
                last_error: Optional[str] = None
                for store in candidates:
                    shop_domain_raw = str((store or {}).get("domain") or "").strip()
                    shop_domain = _normalize_shopify_domain(shop_domain_raw)
                    store_id = str((store or {}).get("store_id") or "").strip() or None
                    access_token, token_meta = await resolve_shopify_admin_access_token(
                        shop_domain=shop_domain,
                        api_key_raw=(store or {}).get("api_key_raw") or (store or {}).get("api_key"),
                        store_id=store_id,
                    )

                    if not shop_domain or not access_token:
                        continue

                    token_fp = _token_fingerprint(access_token)
                    logger.info(
                        "[Shopify] Attempt create: order_id=%s store_id=%s domain=%s token_fp=%s",
                        order_id,
                        store_id,
                        shop_domain,
                        token_fp,
                    )

                    # Idempotency guardrail: if Shopify already has an order with our tag, reuse it.
                    existing_id = await _find_existing_order_id_best_effort(
                        shop_domain=shop_domain, access_token=access_token
                    )
                    if existing_id:
                        return await _finalize_success(
                            shopify_order_id=existing_id,
                            store_used=store,
                            shop_domain=shop_domain,
                            access_token=access_token,
                            event_type="shopify_order_reused",
                            shopify_order_payload=None,
                        )

                    if shopify_write_policy.get("write_path") == "draft_order":
                        try:
                            draft_result = await _create_shopify_draft_order_from_quote(
                                order_id=order_id,
                                order=order,
                                shop_domain=shop_domain,
                                access_token=access_token,
                                customer_email=customer_email,
                                shopify_shipping=shopify_shipping,
                                currency_code=currency_code,
                                pricing_quote_meta=pricing_quote_meta,
                                shopify_tags=shopify_tags,
                                discount_note_attributes=discount_note_attributes,
                            )
                        except Exception as e:
                            last_error = f"Draft Order error: {type(e).__name__}: {str(e)}"
                            await log_order_event(
                                event_type="shopify_draft_order_failed",
                                order_id=order_id,
                                merchant_id=order["merchant_id"],
                                metadata={
                                    "store_id": store_id,
                                    "domain": shop_domain,
                                    "api_key_fp": token_fp,
                                    "error": last_error,
                                    "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                                    "receipt_policy": shopify_write_policy.get("receipt_policy"),
                                    "representation_status": shopify_write_policy.get("representation_status"),
                                },
                            )
                            continue

                        return await _finalize_success(
                            shopify_order_id=str(draft_result.get("shopify_order_id") or "").strip(),
                            store_used=store,
                            shop_domain=shop_domain,
                            access_token=access_token,
                            event_type="shopify_order_created",
                            shopify_order_payload=draft_result.get("order_payload"),
                        )

                    url = f"https://{shop_domain}/admin/api/2025-10/orders.json"
                    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}

                    # Retry once on transient upstream failures.
                    for attempt in range(2):
                        try:
                            response = await client.post(
                                url,
                                json=shopify_order_data,
                                headers=headers,
                                timeout=12.0,
                            )
                        except Exception as e:
                            last_error = f"{type(e).__name__}: {str(e)}"
                            if attempt == 0:
                                continue
                            await log_order_event(
                                event_type="shopify_order_error",
                                order_id=order_id,
                                merchant_id=order["merchant_id"],
                                metadata={
                                    "store_id": store_id,
                                    "domain": shop_domain,
                                    "api_key_fp": token_fp,
                                    "token_refreshed": bool((token_meta or {}).get("refreshed")),
                                    "token_refresh_error": (token_meta or {}).get("refresh_error"),
                                    "error": last_error,
                                    "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                                    "receipt_policy": shopify_write_policy.get("receipt_policy"),
                                    "representation_status": shopify_write_policy.get("representation_status"),
                                },
                            )
                            return False

                        logger.info("[Shopify] API response: %s", response.status_code)

                        if response.status_code == 201:
                            shopify_order = response.json().get("order") or {}
                            shopify_order_id = str(shopify_order.get("id") or "").strip()
                            if not shopify_order_id:
                                last_error = "Missing Shopify order id in response"
                                break
                            return await _finalize_success(
                                shopify_order_id=shopify_order_id,
                                store_used=store,
                                shop_domain=shop_domain,
                                access_token=access_token,
                                event_type="shopify_order_created",
                                shopify_order_payload=shopify_order,
                            )

                        # Auth errors: try another store row (stale token recovery).
                        if response.status_code in (401, 403):
                            error_msg = (response.text or "")[:800]
                            await log_order_event(
                                event_type="shopify_order_failed",
                                order_id=order_id,
                                merchant_id=order["merchant_id"],
                                metadata={
                                    "status_code": response.status_code,
                                    "store_id": store_id,
                                    "domain": shop_domain,
                                    "api_key_fp": token_fp,
                                    "error": error_msg,
                                    "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                                    "receipt_policy": shopify_write_policy.get("receipt_policy"),
                                    "representation_status": shopify_write_policy.get("representation_status"),
                                },
                            )
                            last_error = f"Auth failed {response.status_code}"
                            break

                        # Retryable upstream issues.
                        if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                            continue

                        error_msg = (response.text or "")[:800]
                        await log_order_event(
                            event_type="shopify_order_failed",
                            order_id=order_id,
                            merchant_id=order["merchant_id"],
                            metadata={
                                "status_code": response.status_code,
                                "store_id": store_id,
                                "domain": shop_domain,
                                "api_key_fp": token_fp,
                                "error": error_msg,
                                "shopify_write_strategy": shopify_write_policy.get("shopify_write_strategy"),
                                "receipt_policy": shopify_write_policy.get("receipt_policy"),
                                "representation_status": shopify_write_policy.get("representation_status"),
                            },
                        )
                        last_error = f"Shopify API error {response.status_code}"
                        break

                if last_error:
                    logger.error("[Shopify] ❌ Failed to create order_id=%s err=%s", order_id, last_error)
                return False
    except Exception as e:
        logger.error(f"[Shopify] ❌ Exception in create_shopify_order: {type(e).__name__}: {e}", exc_info=True)
        
        # 记录异常
        try:
            await log_order_event(
                event_type="shopify_order_error",
                order_id=order_id,
                merchant_id=order.get("merchant_id", "unknown") if order else "unknown",
                metadata={"error": str(e), "error_type": type(e).__name__}
            )
        except Exception as log_error:
            logger.error(f"[Shopify] Failed to log order event: {log_error}")
            
        return False
    finally:
        await _release_shopify_order_lock(lock_key, lock_acquired=lock_acquired)


# ============================================================================
# 订单状态更新（Admin/Webhook 调用）
# ============================================================================

@router.get("/{order_id}/debug")
async def debug_order_data(
    order_id: str,
    _: dict = Depends(require_admin_or_key),
):
    """调试端点：查看订单的原始数据结构和Shopify credentials"""
    try:
        order = await get_order(order_id)
        if not order:
            return {"error": "Order not found"}
        
        order_store_id = str(order.get("store_id") or "").strip() or None

        # Primary (latest active) store for this merchant.
        from services.merchant_store_service import get_primary_store
        primary_store = await get_primary_store(order["merchant_id"])

        # Bound store referenced by the order row (may be stale/inactive).
        bound_store = None
        if order_store_id:
            try:
                row = await database.fetch_one(
                    """
                    SELECT store_id, platform, domain, api_key, status, connected_at
                    FROM merchant_stores
                    WHERE store_id = :store_id
                    LIMIT 1
                    """,
                    {"store_id": order_store_id},
                )
                if row:
                    bound_store = dict(row)
                    bound_store["api_key_raw"] = bound_store.get("api_key")
                    bound_store["source"] = "merchant_stores"
            except Exception:
                bound_store = None

        def _summarize_store(store: Dict[str, Any] | None) -> Dict[str, Any]:
            if not store:
                return {}
            token = extract_shopify_access_token((store or {}).get("api_key_raw") or (store or {}).get("api_key"))
            token_fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else None
            return {
                "store_id": store.get("store_id"),
                "platform": store.get("platform"),
                "domain": store.get("domain"),
                "status": store.get("status"),
                "source": store.get("source"),
                "has_api_key": bool(token),
                "api_key_length": len(token) if token else 0,
                "api_key_fp": token_fp,
            }
        
        # 检查数据类型
        return {
            "order_id": order_id,
            "merchant_id": order["merchant_id"],
            "order_store_id": order_store_id,
            "bound_store": _summarize_store(bound_store),
            "primary_store": _summarize_store(primary_store),
            "data_types": {
                "items": str(type(order.get("items"))),
                "items_count": len(order.get("items", [])),
                "shipping_address": str(type(order.get("shipping_address"))),
                "has_customer_email": bool(str(order.get("customer_email") or "").strip()),
            }
        }
    except Exception as e:
        logger.error(f"Debug error: {type(e).__name__}: {e}", exc_info=True)
        return {"error": str(e), "error_type": type(e).__name__}


@router.post("/{order_id}/create-shopify")
async def trigger_shopify_order(
    order_id: str,
    _: dict = Depends(require_admin_or_key),
):
    """Manually trigger Shopify order creation for debugging"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.get("shopify_order_id"):
        return {"status": "already_exists", "shopify_order_id": order["shopify_order_id"]}
    
    if not _order_payment_allows_merchant_order_write(order, platform="shopify"):
        return {"status": "not_paid", "payment_status": order.get("payment_status")}
    
    try:
        success = await create_shopify_order(order_id)
        if success:
            updated_order = await get_order(order_id)
            return {
                "status": "success",
                "shopify_order_id": updated_order.get("shopify_order_id"),
                "message": "Shopify order created"
            }
        else:
            # 查询最近的order事件日志来获取错误
            event_query = """
                SELECT event_type, metadata, created_at
                FROM order_events
                WHERE order_id = :order_id
                ORDER BY created_at DESC
                LIMIT 5
            """
            events = await database.fetch_all(event_query, {"order_id": order_id})
            
            error_details = []
            for event in events:
                if event["event_type"] in ["shopify_order_failed", "shopify_order_error"]:
                    error_details.append({
                        "event": event["event_type"],
                        "metadata": event["metadata"],
                        "time": str(event["created_at"])
                    })
            
            return {
                "status": "failed",
                "message": "Shopify order creation failed",
                "error_details": error_details if error_details else "No error events found - check Railway logs for [Shopify] entries"
            }
    except Exception as e:
        logger.error(f"Exception in trigger_shopify_order: {type(e).__name__}: {e}", exc_info=True)
        return {"status": "error", "error": str(e), "error_type": type(e).__name__}


@router.post("/reconcile-missing-shopify")
async def reconcile_missing_shopify_orders(
    merchant_id: Optional[str] = Query(None, description="Optional merchant_id to scope reconciliation"),
    limit: int = Query(50, ge=1, le=500),
    min_age_seconds: int = Query(
        120,
        ge=0,
        le=7 * 24 * 3600,
        description="Only reconcile orders paid at least this many seconds ago",
    ),
    dry_run: bool = Query(False),
    current_user: dict = Depends(require_admin_or_key),
):
    """
    Ops endpoint: reconcile paid orders that are missing `shopify_order_id`.

    This is a guardrail against transient failures (DB busy, timeouts, stale store rows).
    Intended to be called by a cron/scheduler or manually during incidents.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=int(min_age_seconds))

    conditions = [
        orders_table.c.is_deleted.is_(False),
        orders_table.c.payment_status == "paid",
        or_(orders_table.c.shopify_order_id.is_(None), orders_table.c.shopify_order_id == ""),
        or_(
            and_(orders_table.c.paid_at.isnot(None), orders_table.c.paid_at <= cutoff),
            and_(orders_table.c.paid_at.is_(None), orders_table.c.created_at <= cutoff),
        ),
    ]
    if merchant_id:
        conditions.append(orders_table.c.merchant_id == merchant_id)

    try:
        base_query = select(orders_table.c.order_id)
    except Exception:
        # SQLAlchemy 1.x compatibility
        base_query = select([orders_table.c.order_id])

    query = (
        base_query.where(and_(*conditions))
        .order_by(orders_table.c.created_at.asc())
        .limit(int(limit))
    )
    rows = await database.fetch_all(query)
    order_ids: List[str] = []
    for row in (rows or []):
        if not row:
            continue
        order_id_value = None
        try:
            # `databases` can return row wrappers that support `__getitem__` but not `.get()`.
            order_id_value = row["order_id"]
        except Exception:
            if isinstance(row, dict):
                order_id_value = row.get("order_id")
            else:
                order_id_value = getattr(row, "order_id", None)
        if order_id_value in (None, ""):
            continue
        order_ids.append(str(order_id_value))

    if dry_run:
        return {
            "status": "success",
            "dry_run": True,
            "merchant_id": merchant_id,
            "cutoff_utc": cutoff.isoformat() + "Z",
            "candidates": order_ids,
            "count": len(order_ids),
        }

    succeeded: List[str] = []
    failed: List[Dict[str, Any]] = []
    for oid in order_ids:
        try:
            ok = await create_shopify_order(oid)
            if ok:
                succeeded.append(oid)
            else:
                failed.append({"order_id": oid, "error": "create_shopify_order returned false"})
        except Exception as e:
            failed.append({"order_id": oid, "error": f"{type(e).__name__}: {str(e)}"})

    return {
        "status": "success",
        "dry_run": False,
        "merchant_id": merchant_id,
        "cutoff_utc": cutoff.isoformat() + "Z",
        "attempted": len(order_ids),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "succeeded_orders": succeeded,
        "failed_orders": failed[:50],
    }


@router.post("/{order_id}/ship")
async def mark_order_as_shipped(
    order_id: str,
    tracking_number: str,
    background_tasks: BackgroundTasks,
    carrier: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """标记订单已发货"""
    success = await mark_order_shipped(order_id, tracking_number, carrier)
    
    if not success:
        raise HTTPException(status_code=404, detail="Order not found")
    
    order = await get_order(order_id)
    
    # 记录发货事件
    await log_order_event(
        event_type="order_shipped",
        order_id=order_id,
        merchant_id=order["merchant_id"],
        metadata={
            "tracking_number": tracking_number,
            "carrier": carrier
        }
    )

    # 后台任务：订单发货后发送评价邀请邮件
    async def send_review_invitation_task():
        try:
            internal_key = (_reviews_invitation_internal_key() or "").strip()
            if not internal_key:
                logger.info("Reviews invitation issuer disabled; skip send.")
                return
            delay = _reviews_invitation_send_delay_seconds()
            worker_enabled = (os.getenv("REVIEWS_INVITATION_WORKER_ENABLED") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if delay > 0 or worker_enabled:
                ok = await enqueue_reviews_invitation_send_job_from_order(
                    merchant_id=order["merchant_id"],
                    order_id=order_id,
                    force_reschedule=False,
                )
                logger.info(f"Reviews invitation job enqueued for order {order_id} ok={ok}")
                return
            req = SendInvitationEmailFromOrderRequest(
                merchant_id=order["merchant_id"],
                order_id=order_id,
                ttl_seconds=7 * 24 * 3600,
            )
            await send_invitation_email_from_order(
                body=req,
                response=Response(),
                x_internal_key=internal_key,
            )
            logger.info(
                f"Reviews invitation email dispatched for order {order_id}"
            )
        except HTTPException as e:
            logger.warning(
                f"Reviews invitation skipped for order {order_id}: {e.detail}"
            )
        except Exception as e:
            logger.error(
                f"Reviews invitation error for order {order_id}: {e}"
            )

    background_tasks.add_task(send_review_invitation_task)
    
    return {
        "status": "success",
        "message": "Order marked as shipped",
        "order_id": order_id,
        "tracking_number": tracking_number
    }


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    reason: Optional[str] = None,
    current_user: dict = Depends(require_admin)
):
    """取消订单"""
    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order["payment_status"] == "paid":
        raise HTTPException(
            status_code=400, 
            detail="Cannot cancel paid order. Please process refund first."
        )
    
    success = await update_order_status(
        order_id=order_id,
        status="cancelled",
        cancelled_at=datetime.now(),
        payment_status="cancelled",
        metadata={**(order.get("metadata") or {}), "cancellation_reason": reason}
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to cancel order")
    
    # 记录取消事件
    await log_order_event(
        event_type="order_cancelled",
        order_id=order_id,
        merchant_id=order["merchant_id"],
        metadata={"reason": reason}
    )
    
    return {
        "status": "success",
        "message": "Order cancelled",
        "order_id": order_id
    }
