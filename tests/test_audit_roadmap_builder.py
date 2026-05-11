"""Tests for the implementation roadmap generator (PR-8c).

Coverage:
  - Phase bucketing from action_items v2 phase field
  - Owner rollup per phase
  - Expected-outcome composition (combine action outcomes; fall back
    to phase template)
  - Empty roadmap defensive behavior
  - Integration: build_structured_report surfaces roadmap with
    real bucketed phases
"""

from __future__ import annotations


# ---------------------------------------------------------------------
# Phase bucketing
# ---------------------------------------------------------------------


def test_groups_actions_by_phase():
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {"title": "Index PDPs", "phase": "week_1_to_4", "owner": "pivota_ops"},
        {"title": "Pitch Forbes", "phase": "week_4_to_12", "owner": "merchant_brand_team"},
        {"title": "Trail and Kale outreach", "phase": "week_4_to_12", "owner": "merchant_brand_team"},
        {"title": "Monitor drift", "phase": "week_12_to_24", "owner": "joint"},
    ]
    roadmap = build_implementation_roadmap(actions)
    assert len(roadmap["phases"]) == 3
    assert [p["phase_id"] for p in roadmap["phases"]] == [
        "week_1_to_4", "week_4_to_12", "week_12_to_24",
    ]
    # Activity counts match
    assert roadmap["phases"][0]["activity_count"] == 1
    assert roadmap["phases"][1]["activity_count"] == 2
    assert roadmap["phases"][2]["activity_count"] == 1
    assert roadmap["total_activities"] == 4


def test_skips_empty_phases():
    """Phases with no actions are omitted — don't render placeholder
    rows for empty buckets."""
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {"title": "Index PDPs", "phase": "week_1_to_4", "owner": "pivota_ops"},
        # No actions in week_4_to_12
        {"title": "Monitor drift", "phase": "week_12_to_24", "owner": "joint"},
    ]
    roadmap = build_implementation_roadmap(actions)
    assert len(roadmap["phases"]) == 2
    phase_ids = [p["phase_id"] for p in roadmap["phases"]]
    assert "week_1_to_4" in phase_ids
    assert "week_12_to_24" in phase_ids
    assert "week_4_to_12" not in phase_ids


def test_drops_actions_without_phase_field():
    """Older audits pre-PR-8b don't have phase field — those actions
    are dropped (better to render empty roadmap than mis-attribute)."""
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {"title": "Old action", "owner": "joint"},  # no phase
        {"title": "Index PDPs", "phase": "week_1_to_4", "owner": "pivota_ops"},
    ]
    roadmap = build_implementation_roadmap(actions)
    assert roadmap["total_activities"] == 1
    assert roadmap["phases"][0]["activities"][0]["title"] == "Index PDPs"


def test_drops_actions_with_unknown_phase():
    """Unknown phase value (defensive — caller bug or future field
    that wasn't added to PHASE_ORDER yet)."""
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {"title": "Index PDPs", "phase": "week_1_to_4", "owner": "pivota_ops"},
        {"title": "Bogus phase action", "phase": "year_5", "owner": "joint"},
    ]
    roadmap = build_implementation_roadmap(actions)
    assert roadmap["total_activities"] == 1


# ---------------------------------------------------------------------
# Owner rollup per phase
# ---------------------------------------------------------------------


def test_phase_owners_dedup_preserve_order():
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {"title": "A", "phase": "week_1_to_4", "owner": "pivota_ops"},
        {"title": "B", "phase": "week_1_to_4", "owner": "merchant_brand_team"},
        {"title": "C", "phase": "week_1_to_4", "owner": "pivota_ops"},  # dup
    ]
    roadmap = build_implementation_roadmap(actions)
    owners = roadmap["phases"][0]["owners"]
    assert owners == ["pivota_ops", "merchant_brand_team"]


# ---------------------------------------------------------------------
# Expected outcome composition
# ---------------------------------------------------------------------


def test_phase_outcome_composes_from_action_outcomes():
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {
            "title": "Index PDPs",
            "phase": "week_1_to_4",
            "owner": "pivota_ops",
            "expected_outcome": "First grounded citations within 30-60 days.",
        },
        {
            "title": "Pitch Forbes",
            "phase": "week_1_to_4",
            "owner": "merchant_brand_team",
            "expected_outcome": "Editorial inclusion in Q3 refresh.",
        },
    ]
    roadmap = build_implementation_roadmap(actions)
    outcome = roadmap["phases"][0]["expected_outcome"]
    assert "First grounded citations" in outcome
    assert "Editorial inclusion" in outcome


def test_phase_outcome_falls_back_to_template_when_no_action_outcome():
    """When no action in the phase has expected_outcome populated,
    use the phase's default template."""
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {"title": "Generic action", "phase": "week_1_to_4", "owner": "joint"},
    ]
    roadmap = build_implementation_roadmap(actions)
    outcome = roadmap["phases"][0]["expected_outcome"]
    assert "Foundation" in outcome or "indexing" in outcome.lower()


def test_phase_outcome_dedups_repeat_outcomes():
    """If multiple actions emit identical expected_outcome strings
    (common for phase-1 indexing), de-dup so the rollup doesn't
    show 'X within 30-60 days; X within 30-60 days'."""
    from services.audit_roadmap_builder import build_implementation_roadmap
    same = "First grounded citations within 30-60 days."
    actions = [
        {"title": "A", "phase": "week_1_to_4", "owner": "pivota_ops",
         "expected_outcome": same},
        {"title": "B", "phase": "week_1_to_4", "owner": "pivota_ops",
         "expected_outcome": same},
    ]
    roadmap = build_implementation_roadmap(actions)
    outcome = roadmap["phases"][0]["expected_outcome"]
    # Should appear only once in the composed outcome
    assert outcome.count("First grounded citations") == 1


def test_phase_outcome_truncates_long_composed_text():
    from services.audit_roadmap_builder import build_implementation_roadmap
    long1 = "X. " * 100
    long2 = "Y. " * 100
    actions = [
        {"title": "A", "phase": "week_1_to_4", "owner": "p",
         "expected_outcome": long1},
        {"title": "B", "phase": "week_1_to_4", "owner": "p",
         "expected_outcome": long2},
    ]
    roadmap = build_implementation_roadmap(actions)
    outcome = roadmap["phases"][0]["expected_outcome"]
    assert len(outcome) <= 320
    assert outcome.endswith("...")


# ---------------------------------------------------------------------
# Empty / defensive
# ---------------------------------------------------------------------


def test_empty_action_list_returns_empty_roadmap():
    from services.audit_roadmap_builder import build_implementation_roadmap
    roadmap = build_implementation_roadmap([])
    assert roadmap["phases"] == []
    assert roadmap["total_weeks"] == 0
    assert roadmap["total_activities"] == 0


def test_none_action_list_returns_empty_roadmap():
    from services.audit_roadmap_builder import build_implementation_roadmap
    roadmap = build_implementation_roadmap(None)
    assert roadmap["phases"] == []


def test_total_weeks_uses_max_phase_high():
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {"title": "A", "phase": "week_1_to_4", "owner": "p"},
        {"title": "B", "phase": "week_4_to_12", "owner": "p"},
    ]
    roadmap = build_implementation_roadmap(actions)
    assert roadmap["total_weeks"] == 12  # max of (4, 12)


# ---------------------------------------------------------------------
# Activities rollup shape
# ---------------------------------------------------------------------


def test_activity_summary_has_title_and_owner():
    """Activity rollup is intentionally minimal — title + owner only.
    Full action lives in report.action_items; roadmap is a navigation
    layer above that."""
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {
            "title": "Index PDPs", "phase": "week_1_to_4",
            "owner": "pivota_ops",
            "body": "Long body text",
            "kpi_to_track": "Number indexed",
            "evidence": {"some": "data"},
        },
    ]
    roadmap = build_implementation_roadmap(actions)
    activity = roadmap["phases"][0]["activities"][0]
    assert activity == {"title": "Index PDPs", "owner": "pivota_ops"}
    # Body / kpi / evidence intentionally NOT in the rollup
    assert "body" not in activity
    assert "kpi_to_track" not in activity


def test_activity_owner_defaults_to_joint_when_missing():
    from services.audit_roadmap_builder import build_implementation_roadmap
    actions = [
        {"title": "X", "phase": "week_1_to_4"},  # no owner
    ]
    roadmap = build_implementation_roadmap(actions)
    activity = roadmap["phases"][0]["activities"][0]
    assert activity["owner"] == "joint"


# ---------------------------------------------------------------------
# Integration: build_structured_report
# ---------------------------------------------------------------------


def test_build_structured_report_includes_implementation_roadmap():
    """End-to-end: response includes implementation_roadmap with
    phases bucketed from PR-8b enriched action items."""
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
    roadmap = report.get("implementation_roadmap")
    assert roadmap is not None
    assert "phases" in roadmap
    assert "total_weeks" in roadmap
    assert "total_activities" in roadmap
    # An INVISIBLE-verdict audit produces actions; phases populate
    if roadmap["total_activities"] > 0:
        assert len(roadmap["phases"]) > 0
        first = roadmap["phases"][0]
        assert first["phase_id"] in {
            "week_1_to_4", "week_4_to_12", "week_12_to_24",
        }
        assert first["label"].startswith("Phase")
        assert first["weeks"]
        assert first["expected_outcome"]
