"""
Unified merchant payment initiation helpers.

This module normalizes payment initiation responses so merchant `/payment/execute`
and order creation return the same PSP-facing action contract.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi.encoders import jsonable_encoder

from adapters.multi_psp_orchestrator import create_payment_with_failover
from adapters.psp_adapter import get_psp_adapter
from services.merchant_psp_config_service import (
    build_runtime_adapter_kwargs,
    evaluate_psp_readiness,
)


def _normalize_raw_payload(raw: Any) -> Dict[str, Any]:
    candidate: Dict[str, Any]
    if isinstance(raw, dict):
        candidate = dict(raw)
        return jsonable_encoder(candidate)
    if hasattr(raw, "to_dict"):
        try:
            candidate = dict(raw.to_dict())
            return jsonable_encoder(candidate)
        except Exception:
            return {}
    try:
        candidate = dict(raw or {})
        return jsonable_encoder(candidate)
    except Exception:
        return {}


def build_payment_action(payment_intent: Any, *, psp_used: str) -> Dict[str, Any]:
    raw = _normalize_raw_payload(getattr(payment_intent, "raw_response", None))
    client_secret = getattr(payment_intent, "client_secret", None)
    redirect_url = getattr(payment_intent, "redirect_url", None)
    payment_type = str(psp_used or getattr(payment_intent, "psp_type", "") or "").strip().lower()

    if redirect_url:
        return {
            "type": "redirect_url",
            "url": redirect_url,
            "raw": raw,
        }

    if payment_type == "stripe" and client_secret:
        return {
            "type": "stripe_client_secret",
            "client_secret": client_secret,
            "raw": raw,
        }

    if payment_type == "adyen" and client_secret:
        return {
            "type": "adyen_session",
            "client_secret": client_secret,
            "session_data": client_secret,
            "client_key": raw.get("clientKey"),
            "raw": raw,
        }

    if payment_type == "checkout":
        action: Dict[str, Any] = {
            "type": "checkout_session",
            "client_secret": client_secret,
            "session_token": client_secret,
            "public_key": raw.get("public_key"),
            "processing_channel_id": raw.get("processing_channel_id"),
            "raw": raw,
        }
        if isinstance(client_secret, str) and client_secret.startswith("http"):
            action = {
                "type": "redirect_url",
                "url": client_secret,
                "raw": raw,
            }
        return action

    if isinstance(client_secret, str) and client_secret.startswith("http"):
        return {
            "type": "redirect_url",
            "url": client_secret,
            "raw": raw,
        }

    return {
        "type": None,
        "client_secret": client_secret,
        "raw": raw,
    }


def build_payment_initiation_result(
    *,
    success: bool,
    payment_intent: Any = None,
    error: Optional[str] = None,
    psp_used: Optional[str] = None,
) -> Dict[str, Any]:
    payment_id = getattr(payment_intent, "id", None) or ""
    raw = _normalize_raw_payload(getattr(payment_intent, "raw_response", None))
    status = str(getattr(payment_intent, "status", "") or "").strip().lower() or "failed"
    psp_value = str(psp_used or getattr(payment_intent, "psp_type", "") or "unknown").strip().lower()
    payment_action = build_payment_action(payment_intent, psp_used=psp_value) if payment_intent else None
    requires_customer_action = bool(payment_action and payment_action.get("type") in {
        "stripe_client_secret",
        "adyen_session",
        "checkout_session",
        "redirect_url",
    })

    transaction_id = (
        raw.get("pspReference")
        or raw.get("action_id")
        or raw.get("transaction_id")
        or payment_id
    )

    normalized_status = status
    if success and requires_customer_action and status in {"requires_action", "pending"}:
        normalized_status = "requires_action"
    elif success and status in {"authorized", "processing"}:
        normalized_status = "pending"
    elif success and status in {"succeeded", "completed", "authorised", "authorized"}:
        normalized_status = "succeeded"

    return {
        "success": success,
        "status": normalized_status,
        "psp_used": psp_value,
        "payment_id": payment_id,
        "transaction_id": transaction_id,
        "requires_customer_action": requires_customer_action,
        "payment_action": payment_action,
        "error_message": error,
    }


async def initiate_merchant_payment(
    *,
    merchant_id: str,
    amount: Decimal,
    currency: str,
    metadata: Dict[str, Any],
    preferred_psps: Optional[List[str]] = None,
    candidates: Optional[List[Dict[str, Any]]] = None,
    canonical_psp_required: bool = False,
    enforce_live_readiness: bool = False,
) -> Dict[str, Any]:
    enforce_live_readiness = bool(
        enforce_live_readiness or metadata.get("enforce_live_readiness")
    )
    canonical_psp_required = bool(
        canonical_psp_required or metadata.get("canonical_psp_required")
    )

    if candidates:
        last_error = "No supported PSP candidates"
        last_psp = "unknown"
        for candidate in candidates:
            provider = str(candidate.get("provider") or "").strip().lower()
            api_key = str(candidate.get("api_key") or "").strip()
            if not provider or not api_key:
                continue
            if enforce_live_readiness:
                readiness = evaluate_psp_readiness(
                    provider,
                    status=candidate.get("status"),
                    api_key=api_key,
                    account_id=candidate.get("account_id"),
                    provider_config=candidate.get("provider_config"),
                    environment=candidate.get("environment"),
                    validation_status=candidate.get("validation_status"),
                    validation_error=candidate.get("validation_error"),
                )
                if not readiness["live_charge_ready"]:
                    last_psp = provider
                    last_error = "; ".join(readiness["readiness_blockers"]) or f"{provider} is not ready for live charge"
                    continue
            last_psp = provider
            try:
                adapter = get_psp_adapter(
                    provider,
                    api_key,
                    **build_runtime_adapter_kwargs(
                        provider,
                        account_id=candidate.get("account_id"),
                        provider_config=candidate.get("provider_config"),
                        environment=candidate.get("environment"),
                        secret_key=candidate.get("secret_key"),
                    ),
                )
                success, payment_intent, error = await adapter.create_payment_intent(
                    amount=amount,
                    currency=currency,
                    metadata={
                        **metadata,
                        "psp_type": provider,
                    },
                )
                if success and payment_intent:
                    return build_payment_initiation_result(
                        success=True,
                        payment_intent=payment_intent,
                        error=None,
                        psp_used=provider,
                    )
                last_error = error or f"{provider} initiation failed"
            except Exception as exc:
                last_error = str(exc)

        return {
            "success": False,
            "status": "failed",
            "psp_used": last_psp,
            "payment_id": "",
            "transaction_id": None,
            "requires_customer_action": False,
            "payment_action": None,
            "error_message": last_error,
        }

    success, payment_intent, error, psp_used = await create_payment_with_failover(
        merchant_id=merchant_id,
        amount=amount,
        currency=currency,
        metadata=metadata,
        preferred_psps=preferred_psps,
        canonical_psp_required=canonical_psp_required,
        enforce_live_readiness=enforce_live_readiness,
    )
    return build_payment_initiation_result(
        success=success,
        payment_intent=payment_intent,
        error=error,
        psp_used=psp_used,
    )
