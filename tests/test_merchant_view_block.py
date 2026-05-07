"""
Phase C-4 (PR-B): merchant_view layered IA block on the structured
report. Frontend portal renders directly from this block — headline,
receipts, diagnosis, actions, tracking, pivota_value_prop — without
re-deriving from the legacy fields. Existing legacy keys remain
untouched (BD renderer keeps working).

These tests assert:
  1. The block exists and has all 6 sub-keys.
  2. Every field projects from data the engine already computed (no
     dangling pointers, no re-extraction).
  3. The `audited_via_pivota_canonical` flag is plumbed per-product
     from the audit route's url_source.
  4. Indexing-arc state is the static caveat for now (PR-D wires the
     real computation).
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _vis_run(
    query: str, *, visible: bool = True, grounding: List[str] | None = None
) -> Dict[str, Any]:
    return {
        "query": query,
        "parsed": {"product_visible": visible},
        "grounding_chunks": list(grounding or []),
    }


def _attr_run(
    query: str, *, found: bool = False, grounding: List[str] | None = None
) -> Dict[str, Any]:
    return {
        "query": query,
        "parsed": {"merchant_url_found": found},
        "grounding_chunks": list(grounding or []),
    }


def _category_run(query: str, *, excerpt: str = "", grounding_sources=None):
    return {
        "query": query,
        "parsed": {"brand_appears": True, "evidence_text": excerpt},
        "grounding_chunks": [s.get("uri") for s in (grounding_sources or [])],
        "grounding_sources": grounding_sources or [],
    }


def _build_invisible_report(url_source: str | None = None) -> Dict[str, Any]:
    """Helper: build a structured report for an INVISIBLE-tier audit
    matching the test merchant's sleepwear scenario shape. Inputs are
    minimal so the assertions can target merchant_view shape precisely."""
    from services.agent_center_bd_report_service import build_structured_report
    return build_structured_report(
        merchant_name="TestSleepwear",
        merchant_pdp_url="https://testsleepwear.com/p/x",
        product_title="Women's Pajama Set",
        product_vendor="TestSleepwear",
        product_type="Sleepwear",
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _vis_run("q1", visible=False),
                _vis_run("q2", visible=False),
            ],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _attr_run("buyer query 1", grounding=["https://nordstrom.com/p/x"]),
                _attr_run("buyer query 2", grounding=["https://macys.com/p/y"]),
                _attr_run("buyer query 3"),
            ],
        },
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _category_run(
                    "best women's pajama under 100",
                    excerpt="Lunya is a top pick...",
                    grounding_sources=[{"uri": "https://nordstrom.com/", "title": "nordstrom.com"}],
                ),
                _category_run(
                    "soft sleepwear sets",
                    excerpt="Eberjey makes...",
                    grounding_sources=[{"uri": "https://macys.com/", "title": "macys.com"}],
                ),
            ],
        },
        provider="gemini",
        url_source=url_source,
    )


# ---------------------------------------------------------------------
# 1. Block shape — all 6 sub-keys present, additive (legacy keys remain).
# ---------------------------------------------------------------------


def test_merchant_view_block_present_with_all_six_subkeys():
    report = _build_invisible_report()
    assert "merchant_view" in report
    mv = report["merchant_view"]
    for key in ("headline", "receipts", "diagnosis", "actions", "tracking", "pivota_value_prop"):
        assert key in mv, f"missing merchant_view.{key}"


def test_legacy_top_level_keys_still_present():
    """Additive only — BD renderer reads from these; portal team can
    migrate at its own cadence."""
    report = _build_invisible_report()
    for key in (
        "verdict",
        "industry_context",
        "action_items",
        "competitive_pressure",
        "what_pivota_changes",
        "visibility",
        "attribution",
        "category_visibility",
    ):
        assert key in report, f"legacy key {key} dropped"


# ---------------------------------------------------------------------
# 2. Headline — verdict label + scores match legacy fields (consistency,
#    not re-computation).
# ---------------------------------------------------------------------


def test_headline_scores_consistent_with_legacy_verdict():
    report = _build_invisible_report()
    h = report["merchant_view"]["headline"]
    v = report["verdict"]
    assert h["verdict_label"] == v["label"]
    assert h["scores"]["visibility"] == v["visibility_score"]
    assert h["scores"]["attribution"] == v["attribution_score"]
    assert h["scores"]["category_visibility"] == v["category_visibility_score"]


def test_headline_one_liner_is_first_sentence_of_explanation():
    report = _build_invisible_report()
    h = report["merchant_view"]["headline"]
    expl = report["verdict"]["explanation"]
    assert h["one_liner"] is not None
    # first sentence is a prefix of the full explanation
    first_sentence = h["one_liner"].rstrip(".")
    assert first_sentence in expl


def test_headline_what_is_at_stake_from_industry_context():
    report = _build_invisible_report()
    h = report["merchant_view"]["headline"]
    assert h["what_is_at_stake"] == report["industry_context"].get("blurb")


# ---------------------------------------------------------------------
# 3. url_source plumb-through — audited_via_pivota_canonical flag.
# ---------------------------------------------------------------------


def test_audited_via_pivota_canonical_true_when_url_source_is_pivota():
    report = _build_invisible_report(url_source="pivota_canonical_pdp")
    h = report["merchant_view"]["headline"]
    assert h["audited_via_pivota_canonical"] is True
    assert h["url_source"] == "pivota_canonical_pdp"


def test_audited_via_pivota_canonical_false_for_merchant_url():
    report = _build_invisible_report(url_source="merchant_canonical_url")
    h = report["merchant_view"]["headline"]
    assert h["audited_via_pivota_canonical"] is False
    assert h["url_source"] == "merchant_canonical_url"


def test_audited_via_pivota_canonical_false_when_url_source_unknown():
    report = _build_invisible_report(url_source=None)
    h = report["merchant_view"]["headline"]
    assert h["audited_via_pivota_canonical"] is False
    assert h["url_source"] is None


# ---------------------------------------------------------------------
# 4. Receipts — projections from existing fields.
# ---------------------------------------------------------------------


def test_receipts_queries_tested_matches_attribution_runs():
    report = _build_invisible_report()
    r = report["merchant_view"]["receipts"]
    assert r["queries_tested"] == report["attribution"]["runs"]


def test_receipts_merchant_cited_in_matches_attribution():
    report = _build_invisible_report()
    r = report["merchant_view"]["receipts"]
    assert r["merchant_cited_in"] == report["attribution"]["merchant_cited_runs"]


def test_receipts_top_cited_hosts_from_category():
    """Real-data example: sleepwear merchant. The hosts list contains
    everything Gemini cited that wasn't the merchant — could be
    retailers, competitor brand .coms, or editorial sites; we don't
    classify, we just surface honestly."""
    report = _build_invisible_report()
    r = report["merchant_view"]["receipts"]
    hosts = r["top_cited_hosts"]
    assert "nordstrom.com" in hosts or "macys.com" in hosts
    # Defensive: hardcoded beauty retailers should not appear unless
    # the audit really cited them (which this fixture doesn't).
    assert "sephora.com" not in hosts
    assert "ulta.com" not in hosts


def test_receipts_field_is_top_cited_hosts_not_top_retailers():
    """Naming is honest: the list mixes brand .coms / retailers /
    media; calling it 'retailers' would mislead merchants when (e.g.)
    forbes.com or hillhousehome.com appears."""
    report = _build_invisible_report()
    r = report["merchant_view"]["receipts"]
    assert "top_cited_hosts" in r
    assert "top_retailers_eating_funnel" not in r


# ---------------------------------------------------------------------
# 5. Diagnosis — primary framing + indexing arc state.
# ---------------------------------------------------------------------


def test_diagnosis_primary_uses_competitive_pressure_framing():
    report = _build_invisible_report()
    d = report["merchant_view"]["diagnosis"]
    cp = report["competitive_pressure"]
    assert d["primary"] == cp.get("framing")


def test_diagnosis_indexing_arc_state_present_when_pivota_audit():
    report = _build_invisible_report(url_source="pivota_canonical_pdp")
    d = report["merchant_view"]["diagnosis"]
    arc = d["indexing_arc_state"]
    assert arc is not None
    assert arc["phase"] == "indexing-up"  # PR-D will compute real phase
    assert "30-90 day" in arc["caveat"]


def test_diagnosis_indexing_arc_state_omitted_for_merchant_url():
    """When the audit ran against the merchant's own URL (not Pivota
    canonical fallback), the indexing-arc caveat is irrelevant."""
    report = _build_invisible_report(url_source="merchant_canonical_url")
    d = report["merchant_view"]["diagnosis"]
    assert d["indexing_arc_state"] is None


# ---------------------------------------------------------------------
# 6. Actions + tracking + pivota_value_prop projection.
# ---------------------------------------------------------------------


def test_actions_block_references_existing_action_items():
    """Phase C-4 PR-G: merchant_view.actions is now strategic actions
    (from `action_items`) PREFIX + playbook actions (per cited host).
    Strategic actions still appear unchanged at the start; legacy
    `action_items` continues to hold only strategic."""
    report = _build_invisible_report()
    actions = report["merchant_view"]["actions"]
    legacy = report["action_items"]
    # Strategic prefix matches legacy action_items exactly.
    assert actions[: len(legacy)] == legacy
    # Anything beyond the strategic prefix is a playbook action.
    for tail_action in actions[len(legacy):]:
        assert "playbook_step_id" in tail_action


def test_tracking_baseline_reference_from_pivota_pdp_baseline():
    from services.agent_center_bd_report_service import PIVOTA_PDP_BASELINE_REFERENCE
    report = _build_invisible_report()
    t = report["merchant_view"]["tracking"]
    bl = t["pivota_baseline_reference"]
    assert bl["visibility"] == PIVOTA_PDP_BASELINE_REFERENCE["median_visibility"]
    assert bl["attribution"] == PIVOTA_PDP_BASELINE_REFERENCE["median_attribution"]
    assert bl["as_of"] == PIVOTA_PDP_BASELINE_REFERENCE["as_of_date"]


def test_tracking_history_link_set_to_history_endpoint_after_pr_c():
    """Phase C-4 PR-C populated history_link with the
    /api/merchant-center/audit/history endpoint. next_audit_eligible_at
    is still None — computed at the route layer from rate-limit
    query, not by the structured-report builder."""
    report = _build_invisible_report()
    t = report["merchant_view"]["tracking"]
    assert t["history_link"] == "/api/merchant-center/audit/history"
    assert t["next_audit_eligible_at"] is None


def test_tracking_gap_to_baseline_computed_from_scores():
    report = _build_invisible_report()
    t = report["merchant_view"]["tracking"]
    gap = t["your_gap_to_baseline"]
    bl = t["pivota_baseline_reference"]
    expected_vis_gap = report["verdict"]["visibility_score"] - (bl["visibility"] or 0)
    expected_attr_gap = report["verdict"]["attribution_score"] - (bl["attribution"] or 0)
    assert gap["visibility"] == expected_vis_gap
    assert gap["attribution"] == expected_attr_gap


def test_pivota_value_prop_references_what_pivota_changes():
    """Re-projection — the pitch lives in one place (what_pivota_changes)
    and is surfaced in merchant_view under a clearly-labeled key."""
    report = _build_invisible_report()
    assert report["merchant_view"]["pivota_value_prop"] == report["what_pivota_changes"]
