"""ACP off-session payment dispatch — the ONE home of the Tier-2 gate chain.

Extracted from routes/agent_payment_sdk.create_payment so the gates
(kill-switch → test-capture cap → live-capture cap), the off-session capture
dispatch, and the synchronous paid-transition finalizer live exactly once and
are shared by BOTH charge doors:

- POST /agent/v1/payments (routes/agent_payment_sdk) — the Agent API test lane;
  behavior there is unchanged (the route calls these functions at the same
  points its inline code used to run).
- services/acp_checkout_session_service.complete_session — the in-process ACP
  checkout-session completion (the durable protocol execution layer; replaces
  the retired pivota-acp service's call BACK into /agent/v1/payments).

Money-safety posture is inherited, not re-invented: every refusal raises the
same HTTPException the route raised (403 + kill-switch as_error_detail / over-cap
shapes), and the actual charge still runs through
services.acp_offsession_capture.capture_offsession with all its invariants.

While extracting, one known bug is fixed: the route hardcoded
``psp_used = "stripe"`` for every off-session capture even when the capture ran
on a different provider (e.g. Adyen). `psp_used` is now derived from the capture
result's actual provider (falling back to "stripe" only when the provider could
not be resolved, preserving the historical value).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import HTTPException

from utils.logger import logger

if TYPE_CHECKING:
    # Gate symbols are imported IN-FUNCTION at runtime (below), exactly as the
    # route's original inline code did — that keeps the established monkeypatch
    # surface (tests patch services.agent_checkout_kill_switch attributes and
    # expect resolution at call time, not import time).
    from services.agent_checkout_kill_switch import (
        AcpLiveCaptureDecision,
        AcpTestCaptureDecision,
        KillSwitchDecision,
    )


@dataclass(frozen=True)
class AcpOffsessionGates:
    """The three Tier-2 gate decisions for one charge, evaluated together."""

    kill_switch: "KillSwitchDecision"
    test_capture: "AcpTestCaptureDecision"
    live_capture: "AcpLiveCaptureDecision"
    amount_cents: int

    @property
    def bypass_live_readiness(self) -> bool:
        return self.test_capture.bypass_live_readiness

    @property
    def allow_live(self) -> bool:
        return self.live_capture.allow_live

    @property
    def engaged(self) -> bool:
        """True when the charge runs in the off-session capture lane (test or live)."""
        return self.bypass_live_readiness or self.allow_live


def evaluate_acp_offsession_gates(
    *,
    protocol_name: Optional[str],
    merchant_id: str,
    amount_cents: int,
    order_id: str,
) -> AcpOffsessionGates:
    """Evaluate the Tier-2 gate chain for a charge. Fail-closed: raises
    HTTPException(403) on a kill-switch block or an engaged-but-over-cap charge —
    exactly the refusals create_payment used to raise inline. Safe to call
    unconditionally on the shared charge path (a non-protocol charge evaluates
    allowed=True, guarded=False and never engages a capture lane)."""
    from services.agent_checkout_kill_switch import (
        evaluate_tier2_charge,
        resolve_acp_live_capture,
        resolve_acp_test_capture,
    )

    # P-T2.2 kill-switch: fail-closed gate on the Tier-2 (in-chat protocol)
    # charge lane. Scoped to protocol-tier orders (protocol_name in
    # {acp,ucp,ap2}); the live redirect floor + REST/hosted flows
    # (protocol_name="rest"/unset) evaluate as guarded=False and pass through
    # untouched. Strict is ON by default and cannot be silently absent, so a
    # protocol charge is blocked until SUBMIT_PAYMENT is explicitly enabled
    # for a canary (P-T2.3). Evaluated before any PSP call so nothing charges.
    kill_switch = evaluate_tier2_charge(protocol_name, merchant_id=str(merchant_id))
    if not kill_switch.allowed:
        logger.warning(
            "[AgentPayments] Tier-2 protocol charge blocked by kill-switch "
            "order=%s protocol=%s reason=%s (strict=%s submit_payment=%s)",
            order_id,
            kill_switch.protocol,
            kill_switch.reason,
            kill_switch.strict,
            kill_switch.submit_payment_enabled,
        )
        raise HTTPException(status_code=403, detail=kill_switch.as_error_detail())
    if kill_switch.guarded:
        logger.info(
            "[AgentPayments] Tier-2 protocol charge permitted order=%s "
            "protocol=%s reason=%s",
            order_id,
            kill_switch.protocol,
            kill_switch.reason,
        )

    # P-T2.3.2: scoped test-mode capture. When AGENT_ACP_TEST_CAPTURE is on,
    # a kill-switch-permitted ACP charge may run against a TEST-MODE PSP by
    # bypassing live-readiness — but only up to a hard amount cap. Default off
    # → engaged=False → live readiness enforced as before. An engaged charge
    # over the cap is refused outright (never downgraded).
    acp_test_capture = resolve_acp_test_capture(
        protocol_name,
        amount_cents=amount_cents,
        kill_switch_allowed=kill_switch.allowed,
    )
    if acp_test_capture.engaged and not acp_test_capture.within_cap:
        logger.warning(
            "[AgentPayments] ACP test-mode capture REFUSED (over cap) order=%s "
            "amount_cents=%s cap=%s",
            order_id, amount_cents, acp_test_capture.max_cents,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "TIER2_TEST_CAPTURE_OVER_CAP",
                "amount_cents": amount_cents,
                "max_cents": acp_test_capture.max_cents,
                "message": "ACP test-mode capture exceeds the configured amount cap.",
            },
        )
    if acp_test_capture.bypass_live_readiness:
        logger.warning(
            "[AgentPayments] ACP TEST-MODE capture lane engaged order=%s "
            "amount_cents=%s cap=%s — bypassing live PSP readiness for the "
            "test PSP (test canary only)",
            order_id, amount_cents, acp_test_capture.max_cents,
        )

    # P-T2.3.5: scoped LIVE-money capture. A SEPARATE, stricter gate than the
    # test lane — its own master switch + required per-merchant allowlist + a
    # low cap. Engages only when the kill-switch already permits, the flag is
    # on, and the merchant is allowlisted. Over-cap engaged charges are refused
    # (never downgraded). Default off → engaged=False → nothing changes.
    acp_live_capture = resolve_acp_live_capture(
        protocol_name,
        merchant_id=str(merchant_id),
        amount_cents=amount_cents,
        kill_switch_allowed=kill_switch.allowed,
    )
    if acp_live_capture.engaged and not acp_live_capture.within_cap:
        logger.warning(
            "[AgentPayments] ACP LIVE-money capture REFUSED (over cap) order=%s "
            "amount_cents=%s cap=%s",
            order_id, amount_cents, acp_live_capture.max_cents,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "TIER2_LIVE_CAPTURE_OVER_CAP",
                "amount_cents": amount_cents,
                "max_cents": acp_live_capture.max_cents,
                "message": "ACP live-money capture exceeds the configured live amount cap.",
            },
        )
    if acp_live_capture.allow_live:
        logger.warning(
            "[AgentPayments] ACP LIVE-MONEY capture lane engaged order=%s "
            "amount_cents=%s cap=%s merchant=%s — REAL money on the merchant's "
            "live PSP (P-T2.3.5 canary)",
            order_id, amount_cents, acp_live_capture.max_cents, merchant_id,
        )

    return AcpOffsessionGates(
        kill_switch=kill_switch,
        test_capture=acp_test_capture,
        live_capture=acp_live_capture,
        amount_cents=int(amount_cents),
    )


@dataclass(frozen=True)
class AcpOffsessionPaymentOutcome:
    success: bool
    status: Optional[str]
    payment_intent_id: str
    psp_used: str
    error: Optional[str]
    error_code: Optional[str]
    # SimpleNamespace(id, status, client_secret) — the shape the shared
    # /agent/v1/payments downstream code (record insert, finalize, response
    # building) consumes in place of a PSP adapter's PaymentIntent.
    payment_intent: SimpleNamespace


async def execute_acp_offsession_payment(
    *,
    gates: AcpOffsessionGates,
    merchant_id: str,
    order_id: str,
    currency: str,
    idempotency_key: Optional[str],
    payment_method_token: Optional[str],
    agent_id: Optional[str],
    order_metadata: Dict[str, Any],
) -> AcpOffsessionPaymentOutcome:
    """P-T2.3.3: real server-side off-session capture on the MERCHANT's PSP key
    (the shared path only client-confirms, which can't complete an in-chat
    charge). Isolated, amount-capped, idempotent. `gates.allow_live` switches
    capture_offsession into the live lane (live key permitted, real payment
    method required, live cap). Never raises for a capture failure — returns the
    normalized outcome so each caller maps it (the /payments route to its 500,
    complete_session to a named fail-closed error)."""
    from services.acp_offsession_capture import capture_offsession

    _acp_allow_live = gates.allow_live
    _oc = await capture_offsession(
        merchant_id=str(merchant_id),
        amount_cents=gates.amount_cents,
        currency=str(currency),
        idempotency_key=(idempotency_key or f"acp_off:{order_id}"),
        payment_method=(payment_method_token or None),
        metadata={
            "order_id": str(order_id),
            "agent_id": str(agent_id or ""),
            **{
                k: str(order_metadata[k])
                for k in ("protocol_name", "pvt_click_id", "checkout_session_id")
                if order_metadata.get(k)
            },
        },
        allow_live=_acp_allow_live,
        max_cents=(gates.live_capture.max_cents if _acp_allow_live else None),
    )
    # Bug fix (was: psp_used = "stripe" hardcoded): record the provider the
    # capture actually ran on. Falls back to "stripe" when the provider could
    # not be resolved (e.g. the merchant-PSP lookup failed before dispatch),
    # preserving the historical value for that path.
    psp_used = str(_oc.provider or "stripe")
    payment_intent = SimpleNamespace(
        id=_oc.payment_intent_id or f"acp_off_{order_id}",
        status=_oc.status or ("succeeded" if _oc.success else "failed"),
        client_secret=None,
    )
    return AcpOffsessionPaymentOutcome(
        success=_oc.success,
        status=_oc.status,
        payment_intent_id=payment_intent.id,
        psp_used=psp_used,
        error=_oc.error,
        error_code=_oc.error_code,
        payment_intent=payment_intent,
    )


async def _finalize_paid_transition(
    *,
    order: Dict[str, Any],
    psp_used: str,
    payment_reference: str,
    currency: str,
    allow_live: bool,
    protocol_name: Optional[str],
) -> None:
    """The raw paid-transition (raises on failure). Callers choose the posture:
    the /payments route swallows via finalize_acp_offsession_success; the
    session settle path retries and flags reconciliation."""
    from services.psp_payment_finalizer import finalize_payment_success
    from db.orders import mark_order_paid, update_payment_info
    from routes.order_routes import log_order_event

    await finalize_payment_success(
        order,
        psp=psp_used,
        payment_reference=payment_reference,
        transaction_id=payment_reference,
        amount_minor=None,
        currency=str(currency),
        source_event="acp_offsession_capture",
        metadata_extra={
            ("acp_live_capture" if allow_live else "acp_test_capture"): True,
            "protocol_name": protocol_name,
        },
        update_payment_info_fn=update_payment_info,
        mark_order_paid_fn=mark_order_paid,
        log_order_event_fn=log_order_event,
    )


async def finalize_acp_offsession_success(
    *,
    order: Dict[str, Any],
    order_id: str,
    psp_used: str,
    payment_reference: str,
    currency: str,
    allow_live: bool,
    protocol_name: Optional[str],
) -> None:
    """P-T2.3.3b: the off-session capture succeeded SYNCHRONOUSLY, so no PSP
    webhook will arrive to finalize the order. Transition it to PAID via the
    same shared finalizer the webhook path uses (services.psp_payment_finalizer)
    — it flips the order to paid atomically, stamps gross attributed GMV on the
    attribution edge, and logs the conversion event, all idempotently
    (terminal-state guarded). Best-effort: the capture already succeeded, so a
    finalize failure is logged, never raised (the /payments route's posture)."""
    try:
        await _finalize_paid_transition(
            order=order,
            psp_used=psp_used,
            payment_reference=payment_reference,
            currency=currency,
            allow_live=allow_live,
            protocol_name=protocol_name,
        )
    except Exception as finalize_exc:  # noqa: BLE001 — capture already succeeded; don't fail the response
        logger.warning(
            "[AgentPayments] ACP off-session paid-transition failed order=%s: %s",
            order_id, finalize_exc,
        )


async def settle_acp_offsession_success(
    *,
    gates: AcpOffsessionGates,
    outcome: AcpOffsessionPaymentOutcome,
    order: Dict[str, Any],
    order_id: str,
    merchant_id: str,
    currency: str,
    idempotency_key: Optional[str],
    agent_id: Optional[str],
    order_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Post-capture settlement for the in-process session path: the same durable
    writes /agent/v1/payments performs after a successful off-session capture —
    payments record, order payment info, attribution edge, paid-transition
    finalizer — each wrapped in the route's `_with_asyncpg_busy_retry` (matching
    routes/agent_payment_sdk's posture for these exact writes). The /payments
    route keeps its own inline record/attribution code (shared with the
    client-confirm lane) and calls only finalize_acp_offsession_success; this
    composition exists for callers outside that route.

    Returns {"reconciliation_needed": bool}: the capture already happened, so a
    write that still fails after retries never fails the response — instead the
    caller durably stamps the flag on the stored completion (queryable marker
    for ops reconciliation) and the failure is logged at ERROR."""
    from datetime import datetime

    from db.database import database
    from db.orders import update_payment_info
    from routes.agent_payment_sdk import _with_asyncpg_busy_retry

    reconciliation_needed = False

    amount_major = gates.amount_cents / 100.0
    payment_id = f"pay_{outcome.payment_intent_id}"
    status = "succeeded" if outcome.payment_intent.status == "succeeded" else str(outcome.payment_intent.status)
    try:
        await _with_asyncpg_busy_retry(
            "payment record insert",
            lambda: database.execute(
                """INSERT INTO payments
                   (payment_id, order_id, payment_intent_id, amount, currency,
                    psp_type, status, idempotency_key, created_at, agent_id)
                   VALUES (:payment_id, :order_id, :intent_id, :amount, :currency,
                           :psp, :status, :idem_key, :created_at, :agent_id)
                   ON CONFLICT (payment_id) DO UPDATE
                   SET status = EXCLUDED.status,
                       updated_at = EXCLUDED.created_at""",
                {
                    "payment_id": payment_id,
                    "order_id": order_id,
                    "intent_id": outcome.payment_intent_id,
                    "amount": float(amount_major),
                    "currency": currency,
                    "psp": outcome.psp_used,
                    "status": status,
                    "idem_key": idempotency_key,
                    "created_at": datetime.now(),
                    "agent_id": agent_id,
                },
            ),
        )
    except Exception as record_exc:  # noqa: BLE001 — capture succeeded; flag, never fail the response
        reconciliation_needed = True
        logger.error(
            "[AcpOffsession] payments record insert failed after retries order=%s: %s",
            order_id, str(record_exc)[:200],
        )

    try:
        await _with_asyncpg_busy_retry(
            "order payment info update",
            lambda: update_payment_info(
                order_id=order_id,
                payment_intent_id=outcome.payment_intent_id,
                client_secret="",
                payment_status="processing",
                psp_used=outcome.psp_used,
            ),
        )
    except Exception as info_exc:  # noqa: BLE001
        reconciliation_needed = True
        logger.error(
            "[AcpOffsession] order payment info update failed after retries order=%s: %s",
            order_id, str(info_exc)[:200],
        )

    # P-T2.0 attribution parity: deposit a commerce_attribution_edge for this
    # protocol charge (idempotent on order_id; self-gates on
    # has_attribution_signal). Best-effort: never break the payment (and not a
    # reconciliation marker — the edge upsert is idempotently re-derivable).
    try:
        from services.commerce_attribution_service import upsert_order_attribution_edge

        await upsert_order_attribution_edge(
            order_id=str(order_id),
            merchant_id=str(merchant_id),
            metadata=order_metadata,
        )
    except Exception as attribution_exc:  # noqa: BLE001 — attribution is best-effort
        logger.warning(
            "[AcpOffsession] Failed to persist commerce attribution edge for "
            "order %s: %s",
            order_id,
            attribution_exc,
        )

    if outcome.payment_intent.status == "succeeded":
        try:
            await _with_asyncpg_busy_retry(
                "acp offsession paid transition",
                lambda: _finalize_paid_transition(
                    order=order,
                    psp_used=outcome.psp_used,
                    payment_reference=outcome.payment_intent_id,
                    currency=currency,
                    allow_live=gates.allow_live,
                    protocol_name=order_metadata.get("protocol_name"),
                ),
            )
        except Exception as finalize_exc:  # noqa: BLE001 — capture happened; don't fail the response
            reconciliation_needed = True
            logger.error(
                "[AcpOffsession] paid-transition finalize failed after retries order=%s: %s",
                order_id, str(finalize_exc)[:200],
            )

    return {"reconciliation_needed": reconciliation_needed}
