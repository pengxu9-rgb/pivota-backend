"""
Phase 0 — Pivota integration-state-aware actions.

When a merchant hasn't completed Pivota onboarding (Store Platform +
PSP), the audit's #1 critical-priority action is "Complete Pivota
integration." Body explains the dual visibility paths (merchant URL +
Pivota PDP offers) and in-chat checkout. Once both integrations are
connected, the action retires; per-host playbook actions (Phase A/B/D/E)
take over the top slots.

This action is the ONLY one allowed to mention Pivota in the diagnostic
surface — `lever="pivota_integration"` carves it out from the
PITCH_TOKENS test in tests/test_diagnostic_no_pitch.py because the
whole point of this action is to surface Pivota's value prop where
the merchant can act on it. All other actions remain pitch-free.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from services.merchant_integration_state import build_integration_action


# -----------------------------------------------------------------
# build_integration_action — pure function, easy to unit test
# -----------------------------------------------------------------


def _state(*, store: bool, psp: bool) -> Dict[str, Any]:
    missing = []
    if not store:
        missing.append("store_platform")
    if not psp:
        missing.append("psp")
    return {
        "store_platform_integrated": store,
        "psp_integrated": psp,
        "fully_integrated": store and psp,
        "missing_pieces": missing,
        "integration_completed_at": None,
    }


def test_action_emitted_when_both_missing():
    action = build_integration_action(_state(store=False, psp=False))
    assert action is not None
    assert action["severity"] == "critical"
    assert action["priority_order"] == 1
    assert action["lever"] == "pivota_integration"
    assert action["title"] == "Complete Pivota integration"
    # Body covers BOTH visibility paths AND in-chat checkout.
    assert "canonical PDP" in action["body"]
    assert "in-chat checkout" in action["body"]
    assert "alongside your own URL" in action["body"]
    # CTA fields present for portal rendering.
    assert action["cta_url"]
    assert action["cta_label"]
    assert action["concrete_next_step"]


def test_action_emitted_when_only_psp_missing():
    """Half-integrated: store connected but PSP isn't.
    Body should pivot to 'connect PSP for in-chat checkout'."""
    action = build_integration_action(_state(store=True, psp=False))
    assert action is not None
    assert action["lever"] == "pivota_integration"
    body = action["body"]
    assert "store platform is connected" in body
    assert "in-chat checkout" in body
    assert "Stripe" in body or "Adyen" in body
    # Should NOT lead with the "both missing" framing.
    assert "Connect your store platform and a payment provider" not in body


def test_action_emitted_when_only_store_missing():
    """Half-integrated: PSP connected but store isn't.
    Body should pivot to 'connect store for canonical PDP'."""
    action = build_integration_action(_state(store=False, psp=True))
    assert action is not None
    assert action["lever"] == "pivota_integration"
    body = action["body"]
    assert "payment provider is connected" in body
    assert "canonical PDP" in body
    # Should NOT lead with the "both missing" framing.
    assert "Connect your store platform and a payment provider" not in body


def test_action_not_emitted_when_fully_integrated():
    """Fully integrated → no integration action; per-host playbook
    actions take over the top slots."""
    action = build_integration_action(_state(store=True, psp=True))
    assert action is None


def test_action_evidence_carries_state_snapshot():
    """Evidence dict on the action records the state snapshot —
    useful for telemetry + audit history without re-querying."""
    action = build_integration_action(_state(store=True, psp=False))
    assert action["evidence"]["missing_pieces"] == ["psp"]
    assert action["evidence"]["store_platform_integrated"] is True
    assert action["evidence"]["psp_integrated"] is False


def test_custom_onboarding_url():
    action = build_integration_action(
        _state(store=False, psp=False),
        onboarding_url="/custom/onboarding/path",
    )
    assert action["cta_url"] == "/custom/onboarding/path"


# -----------------------------------------------------------------
# get_integration_state — best-effort: any failure → un-integrated
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_integration_state_empty_merchant_id():
    from services.merchant_integration_state import get_integration_state
    state = await get_integration_state("")
    assert state["fully_integrated"] is False
    assert state["store_platform_integrated"] is False
    assert state["psp_integrated"] is False
    assert state["missing_pieces"] == ["store_platform", "psp"]


@pytest.mark.asyncio
async def test_get_integration_state_handles_missing_helpers_gracefully(monkeypatch):
    """If the helper modules raise (DB unavailable, schema drift),
    state defaults to un-integrated. Better to over-prompt onboarding
    than to mislead a half-integrated merchant into 'you're done'."""
    from services import merchant_integration_state as mod

    async def boom(*a, **kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(
        "services.merchant_store_service.get_primary_store",
        boom,
    )
    monkeypatch.setattr(
        "services.merchant_psp_config_service.fetch_active_merchant_psps",
        boom,
    )
    state = await mod.get_integration_state("merch_test")
    assert state["fully_integrated"] is False
    assert state["store_platform_integrated"] is False
    assert state["psp_integrated"] is False


# -----------------------------------------------------------------
# Audit-engine integration: action prepended at priority_order=1
# -----------------------------------------------------------------


def test_merchant_view_prepends_integration_action_when_state_incomplete():
    """Use a half-integrated state here. The both-missing state is the
    cold-start audit sentinel and is intentionally suppressed by
    _build_merchant_view so cold BD audits do not lead with integration.
    """
    from services.agent_center_bd_report_service import (
        VERDICT_INVISIBLE,
        _build_merchant_view,
    )

    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[
            {"severity": "high", "title": "Strategic action", "body": "x"},
        ],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=_state(store=True, psp=False),
    )
    actions = mv["actions"]
    assert len(actions) >= 1
    assert actions[0]["lever"] == "pivota_integration"
    assert actions[0]["priority_order"] == 1
    # Strategic action follows.
    if len(actions) >= 2:
        assert actions[1]["title"] == "Strategic action"
        assert actions[1]["priority_order"] == 2


def test_merchant_view_no_integration_action_when_fully_integrated():
    from services.agent_center_bd_report_service import (
        VERDICT_INVISIBLE,
        _build_merchant_view,
    )

    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[
            {"severity": "high", "title": "Strategic action", "body": "x"},
        ],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=_state(store=True, psp=True),
    )
    actions = mv["actions"]
    # No integration action; first action is the strategic one.
    assert all(a.get("lever") != "pivota_integration" for a in actions)


def test_merchant_view_no_integration_action_when_state_is_none():
    """integration_state=None (e.g., legacy callers / lookup failed
    silently) → no integration action emitted; let existing actions
    take over."""
    from services.agent_center_bd_report_service import (
        VERDICT_INVISIBLE,
        _build_merchant_view,
    )

    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[
            {"severity": "high", "title": "Strategic action", "body": "x"},
        ],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=None,
    )
    actions = mv["actions"]
    assert all(a.get("lever") != "pivota_integration" for a in actions)


# -----------------------------------------------------------------
# Pitch-token carve-out for lever="pivota_integration"
# -----------------------------------------------------------------


def test_integration_action_legitimately_mentions_pivota_value_prop():
    """The integration action MUST mention 'Pivota' / 'in-chat checkout'
    / 'canonical PDP' — it's the action that tells the merchant what
    integrating gets them. The PITCH_TOKENS test in
    test_diagnostic_no_pitch.py exempts lever='pivota_integration'
    by design."""
    action = build_integration_action(_state(store=False, psp=False))
    body = action["body"]
    # Pivota value-prop tokens are EXPECTED here:
    assert "Pivota" in body
    assert "canonical PDP" in body
    assert "in-chat checkout" in body
