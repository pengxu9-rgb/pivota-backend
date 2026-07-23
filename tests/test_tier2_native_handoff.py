"""Tier-2 three-tier routing: in-chat charge > NATIVE HANDOFF > redirect floor.

Mid-man alignment (2026-07-23): when Pivota cannot (or may not) execute the
charge, a merchant whose OWN checkout is an available settlement rail
(platform_native / delegated_token, PR #1576) routes to `native_handoff` instead
of the cold redirect. Flag-dark: TIER2_NATIVE_HANDOFF_ENABLED default OFF must be
byte-identical to the two-tier behavior; the charge lane always outranks the
handoff. The existing test_tier2_acp_lane suite runs flag-off and must stay green
untouched.
"""

from __future__ import annotations

import pytest

from config.settings import settings
from services import tier2_acp_lane as lane
from services.tier2_acp_lane import (
    LANE_ACP_IN_CHAT,
    LANE_NATIVE_HANDOFF,
    LANE_REDIRECT_FLOOR,
    REASON_NATIVE_RAIL_AVAILABLE,
    resolve_acp_lane_decision,
)

_NATIVE_RAIL = {
    "rail": "platform_native",
    "available": True,
    "requirement": "shopify_payments_verified_on_merchant_store",
    "protocol_scope": [],
    "source": "pcs_shopify_verify",
    "as_of": "2026-07-01T00:00:00Z",
}
_PSP_RAIL = {"rail": "pivota_psp", "available": True, "requirement": "live_charge_ready_psp", "protocol_scope": ["acp"]}
_DARK_RAIL = {"rail": "delegated_token", "available": False, "requirement": "x", "protocol_scope": ["ucp"]}


def _cap(protocols, rails, *, platform="shopify", psp=None):
    return {
        "merchant_id": "merch_x",
        "platform": platform,
        "platform_source": "connected",
        "psp": psp,
        "has_live_psp": bool(psp),
        "protocols": list(protocols),
        "has_native_payments": True,
        "settlement_rails": list(rails),
    }


def _patch(monkeypatch, capability, *, handoff=True, strict=True, submit=False, merchants=frozenset()):
    async def fake_capability(mid, *, store_id=None, platform_override=None):
        return capability

    monkeypatch.setattr(lane, "resolve_merchant_capability", fake_capability)
    monkeypatch.setattr(settings, "tier2_native_handoff_enabled", handoff, raising=False)
    monkeypatch.setattr(settings, "agent_checkout_strict", strict, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_enabled", submit, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_merchants", frozenset(merchants), raising=False)
    monkeypatch.setattr(settings, "agent_acp_test_capture", False, raising=False)


@pytest.mark.asyncio
async def test_flag_off_is_byte_identical_redirect(monkeypatch):
    # Available native rail but flag OFF → exactly the pre-existing redirect.
    _patch(monkeypatch, _cap([], [_NATIVE_RAIL]), handoff=False)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR
    assert d.settlement_rail is None


@pytest.mark.asyncio
async def test_not_charge_capable_with_native_rail_routes_to_handoff(monkeypatch):
    # THE mid-man case: no PSP handover, merchant settles on their own checkout.
    _patch(monkeypatch, _cap([], [_NATIVE_RAIL]))
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_NATIVE_HANDOFF
    assert d.is_native_handoff and not d.is_acp
    assert d.reason == f"{REASON_NATIVE_RAIL_AVAILABLE}:platform_native"
    assert d.settlement_rail == _NATIVE_RAIL
    assert d.as_dict()["settlement_rail"] == _NATIVE_RAIL


@pytest.mark.asyncio
async def test_charge_lane_outranks_native_handoff(monkeypatch):
    # ACP-capable AND charge-permitted → in-chat charge wins even with a rail.
    _patch(monkeypatch, _cap(["acp"], [_PSP_RAIL, _NATIVE_RAIL], psp="stripe"),
           submit=True, merchants={"merch_x"})
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_ACP_IN_CHAT


@pytest.mark.asyncio
async def test_kill_switch_blocked_falls_to_handoff_not_redirect(monkeypatch):
    # ACP-capable but the fail-closed kill-switch refuses the charge — the
    # allowlist is ACTIVE and this merchant is not on it (an empty allowlist
    # would mean not-active → allowed). With a native rail, the buyer still gets
    # the merchant's own checkout instead of the cold redirect.
    _patch(monkeypatch, _cap(["acp"], [_PSP_RAIL, _NATIVE_RAIL], psp="stripe"),
           submit=True, merchants={"some_other_merchant"})
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_NATIVE_HANDOFF
    assert d.kill_switch is not None and d.kill_switch["allowed"] is False


@pytest.mark.asyncio
async def test_pivota_psp_rail_never_backs_a_handoff(monkeypatch):
    # pivota_psp IS the charge lane; it must never masquerade as a native rail.
    _patch(monkeypatch, _cap([], [_PSP_RAIL]))
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR


@pytest.mark.asyncio
async def test_dark_rails_do_not_light_the_handoff(monkeypatch):
    # available=False (and any non-True) rails never route; unknown is never yes.
    _patch(monkeypatch, _cap([], [_DARK_RAIL, {**_NATIVE_RAIL, "available": 1}]))
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR


@pytest.mark.asyncio
async def test_capability_error_still_falls_to_redirect(monkeypatch):
    # Resolver failure → redirect floor, exactly as before (no rail to consult).
    async def boom(mid, *, store_id=None, platform_override=None):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(lane, "resolve_merchant_capability", boom)
    monkeypatch.setattr(settings, "tier2_native_handoff_enabled", True, raising=False)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR


@pytest.mark.asyncio
async def test_merchant_allowlist_narrows_the_handoff(monkeypatch):
    # Staged rollout: with a non-empty allowlist, only listed merchants get the
    # handoff; everyone else keeps the exact redirect behavior.
    _patch(monkeypatch, _cap([], [_NATIVE_RAIL]))
    monkeypatch.setattr(settings, "tier2_native_handoff_merchants_raw", "merch_other", raising=False)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR

    monkeypatch.setattr(settings, "tier2_native_handoff_merchants_raw", "merch_x,merch_other", raising=False)
    d2 = await resolve_acp_lane_decision("merch_x")
    assert d2.lane == LANE_NATIVE_HANDOFF


# --- route layer: the /acp response contract for the handoff branch -----------


class _Ctx:
    def can_access_merchant(self, merchant_id):
        return True


def _acp_req():
    from routes.agent_checkout_intents import CreateAcpCheckoutRequest, CheckoutIntentItem

    return CreateAcpCheckoutRequest(
        items=[CheckoutIntentItem(product_id="p1", merchant_id="merch_x", sku="SKU1", quantity=1)],
    )


@pytest.mark.asyncio
async def test_route_handoff_response_shape_sanitized_with_fallback(monkeypatch):
    from routes import agent_checkout_intents as m
    from services.tier2_acp_lane import AcpLaneDecision, LANE_NATIVE_HANDOFF

    dirty_rail = {
        **_NATIVE_RAIL,
        "source": "pcs_shopify_verify",   # internal provenance — must not leak
        "internal_debug": "secret",        # future internal field — must not leak
    }

    async def fake_decision(mid, *, store_id=None, platform_override=None):
        return AcpLaneDecision(
            lane=LANE_NATIVE_HANDOFF,
            reason="native_settlement_rail_available:platform_native",
            merchant_id="merch_x", protocol=None, platform="shopify", psp=None,
            settlement_rail=dirty_rail,
        )

    monkeypatch.setattr(m, "resolve_acp_lane_decision", fake_decision)
    out = await m.create_acp_checkout(_acp_req(), _Ctx())

    assert out["status"] == "requires_native_checkout_handoff"
    assert out["lane"] == "native_handoff"
    assert out["checkout_source"] == "native_handoff"
    # Whitelisted projection only — internal fields never auto-leak.
    assert set(out["settlement_rail"].keys()) == {
        "rail", "available", "requirement", "protocol_scope", "as_of"
    }
    assert "internal_debug" not in out["settlement_rail"]
    assert "source" not in out["settlement_rail"]
    # "Never a dead end": the actionable redirect floor rides along explicitly.
    assert out["fallback"]["status"] == "requires_redirect_floor"
    assert out["fallback"]["lane"] == "redirect_floor"
    assert "external-platform" in out["fallback"]["message"]
