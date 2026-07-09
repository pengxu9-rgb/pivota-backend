"""P-T2.3.3 — isolated off-session Stripe capture for the ACP test canary.

⚠️ UNVERIFIED AGAINST REAL STRIPE. This is mock-tested only; it has never run a
real capture. It MUST be proven in the deployed test-mode canary (merch_efbc,
test key, pm_card_visa) before AGENT_ACP_TEST_CAPTURE is trusted.

Why this exists: the shared charge path (create_payment_with_failover →
stripe_adapter.create_payment_intent) creates a CLIENT-CONFIRMED PaymentIntent
(automatic_payment_methods, no payment_method, no confirm) — it hands back a
client_secret for a frontend to confirm. That cannot complete an in-chat charge
where the buyer never leaves the agent. This helper does the server-side,
off-session create+confirm against the MERCHANT's own Stripe key.

Scope / safety:
- Called ONLY from create_payment's ACP test-capture lane
  (kill-switch permitted + guarded protocol + AGENT_ACP_TEST_CAPTURE on + within
  the amount cap). Never on any other flow.
- Charges the MERCHANT's runtime Stripe key (merchant of record); never falls
  back to Pivota's platform key.
- Belt-and-suspenders amount cap in addition to the caller's cap.
- Idempotency-keyed create so a retry cannot double-charge.
- Never raises: returns a normalized result the caller maps into the existing
  success path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from config.settings import settings
from services.merchant_psp_config_service import fetch_active_runtime_merchant_psp

logger = logging.getLogger("acp_offsession_capture")

# Stripe test payment method that succeeds off_session with no real card. Used
# for the first test-mode proof; a real buyer-tokenized pm_ replaces it later.
DEFAULT_TEST_PAYMENT_METHOD = "pm_card_visa"


@dataclass(frozen=True)
class OffSessionCaptureResult:
    success: bool
    status: Optional[str]  # succeeded | requires_action | failed | ...
    payment_intent_id: Optional[str]
    amount_cents: Optional[int]
    currency: Optional[str]
    error: Optional[str]
    error_code: Optional[str]


def _fail(amount_cents, currency, error, code, status="failed") -> OffSessionCaptureResult:
    return OffSessionCaptureResult(
        success=False, status=status, payment_intent_id=None,
        amount_cents=amount_cents, currency=currency, error=error, error_code=code,
    )


async def capture_offsession(
    *,
    merchant_id: str,
    amount_cents: int,
    currency: str,
    idempotency_key: str,
    payment_method: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    max_cents: Optional[int] = None,
) -> OffSessionCaptureResult:
    """Create + confirm an off_session PaymentIntent on the merchant's Stripe key.

    Returns a normalized result; never raises. `payment_method` defaults to the
    Stripe test PM for the canary. `max_cents` defaults to the configured ACP
    test cap (defense-in-depth against the caller).
    """
    cap = settings.agent_acp_test_max_cents if max_cents is None else int(max_cents)
    try:
        amount_cents = int(amount_cents)
    except (TypeError, ValueError):
        return _fail(amount_cents, currency, "invalid_amount", "invalid_amount")
    if amount_cents <= 0:
        return _fail(amount_cents, currency, "non_positive_amount", "invalid_amount")
    if amount_cents > cap:
        # Should never reach here (caller caps first), but refuse hard regardless.
        return _fail(amount_cents, currency, f"amount_exceeds_cap:{cap}", "over_cap")

    pm = str(payment_method or "").strip() or DEFAULT_TEST_PAYMENT_METHOD

    # Resolve the MERCHANT's runtime Stripe key (merchant of record).
    try:
        row = await fetch_active_runtime_merchant_psp(merchant_id=merchant_id, provider="stripe")
    except Exception as exc:  # noqa: BLE001
        logger.warning("acp_offsession: merchant PSP lookup failed merchant_id=%s: %s", merchant_id, str(exc)[:200])
        row = None
    api_key = str((row or {}).get("api_key") or (row or {}).get("secret_key") or "").strip()
    if not api_key:
        return _fail(amount_cents, currency, "merchant_stripe_key_unresolved", "no_merchant_psp")
    # Hard refuse to charge a LIVE key from this test-only path (defense-in-depth:
    # the canary must run on a test key; a live key here is a misconfiguration).
    if api_key.startswith("sk_live_") or api_key.startswith("rk_live_"):
        logger.error("acp_offsession: REFUSING live Stripe key in test-capture lane merchant_id=%s", merchant_id)
        return _fail(amount_cents, currency, "live_key_in_test_lane", "live_key_refused")

    try:
        import stripe  # local import: keep module import-safe without stripe configured

        client = stripe.StripeClient(api_key)
        intent = await asyncio.to_thread(
            client.v1.payment_intents.create,
            {
                "amount": amount_cents,
                "currency": str(currency or "usd").lower(),
                "payment_method": pm,
                "off_session": True,
                "confirm": True,
                "metadata": {**(metadata or {}), "pivota_acp_test_capture": "true"},
            },
            {"idempotency_key": str(idempotency_key)},
        )
    except Exception as exc:  # noqa: BLE001 — Stripe CardError etc.; normalize
        code = (
            getattr(exc, "code", None)
            or getattr(getattr(exc, "error", None), "code", None)
            or "stripe_error"
        )
        logger.warning("acp_offsession: capture failed merchant_id=%s code=%s: %s", merchant_id, code, str(exc)[:200])
        return _fail(amount_cents, currency, str(exc)[:300], str(code))

    status = getattr(intent, "status", None)
    intent_id = getattr(intent, "id", None)
    if status == "succeeded":
        logger.info("acp_offsession: captured merchant_id=%s intent=%s amount=%s", merchant_id, intent_id, amount_cents)
        return OffSessionCaptureResult(
            success=True, status=status, payment_intent_id=intent_id,
            amount_cents=getattr(intent, "amount", amount_cents),
            currency=getattr(intent, "currency", currency), error=None, error_code=None,
        )
    # Off-session that needs SCA/action can't be completed without the buyer.
    logger.warning("acp_offsession: non-succeeded status merchant_id=%s intent=%s status=%s", merchant_id, intent_id, status)
    return OffSessionCaptureResult(
        success=False, status=status, payment_intent_id=intent_id,
        amount_cents=amount_cents, currency=currency,
        error=f"unexpected_status:{status}",
        error_code="requires_action" if status == "requires_action" else "not_succeeded",
    )
