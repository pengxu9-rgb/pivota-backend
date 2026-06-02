"""
Phase C-4 follow-up: per-action `concrete_next_step` + `priority_order`.

User feedback: actions read as "sample format" instead of deep-dive
guidance. Two improvements:

  1. Each playbook playbook produces a `concrete_next_step` field —
     a BD-curated 1-sentence "this week" task with specifics
     (URLs, who to email, sample sizes, required docs). Distinct
     from `body` which describes strategic rationale.

  2. Every action (strategic + playbook) gets a 1-indexed
     `priority_order` so the frontend can render "Step 1, Step 2..."
     without re-deriving ordering.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


@pytest.fixture(autouse=True)
def reset_caches():
    from services.audit_playbook_engine import reset_playbook_cache
    from services.cited_host_classifier import reset_registry_cache
    reset_playbook_cache()
    reset_registry_cache()
    yield
    reset_playbook_cache()
    reset_registry_cache()


# ---------------------------------------------------------------------
# 1. concrete_next_step on playbook actions
# ---------------------------------------------------------------------


def _cited(host, *, type_, subtype, applies=True, times_cited=2,
           coverage_note="Covers your category.",
           outreach_hint="Editorial pitch."):
    return {
        "host": host, "times_cited": times_cited,
        "type": type_, "subtype": subtype,
        "categories": ["sleepwear"],
        "coverage_note": coverage_note,
        "outreach_hint": outreach_hint,
        "applies_to_merchant_category": applies,
    }


def test_playbook_actions_carry_concrete_next_step():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site"),
        ],
        failed_queries_detailed=[],
    )
    assert len(actions) == 1
    cns = actions[0]["concrete_next_step"]
    assert cns is not None
    assert "nymag.com" in cns  # template interpolation


def test_concrete_next_step_renders_template_placeholders():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited(
                "nordstrom.com",
                type_="retailer", subtype="department_store",
                coverage_note="Department store.",
                outreach_hint="Vendor portal.",
            ),
        ],
        failed_queries_detailed=[],
    )
    cns = actions[0]["concrete_next_step"]
    # Specific instructions present (line sheet / vendor portal etc.)
    assert "line sheet" in cns.lower()
    assert "nordstrom.com" in cns


def test_concrete_next_step_for_youtube_creator_partnerships():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited(
                "youtube.com",
                type_="video", subtype="creator_platform",
                coverage_note="Creator hauls.",
                outreach_hint="Sponsor reviewers.",
            ),
        ],
        failed_queries_detailed=[],
    )
    cns = actions[0]["concrete_next_step"]
    # The youtube playbook tells merchant to find 3-5 mid-tier creators
    assert "10k-100k" in cns or "mid-tier" in cns


def test_concrete_next_step_for_unclassified_host_says_research():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[{
            "host": "unknown.example",
            "times_cited": 2,
            "type": "unclassified", "subtype": None,
            "categories": [],
            "coverage_note": None, "outreach_hint": None,
            "applies_to_merchant_category": None,
        }],
        failed_queries_detailed=[],
    )
    cns = actions[0]["concrete_next_step"]
    assert "visit" in cns.lower() or "investigate" in cns.lower() or "research" in cns.lower()


# ---------------------------------------------------------------------
# 2. priority_order on every action
# ---------------------------------------------------------------------


def _vis_run(q): return {"query": q, "parsed": {"product_visible": False}, "grounding_chunks": []}
def _attr_run(q, **kw):
    parsed = {"merchant_url_found": kw.get("found", False)}
    if "competitors" in kw: parsed["competitors_appearing"] = kw["competitors"]
    return {"query": q, "parsed": parsed, "grounding_chunks": kw.get("grounding", [])}
def _category_run(q, *, sources):
    return {
        "query": q,
        "parsed": {"brand_appears": True, "evidence_text": "TestSleepwear sleepwear brand."},
        "grounding_chunks": [s["uri"] for s in sources],
        "grounding_sources": sources,
    }


def test_every_action_has_priority_order_starting_at_1():
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="TestSleepwear",
        merchant_pdp_url="https://testsleepwear.com/p/x",
        product_title="Pajama set",
        product_vendor="TestSleepwear",
        product_type="Sleepwear",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": [_vis_run("v1")]},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": [
            _attr_run("buy q1", grounding=["https://nymag.com/x"], competitors=["Lunya"]),
        ]},
        category_visibility_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": [
            _category_run("best pajamas", sources=[
                {"uri": "https://nymag.com/", "title": "TestSleepwear in NYMag"},
            ]),
        ]},
        provider="gemini",
    )
    actions = report["merchant_view"]["actions"]
    assert len(actions) > 0
    expected_orders = list(range(1, len(actions) + 1))
    actual_orders = [a.get("priority_order") for a in actions]
    assert actual_orders == expected_orders


def test_strategic_actions_have_lower_priority_than_playbook():
    """Strategic actions (verdict-tier) come before per-host
    playbook actions in the merged list — `priority_order=1` should
    NOT be a playbook action when both kinds are present."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="TestSleepwear",
        merchant_pdp_url="https://testsleepwear.com/p/x",
        product_title="Pajama set",
        product_vendor="TestSleepwear",
        product_type="Sleepwear",
        visibility_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": [_vis_run("v1")]},
        attribution_result={"provider": "gemini", "scores": {"visibility_score": 0}, "raw_runs": [
            _attr_run("buy q1", grounding=["https://nymag.com/x"], competitors=["Lunya"]),
        ]},
        category_visibility_result={"provider": "gemini", "scores": {"visibility_score": 50}, "raw_runs": [
            _category_run("best pajamas", sources=[
                {"uri": "https://nymag.com/", "title": "TestSleepwear in NYMag"},
            ]),
        ]},
        provider="gemini",
    )
    actions = report["merchant_view"]["actions"]
    strategic = [a for a in actions if "playbook_step_id" not in a]
    playbook = [a for a in actions if "playbook_step_id" in a]
    if strategic and playbook:
        # Smallest playbook priority > largest strategic priority
        max_strategic = max(a["priority_order"] for a in strategic)
        min_playbook = min(a["priority_order"] for a in playbook)
        assert min_playbook > max_strategic


# ---------------------------------------------------------------------
# 3. playbook registry schema_v2 — every playbook has concrete_next_step
# ---------------------------------------------------------------------


def test_every_seed_playbook_has_concrete_next_step():
    """Schema invariant: BD-curated registry must include
    concrete_next_step for every entry. Catches accidental drops
    when adding new playbooks."""
    from services.audit_playbook_engine import _load_playbooks
    pbs = _load_playbooks()
    assert pbs, "no playbooks loaded"
    for pid, pb in pbs.items():
        cns = pb.get("concrete_next_step")
        assert cns and isinstance(cns, str), (
            f"playbook {pid!r} missing concrete_next_step"
        )
        assert len(cns) >= 30, (
            f"playbook {pid!r} concrete_next_step too short to be useful: {cns!r}"
        )
