"""
Unified merchant payment initiation helpers.

This module normalizes payment initiation responses so merchant `/payment/execute`
and order creation return the same PSP-facing action contract.
"""

from __future__ import annotations

from decimal import Decimal
import json
from typing import Any, Dict, List, Optional, Tuple

from fastapi.encoders import jsonable_encoder

from adapters.multi_psp_orchestrator import create_payment_with_failover
from adapters.psp_adapter import get_psp_adapter
from services.merchant_psp_config_service import (
    build_runtime_adapter_kwargs,
    evaluate_psp_readiness,
)


def _normalize_preferred_psps(preferred_psps: Optional[List[str]]) -> List[str]:
    normalized: List[str] = []
    for item in preferred_psps or []:
        provider = str(item or "").strip().lower()
        if provider and provider not in normalized:
            normalized.append(provider)
    return normalized


def _extract_payment_public_key(payment_intent: Any, raw: Dict[str, Any]) -> Optional[str]:
    value = str(
        getattr(payment_intent, "public_key", None)
        or raw.get("public_key")
        or raw.get("publicKey")
        or raw.get("publishable_key")
        or raw.get("publishableKey")
        or ""
    ).strip()
    return value or None


def _extract_payment_stripe_account(payment_intent: Any, raw: Dict[str, Any]) -> Optional[str]:
    value = str(
        getattr(payment_intent, "stripe_account", None)
        or raw.get("stripe_account")
        or raw.get("stripeAccount")
        or raw.get("account_id")
        or raw.get("accountId")
        or ""
    ).strip()
    return value or None


CLIENT_OWNED_PAYMENT_ACTION_TYPES = {
    "stripe_client_secret",
    "adyen_session",
    "checkout_session",
    "redirect_url",
}

KNOWN_PAYMENT_STATUSES = {
    "pending",
    "requires_action",
    "processing",
    "paid",
    "payment_failed",
    "cancelled",
    "refunded",
    "partially_refunded",
    "unknown",
}


def _normalize_payment_status(raw_status: Any) -> Tuple[str, Optional[str]]:
    token = str(raw_status or "").strip().lower()
    if not token:
        return "unknown", None

    aliases = {
        "requires_payment_method": "requires_action",
        "requires_confirmation": "requires_action",
        "requires_action": "requires_action",
        "authorised": "processing",
        "authorized": "processing",
        "requires_capture": "processing",
        "processing": "processing",
        "pending": "pending",
        "paid": "paid",
        "completed": "paid",
        "succeeded": "paid",
        "success": "paid",
        "settled": "paid",
        "failed": "payment_failed",
        "payment_failed": "payment_failed",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "refunded": "refunded",
        "partially_refunded": "partially_refunded",
        "partial_refund": "partially_refunded",
    }
    normalized = aliases.get(token)
    if normalized:
        return normalized, None
    return "unknown", token


def _build_payment_surface_contract(action: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    action_type = str((action or {}).get("type") or "").strip().lower()
    if action_type == "stripe_client_secret":
        return {
            "confirmation_owner": "client",
            "requires_client_confirmation": True,
            "submit_owner": "external_button",
            "component_kind": "stripe_payment_element",
            "supported_in_shopping_ui": True,
        }
    if action_type == "adyen_session":
        return {
            "confirmation_owner": "client",
            "requires_client_confirmation": True,
            "submit_owner": "component",
            "component_kind": "adyen_dropin",
            "supported_in_shopping_ui": True,
        }
    if action_type == "redirect_url":
        return {
            "confirmation_owner": "client",
            "requires_client_confirmation": True,
            "submit_owner": "redirect",
            "component_kind": None,
            "supported_in_shopping_ui": True,
        }
    if action_type == "checkout_session":
        return {
            "confirmation_owner": "client",
            "requires_client_confirmation": True,
            "submit_owner": "unsupported",
            "component_kind": "checkout_embedded",
            "supported_in_shopping_ui": False,
        }
    return {
        "confirmation_owner": "backend",
        "requires_client_confirmation": False,
        "submit_owner": None,
        "component_kind": None,
        "supported_in_shopping_ui": True,
    }


def _apply_candidate_preference(
    candidates: List[Dict[str, Any]],
    preferred_psps: Optional[List[str]],
) -> List[Dict[str, Any]]:
    preferred_order = _normalize_preferred_psps(preferred_psps)
    if not preferred_order:
        return list(candidates or [])

    order_map = {provider: index for index, provider in enumerate(preferred_order)}
    filtered_candidates = [
        candidate
        for candidate in (candidates or [])
        if str(candidate.get("provider") or "").strip().lower() in order_map
    ]
    return sorted(
        filtered_candidates,
        key=lambda candidate: order_map.get(str(candidate.get("provider") or "").strip().lower(), 999),
    )


def _normalize_raw_payload(raw: Any) -> Dict[str, Any]:
    def _encode_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return jsonable_encoder(candidate)
        except Exception:
            try:
                return json.loads(json.dumps(candidate, default=str))
            except Exception:
                return {}

    candidate: Dict[str, Any]
    if isinstance(raw, dict):
        candidate = dict(raw)
        return _encode_candidate(candidate)
    if hasattr(raw, "to_dict"):
        try:
            candidate = dict(raw.to_dict())
            return _encode_candidate(candidate)
        except Exception:
            return {}
    try:
        candidate = dict(raw or {})
        return _encode_candidate(candidate)
    except Exception:
        return {}


def build_payment_action(payment_intent: Any, *, psp_used: str) -> Dict[str, Any]:
    raw = _normalize_raw_payload(getattr(payment_intent, "raw_response", None))
    client_secret = getattr(payment_intent, "client_secret", None)
    redirect_url = getattr(payment_intent, "redirect_url", None)
    public_key = _extract_payment_public_key(payment_intent, raw)
    payment_type = str(psp_used or getattr(payment_intent, "psp_type", "") or "").strip().lower()

    if redirect_url:
        action = {
            "type": "redirect_url",
            "url": redirect_url,
            "raw": raw,
        }
        action.update(_build_payment_surface_contract(action))
        return action

    if payment_type == "stripe" and client_secret:
        stripe_account = _extract_payment_stripe_account(payment_intent, raw)
        action = {
            "type": "stripe_client_secret",
            "client_secret": client_secret,
            "public_key": public_key,
            "stripe_account": stripe_account,
            "raw": raw,
        }
        action.update(_build_payment_surface_contract(action))
        return action

    if payment_type == "adyen" and client_secret:
        action = {
            "type": "adyen_session",
            "client_secret": client_secret,
            "session_data": client_secret,
            "client_key": raw.get("clientKey"),
            "raw": raw,
        }
        action.update(_build_payment_surface_contract(action))
        return action

    if payment_type == "checkout":
        hosted_url = str(raw.get("hosted_url") or raw.get("hostedUrl") or "").strip()
        if hosted_url:
            action = {
                "type": "redirect_url",
                "url": hosted_url,
                "raw": raw,
            }
            action.update(_build_payment_surface_contract(action))
            return action
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
        action.update(_build_payment_surface_contract(action))
        return action

    if isinstance(client_secret, str) and client_secret.startswith("http"):
        action = {
            "type": "redirect_url",
            "url": client_secret,
            "raw": raw,
        }
        action.update(_build_payment_surface_contract(action))
        return action

    action = {
        "type": None,
        "client_secret": client_secret,
        "public_key": public_key,
        "raw": raw,
    }
    action.update(_build_payment_surface_contract(action))
    return action


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
    requires_customer_action = bool(
        payment_action and payment_action.get("type") in CLIENT_OWNED_PAYMENT_ACTION_TYPES
    )

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

    payment_status, payment_status_raw = _normalize_payment_status(normalized_status)
    if payment_status == "unknown" and not success and error:
        payment_status = "payment_failed"
        payment_status_raw = None
    surface_contract = _build_payment_surface_contract(payment_action)
    if payment_status in {"paid", "payment_failed", "cancelled", "refunded", "partially_refunded"}:
        surface_contract = {
            **surface_contract,
            "confirmation_owner": "backend",
            "requires_client_confirmation": False,
            "submit_owner": None,
            "component_kind": None,
            "supported_in_shopping_ui": True,
        }

    return {
        "success": success,
        "status": normalized_status,
        "payment_status": payment_status,
        **({"payment_status_raw": payment_status_raw} if payment_status_raw else {}),
        "confirmation_owner": surface_contract["confirmation_owner"],
        "requires_client_confirmation": surface_contract["requires_client_confirmation"],
        "psp_used": psp_value,
        "payment_id": payment_id,
        "transaction_id": transaction_id,
        "requires_customer_action": requires_customer_action,
        "payment_action": payment_action,
        "public_key": _extract_payment_public_key(payment_intent, raw),
        "stripe_account": _extract_payment_stripe_account(payment_intent, raw),
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
        ordered_candidates = _apply_candidate_preference(candidates, preferred_psps)
        last_error = "No supported PSP candidates"
        last_psp = "unknown"
        for candidate in ordered_candidates:
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
                        api_key=api_key,
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
