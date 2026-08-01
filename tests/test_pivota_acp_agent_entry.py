"""P-T2.3.1b — pivota-acp session client + agent-facing /acp entry point.

Covers: the client builds the ACP request (items, headers, pvt_* metadata) and
parses the session, and raises AcpClientError on failure; the agent endpoint
routes to the ACP lane only when capable+charge-permitted+integration-enabled,
and otherwise (or on any ACP error) fails open to the redirect floor. DARK: no
order/charge is ever created here.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


class _Resp:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _Client:
    captured: List[Dict[str, Any]] = []
    resp = _Resp(201, {"id": "csn_abc", "status": "ready_for_payment", "currency": "USD",
                       "totals": [{"type": "total", "amount": 4599}]})

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None, **k):
        _Client.captured.append({"url": url, "headers": headers, "json": json})
        return _Client.resp


# --- client -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_builds_request_and_parses_session(monkeypatch):
    import httpx
    from services import pivota_acp_client as c

    _Client.captured = []
    _Client.resp = _Resp(201, {"id": "csn_abc", "status": "ready_for_payment",
                               "currency": "USD", "totals": [{"type": "total", "amount": 4599}]})
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    session = await c.create_checkout_session(
        merchant_id="merch_x",
        platform="shopify",
        items=[{"sku": "SKU1", "quantity": 2, "product_id": "p1"}],
        metadata={"pvt_click_id": "clk_abc", "pvt_surface": "chatgpt"},
    )
    assert session.session_id == "csn_abc"
    assert session.checkout_url.endswith("/checkout/csn_abc")
    assert session.total_cents == 4599
    assert session.currency == "USD"

    sent = _Client.captured[0]
    assert sent["url"].endswith("/checkout_sessions")
    assert sent["headers"]["API-Version"] == c.PIVOTA_ACP_API_VERSION
    assert sent["headers"]["X-Merchant-Id"] == "merch_x"
    assert sent["headers"]["X-Platform"] == "shopify"
    # P-T2.3.4: product_id/variant_id ride the ACP item so the session persists
    # them for the real-capture quote (id still prefers sku).
    assert sent["json"]["items"] == [{"id": "SKU1", "quantity": 2, "product_id": "p1"}]
    # pvt_click_id threaded into session metadata for attribution parity.
    assert sent["json"]["metadata"]["pvt_click_id"] == "clk_abc"


@pytest.mark.asyncio
async def test_client_raises_on_http_error(monkeypatch):
    import httpx
    from services import pivota_acp_client as c

    _Client.resp = _Resp(500, "boom")
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(c.AcpClientError) as ei:
        await c.create_checkout_session(merchant_id="m", platform="shopify",
                                        items=[{"sku": "S", "quantity": 1}])
    assert ei.value.status_code == 500


@pytest.mark.asyncio
async def test_client_raises_on_missing_session_id(monkeypatch):
    import httpx
    from services import pivota_acp_client as c

    _Client.resp = _Resp(201, {"status": "ready_for_payment"})  # no id
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(c.AcpClientError) as ei:
        await c.create_checkout_session(merchant_id="m", platform="shopify",
                                        items=[{"sku": "S", "quantity": 1}])
    assert ei.value.code == "acp_no_session"


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
async def test_endpoint_redirects_when_integration_disabled(monkeypatch):
    from routes import agent_checkout_intents as m

    async def fake_decision(mid, *, store_id=None, platform_override=None):
        return _decision(is_acp=True)

    monkeypatch.setattr(m, "resolve_acp_lane_decision", fake_decision)
    monkeypatch.setattr(m.settings, "enable_platform_orders_acp", False, raising=False)
    out = await m.create_acp_checkout(_req(), _Ctx())
    assert out["lane"] == "redirect_floor"
    assert out["reason"] == "acp_integration_disabled"


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
                                totals=[{"type": "total", "amount": 4599}])

    monkeypatch.setattr(m, "resolve_acp_lane_decision", fake_decision)
    monkeypatch.setattr(m, "create_session", fake_session)
    monkeypatch.setattr(m.settings, "enable_platform_orders_acp", True, raising=False)

    out = await m.create_acp_checkout(_req(), _Ctx())
    assert out["status"] == "requires_in_chat_acp_checkout"
    assert out["lane"] == "acp_in_chat"
    assert out["acp_session_id"] == "csn_abc"
    assert out["pvt_click_id"] == "clk_abc"
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
    monkeypatch.setattr(m.settings, "enable_platform_orders_acp", True, raising=False)

    out = await m.create_acp_checkout(_req(), _Ctx())
    assert out["lane"] == "redirect_floor"
    assert out["reason"] == "acp_unavailable:acp_quote_failed"
