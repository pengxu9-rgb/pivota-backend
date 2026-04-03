"""
Unified Payment Endpoint for Agent SDK
Provides production-ready payment processing with PSP integration
"""
import asyncio
import hashlib
import json
import os
import secrets
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Header
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from routes.agent_auth import AgentContext, get_agent_context, log_agent_request
from db.merchant_onboarding import get_merchant_onboarding
from db.orders import get_order, update_payment_info
from adapters.psp_adapter import get_psp_adapter
from adapters.multi_psp_orchestrator import MultiPSPOrchestrator, create_payment_with_failover
from services.payment_routing_service import PaymentRoutingService
from services.merchant_payment_initiation_service import build_payment_action
from services.merchant_psp_config_service import (
    build_runtime_adapter_kwargs,
    fetch_active_runtime_merchant_psp,
)
from db.database import database
from utils.logger import logger

router = APIRouter(prefix="/agent/v1", tags=["agent-payments"])

AGENT_PAYMENT_INITIATION_TIMEOUT_SECONDS = max(
    1.0,
    float(
        os.getenv("AGENT_PAYMENT_INITIATION_TIMEOUT_SECONDS", "25") or "25"
    ),
)

# ============================================================================
# Request/Response Models
# ============================================================================

class PaymentMethod(BaseModel):
    """Payment method details"""
    type: str = Field(..., description="card, bank_transfer, wallet")
    token: Optional[str] = Field(None, description="Payment token from PSP (e.g., Stripe token)")
    card_last4: Optional[str] = Field(None, description="Last 4 digits of card")
    brand: Optional[str] = Field(None, description="Card brand (visa, mastercard)")

class PaymentRequest(BaseModel):
    """Unified payment request"""
    order_id: str = Field(..., description="Order ID to create payment for")
    payment_method: PaymentMethod = Field(..., description="Payment method details")
    return_url: Optional[str] = Field(None, description="URL for 3DS redirect callback")
    idempotency_key: Optional[str] = Field(None, description="Prevent duplicate payments")
    save_payment_method: bool = Field(False, description="Save for future use")

class NextAction(BaseModel):
    """Next action for 3DS or additional verification"""
    type: str  # redirect_to_url, display_qr_code, etc.
    redirect_url: Optional[str] = None
    qr_code_data: Optional[str] = None

class PaymentResponse(BaseModel):
    """Unified payment response"""
    status: str  # requires_action, processing, succeeded, failed
    payment_id: str
    payment_intent_id: str
    client_secret: Optional[str] = None
    amount: float
    currency: str
    psp_used: str
    # Optional unified PSP fields for frontend/gateway usage
    # (kept flat for backward compatibility; Shopping Gateway may wrap into `payment` object)
    psp: Optional[str] = None
    payment_action: Optional[Dict[str, Any]] = None
    next_action: Optional[NextAction] = None
    error: Optional[str] = None
    created_at: str


class AuthorizePaymentRequest(BaseModel):
    """Agentic payment authorization request (PSP integration is not wired yet)."""

    mandate_id: str = Field(..., description="Buyer mandate id")
    amount: float = Field(..., gt=0, description="Amount to authorize")
    currency: str = Field(..., min_length=1, description="Currency (e.g., USD)")
    agent_user_ref: Optional[str] = Field(None, description="Agent-scoped user reference (opaque)")
    merchant_context: Optional[Dict[str, Any]] = Field(None, description="Merchant context for allowlist checks")
    shipping_address_id: Optional[str] = Field(None, description="Buyer address id (optional)")


class AuthorizePaymentResponse(BaseModel):
    authorization_token: Optional[str] = None
    expires_at: Optional[str] = None
    requires_user_confirmation: Optional[bool] = None
    confirm_url: Optional[str] = None


def _checkout_ui_base() -> str:
    return (os.getenv("CHECKOUT_UI_BASE_URL") or "https://agent.pivota.cc").rstrip("/")


def _first_non_empty_string(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str):
            trimmed = value.strip()
            if trimmed:
                return trimmed
            continue
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return None


def _coerce_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _coerce_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _resolve_order_merchant_id(order: Dict[str, Any]) -> Optional[str]:
    metadata = _coerce_json_object(order.get("metadata"))
    direct_merchant_id = _first_non_empty_string(
        order.get("resolved_merchant_id"),
        order.get("merchant_id"),
        metadata.get("resolved_merchant_id"),
        metadata.get("resolvedMerchantId"),
        metadata.get("merchant_id"),
        metadata.get("merchantId"),
    )
    if direct_merchant_id:
        return direct_merchant_id

    item_merchant_ids: List[str] = []
    for item in _coerce_json_list(order.get("items")):
        if not isinstance(item, dict):
            continue
        item_metadata = _coerce_json_object(item.get("metadata"))
        resolved = _first_non_empty_string(
            item.get("resolved_merchant_id"),
            item.get("resolvedMerchantId"),
            item.get("merchant_id"),
            item.get("merchantId"),
            item_metadata.get("resolved_merchant_id"),
            item_metadata.get("resolvedMerchantId"),
            item_metadata.get("merchant_id"),
            item_metadata.get("merchantId"),
        )
        if resolved and resolved not in item_merchant_ids:
            item_merchant_ids.append(resolved)

    if len(item_merchant_ids) == 1:
        return item_merchant_ids[0]
    return None


async def _build_existing_order_payment_surface(
    order: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    merchant_id = _resolve_order_merchant_id(order)
    if not merchant_id:
        return None

    payment_status = str(order.get("payment_status") or "").strip().lower()
    if payment_status not in {"unpaid", "pending", "processing", "requires_action"}:
        return None

    psp_used = str(order.get("psp_used") or "").strip().lower()
    payment_intent_id = str(order.get("payment_intent_id") or "").strip()
    client_secret = str(order.get("client_secret") or "").strip()
    if not psp_used or not client_secret:
        return None

    runtime_row = None
    try:
        runtime_row = await fetch_active_runtime_merchant_psp(
            merchant_id=merchant_id,
            provider=psp_used,
        )
    except Exception:
        runtime_row = None

    raw_response: Dict[str, Any] = {}
    if runtime_row:
        adapter_kwargs = build_runtime_adapter_kwargs(
            psp_used,
            api_key=runtime_row.get("api_key"),
            account_id=runtime_row.get("account_id"),
            provider_config=runtime_row.get("provider_config"),
            environment=runtime_row.get("environment"),
            secret_key=runtime_row.get("secret_key"),
        )
        if psp_used == "stripe":
            if adapter_kwargs.get("public_key"):
                raw_response["public_key"] = adapter_kwargs["public_key"]
            if adapter_kwargs.get("account_id"):
                raw_response["stripe_account"] = adapter_kwargs["account_id"]
        elif psp_used == "adyen":
            if adapter_kwargs.get("client_key"):
                raw_response["clientKey"] = adapter_kwargs["client_key"]
        elif psp_used == "checkout":
            if adapter_kwargs.get("public_key"):
                raw_response["public_key"] = adapter_kwargs["public_key"]
            if adapter_kwargs.get("processing_channel_id"):
                raw_response["processing_channel_id"] = adapter_kwargs["processing_channel_id"]

    payment_intent = SimpleNamespace(
        id=payment_intent_id or None,
        client_secret=client_secret,
        raw_response=raw_response,
        psp_type=psp_used,
    )
    payment_action = build_payment_action(payment_intent, psp_used=psp_used)
    if not payment_action or not payment_action.get("type"):
        return None

    return {
        "merchant_id": merchant_id,
        "psp_used": psp_used,
        "payment_intent_id": payment_intent_id,
        "client_secret": client_secret,
        "payment_action": payment_action,
    }


# ============================================================================
# Payment Endpoint
# ============================================================================

@router.post("/payments", response_model=PaymentResponse)
async def create_payment(
    request: PaymentRequest,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
    x_hil_approval: Optional[str] = Header(None, alias="X-HIL-Approval"),
):
    """
    Create payment for an order
    
    Flow:
    1. Validate order exists and not already paid
    2. Get merchant PSP configuration
    3. Create payment intent with PSP
    4. Handle 3DS if required
    5. Return payment status
    
    Features:
    - Automatic PSP failover
    - 3DS authentication support
    - Idempotency protection
    - Payment retry logic
    """
    try:
        # 1. Get order and validate
        order = await get_order(request.order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        if order.get("payment_status") == "paid":
            raise HTTPException(status_code=400, detail="Order already paid")
        
        merchant_id = _resolve_order_merchant_id(order)
        if not merchant_id:
            raise HTTPException(status_code=400, detail="Order merchant could not be resolved")
        
        # Get order total with fallback (support both total_amount and total fields)
        order_total = order.get("total_amount") or order.get("total")
        if order_total is None:
            raise HTTPException(status_code=400, detail="Order total not found")
        order_total = float(order_total)
        
        # 2. Verify agent has access to merchant
        if not context.can_access_merchant(merchant_id):
            raise HTTPException(status_code=403, detail="Not authorized for this merchant")

        # MVP governance (ACE v0.1): risk-tier evaluation + optional HIL enforcement (configurable).
        # Always emits immutable audit events for intent → approval → execution → receipt (best-effort).
        decision = None
        geo = None
        try:
            from mvp.governance import PolicyInput, governance

            ship = order.get("shipping_address") or {}
            if isinstance(ship, dict):
                geo = {
                    "country": (ship.get("country") or "").upper()[:2] or None,
                    "postal_code": ship.get("postal_code") or ship.get("zip"),
                    "city": ship.get("city"),
                    "state": ship.get("state") or ship.get("province"),
                }

            decision = governance.evaluate(
                PolicyInput(
                    merchant_id=str(merchant_id),
                    actor_type="agent",
                    actor_ref=str(getattr(context, "agent_id", "")) or None,
                    action="submit_payment",
                    amount=float(order_total),
                    currency=str(order.get("currency") or "USD"),
                    geo=geo,
                    consent_scopes=[],
                    approval_id=x_hil_approval,
                )
            )

            # Intent is immutable, even if later blocked.
            try:
                governance.record_audit_event(
                    merchant_id=str(merchant_id),
                    actor_type="agent",
                    actor_ref=str(getattr(context, "agent_id", "")) or None,
                    action="submit_payment.intent",
                    subject={"order_id": request.order_id},
                    request_context={
                        "request_id": request.idempotency_key,
                        "session_id": getattr(context, "session_id", None),
                        "surface": "backend",
                        "adapter": "agent_payments",
                        "geo": geo,
                    },
                    consent={"scope": decision.required_scopes, "nonce": None, "consent_token_ref": None},
                    payload={
                        "decision": decision.decision,
                        "reason_codes": decision.reason_codes,
                        "amount": float(order_total),
                        "currency": str(order.get("currency") or "USD"),
                        "idempotency_key": request.idempotency_key,
                    },
                    risk_tier=decision.risk_tier,
                )
            except Exception:
                pass

            if x_hil_approval:
                try:
                    governance.record_audit_event(
                        merchant_id=str(merchant_id),
                        actor_type="agent",
                        actor_ref=str(getattr(context, "agent_id", "")) or None,
                        action="submit_payment.approval",
                        subject={"order_id": request.order_id},
                        request_context={
                            "request_id": request.idempotency_key,
                            "session_id": getattr(context, "session_id", None),
                            "surface": "backend",
                            "adapter": "agent_payments",
                            "geo": geo,
                        },
                        consent={"scope": decision.required_scopes, "nonce": None, "consent_token_ref": None},
                        payload={
                            "approval_id": x_hil_approval,
                            "decision": "approved",
                        },
                        risk_tier=decision.risk_tier,
                    )
                except Exception:
                    pass

            if decision.decision == "require_hil":
                approval = governance.request_hil(
                    intent={
                        "action": "submit_payment",
                        "order_id": request.order_id,
                        "merchant_id": merchant_id,
                        "amount": float(order_total),
                        "currency": str(order.get("currency") or "USD"),
                        "geo": geo,
                    }
                )
                try:
                    governance.record_audit_event(
                        merchant_id=str(merchant_id),
                        actor_type="agent",
                        actor_ref=str(getattr(context, "agent_id", "")) or None,
                        action="submit_payment.hil_requested",
                        subject={"order_id": request.order_id},
                        request_context={
                            "request_id": request.idempotency_key,
                            "session_id": getattr(context, "session_id", None),
                            "surface": "backend",
                            "adapter": "agent_payments",
                            "geo": geo,
                        },
                        consent={"scope": decision.required_scopes, "nonce": None, "consent_token_ref": None},
                        payload={"approval": approval},
                        risk_tier=decision.risk_tier,
                    )
                except Exception:
                    pass
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "HIL_REQUIRED",
                        "message": "Step-up approval required before submitting payment",
                        "approval": approval,
                        "required_scopes": decision.required_scopes,
                    },
                )
        except HTTPException:
            raise
        except Exception:
            pass
        
        # 3. Check idempotency
        if request.idempotency_key:
            # Check if payment with this key already exists
            existing = await database.fetch_one(
                """SELECT payment_id, payment_intent_id, status 
                   FROM payments 
                   WHERE idempotency_key = :key AND order_id = :order_id""",
                {"key": request.idempotency_key, "order_id": request.order_id}
            )
            if existing:
                logger.info(f"Returning existing payment for idempotency key: {request.idempotency_key}")
                return PaymentResponse(
                    status=existing["status"],
                    payment_id=existing["payment_id"],
                    payment_intent_id=existing["payment_intent_id"],
                    amount=order_total,
                    currency=order.get("currency", "USD"),
                    psp_used="cached",
                    created_at=datetime.now().isoformat()
                )

        existing_payment_surface = await _build_existing_order_payment_surface(order)
        if existing_payment_surface:
            logger.info(
                "[AgentPayments] Reusing existing %s payment surface for order %s",
                existing_payment_surface["psp_used"],
                request.order_id,
            )
            return PaymentResponse(
                status="requires_action",
                payment_id=f"pay_{existing_payment_surface['payment_intent_id'] or request.order_id}",
                payment_intent_id=existing_payment_surface["payment_intent_id"] or "",
                client_secret=existing_payment_surface["client_secret"],
                amount=order_total,
                currency=order.get("currency", "USD"),
                psp_used=existing_payment_surface["psp_used"],
                psp=existing_payment_surface["psp_used"],
                payment_action=existing_payment_surface["payment_action"],
                created_at=datetime.now().isoformat(),
            )

        # MVP measurement scaffolding: checkout attempted (payment stage).
        try:
            from mvp.constants import EVENT_CHECKOUT_ATTEMPTED, SURFACE_BACKEND
            from mvp.events import emit_best_effort

            ship = order.get("shipping_address") or {}
            geo = None
            if isinstance(ship, dict):
                geo = {
                    "country": (ship.get("country") or "").upper()[:2] or None,
                    "postal_code": ship.get("postal_code") or ship.get("zip"),
                    "city": ship.get("city"),
                    "state": ship.get("state") or ship.get("province"),
                }

            emit_best_effort(
                event_type=EVENT_CHECKOUT_ATTEMPTED,
                payload={
                    "stage": "payment",
                    "order_id": request.order_id,
                    "merchant_id": merchant_id,
                    "amount": float(order_total),
                    "currency": str(order.get("currency") or "USD"),
                    "idempotency_key": request.idempotency_key,
                },
                merchant_id=str(merchant_id) if merchant_id else None,
                geo=geo,
                surface=SURFACE_BACKEND,
                adapter="agent_payments",
                risk_tier=(getattr(decision, "risk_tier", None) or "unknown"),
                idempotency_key=request.idempotency_key,
            )
        except Exception:
            pass

        # 4. Select PSP using routing rules (Integration / payment_routes)
        routing_service = PaymentRoutingService(database)
        currency_code = order.get("currency", "USD")

        selected_psp, route_config = await routing_service.select_psp(
            agent_id=context.agent_id,
            merchant_id=merchant_id,
            amount=order_total,
            currency=currency_code,
        )

        logger.info(
            f"[AgentPayments] Routing selected PSP '{selected_psp}' for order {request.order_id} "
            f"(agent={context.agent_id}, merchant={merchant_id})"
        )

        # 5. Resolve merchant config and create payment intent with the selected PSP
        merchant = await get_merchant_onboarding(merchant_id)
        if not merchant:
            raise HTTPException(status_code=404, detail="Merchant not found")

        amount = Decimal(str(order_total))
        currency = currency_code

        # Build preferred PSP ordering from routing config for MultiPSPOrchestrator
        preferred_psps: Optional[List[str]] = None
        try:
            if isinstance(route_config, dict):
                raw_priority = route_config.get("psp_priority") or []
                if isinstance(raw_priority, str):
                    try:
                        raw_priority = json.loads(raw_priority)
                    except Exception:
                        raw_priority = []
                if isinstance(raw_priority, list) and raw_priority:
                    preferred_psps = [
                        str(entry.get("psp", "")).lower()
                        for entry in sorted(
                            raw_priority, key=lambda e: e.get("priority", 999)
                        )
                        if entry.get("psp")
                    ]
        except Exception as pref_err:
            logger.warning(
                f"[AgentPayments] Failed to build preferred_psps list from route_config: {pref_err}"
            )
            preferred_psps = None

        # Use MultiPSPOrchestrator for real payment creation with failover
        try:
            from mvp.governance import governance

            if decision is not None:
                governance.record_audit_event(
                    merchant_id=str(merchant_id),
                    actor_type="agent",
                    actor_ref=str(getattr(context, "agent_id", "")) or None,
                    action="submit_payment.execution",
                    subject={"order_id": request.order_id},
                    request_context={
                        "request_id": request.idempotency_key,
                        "session_id": getattr(context, "session_id", None),
                        "surface": "backend",
                        "adapter": "agent_payments",
                        "geo": geo,
                    },
                    consent={"scope": getattr(decision, "required_scopes", []) or [], "nonce": None, "consent_token_ref": None},
                    payload={
                        "selected_psp": selected_psp,
                        "route_config": route_config,
                        "amount": float(order_total),
                        "currency": currency_code,
                    },
                    risk_tier=getattr(decision, "risk_tier", "unknown"),
                )
        except Exception:
            pass

        try:
            success, payment_intent, error, psp_used = await asyncio.wait_for(
                create_payment_with_failover(
                    merchant_id=merchant_id,
                    amount=amount,
                    currency=currency,
                    metadata={
                        "order_id": request.order_id,
                        "agent_id": context.agent_id,
                        "payment_method_type": request.payment_method.type,
                        "idempotency_key": request.idempotency_key,
                        **({"return_url": request.return_url} if request.return_url else {}),
                    },
                    preferred_psps=preferred_psps,
                    canonical_psp_required=True,
                    enforce_live_readiness=True,
                ),
                timeout=AGENT_PAYMENT_INITIATION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            logger.error(
                "[AgentPayments] Payment initiation timed out for order %s after %.2fs",
                request.order_id,
                AGENT_PAYMENT_INITIATION_TIMEOUT_SECONDS,
            )
            raise HTTPException(
                status_code=504,
                detail={
                    "error": "UPSTREAM_TIMEOUT",
                    "message": (
                        "Payment initiation timed out before a PSP surface was created"
                    ),
                    "timeout_seconds": AGENT_PAYMENT_INITIATION_TIMEOUT_SECONDS,
                    "order_id": request.order_id,
                    "selected_psp": selected_psp,
                },
            ) from exc

        if not success:
            logger.error(f"Payment intent creation failed via {psp_used}: {error}")
            raise HTTPException(status_code=500, detail=f"Payment failed: {error}")
        
        # 6. Determine if 3DS or other action required
        next_action = None
        status = "processing"
        
        if payment_intent.status == "requires_action":
            status = "requires_action"
            # Check if 3DS redirect needed
            if hasattr(payment_intent, 'next_action'):
                next_action = NextAction(
                    type="redirect_to_url",
                    redirect_url=payment_intent.next_action.get("redirect_to_url", {}).get("url")
                )
        elif payment_intent.status == "succeeded":
            status = "succeeded"
        
        # 7. Store payment record
        payment_id = f"pay_{payment_intent.id}"
        
        await database.execute(
            """INSERT INTO payments 
               (payment_id, order_id, payment_intent_id, amount, currency, 
                psp_type, status, idempotency_key, created_at, agent_id)
               VALUES (:payment_id, :order_id, :intent_id, :amount, :currency,
                       :psp, :status, :idem_key, :created_at, :agent_id)""",
            {
                "payment_id": payment_id,
                "order_id": request.order_id,
                "intent_id": payment_intent.id,
                "amount": float(amount),
                "currency": currency,
                "psp": psp_used,
                "status": status,
                "idem_key": request.idempotency_key,
                "created_at": datetime.now(),
                "agent_id": context.agent_id
            }
        )
        
        # 8. Update order payment status
        await update_payment_info(
            order_id=request.order_id,
            payment_intent_id=payment_intent.id,
            client_secret=payment_intent.client_secret if hasattr(payment_intent, 'client_secret') else "",
            payment_status="processing",
            psp_used=psp_used,
        )

        # PCS v0.2-b (best-effort): internal payment fact for reducer replay (no PII).
        try:
            from services.pcs_fact_ingest import append_internal_fact_best_effort

            await append_internal_fact_best_effort(
                merchant_id=str(merchant_id) if merchant_id else "",
                order_id=str(request.order_id),
                fact_type="internal.payment_updated",
                payload={
                    "order_id": str(request.order_id),
                    "merchant_id": str(merchant_id) if merchant_id else None,
                    "status": status,
                    "payment_intent_id": payment_intent.id,
                    "amount": float(amount),
                    "currency": str(currency),
                    "psp_used": str(psp_used),
                    "idempotency_key": request.idempotency_key,
                },
                idempotency_key=request.idempotency_key or f"{request.order_id}:{payment_intent.id}:{status}",
            )
        except Exception:
            pass
        
        # 9. Log request
        background_tasks.add_task(
            log_agent_request,
            context=context,
            status_code=200,
            merchant_id=merchant_id
        )
        
        logger.info(f"Payment created: {payment_id} for order {request.order_id} via {psp_used}")

        payment_action = build_payment_action(payment_intent, psp_used=psp_used)

        # MVP ledger event (best-effort): payment timeline entry.
        try:
            from mvp.ledger_events import emit_ledger_event_best_effort

            ledger_type = (
                "payment_succeeded"
                if status == "succeeded"
                else "payment_requires_action"
                if status == "requires_action"
                else "payment_processing"
            )

            emit_ledger_event_best_effort(
                merchant_id=str(merchant_id),
                event_type=ledger_type,
                order_id=str(request.order_id),
                source={"type": "psp", "psp": psp_used, "external_event_id": payment_intent.id},
                amount={"value": float(amount), "currency": str(currency)},
                refs={"payment_intent_id": payment_intent.id},
                geo=geo,
                surface="backend",
                adapter="agent_payments",
                risk_tier=(getattr(decision, "risk_tier", None) or "unknown"),
                idempotency_key=request.idempotency_key,
                signature_verified=False,
            )
        except Exception:
            pass

        # Receipt audit event (best-effort).
        try:
            from mvp.governance import governance

            if decision is not None:
                governance.record_audit_event(
                    merchant_id=str(merchant_id),
                    actor_type="agent",
                    actor_ref=str(getattr(context, "agent_id", "")) or None,
                    action="submit_payment.receipt",
                    subject={"order_id": request.order_id},
                    request_context={
                        "request_id": request.idempotency_key,
                        "session_id": getattr(context, "session_id", None),
                        "surface": "backend",
                        "adapter": "agent_payments",
                        "geo": geo,
                    },
                    consent={"scope": getattr(decision, "required_scopes", []) or [], "nonce": None, "consent_token_ref": None},
                    payload={
                        "payment_id": payment_id,
                        "payment_intent_id": payment_intent.id,
                        "psp_used": psp_used,
                        "status": status,
                    },
                    risk_tier=getattr(decision, "risk_tier", "unknown"),
                )
        except Exception:
            pass

        # MVP measurement scaffolding: checkout result (payment stage).
        try:
            from mvp.constants import EVENT_CHECKOUT_FAILED, EVENT_CHECKOUT_SUCCEEDED, SURFACE_BACKEND
            from mvp.events import emit_best_effort

            ship = order.get("shipping_address") or {}
            geo = None
            if isinstance(ship, dict):
                geo = {
                    "country": (ship.get("country") or "").upper()[:2] or None,
                    "postal_code": ship.get("postal_code") or ship.get("zip"),
                    "city": ship.get("city"),
                    "state": ship.get("state") or ship.get("province"),
                }

            evt = EVENT_CHECKOUT_SUCCEEDED if status == "succeeded" else EVENT_CHECKOUT_FAILED
            brief_id = None
            brief_schema_version = None
            try:
                meta = (order.get("metadata") or {}) if isinstance(order, dict) else {}
                if isinstance(meta, dict):
                    brief_id = meta.get("brief_id") or meta.get("briefId") or None
                    brief_schema_version = meta.get("brief_schema_version") or meta.get("briefSchemaVersion") or None
            except Exception:
                pass
            emit_best_effort(
                event_type=evt,
                payload={
                    "stage": "payment",
                    "order_id": request.order_id,
                    "merchant_id": merchant_id,
                    "payment_id": payment_id,
                    "payment_intent_id": payment_intent.id,
                    "psp_used": psp_used,
                    "status": status,
                    **({"brief_id": brief_id} if brief_id else {}),
                    **({"brief_schema_version": brief_schema_version} if brief_schema_version else {}),
                },
                merchant_id=str(merchant_id) if merchant_id else None,
                geo=geo,
                surface=SURFACE_BACKEND,
                adapter="agent_payments",
                risk_tier=(getattr(decision, "risk_tier", None) or "unknown"),
                idempotency_key=request.idempotency_key,
            )
        except Exception:
            pass
        
        return PaymentResponse(
            status=status,
            payment_id=payment_id,
            payment_intent_id=payment_intent.id,
            client_secret=payment_intent.client_secret if hasattr(payment_intent, 'client_secret') else None,
            amount=float(amount),
            currency=currency,
            psp_used=psp_used,
            psp=psp_used,
            payment_action=payment_action,
            next_action=next_action,
            created_at=datetime.now().isoformat()
        )
    
    except HTTPException as e:
        # MVP measurement scaffolding: ensure we emit a failure event even on early exits.
        try:
            from mvp.constants import EVENT_CHECKOUT_FAILED, SURFACE_BACKEND
            from mvp.events import emit_best_effort

            ship = None
            try:
                ship = (order or {}).get("shipping_address") if isinstance(order, dict) else None
            except Exception:
                ship = None

            geo = None
            if isinstance(ship, dict):
                geo = {
                    "country": (ship.get("country") or "").upper()[:2] or None,
                    "postal_code": ship.get("postal_code") or ship.get("zip"),
                    "city": ship.get("city"),
                    "state": ship.get("state") or ship.get("province"),
                }

            brief_id = None
            brief_schema_version = None
            try:
                meta = (order.get("metadata") or {}) if isinstance(order, dict) else {}
                if isinstance(meta, dict):
                    brief_id = meta.get("brief_id") or meta.get("briefId") or None
                    brief_schema_version = meta.get("brief_schema_version") or meta.get("briefSchemaVersion") or None
            except Exception:
                pass

            emit_best_effort(
                event_type=EVENT_CHECKOUT_FAILED,
                payload={
                    "stage": "payment",
                    "order_id": getattr(request, "order_id", None),
                    "merchant_id": merchant_id if "merchant_id" in locals() else None,
                    "error_status": getattr(e, "status_code", None),
                    "error": str(getattr(e, "detail", ""))[:500],
                    **({"brief_id": brief_id} if brief_id else {}),
                    **({"brief_schema_version": brief_schema_version} if brief_schema_version else {}),
                },
                merchant_id=str(merchant_id) if "merchant_id" in locals() else None,
                geo=geo,
                surface=SURFACE_BACKEND,
                adapter="agent_payments",
                risk_tier=(getattr(decision, "risk_tier", None) or "unknown"),
                idempotency_key=getattr(request, "idempotency_key", None),
            )
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(f"Payment creation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Payment processing failed: {str(e)}")


@router.post("/payments/authorize", response_model=AuthorizePaymentResponse)
async def authorize_payment(
    body: AuthorizePaymentRequest,
    context: AgentContext = Depends(get_agent_context),
):
    """
    Mint a constrained authorization token for future agentic payments (skeleton; no PSP charge).

    Security:
    - Agent can only authorize against mandates belonging to its own agent_id.
    - Only stores token hash (never stores raw token).
    """
    from db.buyer_vault import audit_buyer_action, authorization_tokens, mandates

    mandate_id = str(body.mandate_id or "").strip()
    if not mandate_id:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "mandate_id is required"})

    currency = str(body.currency or "").strip().upper()
    if not currency:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "currency is required"})

    amount = Decimal(str(body.amount))
    if amount <= 0:
        raise HTTPException(status_code=400, detail={"error": "INVALID_REQUEST", "message": "amount must be > 0"})

    row = await database.fetch_one(
        mandates.select()
        .where((mandates.c.id == mandate_id) & (mandates.c.agent_id == context.agent_id))
        .limit(1)
    )
    if not row:
        raise HTTPException(status_code=404, detail={"error": "MANDATE_NOT_FOUND", "message": "Mandate not found"})

    mandate = dict(row)
    status = str(mandate.get("status") or "").lower()
    if status != "active":
        raise HTTPException(status_code=403, detail={"error": "MANDATE_NOT_ACTIVE", "message": "Mandate is not active"})

    expires_at = mandate.get("expires_at")
    if expires_at and hasattr(expires_at, "timestamp") and expires_at.timestamp() < datetime.now(timezone.utc).timestamp():
        raise HTTPException(status_code=403, detail={"error": "EXPIRED_MANDATE", "message": "Mandate expired"})

    constraints = mandate.get("constraints_json") if isinstance(mandate.get("constraints_json"), dict) else {}

    allowed_currency = str(constraints.get("currency") or "").strip().upper() or None
    if allowed_currency and allowed_currency != currency:
        raise HTTPException(status_code=403, detail={"error": "CURRENCY_NOT_ALLOWED", "message": "Currency not allowed"})

    per_txn_limit = constraints.get("per_txn_limit")
    if per_txn_limit is not None:
        try:
            if amount > Decimal(str(per_txn_limit)):
                raise HTTPException(
                    status_code=403,
                    detail={"error": "EXCEEDED_LIMIT", "message": "Amount exceeds per_txn_limit"},
                )
        except HTTPException:
            raise
        except Exception:
            pass

    merchant_allowlist = constraints.get("merchant_allowlist")
    if isinstance(merchant_allowlist, list) and merchant_allowlist:
        merchant_id = None
        if isinstance(body.merchant_context, dict):
            merchant_id = body.merchant_context.get("merchant_id") or body.merchant_context.get("merchantId") or body.merchant_context.get("id")
        if merchant_id and str(merchant_id) not in {str(x) for x in merchant_allowlist}:
            raise HTTPException(
                status_code=403,
                detail={"error": "NOT_ALLOWED_MERCHANT", "message": "Merchant not allowed by mandate"},
            )

    confirmation_policy = str(constraints.get("confirmation_policy") or "").strip().lower() or "auto_under_limit"
    address_allowlist = constraints.get("address_allowlist_ids")
    address_allowlist_set = {str(x) for x in address_allowlist} if isinstance(address_allowlist, list) else set()
    shipping_address_id = str(body.shipping_address_id or "").strip() or None

    requires_confirmation = False
    if confirmation_policy == "always":
        requires_confirmation = True
    elif confirmation_policy == "step_up_on_new_address":
        if not shipping_address_id:
            requires_confirmation = True
        elif address_allowlist_set and shipping_address_id not in address_allowlist_set:
            requires_confirmation = True

    if requires_confirmation:
        confirm_url = f"{_checkout_ui_base()}/mandates/confirm?mandate_id={mandate_id}"
        await audit_buyer_action(
            buyer_id=mandate.get("buyer_id"),
            agent_id=context.agent_id,
            action="mandate.authorization_token.step_up_required",
            details={
                "mandate_id": mandate_id,
                "amount": float(amount),
                "currency": currency,
                "confirmation_policy": confirmation_policy,
            },
            ip_address=context.request.client.host if context.request.client else None,
            user_agent=context.request.headers.get("user-agent"),
        )
        return AuthorizePaymentResponse(requires_user_confirmation=True, confirm_url=confirm_url)

    # Mint + store hash only (never store raw token).
    now = datetime.now(timezone.utc)
    token_expires_at = now + timedelta(minutes=10)
    scope_json: Dict[str, Any] = {
        "type": "agentic_payment",
        "agent_user_ref": (str(body.agent_user_ref)[:256] if body.agent_user_ref else None),
        "merchant_context": body.merchant_context if isinstance(body.merchant_context, dict) else None,
    }

    for _ in range(5):
        token_id = f"at_{secrets.token_hex(12)}"
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        try:
            await database.execute(
                authorization_tokens.insert().values(
                    id=token_id,
                    mandate_id=mandate_id,
                    intent_id=None,
                    token_hash=token_hash,
                    scope_json=scope_json,
                    amount=amount,
                    currency=currency,
                    expires_at=token_expires_at,
                    created_at=now,
                    used_at=None,
                )
            )
            await audit_buyer_action(
                buyer_id=mandate.get("buyer_id"),
                agent_id=context.agent_id,
                action="mandate.authorization_token.issued",
                details={
                    "mandate_id": mandate_id,
                    "authorization_token_id": token_id,
                    "amount": float(amount),
                    "currency": currency,
                    "expires_at": token_expires_at.isoformat(),
                },
                ip_address=context.request.client.host if context.request.client else None,
                user_agent=context.request.headers.get("user-agent"),
            )
            return AuthorizePaymentResponse(authorization_token=raw_token, expires_at=token_expires_at.isoformat())
        except Exception:
            continue

    raise HTTPException(
        status_code=503,
        detail={"error": "TEMPORARY_UNAVAILABLE", "message": "Failed to mint authorization token, please retry"},
    )


# ============================================================================
# Payment Status Check
# ============================================================================

@router.get("/payments/{payment_id}")
async def get_payment_status(
    payment_id: str,
    context: AgentContext = Depends(get_agent_context)
):
    """
    Get payment status
    
    Returns current status of payment including:
    - Payment status (requires_action, processing, succeeded, failed)
    - PSP used
    - Amount and currency
    - Next action if required
    """
    try:
        from db.database import database
        
        payment = await database.fetch_one(
            """SELECT p.*, o.merchant_id 
               FROM payments p
               JOIN orders o ON p.order_id = o.order_id
               WHERE p.payment_id = :payment_id""",
            {"payment_id": payment_id}
        )
        
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        
        # Verify access
        if not context.can_access_merchant(payment["merchant_id"]):
            raise HTTPException(status_code=403, detail="Not authorized")
        
        return {
            "status": "success",
            "payment": {
                "payment_id": payment["payment_id"],
                "order_id": payment["order_id"],
                "status": payment["status"],
                "amount": payment["amount"],
                "currency": payment["currency"],
                "psp_used": payment["psp_type"],
                "created_at": payment["created_at"].isoformat(),
                "updated_at": payment.get("updated_at").isoformat() if payment.get("updated_at") else None
            }
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get payment status error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payment status")
