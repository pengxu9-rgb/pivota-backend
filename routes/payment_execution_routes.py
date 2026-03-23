"""
Phase 3: Unified Payment Execution Router
Merchants use their API keys to execute payments through their connected PSP
"""

import hmac
import os
from datetime import datetime
import logging
import secrets
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from config.settings import settings
from db.database import database
from db.merchant_onboarding import get_merchant_by_api_key, get_merchant_onboarding
from services.merchant_payment_initiation_service import initiate_merchant_payment
from services.merchant_psp_config_service import evaluate_psp_readiness
from services.merchant_webhook_service import emit_merchant_webhook_event
from services.payment_routing_service import PaymentRoutingService


logger = logging.getLogger("payment_execution")
router = APIRouter(prefix="/payment", tags=["payment-execution"])


SUPPORTED_MERCHANT_PAYMENT_PROVIDERS = {"stripe", "adyen", "checkout"}


class PaymentExecuteRequest(BaseModel):
    amount: float
    currency: str
    order_id: str
    customer_email: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[dict] = None


class PaymentExecuteResponse(BaseModel):
    success: bool
    payment_id: str
    order_id: str
    amount: float
    currency: str
    psp_used: str
    status: str
    transaction_id: Optional[str] = None
    requires_customer_action: bool = False
    payment_action: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    timestamp: str


class InternalPaymentExecuteRequest(PaymentExecuteRequest):
    emit_merchant_webhook: bool = False


async def verify_merchant_api_key(api_key: str) -> dict:
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Include 'X-Merchant-API-Key' header",
        )

    merchant = await get_merchant_by_api_key(api_key)
    if not merchant:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    if merchant["status"] != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Merchant account is {merchant['status']}. Only approved merchants can process payments.",
        )

    return merchant


def _extract_internal_key(request: Request, x_pivota_internal_key: Optional[str]) -> str:
    if x_pivota_internal_key:
        return x_pivota_internal_key.strip()
    auth = str(request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _require_internal_canary_access(request: Request, x_pivota_internal_key: Optional[str]) -> None:
    host = str(request.url.hostname or "").strip().lower()
    if host in {"localhost", "127.0.0.1", "::1", "testserver"} and os.getenv("DEV_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return

    secret = (settings.readiness_internal_api_key or "").strip() or (os.getenv("UCP_INTERNAL_API_KEY") or "").strip()
    if not secret:
        raise HTTPException(status_code=404, detail="Not Found")

    provided = _extract_internal_key(request, x_pivota_internal_key)
    if not provided or not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


async def _load_canary_merchant(merchant_id: str) -> Dict[str, Any]:
    merchant = await get_merchant_onboarding(merchant_id)
    if not merchant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Merchant not found")
    if merchant.get("status") != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Merchant account is {merchant.get('status')}. Only approved merchants can process payments.",
        )
    return merchant


async def _load_active_merchant_psps(merchant_id: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT psp_id, provider, api_key, secret_key, account_id, status, connected_at,
               environment, provider_config, validation_status, validation_error, last_validated_at
        FROM merchant_psps
        WHERE merchant_id = :merchant_id
          AND status = 'active'
        ORDER BY connected_at DESC NULLS LAST, psp_id ASC
        """,
        {"merchant_id": merchant_id},
    )
    return [dict(row) for row in rows or []]


def _normalize_priority_list(route_config: Dict[str, Any]) -> List[str]:
    raw_priority = route_config.get("psp_priority") or []
    if isinstance(raw_priority, str):
        try:
            import json

            raw_priority = json.loads(raw_priority)
        except Exception:
            raw_priority = []
    providers: List[str] = []
    for entry in sorted(raw_priority, key=lambda item: (item or {}).get("priority", 999)):
        provider = str((entry or {}).get("psp") or "").strip().lower()
        if provider and provider not in providers:
            providers.append(provider)
    return providers


def _build_candidate_readiness(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return evaluate_psp_readiness(
        str(candidate.get("provider") or ""),
        status=candidate.get("status"),
        api_key=candidate.get("api_key"),
        account_id=candidate.get("account_id"),
        provider_config=candidate.get("provider_config"),
        environment=candidate.get("environment"),
        validation_status=candidate.get("validation_status"),
        validation_error=candidate.get("validation_error"),
    )


def _describe_blocked_candidates(candidates: List[Dict[str, Any]]) -> str:
    blocked_descriptions: List[str] = []
    for candidate in candidates:
        provider = str(candidate.get("provider") or "").strip().lower()
        if provider not in SUPPORTED_MERCHANT_PAYMENT_PROVIDERS:
            continue
        readiness = candidate.get("readiness") or _build_candidate_readiness(candidate)
        if readiness.get("live_charge_ready"):
            continue
        blockers = readiness.get("readiness_blockers") or []
        blocked_descriptions.append(
            f"{provider}: {', '.join(blockers) if blockers else 'not ready for live charge'}"
        )
    return "; ".join(blocked_descriptions[:3])


def _build_preferred_psps(
    candidates: List[Dict[str, Any]],
    *,
    enforce_live_readiness: bool,
) -> List[str]:
    preferred: List[str] = []
    for candidate in candidates:
        provider = str(candidate.get("provider") or "").strip().lower()
        if provider not in SUPPORTED_MERCHANT_PAYMENT_PROVIDERS or provider in preferred:
            continue

        if enforce_live_readiness:
            readiness = candidate.get("readiness") or _build_candidate_readiness(candidate)
            if readiness.get("live_charge_ready"):
                preferred.append(provider)
            continue

        api_key = str(candidate.get("api_key") or "").strip()
        if api_key:
            preferred.append(provider)

    return preferred


async def _resolve_payment_candidates(
    merchant: Dict[str, Any],
    payment_data: PaymentExecuteRequest,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    merchant_id = str(merchant["merchant_id"])
    active_psps = await _load_active_merchant_psps(merchant_id)
    active_by_provider: Dict[str, Dict[str, Any]] = {}
    for psp in active_psps:
        provider = str(psp.get("provider") or "").strip().lower()
        if provider and provider not in active_by_provider:
            active_by_provider[provider] = psp

    route_config: Dict[str, Any] = {}
    ordered_candidates: List[Dict[str, Any]] = []

    try:
        routing_service = PaymentRoutingService(database)
        selected_psp, route_config = await routing_service.select_psp(
            agent_id=None,
            merchant_id=merchant_id,
            amount=payment_data.amount,
            currency=payment_data.currency,
        )
        ordered_providers = _normalize_priority_list(route_config)
        if selected_psp and selected_psp in ordered_providers:
            ordered_providers = [selected_psp] + [provider for provider in ordered_providers if provider != selected_psp]

        for provider in ordered_providers:
            candidate = active_by_provider.get(provider)
            if candidate:
                ordered_candidates.append(candidate)
    except Exception as exc:
        logger.warning("Failed to resolve merchant payment_routes for %s: %s", merchant_id, exc)

    if ordered_candidates:
        return ordered_candidates, route_config

    if active_psps:
        return active_psps, route_config

    return [], route_config


async def _emit_payment_webhook_best_effort(
    merchant_id: str,
    *,
    event_type: str,
    payment_request: PaymentExecuteRequest,
    result: Dict[str, Any],
    psp_used: str,
) -> None:
    try:
        await emit_merchant_webhook_event(
            merchant_id,
            event_type=event_type,
            payload={
                "order_id": payment_request.order_id,
                "payment_id": result.get("payment_id"),
                "transaction_id": result.get("transaction_id"),
                "amount": payment_request.amount,
                "currency": payment_request.currency,
                "psp_used": psp_used,
                "status": result.get("status"),
                "customer_email": payment_request.customer_email,
            },
        )
    except Exception as exc:
        logger.warning(
            "Failed to emit merchant webhook %s for %s: %s",
            event_type,
            merchant_id,
            exc,
        )


async def _execute_payment_for_merchant(
    *,
    merchant: Dict[str, Any],
    payment_request: PaymentExecuteRequest,
    enforce_live_readiness: bool,
    emit_merchant_webhook: bool,
    source: str,
) -> PaymentExecuteResponse:
    logger.info(
        "Payment request from merchant: %s (%s) [%s]",
        merchant["merchant_id"],
        merchant.get("business_name") or merchant.get("contact_email") or "unknown",
        source,
    )

    candidates, route_config = await _resolve_payment_candidates(merchant, payment_request)
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="Payment routing is not configured for any active processor",
        )

    preferred_psps = _build_preferred_psps(
        candidates,
        enforce_live_readiness=enforce_live_readiness,
    )
    if not preferred_psps:
        if enforce_live_readiness:
            blocked_detail = _describe_blocked_candidates(candidates)
            raise HTTPException(
                status_code=400,
                detail=(
                    "No supported live-ready PSPs are configured for this merchant"
                    + (f". {blocked_detail}" if blocked_detail else "")
                ),
            )
        raise HTTPException(
            status_code=400,
            detail="No supported PSP candidates are configured for this canary run",
        )

    result = await initiate_merchant_payment(
        merchant_id=str(merchant["merchant_id"]),
        # Merchant API contract keeps amount in minor units for backward compatibility.
        amount=(Decimal(str(payment_request.amount)) / Decimal("100")),
        currency=payment_request.currency,
        metadata={
            "order_id": payment_request.order_id,
            "merchant_id": merchant["merchant_id"],
            "customer_email": payment_request.customer_email,
            "description": payment_request.description,
            "route_id": route_config.get("route_id") if isinstance(route_config, dict) else None,
            **(payment_request.metadata or {}),
            "source": source,
            "enforce_live_readiness": enforce_live_readiness,
        },
        preferred_psps=preferred_psps,
        candidates=candidates,
        canonical_psp_required=True,
        enforce_live_readiness=enforce_live_readiness,
    )

    payment_id = str(result.get("payment_id") or f"failed_{secrets.token_hex(8)}")
    if emit_merchant_webhook:
        await _emit_payment_webhook_best_effort(
            merchant["merchant_id"],
            event_type="payment.completed" if result.get("success") else "payment.failed",
            payment_request=payment_request,
            result={**result, "payment_id": payment_id},
            psp_used=result.get("psp_used") or preferred_psps[0],
        )
    return PaymentExecuteResponse(
        success=bool(result.get("success")),
        payment_id=payment_id,
        order_id=payment_request.order_id,
        amount=payment_request.amount,
        currency=payment_request.currency,
        psp_used=str(result.get("psp_used") or preferred_psps[0]),
        status=str(result.get("status") or ("requires_action" if result.get("success") else "failed")),
        transaction_id=result.get("transaction_id"),
        requires_customer_action=bool(result.get("requires_customer_action")),
        payment_action=result.get("payment_action"),
        error_message=result.get("error_message"),
        timestamp=datetime.now().isoformat(),
    )


@router.post("/execute", response_model=PaymentExecuteResponse)
async def execute_payment(
    payment_request: PaymentExecuteRequest,
    x_merchant_api_key: str = Header(None, alias="X-Merchant-API-Key"),
):
    try:
        merchant = await verify_merchant_api_key(x_merchant_api_key)
        return await _execute_payment_for_merchant(
            merchant=merchant,
            payment_request=payment_request,
            enforce_live_readiness=True,
            emit_merchant_webhook=True,
            source="payment_execute",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Payment execution failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Payment execution failed: {str(exc)}",
        )


@router.post(
    "/internal/canary/merchants/{merchant_id}/execute",
    response_model=PaymentExecuteResponse,
    include_in_schema=False,
)
async def execute_internal_payment_canary(
    merchant_id: str,
    payment_request: InternalPaymentExecuteRequest,
    request: Request,
    x_pivota_internal_key: Optional[str] = Header(default=None, alias="X-Pivota-Internal-Key"),
):
    try:
        _require_internal_canary_access(request, x_pivota_internal_key)
        merchant = await _load_canary_merchant(merchant_id)
        return await _execute_payment_for_merchant(
            merchant=merchant,
            payment_request=payment_request,
            enforce_live_readiness=False,
            emit_merchant_webhook=bool(payment_request.emit_merchant_webhook),
            source="ops_psp_canary_harness",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Internal payment canary failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Internal payment canary failed: {str(exc)}",
        )


@router.get("/health")
async def payment_router_health():
    return {
        "status": "healthy",
        "service": "payment-execution-router",
        "timestamp": datetime.now().isoformat(),
    }
