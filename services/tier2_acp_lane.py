"""P-T2.3.1 — Tier-2 in-chat ACP lane routing decision (the capability gate).

`resolve_acp_lane_decision(merchant_id)` is the single brain the decision layer
consults to answer: *for this merchant, do we offer an in-chat ACP checkout, or
fall back to the redirect floor?* It composes the two things already shipped:

- **capability** (P-T2.1 `resolve_merchant_capability`) — does this merchant's
  platform/PSP actually support ACP? (`protocols` contains "acp")
- **kill-switch** (P-T2.2/P-T2.3a `evaluate_tier2_charge`) — is a real ACP
  charge permitted for this merchant right now? (strict ON + SUBMIT_PAYMENT on +
  merchant on the canary allowlist)

Two invariants:
- **Fail-open to redirect.** Anything short of "capable AND charge-permitted"
  routes to the redirect floor — never a dead end (plan risk #3). So while the
  lane is dark (SUBMIT_PAYMENT off, the default), *every* merchant routes to
  redirect and the ACP lane is completely inert.
- **Fail-closed on charge.** The ACP lane is only chosen when the fail-closed
  kill-switch actively permits a charge for this specific merchant.

This module makes NO network call and initiates NO checkout — it is a pure
decision. Session creation + the agent-facing entry point are P-T2.3.1b; real
capture is P-T2.3.2+.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import settings
from services.agent_checkout_kill_switch import evaluate_tier2_charge
from services.merchant_capability_resolver import resolve_merchant_capability
from services.platform_capabilities import PROTOCOL_ACP

# Platforms whose merchants can be driven by the ACP test-capture canary even
# without a live PSP. Both now have a pivota-acp real-capture connector wired to
# the shared backend quote→order(acp)→pay flow. This gates only the CANARY (flag +
# allowlist); production ACP-capability is still governed per-platform by the
# capability resolver (e.g. Wix order-writeback), so widening this set does NOT by
# itself claim production readiness — it lets a Wix merchant be canary-tested
# before that claim is made.
_ACP_TEST_CAPABLE_PLATFORMS = frozenset({"shopify", "wix"})


def _is_acp_test_canary(merchant_id: str, platform: Optional[str]) -> bool:
    """P-T2.3.4: the ACP test-capture canary (AGENT_ACP_TEST_CAPTURE on + this
    merchant on SUBMIT_PAYMENT_MERCHANTS) is chargeable via the off-session TEST
    lane even without a live-charge-ready PSP, so route it to ACP. Scoped +
    default-off: production ACP still requires a live PSP (protocols has acp)."""
    return (
        settings.agent_acp_test_capture
        and bool(merchant_id)
        and merchant_id in settings.agent_submit_payment_merchants
        and str(platform or "").strip().lower() in _ACP_TEST_CAPABLE_PLATFORMS
    )

logger = logging.getLogger("tier2_acp_lane")

LANE_ACP_IN_CHAT = "acp_in_chat"
LANE_REDIRECT_FLOOR = "redirect_floor"

REASON_ACP_ENABLED = "acp_enabled"
REASON_NOT_ACP_CAPABLE = "acp_not_capable"
REASON_KILL_SWITCH_BLOCKED = "kill_switch_blocked"


@dataclass(frozen=True)
class AcpLaneDecision:
    lane: str  # LANE_ACP_IN_CHAT | LANE_REDIRECT_FLOOR
    reason: str
    merchant_id: str
    protocol: Optional[str]  # "acp" only when lane == LANE_ACP_IN_CHAT
    platform: Optional[str]
    psp: Optional[str]
    capability: Dict[str, Any] = field(default_factory=dict)
    kill_switch: Optional[Dict[str, Any]] = None

    @property
    def is_acp(self) -> bool:
        return self.lane == LANE_ACP_IN_CHAT

    def as_dict(self) -> Dict[str, Any]:
        return {
            "lane": self.lane,
            "reason": self.reason,
            "merchant_id": self.merchant_id,
            "protocol": self.protocol,
            "platform": self.platform,
            "psp": self.psp,
            "capability": self.capability,
            "kill_switch": self.kill_switch,
        }


async def resolve_acp_lane_decision(merchant_id: str) -> AcpLaneDecision:
    """Decide the checkout lane for `merchant_id`. Always returns a decision;
    never raises (any resolution error falls open to the redirect floor)."""
    mid = str(merchant_id or "").strip()

    def _redirect(reason: str, *, capability=None, kill_switch=None) -> AcpLaneDecision:
        return AcpLaneDecision(
            lane=LANE_REDIRECT_FLOOR,
            reason=reason,
            merchant_id=mid,
            protocol=None,
            platform=(capability or {}).get("platform"),
            psp=(capability or {}).get("psp"),
            capability=capability or {},
            kill_switch=kill_switch,
        )

    try:
        capability = await resolve_merchant_capability(mid)
    except Exception as exc:  # noqa: BLE001 — routing must never break; fall to floor
        logger.warning(
            "tier2_acp_lane: capability resolve failed merchant_id=%s: %s — "
            "routing to redirect floor",
            mid, str(exc)[:200],
        )
        return _redirect(REASON_NOT_ACP_CAPABLE)

    protocols: List[str] = list(capability.get("protocols") or [])
    # ACP-capable if the resolver says so (live PSP), OR this is the armed
    # test-capture canary (chargeable via the off-session test lane).
    acp_capable = PROTOCOL_ACP in protocols or _is_acp_test_canary(
        mid, capability.get("platform")
    )
    if not acp_capable:
        # Not ACP-capable (wrong platform, no live PSP, etc.) → redirect floor.
        return _redirect(REASON_NOT_ACP_CAPABLE, capability=capability)

    # ACP-capable: is a real charge permitted for THIS merchant right now?
    ks = evaluate_tier2_charge(PROTOCOL_ACP, merchant_id=mid)
    ks_dict = {
        "allowed": ks.allowed,
        "reason": ks.reason,
        "strict": ks.strict,
        "submit_payment_enabled": ks.submit_payment_enabled,
        "merchant_allowlist_active": ks.merchant_allowlist_active,
        "merchant_allowlisted": ks.merchant_allowlisted,
    }
    if not ks.allowed:
        # Fail-closed on charge → fall back to the redirect floor (never a dead
        # end). This is the default state until SUBMIT_PAYMENT + the allowlist
        # enable one merchant.
        return _redirect(
            f"{REASON_KILL_SWITCH_BLOCKED}:{ks.reason}",
            capability=capability,
            kill_switch=ks_dict,
        )

    return AcpLaneDecision(
        lane=LANE_ACP_IN_CHAT,
        reason=REASON_ACP_ENABLED,
        merchant_id=mid,
        protocol=PROTOCOL_ACP,
        platform=capability.get("platform"),
        psp=capability.get("psp"),
        capability=capability,
        kill_switch=ks_dict,
    )
