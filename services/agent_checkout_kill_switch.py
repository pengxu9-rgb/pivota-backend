"""P-T2.2 — Tier-2 in-chat protocol checkout kill-switch (fail-closed).

Turns the previously doc-only ``AGENT_CHECKOUT_STRICT`` / ``SUBMIT_PAYMENT``
switches into a real, fail-closed gate on the charge path — the hard prereq
before any Tier-2 (ACP/UCP/AP2) charge can execute (P-T2.3).

Scope is deliberately narrow: the gate applies ONLY to a *protocol-tier* charge
(one whose order carries ``protocol_name`` in {acp, ucp, ap2}). The live redirect
floor and the existing REST / hosted-checkout flows (``protocol_name="rest"``,
or unset) are NOT protocol-tier and pass through untouched — this switch must
never break a flow that is already live.

Posture (see config/settings):
- ``agent_checkout_strict`` — the protective guard, **ON by default** and treated
  as ON when the env is absent/blank (fail-closed). Only an explicit
  ``AGENT_CHECKOUT_STRICT=false`` (dev) disables it.
- ``agent_submit_payment_enabled`` — the ceiling, **OFF by default**. A
  protocol-tier charge is allowed only when strict is ON *and* submit_payment is
  explicitly enabled (the P-T2.3 canary flip), OR when strict is explicitly
  disabled (dev bypass).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config.settings import settings

logger = logging.getLogger("agent_checkout_kill_switch")

# The agentic-commerce protocol lanes this switch guards. REST/hosted/redirect
# (protocol_name="rest" or unset) are intentionally absent — they are the live
# floor and are never gated here. MCP commerce is simulated (not a real charge
# path) so it is also excluded.
GUARDED_PROTOCOLS = frozenset({"acp", "ucp", "ap2"})

# Machine-readable reasons (also the HTTPException error code on the charge path).
REASON_NOT_PROTOCOL = "not_protocol_tier"
REASON_ALLOWED_STRICT_ENABLED = "allowed_strict_submit_enabled"
REASON_ALLOWED_STRICT_DISABLED = "allowed_strict_disabled_dev_bypass"
REASON_BLOCKED_SUBMIT_DISABLED = "blocked_submit_payment_disabled"
REASON_BLOCKED_MERCHANT_NOT_ALLOWLISTED = "blocked_merchant_not_allowlisted"
REASON_ALLOWED_MERCHANT_ALLOWLISTED = "allowed_merchant_allowlisted"


def is_guarded_protocol(protocol: Optional[str]) -> bool:
    """True when `protocol` is a protocol-tier lane this switch gates."""
    return str(protocol or "").strip().lower() in GUARDED_PROTOCOLS


@dataclass(frozen=True)
class KillSwitchDecision:
    allowed: bool
    reason: str
    protocol: Optional[str]
    strict: bool
    submit_payment_enabled: bool
    # True only for a protocol-tier charge that this switch actually governs; a
    # non-protocol (REST/redirect/hosted) charge is `allowed=True, guarded=False`.
    guarded: bool
    # The merchant evaluated (if supplied) and whether a per-merchant canary
    # allowlist was in force. merchant_allowlisted is None when no allowlist is set.
    merchant_id: Optional[str] = None
    merchant_allowlist_active: bool = False
    merchant_allowlisted: Optional[bool] = None

    def as_error_detail(self) -> dict:
        return {
            "error": "TIER2_CHARGE_DISABLED",
            "reason": self.reason,
            "protocol": self.protocol,
            "agent_checkout_strict": self.strict,
            "submit_payment_enabled": self.submit_payment_enabled,
            "merchant_id": self.merchant_id,
            "merchant_allowlist_active": self.merchant_allowlist_active,
            "message": (
                "In-chat protocol checkout is not enabled for this charge. It is "
                "fail-closed until SUBMIT_PAYMENT is turned on"
                + (
                    " and this merchant is on SUBMIT_PAYMENT_MERCHANTS."
                    if self.merchant_allowlist_active
                    else " for a canary."
                )
            ),
        }


def evaluate_tier2_charge(
    protocol: Optional[str],
    *,
    merchant_id: Optional[str] = None,
    strict: Optional[bool] = None,
    submit_payment_enabled: Optional[bool] = None,
    submit_payment_merchants: Optional[frozenset] = None,
) -> KillSwitchDecision:
    """Decide whether a charge on the given protocol lane may proceed.

    `strict` / `submit_payment_enabled` / `submit_payment_merchants` default to
    the live settings; pass explicit values in tests. A non-protocol lane is
    always allowed (guarded=False) so this is safe to call unconditionally on the
    shared charge path.

    When a per-merchant canary allowlist (`submit_payment_merchants`) is
    non-empty, a protocol charge is allowed only for a merchant on the list —
    even with submit_payment ON — so the first live flip opens exactly one
    merchant. The allowlist only *narrows*: it is never consulted unless
    submit_payment is already ON.
    """
    proto = str(protocol or "").strip().lower() or None
    mid = str(merchant_id or "").strip() or None
    strict_on = settings.agent_checkout_strict if strict is None else bool(strict)
    submit_on = (
        settings.agent_submit_payment_enabled
        if submit_payment_enabled is None
        else bool(submit_payment_enabled)
    )
    allowlist = (
        settings.agent_submit_payment_merchants
        if submit_payment_merchants is None
        else frozenset(submit_payment_merchants)
    )
    allowlist_active = bool(allowlist)

    def _decision(allowed: bool, reason: str, *, merchant_allowlisted=None) -> KillSwitchDecision:
        return KillSwitchDecision(
            allowed=allowed,
            reason=reason,
            protocol=proto,
            strict=strict_on,
            submit_payment_enabled=submit_on,
            guarded=is_guarded_protocol(proto),
            merchant_id=mid,
            merchant_allowlist_active=allowlist_active,
            merchant_allowlisted=merchant_allowlisted,
        )

    if not is_guarded_protocol(proto):
        # Redirect floor / REST / hosted — not this switch's concern.
        return _decision(True, REASON_NOT_PROTOCOL)

    if not strict_on:
        # Strict explicitly disabled (dev bypass). Honest, logged.
        return _decision(True, REASON_ALLOWED_STRICT_DISABLED)

    # Strict ON (default): the ceiling is submit_payment.
    if not submit_on:
        return _decision(False, REASON_BLOCKED_SUBMIT_DISABLED)

    # submit_payment ON. If a canary allowlist is in force, the merchant must be
    # on it (safe-by-construction single-merchant canary).
    if allowlist_active:
        if mid and mid in allowlist:
            return _decision(True, REASON_ALLOWED_MERCHANT_ALLOWLISTED, merchant_allowlisted=True)
        return _decision(False, REASON_BLOCKED_MERCHANT_NOT_ALLOWLISTED, merchant_allowlisted=False)

    return _decision(True, REASON_ALLOWED_STRICT_ENABLED)


# --- P-T2.3.2: scoped test-mode capture (readiness bypass, amount-capped) -----


@dataclass(frozen=True)
class AcpTestCaptureDecision:
    # `engaged` = the ACP test-mode capture lane applies: the charge is a guarded
    # protocol, the kill-switch already permits it, and AGENT_ACP_TEST_CAPTURE is
    # ON. In that lane the caller bypasses live-PSP readiness so a test-mode PSP
    # can transact. `within_cap` gates the amount; an engaged-but-over-cap charge
    # must be REFUSED (not silently downgraded to live readiness).
    engaged: bool
    within_cap: bool
    max_cents: int
    amount_cents: Optional[int]
    reason: str

    @property
    def bypass_live_readiness(self) -> bool:
        return self.engaged and self.within_cap


def resolve_acp_test_capture(
    protocol: Optional[str],
    *,
    amount_cents: Optional[int],
    kill_switch_allowed: bool,
    enabled: Optional[bool] = None,
    max_cents: Optional[int] = None,
) -> AcpTestCaptureDecision:
    """Decide whether this charge runs in the ACP test-mode capture lane.

    Engages only when ALL hold: guarded protocol, kill-switch already permitted,
    and AGENT_ACP_TEST_CAPTURE on. Defaults read live settings; pass explicit
    values in tests. Never engages for a non-protocol charge or in production
    (flag off), so it cannot weaken any real flow.
    """
    en = settings.agent_acp_test_capture if enabled is None else bool(enabled)
    cap = settings.agent_acp_test_max_cents if max_cents is None else int(max_cents)
    if not (is_guarded_protocol(protocol) and en and bool(kill_switch_allowed)):
        return AcpTestCaptureDecision(
            engaged=False, within_cap=True, max_cents=cap,
            amount_cents=amount_cents, reason="not_engaged",
        )
    within = amount_cents is not None and int(amount_cents) <= cap
    return AcpTestCaptureDecision(
        engaged=True, within_cap=within, max_cents=cap, amount_cents=amount_cents,
        reason="engaged" if within else "over_cap",
    )


# --- P-T2.3.5: scoped LIVE-money capture (separate gate, low cap) --------------


@dataclass(frozen=True)
class AcpLiveCaptureDecision:
    # `engaged` = the ACP LIVE-money capture lane applies: guarded protocol, the
    # kill-switch already permits the charge, AGENT_ACP_ALLOW_LIVE_CAPTURE is ON,
    # AND the merchant is on the live allowlist. In that lane a live PSP key is
    # permitted (the test lane refuses it). `within_cap` gates the amount against
    # the (low) live cap; an engaged-but-over-cap charge MUST be refused.
    engaged: bool
    within_cap: bool
    max_cents: int
    amount_cents: Optional[int]
    reason: str

    @property
    def allow_live(self) -> bool:
        return self.engaged and self.within_cap


def resolve_acp_live_capture(
    protocol: Optional[str],
    *,
    merchant_id: Optional[str],
    amount_cents: Optional[int],
    kill_switch_allowed: bool,
    enabled: Optional[bool] = None,
    merchants: Optional[frozenset] = None,
    max_cents: Optional[int] = None,
) -> AcpLiveCaptureDecision:
    """Decide whether this charge runs in the ACP LIVE-money capture lane.

    Engages only when ALL hold: guarded protocol, kill-switch already permitted,
    AGENT_ACP_ALLOW_LIVE_CAPTURE on, AND the merchant is on the live allowlist. An
    EMPTY live allowlist means no merchant can go live — the master switch alone is
    inert, by construction — so this can never widen access the way a global flag
    might. Kept separate from the test lane so enabling the test canary never
    implies live money. Defaults read live settings; pass explicit values in tests.
    """
    en = settings.agent_acp_allow_live_capture if enabled is None else bool(enabled)
    allowlist = (
        settings.agent_acp_live_capture_merchants if merchants is None else frozenset(merchants)
    )
    cap = settings.agent_acp_live_max_cents if max_cents is None else int(max_cents)
    mid = str(merchant_id or "").strip() or None
    engaged = bool(
        is_guarded_protocol(protocol)
        and en
        and kill_switch_allowed
        and mid
        and mid in allowlist
    )
    if not engaged:
        return AcpLiveCaptureDecision(
            engaged=False, within_cap=True, max_cents=cap,
            amount_cents=amount_cents, reason="not_engaged",
        )
    within = amount_cents is not None and int(amount_cents) <= cap
    return AcpLiveCaptureDecision(
        engaged=True, within_cap=within, max_cents=cap, amount_cents=amount_cents,
        reason="engaged" if within else "over_cap",
    )
