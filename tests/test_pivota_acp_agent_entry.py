"""P-T2.3.1b — agent-facing /acp entry point.

Covers: the agent endpoint routes to the ACP lane only when the lane decision
says capable + charge-permitted, and otherwise (or on any session-service error)
fails open to the redirect floor. DARK: no order/charge is ever created here.

ADR-021: the outbound pivota-acp client this file also used to cover was
retired with that service; session creation is in-process
(services/acp_checkout_session_service), covered by
tests/test_acp_checkout_session_service.py.
"""

from __future__ import annotations

import pytest


# --- endpoint ---------------------------------------------------------------

class _Ctx:
    def can_access_merchant(self, merchant_id):
        return True


def _req():
    from routes.agent_checkout_intents import CreateAcpCheckoutRequest, CheckoutIntentItem

    return CreateAcpCheckoutRequest(
        items=[CheckoutIntentItem(product_id="p1", merchant_id="merch_x", sku="SKU1", quantity=1)],
        pvt_click_id="clk_abc",
        pvt_surface="chatgpt",
    )


def _decision(is_acp: bool):
    from services.tier2_acp_lane import AcpLaneDecision, LANE_ACP_IN_CHAT, LANE_REDIRECT_FLOOR

    if is_acp:
        return AcpLaneDecision(lane=LANE_ACP_IN_CHAT, reason="acp_enabled", merchant_id="merch_x",
                               protocol="acp", platform="shopify", psp="stripe")
    return AcpLaneDecision(lane=LANE_REDIRECT_FLOOR, reason="kill_switch_blocked:blocked_submit_payment_disabled",
                           merchant_id="merch_x", protocol=None, platform="shopify", psp=None)


@pytest.mark.asyncio
async def test_endpoint_routes_to_redirect_when_not_acp(monkeypatch):
    from routes import agent_checkout_intents as m

    async def fake_decision(mid, *, store_id=None, platform_override=None):
        return _decision(is_acp=False)

    monkeypatch.setattr(m, "resolve_acp_lane_decision", fake_decision)
    out = await m.create_acp_checkout(_req(), _Ctx())
    assert out["lane"] == "redirect_floor"
    assert out["status"] == "requires_redirect_floor"
    assert out["reason"].startswith("kill_switch_blocked:")


@pytest.mark.asyncio
async def test_endpoint_creates_acp_session_when_enabled(monkeypatch):
    # Positive control: a healthy in-process session service → the ACP lane.
    from routes import agent_checkout_intents as m
    from services.acp_checkout_session_service import AcpSessionResult

    async def fake_decision(mid, *, store_id=None, platform_override=None):
        return _decision(is_acp=True)

    async def fake_session(**kwargs):
        assert kwargs["merchant_id"] == "merch_x"
        assert kwargs["metadata"]["pvt_click_id"] == "clk_abc"
        return AcpSessionResult(session_id="csn_abc",
                                checkout_url="https://agents.pivota.cc/checkout/acp/csn_abc",
                                status="ready_for_payment", currency="USD", total_cents=4599,
                                totals=[{"type": "total", "amount": 4599}],
                                raw={"id": "csn_abc",
                                     "payment_provider": {"provider": "adyen",
                                                          "supported_payment_methods": ["card"]}})

    monkeypatch.setattr(m, "resolve_acp_lane_decision", fake_decision)
    monkeypatch.setattr(m, "create_session", fake_session)

    out = await m.create_acp_checkout(_req(), _Ctx())
    assert out["status"] == "requires_in_chat_acp_checkout"
    assert out["lane"] == "acp_in_chat"
    assert out["acp_session_id"] == "csn_abc"
    assert out["pvt_click_id"] == "clk_abc"
    # The response surfaces the merchant's ACTUAL provider (or None when the
    # merchant has none) — never a hardcoded guess.
    assert out["payment_provider"] == {"provider": "adyen",
                                       "supported_payment_methods": ["card"]}
    # Session-create invariants: nothing charges here (completion is a separate,
    # kill-switch-gated endpoint).
    assert out["creates_psp_payment"] is False
    assert out["capture_enabled"] is False


@pytest.mark.asyncio
async def test_endpoint_fails_open_to_redirect_on_acp_error(monkeypatch):
    # Positive control the other way: ANY session-service error (here the
    # in-process quote failing — the replacement for the old client's
    # acp_url_missing transport case) → the redirect floor, never a dead end.
    from routes import agent_checkout_intents as m
    from services.acp_checkout_session_service import AcpCheckoutSessionError

    async def fake_decision(mid, *, store_id=None, platform_override=None):
        return _decision(is_acp=True)

    async def boom(**kwargs):
        raise AcpCheckoutSessionError("down", status_code=502, code="acp_quote_failed")

    monkeypatch.setattr(m, "resolve_acp_lane_decision", fake_decision)
    monkeypatch.setattr(m, "create_session", boom)

    out = await m.create_acp_checkout(_req(), _Ctx())
    assert out["lane"] == "redirect_floor"
    assert out["reason"] == "acp_unavailable:acp_quote_failed"
