"""P-T2.3.1 — Tier-2 ACP lane routing decision.

The gate must fail-open to redirect for anything short of "ACP-capable AND the
fail-closed kill-switch permits a charge for this merchant", and fail-closed on
charge so the lane is inert by default.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

from services import tier2_acp_lane as lane  # noqa: E402
from services.tier2_acp_lane import (  # noqa: E402
    LANE_ACP_IN_CHAT,
    LANE_REDIRECT_FLOOR,
    REASON_ACP_ENABLED,
    REASON_NOT_ACP_CAPABLE,
    resolve_acp_lane_decision,
)


def _cap(protocols, *, platform="shopify", psp="stripe"):
    return {
        "merchant_id": "merch_x",
        "platform": platform,
        "platform_source": "connected",
        "psp": psp,
        "has_live_psp": bool(psp),
        "protocols": list(protocols),
    }


def _patch(monkeypatch, capability, *, strict=True, submit=False, merchants=frozenset(), test_capture=False):
    async def fake_capability(mid):
        return capability

    monkeypatch.setattr(lane, "resolve_merchant_capability", fake_capability)
    # Drive the kill-switch deterministically via settings so we exercise the
    # real evaluate_tier2_charge composition, not a stub.
    from config.settings import settings

    monkeypatch.setattr(settings, "agent_checkout_strict", strict, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_enabled", submit, raising=False)
    monkeypatch.setattr(settings, "agent_submit_payment_merchants", frozenset(merchants), raising=False)
    monkeypatch.setattr(settings, "agent_acp_test_capture", test_capture, raising=False)


@pytest.mark.asyncio
async def test_test_capture_canary_is_acp_capable_without_live_psp(monkeypatch):
    # merch on a TEST PSP → protocols=[] (not live-charge-ready), but the armed
    # test-capture canary (allowlisted + AGENT_ACP_TEST_CAPTURE) routes to ACP.
    _patch(monkeypatch, _cap([], platform="shopify", psp=None),
           strict=True, submit=True, merchants={"merch_x"}, test_capture=True)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_ACP_IN_CHAT
    assert d.reason == REASON_ACP_ENABLED


@pytest.mark.asyncio
async def test_test_capture_canary_off_still_redirects(monkeypatch):
    # Same test-PSP merchant, but the canary flag is off → redirect (default).
    _patch(monkeypatch, _cap([], platform="shopify", psp=None),
           strict=True, submit=True, merchants={"merch_x"}, test_capture=False)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR
    assert d.reason == REASON_NOT_ACP_CAPABLE


@pytest.mark.asyncio
async def test_test_capture_canary_scoped_to_allowlisted_merchant(monkeypatch):
    # Canary on, but this merchant is NOT on the allowlist → redirect.
    _patch(monkeypatch, _cap([], platform="shopify", psp=None),
           strict=True, submit=True, merchants={"other"}, test_capture=True)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR


@pytest.mark.asyncio
async def test_test_capture_canary_includes_wix(monkeypatch):
    # P-T2.3.6: wix now has a real-capture connector → canary-capable like shopify.
    _patch(monkeypatch, _cap([], platform="wix", psp=None),
           strict=True, submit=True, merchants={"merch_x"}, test_capture=True)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_ACP_IN_CHAT
    assert d.reason == REASON_ACP_ENABLED


@pytest.mark.asyncio
async def test_test_capture_canary_excludes_uncapable_platform(monkeypatch):
    # A platform without a real-capture connector (e.g. woocommerce) → redirect.
    _patch(monkeypatch, _cap([], platform="woocommerce", psp=None),
           strict=True, submit=True, merchants={"merch_x"}, test_capture=True)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR


@pytest.mark.asyncio
async def test_not_acp_capable_routes_to_redirect(monkeypatch):
    _patch(monkeypatch, _cap([]))  # no protocols
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR
    assert d.reason == REASON_NOT_ACP_CAPABLE
    assert d.is_acp is False
    assert d.protocol is None


@pytest.mark.asyncio
async def test_acp_capable_but_submit_off_routes_to_redirect(monkeypatch):
    # Default dark state: capable, but kill-switch blocks (submit off) → redirect.
    _patch(monkeypatch, _cap(["acp"]), strict=True, submit=False)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR
    assert d.reason.startswith("kill_switch_blocked:")
    assert d.kill_switch is not None and d.kill_switch["allowed"] is False


@pytest.mark.asyncio
async def test_acp_capable_and_submit_on_routes_to_acp(monkeypatch):
    _patch(monkeypatch, _cap(["acp"]), strict=True, submit=True)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_ACP_IN_CHAT
    assert d.reason == REASON_ACP_ENABLED
    assert d.is_acp is True
    assert d.protocol == "acp"
    assert d.platform == "shopify"
    assert d.psp == "stripe"


@pytest.mark.asyncio
async def test_allowlist_scopes_the_acp_lane_to_one_merchant(monkeypatch):
    # submit on + allowlist=[merch_x]: merch_x gets ACP, others get redirect.
    _patch(monkeypatch, _cap(["acp"]), strict=True, submit=True, merchants={"merch_x"})
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_ACP_IN_CHAT

    # A different merchant, same capability, is NOT on the allowlist → redirect.
    async def fake_capability_other(mid):
        c = _cap(["acp"])
        c["merchant_id"] = "merch_other"
        return c

    monkeypatch.setattr(lane, "resolve_merchant_capability", fake_capability_other)
    d2 = await resolve_acp_lane_decision("merch_other")
    assert d2.lane == LANE_REDIRECT_FLOOR
    assert d2.reason.startswith("kill_switch_blocked:")


@pytest.mark.asyncio
async def test_capability_resolver_error_falls_open_to_redirect(monkeypatch):
    async def boom(mid):
        raise RuntimeError("db down")

    monkeypatch.setattr(lane, "resolve_merchant_capability", boom)
    d = await resolve_acp_lane_decision("merch_x")
    assert d.lane == LANE_REDIRECT_FLOOR
    assert d.reason == REASON_NOT_ACP_CAPABLE


@pytest.mark.asyncio
async def test_decision_serializes(monkeypatch):
    _patch(monkeypatch, _cap(["acp"]), strict=True, submit=True)
    d = await resolve_acp_lane_decision("merch_x")
    blob = d.as_dict()
    assert blob["lane"] == LANE_ACP_IN_CHAT
    assert blob["protocol"] == "acp"
    assert set(blob) >= {"lane", "reason", "merchant_id", "protocol", "platform", "psp", "capability", "kill_switch"}
