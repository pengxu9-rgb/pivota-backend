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

P1 PR-B adds the Stripe SharedPaymentToken (`spt_`) lane behind
ACP_SPT_CAPTURE_ENABLED (default OFF). It changes ONLY how the Stripe adapter
shapes the PaymentIntent for an `spt_` token; every invariant above is
untouched, and with the flag off an `spt_` behaves byte-for-byte as it does
today. See the SPT block below the constants.
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

# --- Stripe SharedPaymentToken (SPT), the ACP delegated-payment rail ---------
#
# ACP delegated payment does NOT hand the business a PSP payment method. OpenAI
# vaults the buyer's card with Stripe and hands the MERCHANT (merchant of
# record) a single-use, amount-capped, merchant-scoped SharedPaymentToken.
# Pivota never sees cardholder data and never becomes a vault; the charge still
# runs on the merchant's OWN secret key through this same adapter.
#
# Verified against Stripe's seller docs 2026-08-04: the charge is
#   POST /v1/payment_intents
#   amount, currency, confirm=true,
#   payment_method_data[shared_payment_granted_token]=spt_…
# — NOT `payment_method=<spt>`, and there is NO `request_delegated_payment`
# parameter in the seller flow. Stripe enforces the token's
# `usage_limits{currency, max_amount, expires_at}` and single-use at
# confirmation. The parameters live behind a preview API version, which we send
# PER REQUEST (see STRIPE_SPT_API_VERSION) so no other Stripe call in the
# process changes version.
SPT_TOKEN_PREFIX = "spt_"

# The preview API version that exposes the SPT parameters. Sent ONLY on SPT
# requests, via the SDK's per-request `stripe_version` option (stripe-python's
# RequestOptions carries it and turns it into the `Stripe-Version` header;
# verified on 15.1.0 and 15.4.0, and requirements.txt bounds the SDK to <16 so
# the mechanism cannot vanish under a major bump) — the global/default API
# version is deliberately left alone so the proven `pm_` path keeps charging on
# exactly the version it charges on today.
STRIPE_SPT_API_VERSION = "2026-04-22.preview"


def is_shared_payment_token(token: Optional[str]) -> bool:
    """The SPT gate, spelled exactly like the wire contract: `spt_`.

    `pm_` (a real PSP payment method) and `vt_` (a registry delegate allowance)
    are NOT shared payment tokens and are unaffected in either flag state."""
    return str(token or "").strip().startswith(SPT_TOKEN_PREFIX)


def _spt_capture_enabled() -> bool:
    """Read ACP_SPT_CAPTURE_ENABLED at CALL time, never at import time, so the
    flag can be toggled (ops or test) without a reimport — the same idiom the
    other ACP lane gates use. Missing attribute ⇒ OFF (fail closed)."""
    return bool(getattr(settings, "acp_spt_capture_enabled", False))


@dataclass(frozen=True)
class OffSessionCaptureResult:
    success: bool
    status: Optional[str]  # succeeded | requires_action | failed | ...
    payment_intent_id: Optional[str]
    amount_cents: Optional[int]
    currency: Optional[str]
    error: Optional[str]
    error_code: Optional[str]
    # The PSP provider the charge was dispatched to (stamped by the
    # orchestrator once the merchant row resolves; None on pre-dispatch
    # failures). Lets callers record the ACTUAL psp instead of assuming Stripe.
    provider: Optional[str] = None


def _fail(amount_cents, currency, error, code, status="failed") -> OffSessionCaptureResult:
    return OffSessionCaptureResult(
        success=False, status=status, payment_intent_id=None,
        amount_cents=amount_cents, currency=currency, error=error, error_code=code,
    )


def _as_config_dict(value: Any) -> Dict[str, Any]:
    """Coerce a PSP row's provider_config (JSONB dict or JSON string) into a dict.
    Never raises — a malformed value resolves to an empty dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            import json

            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


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
        provider_config: Optional[Dict[str, Any]] = None,
    ) -> OffSessionCaptureResult:
        ...


class _StripeCaptureAdapter:
    """Stripe off-session create+confirm. Extracted verbatim from the proven
    P-T2.3.3/P-T2.3.5 path — the `pm_` behavior is unchanged.

    P1 PR-B adds ONE additional branch, behind ACP_SPT_CAPTURE_ENABLED (default
    off): a SharedPaymentToken (`spt_`) is charged as
    `payment_method_data[shared_payment_granted_token]` under Stripe's preview
    API version. With the flag OFF an `spt_` token is treated exactly as any
    other non-`pm_` token is treated TODAY — substituted with the test PM on the
    test lane, and FORWARDED AS `payment_method` on the live lane (the live-lane
    guard refuses an empty or test PM; it has never required a `pm_` prefix, so
    Stripe is what rejects the token there). Every orchestrator invariant
    (merchant-of-record key, amount cap, test/live key lane guard,
    attempt-scoped stored idempotency key, never-raises) is untouched by the
    branch."""

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
        provider_config: Optional[Dict[str, Any]] = None,  # unused (Stripe needs only the key)
    ) -> OffSessionCaptureResult:
        pm = str(payment_method or "").strip()
        # The SPT lane engages only when the flag is ON *and* the token is an
        # `spt_`. Both lanes may charge one: Stripe issues test-mode SPTs, so the
        # test canary charges a REAL test SPT rather than silently substituting
        # the test PM (substituting would make the canary prove nothing about
        # this rail). The orchestrator's test/live KEY guard is unchanged and
        # already ran — an SPT never weakens which key may be used on which lane.
        spt = _spt_capture_enabled() and is_shared_payment_token(pm)

        if not spt:
            # Payment method: the test lane may default to Stripe's test PM; the live
            # lane MUST carry a real buyer-provided pm_ (a test PM would fail on a live
            # key, and silently defaulting to it on the live lane would be dangerously
            # misleading).
            if allow_live:
                if not pm or pm == DEFAULT_TEST_PAYMENT_METHOD:
                    return _fail(
                        amount_cents, currency,
                        "live_capture_requires_real_payment_method", "live_pm_required",
                    )
            else:
                # Test lane: only honor a real Stripe payment method (pm_*). A
                # placeholder or delegate token the surface may forward (e.g.
                # "tok_test", "vt_*", or an `spt_` while the flag is off) is NOT a
                # chargeable test PM, so fall back to the test PM — the test canary
                # always charges a valid PM regardless of what token rode in.
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
            #
            # SKIPPED entirely on the SPT lane: an SPT is not a PaymentMethod, so
            # there is no PM object to retrieve. Skipping is deliberate, not a
            # tolerated failure — calling retrieve() with an `spt_` would burn a
            # round-trip on a guaranteed 404 and log noise for a non-condition.
            customer_id: Optional[str] = None
            if not spt:
                try:
                    pm_obj = await asyncio.to_thread(client.v1.payment_methods.retrieve, pm)
                    customer_id = getattr(pm_obj, "customer", None) or None
                except Exception as _pm_exc:  # noqa: BLE001 — charge without customer on lookup failure
                    logger.warning(
                        "acp_offsession: payment_method lookup failed (charging without customer) "
                        "merchant_id=%s: %s", merchant_id, str(_pm_exc)[:150],
                    )

            # The attempt-scoped key the SESSION layer stored on the row is what
            # every capture — first try or resume — must charge under. It rides
            # in the request options unchanged on BOTH lanes, so a replay is
            # parameter-identical (the SPT path adds only the version option,
            # which is a constant).
            request_options: Dict[str, Any] = {"idempotency_key": str(idempotency_key)}

            intent_params: Dict[str, Any] = {
                "amount": amount_cents,
                "currency": str(currency or "usd").lower(),
            }
            if spt:
                # The exact seller-flow shape. NOTE on `off_session`: Stripe's
                # seller documentation for SharedPaymentTokens does not list it,
                # and the installed SDK (15.x) predates the preview and carries
                # no `shared_payment` surface at all — so there is no contrary
                # evidence available in the dependency either. We
                # therefore do NOT send it: the delegated-payment authorization
                # rides in the token itself (the buyer authorized it with
                # OpenAI), and sending an undocumented flag on a money call is
                # exactly the kind of guess that turns into a decline or a
                # mis-scoped charge. If a sandbox run (scripts/
                # acp_spt_sandbox_conformance.py) shows Stripe wants it, it is a
                # one-line addition backed by evidence.
                intent_params["payment_method_data"] = {
                    "shared_payment_granted_token": pm,
                }
                request_options["stripe_version"] = STRIPE_SPT_API_VERSION
            else:
                intent_params["payment_method"] = pm
                intent_params["off_session"] = True
            intent_params["confirm"] = True
            intent_params["metadata"] = {
                **(metadata or {}),
                ("pivota_acp_live_capture" if allow_live else "pivota_acp_test_capture"): "true",
            }
            if customer_id:
                intent_params["customer"] = customer_id
            intent = await asyncio.to_thread(
                client.v1.payment_intents.create,
                intent_params,
                request_options,
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


class _AdyenCaptureAdapter:
    """Adyen off-session capture via a merchant-initiated transaction (MIT) against
    a stored payment method — the Adyen analog of Stripe's off_session confirm.

    ⚠️ UNVERIFIED against real Adyen — mocked-tested only; must be proven in a
    deployed Adyen test-mode canary before it is trusted (there is no universal
    Adyen test PM like Stripe's pm_card_visa, so the canary must first tokenize an
    Adyen test card to obtain a shopperReference + storedPaymentMethodId).

    Needs, beyond the API key: `merchant_account` (from the PSP row's
    provider_config), a `storedPaymentMethodId` (the forwarded `payment_method`
    token), and a `shopperReference` (threaded via order/session metadata — the
    stored PM is bound to it). Live requires a `live_url_prefix` in provider_config
    (Adyen's per-merchant live endpoint host). Test/live is taken from `allow_live`
    (the orchestrator already proved the key matches the lane).
    """

    TEST_BASE = "https://checkout-test.adyen.com/v71"
    API_VERSION = "v71"

    @staticmethod
    def _shopper_reference(metadata: Dict[str, Any], provider_config: Dict[str, Any]) -> str:
        md = metadata or {}
        cfg = provider_config or {}
        return str(
            md.get("adyen_shopper_reference")
            or md.get("shopper_reference")
            or cfg.get("shopper_reference")
            or ""
        ).strip()

    def _base_url(self, *, allow_live: bool, provider_config: Dict[str, Any]) -> Optional[str]:
        if not allow_live:
            return self.TEST_BASE
        prefix = str(
            (provider_config or {}).get("live_url_prefix")
            or (provider_config or {}).get("endpoint_prefix")
            or ""
        ).strip()
        if not prefix:
            return None  # live lane needs the per-merchant live host prefix
        return f"https://{prefix}-checkout-live.adyenpayments.com/checkout/{self.API_VERSION}"

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
        provider_config: Optional[Dict[str, Any]] = None,
    ) -> OffSessionCaptureResult:
        cfg = provider_config or {}
        merchant_account = str(cfg.get("merchant_account") or cfg.get("merchantAccount") or "").strip()
        if not merchant_account:
            return _fail(amount_cents, currency, "adyen_merchant_account_missing", "adyen_config")

        stored_pm = str(payment_method or "").strip()
        # Adyen has no universal test PM — a real stored payment method is required
        # on BOTH lanes (the token is a storedPaymentMethodId, not a card).
        # `spt_` joins the refused prefixes (review N3): a Stripe SharedPaymentToken
        # is meaningless to Adyen, and forwarding it produced an adyen_http_*/
        # adyen_refused failure — both AMBIGUOUS, so the session wedged in
        # `completing` until TTL. Refusing here is PRE-DISPATCH instead: it
        # releases a fresh claim and holds a resumed one, which is correct on both.
        if not stored_pm or stored_pm.startswith(("pm_", "tok_", "vt_", "spt_")):
            return _fail(amount_cents, currency, "adyen_requires_stored_payment_method", "adyen_pm_required")

        shopper_ref = self._shopper_reference(metadata, cfg)
        if not shopper_ref:
            # MIT against a stored PM requires the shopperReference it is bound to;
            # it must be threaded from tokenization (order/session metadata).
            return _fail(amount_cents, currency, "adyen_requires_shopper_reference", "adyen_shopper_ref_required")

        base = self._base_url(allow_live=allow_live, provider_config=cfg)
        if base is None:
            return _fail(amount_cents, currency, "adyen_live_url_prefix_missing", "adyen_config")

        md = metadata or {}
        adyen_meta = {
            k: str(md[k])
            for k in ("order_id", "protocol_name", "pvt_click_id", "checkout_session_id")
            if md.get(k)
        }
        adyen_meta["pivota_acp_live_capture" if allow_live else "pivota_acp_test_capture"] = "true"

        body: Dict[str, Any] = {
            "merchantAccount": merchant_account,
            "amount": {"value": int(amount_cents), "currency": str(currency or "USD").upper()},
            "reference": str(md.get("order_id") or idempotency_key),
            "paymentMethod": {"type": "scheme", "storedPaymentMethodId": stored_pm},
            "shopperReference": shopper_ref,
            "shopperInteraction": "ContAuth",
            "recurringProcessingModel": "CardOnFile",
            "metadata": adyen_meta,
        }
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Idempotency-Key": str(idempotency_key),
        }

        try:
            import httpx  # local import: keep module import-safe

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{base}/payments", headers=headers, json=body)
        except Exception as exc:  # noqa: BLE001 — normalize any transport error
            logger.warning("acp_offsession: adyen capture transport error merchant_id=%s: %s", merchant_id, str(exc)[:200])
            return _fail(amount_cents, currency, str(exc)[:300], "adyen_network_error")

        if resp.status_code not in (200, 201):
            logger.warning("acp_offsession: adyen capture http %s merchant_id=%s: %s", resp.status_code, merchant_id, resp.text[:200])
            return _fail(amount_cents, currency, f"adyen_http_error:{resp.text[:200]}", f"adyen_http_{resp.status_code}")

        try:
            data = resp.json() or {}
        except Exception:  # noqa: BLE001
            return _fail(amount_cents, currency, "adyen_bad_response", "adyen_bad_response")

        result_code = str(data.get("resultCode") or "")
        psp_reference = data.get("pspReference")
        if result_code == "Authorised":
            logger.info("acp_offsession: adyen captured merchant_id=%s pspRef=%s amount=%s", merchant_id, psp_reference, amount_cents)
            return OffSessionCaptureResult(
                success=True, status="succeeded", payment_intent_id=psp_reference,
                amount_cents=amount_cents, currency=currency, error=None, error_code=None,
            )
        if result_code in {"RedirectShopper", "ChallengeShopper", "IdentifyShopper"}:
            # SCA — cannot complete off-session in-chat. MIT normally carries an
            # exemption, so this should be rare.
            logger.warning("acp_offsession: adyen requires_action merchant_id=%s resultCode=%s", merchant_id, result_code)
            return OffSessionCaptureResult(
                success=False, status="requires_action", payment_intent_id=psp_reference,
                amount_cents=amount_cents, currency=currency,
                error=f"adyen_sca:{result_code}", error_code="requires_action",
            )
        refusal = str(data.get("refusalReason") or result_code or "unknown")
        logger.warning("acp_offsession: adyen refused merchant_id=%s resultCode=%s refusal=%s", merchant_id, result_code, refusal)
        return OffSessionCaptureResult(
            success=False, status="failed", payment_intent_id=psp_reference,
            amount_cents=amount_cents, currency=currency,
            error=f"adyen_{result_code or 'error'}:{refusal}"[:300], error_code="adyen_refused",
        )


# Registry of PSP-specific capture adapters. Any provider absent here fails closed.
_CAPTURE_ADAPTERS: Dict[str, CaptureProvider] = {
    "stripe": _StripeCaptureAdapter(),
    "adyen": _AdyenCaptureAdapter(),
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
    provider_config = _as_config_dict((row or {}).get("provider_config"))
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

    result = await adapter.capture(
        merchant_id=str(merchant_id),
        api_key=api_key,
        amount_cents=amount_cents,
        currency=currency,
        idempotency_key=str(idempotency_key),
        payment_method=payment_method or "",
        metadata=dict(metadata or {}),
        allow_live=allow_live,
        provider_config=provider_config,
    )
    # Stamp the provider the charge actually ran on (the adapters themselves
    # stay provider-field-agnostic; behavior is otherwise unchanged).
    from dataclasses import replace as _dc_replace

    return _dc_replace(result, provider=provider)
