"""Tests for the recommendation engine v2 metadata enrichment (PR-8b).

Coverage:
  - Owner derivation (lever-based + title-keyword heuristic)
  - Phase derivation (severity + lever)
  - KPI / expected_outcome by action category
  - depends_on null in v1
  - Defensive: doesn't overwrite explicit values
  - Integration: build_structured_report response surfaces enriched
    fields on every action
"""

from __future__ import annotations


# ---------------------------------------------------------------------
# Owner derivation
# ---------------------------------------------------------------------


def test_owner_pivota_ops_for_indexing_actions():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "critical",
        "title": "Index your canonical PDPs with Google Search Console",
    })
    assert meta["owner"] == "pivota_ops"


def test_owner_merchant_brand_team_for_editorial_pitch():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "high",
        "title": "Pitch forbes.com editorial team",
    })
    assert meta["owner"] == "merchant_brand_team"


def test_owner_explicit_lever_overrides_keyword_heuristic():
    """When the action carries an explicit `lever` (set by playbook
    engine), use the lever→owner mapping rather than title heuristic."""
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "high",
        "title": "Some action title",  # ambiguous
        "lever": "creator_partnership",
    })
    assert meta["owner"] == "merchant_brand_team"


def test_owner_joint_for_research_actions():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "low",
        "title": "Investigate trailandkale.com",
    })
    assert meta["owner"] == "joint"


def test_owner_merchant_growth_for_wholesale():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "high",
        "title": "Wholesale onboarding: Sephora",
        "lever": "wholesale_onboarding",
    })
    assert meta["owner"] == "merchant_growth_team"


def test_owner_falls_back_to_joint_when_no_keyword_matches():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "medium",
        "title": "Generic action with no clear owner signal",
    })
    assert meta["owner"] == "joint"


# ---------------------------------------------------------------------
# Outcome / KPI coverage — page-usability Step 1: every task must carry a
# concrete "what success looks like" line. The families below used to fall
# through to None for both fields (a task that reads as busywork).
# ---------------------------------------------------------------------


def test_every_action_gets_a_concrete_outcome_and_kpi():
    """No action — including the previously-uncovered families and a wholly
    generic title — may render without an expected_outcome + kpi_to_track."""
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    titles = [
        "Index your canonical PDPs",                       # was covered
        "Pitch editorial host example.com",                # was covered
        "Convert category mentions into first-party cites",  # was None
        "Win category discovery for sleep supplements",      # was None
        "Close the gap on inconsistent queries",             # was None
        "Top citation drain: competitor.com",                # was None
        "Zero direct AI-channel attribution today",          # was None
        "Specific queries where your URL was missing",       # was None
        "Localize for the Japan market",                     # was None
        "Some entirely generic recommendation",             # fallback
    ]
    for t in titles:
        meta = _v2_metadata_for_action({"severity": "medium", "title": t})
        assert meta["expected_outcome"], f"no expected_outcome for: {t}"
        assert meta["kpi_to_track"], f"no kpi_to_track for: {t}"


def test_generic_action_uses_honest_fallback_outcome():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "low",
        "title": "Some entirely generic recommendation",
    })
    assert "AI" in meta["expected_outcome"]
    assert "re-audit" in meta["kpi_to_track"].lower()


# ---------------------------------------------------------------------
# Phase derivation
# ---------------------------------------------------------------------


def test_phase_critical_severity_lands_in_first_window():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "critical",
        "title": "Index your canonical PDPs",
    })
    assert meta["phase"] == "week_1_to_4"


def test_phase_high_pivota_ops_in_first_window():
    """High-severity Pivota-ops actions stay in week_1_to_4 (Pivota
    can execute quickly); high-severity merchant editorial pitches
    push to week_4_to_12 (publication-cycle latency)."""
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta_pivota = _v2_metadata_for_action({
        "severity": "high",
        "title": "Submit sitemap to Search Console",
    })
    assert meta_pivota["phase"] == "week_1_to_4"
    meta_pitch = _v2_metadata_for_action({
        "severity": "high",
        "title": "Pitch forbes.com editorial team",
    })
    assert meta_pitch["phase"] == "week_4_to_12"


def test_phase_medium_in_middle_window():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "medium",
        "title": "Strengthen schema markup",
    })
    assert meta["phase"] == "week_4_to_12"


def test_phase_low_severity_in_late_window():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "low",
        "title": "Maintain attribution with monitoring",
    })
    assert meta["phase"] == "week_12_to_24"


# ---------------------------------------------------------------------
# KPI + expected_outcome derivation
# ---------------------------------------------------------------------


def test_kpi_for_indexing_action():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "critical",
        "title": "Index your canonical PDPs with Google Search Console",
    })
    assert "indexed by Google" in meta["kpi_to_track"]
    assert "30-60 days" in meta["expected_outcome"]


def test_kpi_for_editorial_pitch():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "high",
        "title": "Pitch forbes.com editorial team",
    })
    assert "Editorial inclusion" in meta["kpi_to_track"]
    assert "4-8 weeks" in meta["expected_outcome"]


def test_kpi_for_monitoring_action():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "low",
        "title": "Maintain attribution with monitoring + drift detection",
    })
    assert "trend report" in meta["kpi_to_track"].lower()


def test_kpi_for_reclaim_attribution():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "critical",
        "title": "Reclaim attribution from Sephora and other resellers",
    })
    assert "first-party citation rate" in meta["kpi_to_track"].lower()


def test_unrecognized_action_gets_honest_generic_outcome_not_null():
    """REVERSAL (page-usability Step 1): the old behavior left kpi/outcome
    NULL for unrecognized actions ("better to omit than fabricate"). But in
    production this produced inert title-only tasks the merchant couldn't act
    on — and real action types (creator outreach, content revision, per-SKU
    gap repair, prompt re-test) hit this catch-all. The fallback is NOT a
    fabricated metric: it's a truthful, generic statement of what every audit
    task is for ("improve AI citation, confirmed at re-audit"). A concrete,
    honest success line beats a blank one."""
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "medium",
        "title": "Some completely unfamiliar action",
    })
    assert meta["kpi_to_track"]  # no longer None
    assert meta["expected_outcome"]
    # ...but still generic, not a specific fabricated metric
    assert "re-audit" in meta["kpi_to_track"].lower()


# ---------------------------------------------------------------------
# depends_on stays null in v1
# ---------------------------------------------------------------------


def test_depends_on_is_empty_list_in_v1():
    from services.agent_center_bd_report_service import _v2_metadata_for_action
    meta = _v2_metadata_for_action({
        "severity": "critical",
        "title": "Index your canonical PDPs",
    })
    assert meta["depends_on"] == []


# ---------------------------------------------------------------------
# Enrichment doesn't overwrite explicit values
# ---------------------------------------------------------------------


def test_enrich_does_not_overwrite_explicit_owner():
    """Hand-set owner is preserved (lets test fixtures + future
    per-action overrides take precedence)."""
    from services.agent_center_bd_report_service import _enrich_action_items_v2
    items = [{
        "severity": "critical",
        "title": "Index PDPs",
        "owner": "merchant_tech_team",  # explicit override
    }]
    _enrich_action_items_v2(items)
    assert items[0]["owner"] == "merchant_tech_team"
    # Other fields still get filled
    assert items[0]["phase"] == "week_1_to_4"


def test_enrich_does_not_overwrite_explicit_kpi():
    from services.agent_center_bd_report_service import _enrich_action_items_v2
    items = [{
        "severity": "high",
        "title": "Pitch forbes.com",
        "kpi_to_track": "Custom KPI explicitly set by caller",
    }]
    _enrich_action_items_v2(items)
    assert items[0]["kpi_to_track"] == "Custom KPI explicitly set by caller"
    # Other v2 fields still derived
    assert items[0]["owner"] is not None
    assert items[0]["phase"] is not None


def test_enrich_handles_empty_list():
    from services.agent_center_bd_report_service import _enrich_action_items_v2
    result = _enrich_action_items_v2([])
    assert result == []


def test_enrich_handles_none():
    from services.agent_center_bd_report_service import _enrich_action_items_v2
    result = _enrich_action_items_v2(None)
    assert result is None


# ---------------------------------------------------------------------
# Integration: build_structured_report surfaces v2 fields
# ---------------------------------------------------------------------


def test_build_structured_report_action_items_have_v2_metadata():
    """End-to-end: every action_item in the response payload carries
    owner, phase, kpi_to_track, expected_outcome, depends_on fields
    (nullable for kpi/outcome but always present)."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="TestBrand",
        merchant_pdp_url="https://test.com/p",
        product_title="X",
        product_vendor=None,
        product_type=None,
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        provider="gemini",
    )
    actions = report.get("action_items") or []
    assert len(actions) > 0, "audit should produce at least one action"
    for action in actions:
        # Required v2 fields present
        assert "owner" in action, f"action missing owner: {action.get('title')}"
        assert "phase" in action, f"action missing phase: {action.get('title')}"
        assert "depends_on" in action
        # owner is one of the canonical values
        assert action["owner"] in {
            "pivota_ops", "merchant_brand_team", "merchant_growth_team",
            "merchant_tech_team", "joint",
        }
        # phase is one of the canonical buckets
        assert action["phase"] in {
            "week_1_to_4", "week_4_to_12", "week_12_to_24",
        }


def test_build_structured_report_invisible_verdict_has_pivota_ops_action():
    """For an INVISIBLE-verdict audit, the headline action is
    indexing-related and owned by pivota_ops."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="TestBrand",
        merchant_pdp_url="https://test.com/p",
        product_title="X",
        product_vendor=None,
        product_type=None,
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [],
        },
        provider="gemini",
    )
    # The first strategic action for INVISIBLE verdict is the
    # Search-Console indexing action (per _generate_action_items
    # branch at line 1748+)
    indexing_actions = [
        a for a in (report.get("action_items") or [])
        if "Index" in (a.get("title") or "")
        or "Search Console" in (a.get("title") or "")
    ]
    assert len(indexing_actions) > 0
    assert indexing_actions[0]["owner"] == "pivota_ops"
    assert indexing_actions[0]["phase"] == "week_1_to_4"
