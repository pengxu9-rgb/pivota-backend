"""
Phase 3: Unified Payment Execution Router
Merchants use their API keys to execute payments through their connected PSP
"""

from datetime import datetime
import logging
import secrets
from typing import Any, Dict, List, Optional, Tuple

import httpx
import stripe
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from config.settings import settings
from db.database import database
from db.merchant_onboarding import get_merchant_by_api_key
from db.payment_router import get_merchant_psp_route
from services.merchant_webhook_service import emit_merchant_webhook_event
from services.payment_routing_service import PaymentRoutingService


logger = logging.getLogger("payment_execution")
router = APIRouter(prefix="/payment", tags=["payment-execution"])


SUPPORTED_MERCHANT_PAYMENT_PROVIDERS = {"stripe", "adyen"}


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
    error_message: Optional[str] = None
    timestamp: str


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

    if not merchant["psp_connected"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No PSP connected. Please connect a PSP first.",
        )

    return merchant


async def _load_active_merchant_psps(merchant_id: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT psp_id, provider, api_key, secret_key, account_id, status, connected_at
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
        for provider, candidate in active_by_provider.items():
            if provider not in [c.get("provider") for c in ordered_candidates]:
                ordered_candidates.append(candidate)
    except Exception as exc:
        logger.warning("Failed to resolve merchant payment_routes for %s: %s", merchant_id, exc)

    if ordered_candidates:
        return ordered_candidates, route_config

    legacy_route = await get_merchant_psp_route(merchant_id)
    if legacy_route:
        legacy_credentials = legacy_route.get("psp_credentials") or {}
        if isinstance(legacy_credentials, str):
            try:
                import json

                legacy_credentials = json.loads(legacy_credentials)
            except Exception:
                legacy_credentials = {}
        return [
            {
                "provider": str(legacy_route.get("psp_type") or "").strip().lower(),
                "api_key": legacy_credentials.get("api_key"),
                "secret_key": legacy_credentials.get("secret_key"),
                "account_id": legacy_credentials.get("account_id"),
                "status": "active",
                "source": "legacy_payment_router_config",
            }
        ], route_config

    return [], route_config


async def execute_stripe_payment(
    stripe_key: str,
    merchant: Dict[str, Any],
    payment_data: PaymentExecuteRequest,
) -> Dict[str, Any]:
    try:
        stripe.api_key = stripe_key
        intent = stripe.PaymentIntent.create(
            amount=int(payment_data.amount),
            currency=payment_data.currency.lower(),
            description=payment_data.description or f"Payment for order {payment_data.order_id}",
            metadata={
                "order_id": payment_data.order_id,
                "merchant_id": merchant["merchant_id"],
                **(payment_data.metadata or {}),
            },
            receipt_email=payment_data.customer_email,
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
        )
        return {
            "success": intent.status in ["succeeded", "processing"],
            "payment_id": intent.id,
            "status": "completed" if intent.status == "succeeded" else "pending",
            "transaction_id": intent.id,
            "error_message": None,
        }
    except Exception as exc:
        logger.error("Stripe payment failed: %s", exc)
        return {
            "success": False,
            "payment_id": f"failed_{secrets.token_hex(8)}",
            "status": "failed",
            "transaction_id": None,
            "error_message": str(exc),
        }


async def execute_adyen_payment(
    adyen_key: str,
    merchant_account: str,
    payment_data: PaymentExecuteRequest,
) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://checkout-test.adyen.com/v70/payments",
                headers={
                    "X-API-Key": adyen_key,
                    "Content-Type": "application/json",
                },
                json={
                    "amount": {
                        "value": int(payment_data.amount),
                        "currency": payment_data.currency.upper(),
                    },
                    "reference": payment_data.order_id,
                    "merchantAccount": merchant_account,
                    "paymentMethod": {
                        "type": "scheme",
                        "number": "4111111111111111",
                        "expiryMonth": "03",
                        "expiryYear": "2030",
                        "holderName": "Test User",
                        "cvc": "737",
                    },
                    "shopperEmail": payment_data.customer_email,
                    "metadata": payment_data.metadata or {},
                },
                timeout=30.0,
            )
            result = response.json()
            if response.status_code == 200:
                return {
                    "success": result.get("resultCode") == "Authorised",
                    "payment_id": result.get("pspReference", f"adyen_{secrets.token_hex(8)}"),
                    "status": "completed" if result.get("resultCode") == "Authorised" else "failed",
                    "transaction_id": result.get("pspReference"),
                    "error_message": result.get("refusalReason") if result.get("resultCode") != "Authorised" else None,
                }
            return {
                "success": False,
                "payment_id": f"failed_{secrets.token_hex(8)}",
                "status": "failed",
                "transaction_id": None,
                "error_message": result.get("message", "Adyen payment failed"),
            }
    except Exception as exc:
        logger.error("Adyen payment failed: %s", exc)
        return {
            "success": False,
            "payment_id": f"failed_{secrets.token_hex(8)}",
            "status": "failed",
            "transaction_id": None,
            "error_message": str(exc),
        }


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


@router.post("/execute", response_model=PaymentExecuteResponse)
async def execute_payment(
    payment_request: PaymentExecuteRequest,
    x_merchant_api_key: str = Header(None, alias="X-Merchant-API-Key"),
):
    try:
        merchant = await verify_merchant_api_key(x_merchant_api_key)
        logger.info(
            "Payment request from merchant: %s (%s)",
            merchant["merchant_id"],
            merchant["business_name"],
        )

        candidates, route_config = await _resolve_payment_candidates(merchant, payment_request)
        if not candidates:
            raise HTTPException(
                status_code=400,
                detail="Payment routing is not configured for any active processor",
            )

        last_result: Optional[Dict[str, Any]] = None
        last_provider = "unknown"
        errors: List[str] = []

        for candidate in candidates:
            provider = str(candidate.get("provider") or "").strip().lower()
            last_provider = provider or last_provider

            if provider not in SUPPORTED_MERCHANT_PAYMENT_PROVIDERS:
                errors.append(f"{provider}: unsupported provider")
                continue

            if provider == "stripe":
                api_key = str(candidate.get("api_key") or "").strip()
                if not api_key:
                    errors.append("stripe: missing API key")
                    continue
                result = await execute_stripe_payment(api_key, merchant, payment_request)
            else:
                api_key = str(candidate.get("api_key") or "").strip()
                merchant_account = (
                    str(candidate.get("account_id") or "").strip()
                    or getattr(settings, "adyen_merchant_account", "").strip()
                    or "WoopayECOM"
                )
                if not api_key:
                    errors.append("adyen: missing API key")
                    continue
                result = await execute_adyen_payment(api_key, merchant_account, payment_request)

            last_result = result
            if result.get("success"):
                await _emit_payment_webhook_best_effort(
                    merchant["merchant_id"],
                    event_type="payment.completed",
                    payment_request=payment_request,
                    result=result,
                    psp_used=provider,
                )
                return PaymentExecuteResponse(
                    success=True,
                    payment_id=result["payment_id"],
                    order_id=payment_request.order_id,
                    amount=payment_request.amount,
                    currency=payment_request.currency,
                    psp_used=provider,
                    status=result["status"],
                    transaction_id=result.get("transaction_id"),
                    error_message=result.get("error_message"),
                    timestamp=datetime.now().isoformat(),
                )

            errors.append(f"{provider}: {result.get('error_message') or 'payment failed'}")

        if last_result is None:
            raise HTTPException(
                status_code=400,
                detail="No supported active PSPs are configured for this merchant",
            )

        await _emit_payment_webhook_best_effort(
            merchant["merchant_id"],
            event_type="payment.failed",
            payment_request=payment_request,
            result=last_result,
            psp_used=last_provider,
        )
        return PaymentExecuteResponse(
            success=False,
            payment_id=last_result["payment_id"],
            order_id=payment_request.order_id,
            amount=payment_request.amount,
            currency=payment_request.currency,
            psp_used=last_provider,
            status=last_result["status"],
            transaction_id=last_result.get("transaction_id"),
            error_message=last_result.get("error_message") or "; ".join(errors),
            timestamp=datetime.now().isoformat(),
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Payment execution failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Payment execution failed: {str(exc)}",
        )


@router.get("/health")
async def payment_router_health():
    return {
        "status": "healthy",
        "service": "payment-execution-router",
        "timestamp": datetime.now().isoformat(),
    }
