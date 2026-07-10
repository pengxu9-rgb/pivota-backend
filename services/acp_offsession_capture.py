"""P-T2.3.3 / P-T2.3.7 — isolated off-session capture for the ACP charge lane.

⚠️ The Stripe path is proven (the P-T2.3.3/P-T2.3.5 canary charged real Stripe in
test and live mode). New PSP adapters (Adyen, …) are UNVERIFIED until their own
deployed canary runs.

Why this exists: the shared charge path (create_payment_with_failover →
stripe_adapter.create_payment_intent) creates a CLIENT-CONFIRMED PaymentIntent
(automatic_payment_methods, no payment_method, no confirm) — it hands back a
client_secret for a frontend to confirm. That cannot complete an in-chat charge
where the buyer never leaves the agent. This helper does the server-side,
off-session create+confirm against the MERCHANT's own PSP key.

P-T2.3.7 makes it **PSP-agnostic**: `capture_offsession` is a thin orchestrator
that owns the money-safety invariants shared by every PSP (amount cap, the
test/live-key lane guard, the merchant-of-record key resolution, the normalized
result) and then dispatches to a per-provider `CaptureProvider` adapter for the
actual charge. The Stripe adapter is the extracted, byte-equivalent original.

Scope / safety (unchanged):
- Called ONLY from create_payment's ACP capture lane (kill-switch permitted +
  guarded protocol + AGENT_ACP_TEST_CAPTURE/AGENT_ACP_ALLOW_LIVE_CAPTURE on +
  within the amount cap). Never on any other flow.
- Charges the MERCHANT's own runtime PSP key (merchant of record); never falls
  back to Pivota's platform key.
- Belt-and-suspenders amount cap in addition to the caller's cap.
- Idempotency-keyed create so a retry cannot double-charge.
- test lane HARD-REFUSES a live key; live lane HARD-REFUSES a test key — enforced
  here for every provider, not just Stripe.
- An unsupported PSP fails closed (no charge, normalized error).
- Never raises: returns a normalized result the caller maps into the success path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from config.settings import settings
from services.merchant_psp_config_service import (
    fetch_active_runtime_merchant_psp,
    normalize_psp_environment,
)

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


def _key_is_live(provider: str, api_key: str, environment: Optional[str]) -> bool:
    """Provider-aware live-key detection for the lane guard.

    Stripe keeps its exact original prefix check (secret + restricted live keys);
    every other provider defers to the canonical `normalize_psp_environment`
    (e.g. Adyen `live_`/`test_`). Conservative by construction: anything not
    positively identified as live is treated as non-live, and the caller's guard
    refuses a non-live key on the live lane.
    """
    key = str(api_key or "").strip()
    if provider == "stripe":
        return key.startswith("sk_live_") or key.startswith("rk_live_")
    return normalize_psp_environment(provider, api_key, environment) == "live"


class CaptureProvider(Protocol):
    """A PSP-specific off-session charge. Receives the merchant's own key (already
    lane-checked by the orchestrator) and returns a normalized result; never
    raises."""

    async def capture(
        self,
        *,
        merchant_id: str,
        api_key: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        payment_method: str,
        metadata: Dict[str, Any],
        allow_live: bool,
    ) -> OffSessionCaptureResult:
        ...


class _StripeCaptureAdapter:
    """Stripe off-session create+confirm. Extracted verbatim from the proven
    P-T2.3.3/P-T2.3.5 path — behavior is unchanged."""

    async def capture(
        self,
        *,
        merchant_id: str,
        api_key: str,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        payment_method: str,
        metadata: Dict[str, Any],
        allow_live: bool,
    ) -> OffSessionCaptureResult:
        # Payment method: the test lane may default to Stripe's test PM; the live
        # lane MUST carry a real buyer-provided pm_ (a test PM would fail on a live
        # key, and silently defaulting to it on the live lane would be dangerously
        # misleading).
        pm = str(payment_method or "").strip()
        if allow_live:
            if not pm or pm == DEFAULT_TEST_PAYMENT_METHOD:
                return _fail(
                    amount_cents, currency,
                    "live_capture_requires_real_payment_method", "live_pm_required",
                )
        else:
            # Test lane: only honor a real Stripe payment method (pm_*). A
            # placeholder or delegate token the surface may forward (e.g.
            # "tok_test", "vt_*") is NOT a chargeable test PM, so fall back to the
            # test PM — the test canary always charges a valid PM regardless of
            # what token rode in.
            pm = pm if pm.startswith("pm_") else DEFAULT_TEST_PAYMENT_METHOD

        try:
            import stripe  # local import: keep module import-safe without stripe configured

            client = stripe.StripeClient(api_key)

            # Resolve the customer the PaymentMethod is attached to, if any. A card
            # set up for off-session via a SetupIntent{customer, usage: off_session}
            # carries its customer + mandate; passing the customer lets Stripe apply
            # that mandate so an SCA (EU/UK) card charges off_session WITHOUT
            # re-challenging (which would otherwise come back requires_action). A pm
            # with no customer (e.g. a bare createPaymentMethod for a US/non-SCA
            # card, or the test pm_card_visa) → charge as before. Best-effort: a
            # lookup failure never blocks the charge.
            customer_id: Optional[str] = None
            try:
                pm_obj = await asyncio.to_thread(client.v1.payment_methods.retrieve, pm)
                customer_id = getattr(pm_obj, "customer", None) or None
            except Exception as _pm_exc:  # noqa: BLE001 — charge without customer on lookup failure
                logger.warning(
                    "acp_offsession: payment_method lookup failed (charging without customer) "
                    "merchant_id=%s: %s", merchant_id, str(_pm_exc)[:150],
                )

            intent_params: Dict[str, Any] = {
                "amount": amount_cents,
                "currency": str(currency or "usd").lower(),
                "payment_method": pm,
                "off_session": True,
                "confirm": True,
                "metadata": {
                    **(metadata or {}),
                    ("pivota_acp_live_capture" if allow_live else "pivota_acp_test_capture"): "true",
                },
            }
            if customer_id:
                intent_params["customer"] = customer_id
            intent = await asyncio.to_thread(
                client.v1.payment_intents.create,
                intent_params,
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


# Registry of PSP-specific capture adapters. Adyen (P-T2.3.7 item 3) slots in here
# once its MIT /payments adapter lands; any provider absent here fails closed.
_CAPTURE_ADAPTERS: Dict[str, CaptureProvider] = {
    "stripe": _StripeCaptureAdapter(),
}


async def capture_offsession(
    *,
    merchant_id: str,
    amount_cents: int,
    currency: str,
    idempotency_key: str,
    payment_method: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    max_cents: Optional[int] = None,
    allow_live: bool = False,
) -> OffSessionCaptureResult:
    """Create + confirm an off-session charge on the merchant's own PSP.

    PSP-agnostic orchestrator: owns the shared money-safety invariants and
    dispatches the actual charge to the provider adapter. Returns a normalized
    result; never raises.

    Two lanes:
    - test lane (`allow_live=False`, default): a LIVE merchant key is HARD-REFUSED
      (the canary must run test-mode). The proven P-T2.3.3 path.
    - live lane (`allow_live=True`, P-T2.3.5): a live merchant key is permitted and
      a REAL buyer payment method is REQUIRED. The caller must have cleared the
      separate live gate (`resolve_acp_live_capture`) first; this flag only relaxes
      the test-lane refusal, it does not itself authorize anything.

    `max_cents` defaults to the configured test cap; the live lane caller passes
    the (lower) live cap. Either way it's a belt-and-suspenders check on top of the
    caller's cap.
    """
    cap = (
        settings.agent_acp_test_max_cents if max_cents is None else int(max_cents)
    )
    try:
        amount_cents = int(amount_cents)
    except (TypeError, ValueError):
        return _fail(amount_cents, currency, "invalid_amount", "invalid_amount")
    if amount_cents <= 0:
        return _fail(amount_cents, currency, "non_positive_amount", "invalid_amount")
    if amount_cents > cap:
        # Should never reach here (caller caps first), but refuse hard regardless.
        return _fail(amount_cents, currency, f"amount_exceeds_cap:{cap}", "over_cap")

    # Resolve the MERCHANT's active runtime PSP (merchant of record) — any provider,
    # not just Stripe. Dispatch is by the row's provider.
    try:
        row = await fetch_active_runtime_merchant_psp(merchant_id=merchant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("acp_offsession: merchant PSP lookup failed merchant_id=%s: %s", merchant_id, str(exc)[:200])
        row = None
    provider = str((row or {}).get("provider") or "").strip().lower()
    api_key = str((row or {}).get("api_key") or (row or {}).get("secret_key") or "").strip()
    environment = (row or {}).get("environment")
    if not api_key:
        return _fail(amount_cents, currency, "merchant_psp_key_unresolved", "no_merchant_psp")

    # Test/live-key lane guard — provider-agnostic, enforced BEFORE any adapter runs.
    key_is_live = _key_is_live(provider, api_key, environment)
    if not allow_live and key_is_live:
        # Test lane: a live key is a misconfiguration — hard refuse (the canary
        # must run on a test key). Defense-in-depth (P-T2.3.3).
        logger.error("acp_offsession: REFUSING live key in test-capture lane merchant_id=%s provider=%s", merchant_id, provider)
        return _fail(amount_cents, currency, "live_key_in_test_lane", "live_key_refused")
    if allow_live and not key_is_live:
        # Live lane armed but the merchant key is test-mode: refuse rather than
        # move $0 while reporting a "live" capture (honest + fail-closed).
        logger.error("acp_offsession: live lane requires a live merchant key merchant_id=%s provider=%s", merchant_id, provider)
        return _fail(amount_cents, currency, "live_lane_requires_live_key", "live_key_required")

    adapter = _CAPTURE_ADAPTERS.get(provider)
    if adapter is None:
        # Fail closed: we resolved a key but have no capture adapter for this PSP.
        logger.error("acp_offsession: no capture adapter for provider=%s merchant_id=%s", provider or "<none>", merchant_id)
        return _fail(amount_cents, currency, f"unsupported_capture_provider:{provider or 'unknown'}", "unsupported_provider")

    return await adapter.capture(
        merchant_id=str(merchant_id),
        api_key=api_key,
        amount_cents=amount_cents,
        currency=currency,
        idempotency_key=str(idempotency_key),
        payment_method=payment_method or "",
        metadata=dict(metadata or {}),
        allow_live=allow_live,
    )
