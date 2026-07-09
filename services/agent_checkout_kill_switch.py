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

    def as_error_detail(self) -> dict:
        return {
            "error": "TIER2_CHARGE_DISABLED",
            "reason": self.reason,
            "protocol": self.protocol,
            "agent_checkout_strict": self.strict,
            "submit_payment_enabled": self.submit_payment_enabled,
            "message": (
                "In-chat protocol checkout is not enabled. This charge lane is "
                "fail-closed until SUBMIT_PAYMENT is turned on for a canary."
            ),
        }


def evaluate_tier2_charge(
    protocol: Optional[str],
    *,
    strict: Optional[bool] = None,
    submit_payment_enabled: Optional[bool] = None,
) -> KillSwitchDecision:
    """Decide whether a charge on the given protocol lane may proceed.

    `strict` / `submit_payment_enabled` default to the live settings; pass
    explicit values in tests. A non-protocol lane is always allowed (guarded=
    False) so this is safe to call unconditionally on the shared charge path.
    """
    proto = str(protocol or "").strip().lower() or None
    strict_on = settings.agent_checkout_strict if strict is None else bool(strict)
    submit_on = (
        settings.agent_submit_payment_enabled
        if submit_payment_enabled is None
        else bool(submit_payment_enabled)
    )

    if not is_guarded_protocol(proto):
        # Redirect floor / REST / hosted — not this switch's concern.
        return KillSwitchDecision(
            allowed=True,
            reason=REASON_NOT_PROTOCOL,
            protocol=proto,
            strict=strict_on,
            submit_payment_enabled=submit_on,
            guarded=False,
        )

    if not strict_on:
        # Strict explicitly disabled (dev bypass). Honest, logged.
        return KillSwitchDecision(
            allowed=True,
            reason=REASON_ALLOWED_STRICT_DISABLED,
            protocol=proto,
            strict=strict_on,
            submit_payment_enabled=submit_on,
            guarded=True,
        )

    # Strict ON (default): the ceiling is submit_payment.
    if submit_on:
        return KillSwitchDecision(
            allowed=True,
            reason=REASON_ALLOWED_STRICT_ENABLED,
            protocol=proto,
            strict=strict_on,
            submit_payment_enabled=submit_on,
            guarded=True,
        )

    return KillSwitchDecision(
        allowed=False,
        reason=REASON_BLOCKED_SUBMIT_DISABLED,
        protocol=proto,
        strict=strict_on,
        submit_payment_enabled=submit_on,
        guarded=True,
    )
