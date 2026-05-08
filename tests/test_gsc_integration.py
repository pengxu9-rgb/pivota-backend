"""
Phase D scaffolding tests — GSC auto-submit foundation.

Status: scaffolding only. The Google API wire-up (real OAuth flow +
URL Inspection API calls) lands in a follow-up PR. These tests cover
the data-shape + integration-state-detection + action-emission
contracts so the wire-up can land additively.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest


# -----------------------------------------------------------------
# build_gsc_integration_action — pure helper, easy to unit test
# -----------------------------------------------------------------


def test_gsc_action_has_critical_path_fields():
    from services.gsc_integration import build_gsc_integration_action
    action = build_gsc_integration_action()
    assert action["lever"] == "gsc_integration"
    assert action["severity"] == "high"  # not critical — store+PSP onboarding is critical
    assert action["title"]
    assert action["body"]
    assert action["concrete_next_step"]
    assert action["cta_url"]
    assert action["cta_label"]
    # Body legitimately mentions Pivota's value prop — the carve-out
    # in test_diagnostic_no_pitch makes this allowed.
    assert "Pivota" in action["body"]
    assert "canonical" in action["body"].lower()


def test_gsc_action_custom_onboarding_url():
    from services.gsc_integration import build_gsc_integration_action
    action = build_gsc_integration_action(onboarding_url="/custom/gsc/path")
    assert action["cta_url"] == "/custom/gsc/path"


# -----------------------------------------------------------------
# Stub functions raise GscNotConfiguredError until OAuth wire-up
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_url_to_gsc_raises_until_configured():
    from services.gsc_integration import (
        GscNotConfiguredError,
        submit_url_to_gsc,
    )
    with pytest.raises(GscNotConfiguredError):
        await submit_url_to_gsc("merch_test", "https://example.com/p/1")


@pytest.mark.asyncio
async def test_get_index_status_raises_until_configured():
    from services.gsc_integration import (
        GscNotConfiguredError,
        get_index_status,
    )
    with pytest.raises(GscNotConfiguredError):
        await get_index_status("merch_test", "https://example.com/p/1")


# -----------------------------------------------------------------
# is_gsc_integrated — best-effort DB lookup
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_gsc_integrated_handles_missing_table():
    """Migration 074 may not yet be applied in dev / staging.
    is_gsc_integrated must NOT raise — it returns False so the
    audit surfaces 'Grant GSC access' as the next step rather than
    misleading the merchant into thinking they're done."""
    from services import gsc_integration as mod

    async def boom(*a, **kw):
        raise RuntimeError("table gsc_oauth_tokens does not exist")

    with patch("db.database.database.fetch_one", AsyncMock(side_effect=boom)):
        result = await mod.is_gsc_integrated("merch_test")
    assert result is False


@pytest.mark.asyncio
async def test_is_gsc_integrated_empty_merchant_id():
    from services.gsc_integration import is_gsc_integrated
    assert await is_gsc_integrated("") is False
    assert await is_gsc_integrated("   ") is False


# -----------------------------------------------------------------
# get_gsc_submission_state — aggregation
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_submission_state_empty_when_no_rows():
    from services import gsc_integration as mod

    async def empty(*a, **kw):
        return []

    with patch("db.database.database.fetch_all", AsyncMock(side_effect=empty)):
        state = await mod.get_gsc_submission_state("merch_test")
    assert state["submitted"] == 0
    assert state["indexed"] == 0
    assert state["pending"] == 0
    assert state["errors"] == 0
    assert state["last_submission_at"] is None
    assert state["last_indexed_at"] is None


@pytest.mark.asyncio
async def test_submission_state_aggregates_status_counts():
    from services import gsc_integration as mod

    rows = [
        {"last_status": "indexed", "submitted_at": "2026-05-01", "indexed_at": "2026-05-04"},
        {"last_status": "indexed", "submitted_at": "2026-05-02", "indexed_at": "2026-05-05"},
        {"last_status": "submitted", "submitted_at": "2026-05-06", "indexed_at": None},
        {"last_status": "pending", "submitted_at": "2026-05-07", "indexed_at": None},
        {"last_status": "error", "submitted_at": None, "indexed_at": None},
    ]

    async def fake(*a, **kw):
        return rows

    with patch("db.database.database.fetch_all", AsyncMock(side_effect=fake)):
        state = await mod.get_gsc_submission_state("merch_test")
    assert state["indexed"] == 2
    assert state["submitted"] == 2  # submitted + pending both count as in-flight
    assert state["pending"] == 1
    assert state["errors"] == 1
    assert state["last_submission_at"] == "2026-05-07"
    assert state["last_indexed_at"] == "2026-05-05"


@pytest.mark.asyncio
async def test_submission_state_handles_db_failure():
    from services import gsc_integration as mod

    async def boom(*a, **kw):
        raise RuntimeError("db unavailable")

    with patch("db.database.database.fetch_all", AsyncMock(side_effect=boom)):
        state = await mod.get_gsc_submission_state("merch_test")
    # Returns zero-state on error — never raises.
    assert state == {
        "submitted": 0,
        "indexed": 0,
        "pending": 0,
        "errors": 0,
        "last_submission_at": None,
        "last_indexed_at": None,
    }


# -----------------------------------------------------------------
# Integration with merchant_integration_state — secondary action emission
# -----------------------------------------------------------------


def _state(*, store: bool, psp: bool, gsc: bool) -> Dict[str, Any]:
    missing = []
    if not store:
        missing.append("store_platform")
    if not psp:
        missing.append("psp")
    return {
        "store_platform_integrated": store,
        "psp_integrated": psp,
        "gsc_integrated": gsc,
        "fully_integrated": store and psp,
        "missing_pieces": missing,
        "integration_completed_at": None,
    }


def test_gsc_action_NOT_emitted_when_phase_0_incomplete():
    """When store+PSP onboarding isn't done, GSC action should NOT
    surface — Phase 0's 'Complete Pivota integration' action takes
    the top slot. Asking the merchant for GSC access before they've
    even connected their store is wrong order."""
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
        action_items=[],
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
        integration_state=_state(store=False, psp=False, gsc=False),
    )
    actions = mv["actions"]
    # Phase 0 action should be at the top
    assert actions[0]["lever"] == "pivota_integration"
    # GSC action should NOT appear
    assert all(a.get("lever") != "gsc_integration" for a in actions)


def test_gsc_action_emitted_when_phase_0_done_but_gsc_missing():
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
            {"severity": "medium", "title": "Strategic action", "body": "x"},
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
        integration_state=_state(store=True, psp=True, gsc=False),
    )
    actions = mv["actions"]
    # Phase 0 NOT emitted (store+PSP done)
    assert all(a.get("lever") != "pivota_integration" for a in actions)
    # GSC action IS emitted at top, ahead of strategic actions
    assert actions[0]["lever"] == "gsc_integration"
    assert actions[0]["priority_order"] == 1
    # Strategic action follows
    if len(actions) >= 2:
        assert actions[1]["title"] == "Strategic action"


def test_neither_integration_action_when_fully_onboarded():
    """All three integrations done → audit shows only the strategic +
    playbook actions, no integration CTA."""
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
            {"severity": "medium", "title": "Strategic action", "body": "x"},
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
        integration_state=_state(store=True, psp=True, gsc=True),
    )
    actions = mv["actions"]
    assert all(a.get("lever") != "pivota_integration" for a in actions)
    assert all(a.get("lever") != "gsc_integration" for a in actions)
