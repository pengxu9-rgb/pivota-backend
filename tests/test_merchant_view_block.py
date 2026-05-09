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

from datetime import datetime, timedelta, timezone
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


def _build_invisible_report(
    url_source: str | None = None,
    integration_state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
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
        integration_state=integration_state,
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
    """Phase C-4 PR-D landed: the diagnosis now carries the real
    arc state. Without `pivota_signature_minted_at` (this fixture
    doesn't pass one), phase is `unknown` + the static-style 30-90d
    caveat. With minted_at, phase is `fresh|indexing|expected_steady`
    — covered by tests/test_pivota_indexing_arc.py."""
    report = _build_invisible_report(url_source="pivota_canonical_pdp")
    d = report["merchant_view"]["diagnosis"]
    arc = d["indexing_arc_state"]
    assert arc is not None
    assert arc["phase"] in {"unknown", "fresh", "indexing", "expected_steady"}
    assert "30-90" in arc["caveat"] or "indexing" in arc["caveat"].lower()


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


# ---------------------------------------------------------------------
# Cold-start gating — Phase 0 demote + tracking suppression for BD
# employee-portal flow. The cold-start synthetic integration_state
# (fully_integrated=False AND missing_pieces=[store_platform, psp])
# signals a non-Pivota target; the integration CTA stays in
# pivota_value_prop but does NOT prepend to merchant_view.actions.
# ---------------------------------------------------------------------


_COLD_START_STATE: Dict[str, Any] = {
    "store_platform_integrated": False,
    "psp_integrated": False,
    "gsc_integrated": False,
    "fully_integrated": False,
    "missing_pieces": ["store_platform", "psp"],
    "integration_completed_at": None,
    "store_platform_name": None,
    "psp_provider": None,
    "store_connected_at": None,
}


def test_cold_start_does_not_prepend_phase_0_to_actions():
    """For cold-start audits, the diagnostic action ladder must not be
    led by 'Complete Pivota integration' — that's pitch material, not
    a finding. Pitch lives separately in pivota_value_prop."""
    report = _build_invisible_report(integration_state=_COLD_START_STATE)
    actions = report["merchant_view"]["actions"]
    # actions[] should still be populated (data-bound diagnostic items)
    assert actions, "expected diagnostic actions for INVISIBLE tier"
    # but NONE should have lever=pivota_integration
    levers = [a.get("lever") for a in actions]
    assert "pivota_integration" not in levers, (
        f"Phase 0 leaked into cold-start actions: {levers}"
    )


def test_cold_start_pivota_value_prop_still_present():
    """Phase 0 demote doesn't drop the pitch — it only moves it.
    pivota_value_prop must still surface the integration content."""
    report = _build_invisible_report(integration_state=_COLD_START_STATE)
    vp = report["merchant_view"]["pivota_value_prop"]
    assert vp, "pivota_value_prop should remain populated after Phase 0 demote"


def test_merchant_audit_phase_0_still_prepended_when_unintegrated():
    """Regression guard: the cold-start demote must be narrowly
    targeted. A real merchant audit (only some pieces missing, not
    the cold-start "everything missing" shape) must still surface
    pivota_integration in actions."""
    only_psp_missing = {
        "store_platform_integrated": True,
        "psp_integrated": False,
        "gsc_integrated": True,
        "fully_integrated": False,
        "missing_pieces": ["psp"],
    }
    report = _build_invisible_report(integration_state=only_psp_missing)
    actions = report["merchant_view"]["actions"]
    levers = [a.get("lever") for a in actions]
    assert "pivota_integration" in levers, (
        f"non-cold-start merchant audit should still emit Phase 0: {levers}"
    )


# ---------------------------------------------------------------------
# Brand-level markdown export — render_brand_markdown wraps per-product
# rendering with a brand-level header so the cold-start /export
# endpoint can return one downloadable .md file per audit.
# ---------------------------------------------------------------------


def test_render_brand_markdown_includes_brand_header_and_per_product_sections():
    from services.agent_center_bd_report_service import render_brand_markdown
    # Build a 2-product brand_report shape
    p1 = _build_invisible_report()
    p2 = _build_invisible_report()
    brand_report = {
        "merchant_name": "TestSleepwear",
        "merchant_domain": "testsleepwear.com",
        "timestamp": "2026-05-09T05:30:00Z",
        "aggregate": {
            "brand_verdict_label": "INVISIBLE",
            "brand_verdict_explanation": "0/3 cited.",
            "avg_visibility": 0,
            "avg_attribution": 0,
            "avg_category_visibility": None,
            "products_count": 2,
        },
        "cross_product_competitors": [{"host": "nordstrom.com", "times_cited": 2}],
        "failed": [],
        "per_product": [p1, p2],
    }
    md = render_brand_markdown(brand_report)
    # Brand header
    assert "# AI Commerce Readiness Report — TestSleepwear" in md
    assert "testsleepwear.com" in md
    # Aggregate section + null category gracefully rendered
    assert "## Brand-level summary" in md
    assert "INVISIBLE" in md
    assert "_(not measured)_" in md  # avg_category_visibility=None
    # Cross-product hosts table
    assert "## Hosts capturing this brand's AI traffic" in md
    assert "nordstrom.com" in md
    # Both products embedded
    assert "Product 1 of 2" in md
    assert "Product 2 of 2" in md


# ---------------------------------------------------------------------------
# PR-1a: trend deltas (_build_history_trend) + prospect_merchant_id
# ---------------------------------------------------------------------------

from services.agent_center_bd_report_service import (
    _build_history_trend,
    _days_between,
)


def test_build_history_trend_returns_none_when_no_prior_runs():
    assert _build_history_trend([]) is None
    assert _build_history_trend(None) is None


def test_build_history_trend_skips_running_and_failed_runs():
    """Trend baseline is most-recent SUCCEEDED — running/failed runs
    don't have valid scores."""
    runs = [
        {"status": "running", "visibility_score_avg": None},
        {"status": "failed", "visibility_score_avg": None},
    ]
    assert _build_history_trend(runs) is None


def test_build_history_trend_returns_baseline_without_current_scores():
    """When current_scores not provided (legacy callers), returns
    most-recent + sparkline series but no delta."""
    runs = [
        {
            "status": "succeeded",
            "run_id": "run-1",
            "requested_at": "2026-04-09T10:00:00+00:00",
            "visibility_score_avg": 50,
            "attribution_score_avg": 30,
            "category_visibility_score_avg": 20,
            "verdict_labels": ["PARTIAL"],
        },
    ]
    out = _build_history_trend(runs)
    assert out is not None
    assert out["audits_in_history"] == 1
    assert out["most_recent_audit"]["visibility"] == 50
    assert out["delta_from_most_recent"] is None
    assert len(out["series"]) == 1


def test_build_history_trend_computes_delta_when_current_scores_given():
    """The headline value of PR-1a — '+15 visibility, -5 attribution
    over 14 days'."""
    runs = [
        {
            "status": "succeeded",
            "run_id": "run-1",
            # 14 days ago at audit time
            "requested_at": (
                datetime.now(timezone.utc).replace(microsecond=0)
                - timedelta(days=14)
            ).isoformat(),
            "visibility_score_avg": 50,
            "attribution_score_avg": 30,
            "category_visibility_score_avg": 20,
            "verdict_labels": ["PARTIAL"],
        },
    ]
    out = _build_history_trend(
        runs,
        current_scores={"visibility": 65, "attribution": 25, "category_visibility": 30},
    )
    assert out is not None
    delta = out["delta_from_most_recent"]
    assert delta["visibility"] == 15  # 65 - 50
    assert delta["attribution"] == -5  # 25 - 30
    assert delta["category_visibility"] == 10  # 30 - 20
    assert delta["days_since_last_audit"] == 14


def test_build_history_trend_delta_handles_missing_current_score():
    """category_visibility delta is null when the current audit didn't
    measure it (product_type missing → mode skipped)."""
    runs = [{
        "status": "succeeded",
        "run_id": "r",
        "requested_at": "2026-04-01T00:00:00+00:00",
        "visibility_score_avg": 50,
        "attribution_score_avg": 30,
        "category_visibility_score_avg": 20,
    }]
    out = _build_history_trend(
        runs,
        current_scores={"visibility": 60, "attribution": 35, "category_visibility": None},
    )
    delta = out["delta_from_most_recent"]
    assert delta["visibility"] == 10
    assert delta["attribution"] == 5
    assert delta["category_visibility"] is None


def test_build_history_trend_orders_series_oldest_to_newest():
    """Sparkline rendering wants oldest → newest. The accessor returns
    newest-first, so we reverse."""
    runs = [
        {"status": "succeeded", "run_id": "r3", "requested_at": "2026-04-09T00:00:00+00:00",
         "visibility_score_avg": 70, "attribution_score_avg": 40, "category_visibility_score_avg": 30},
        {"status": "succeeded", "run_id": "r2", "requested_at": "2026-04-02T00:00:00+00:00",
         "visibility_score_avg": 60, "attribution_score_avg": 35, "category_visibility_score_avg": 25},
        {"status": "succeeded", "run_id": "r1", "requested_at": "2026-03-26T00:00:00+00:00",
         "visibility_score_avg": 50, "attribution_score_avg": 30, "category_visibility_score_avg": 20},
    ]
    out = _build_history_trend(runs)
    series = out["series"]
    assert [p["visibility"] for p in series] == [50, 60, 70]


def test_days_between_handles_iso_with_z_suffix():
    """recent_runs_for_merchant returns ISO with +00:00; older entries
    might use Z. Both should parse."""
    iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    assert _days_between(iso) == 7


def test_days_between_returns_none_for_garbage():
    assert _days_between(None) is None
    assert _days_between("") is None
    assert _days_between("not a date") is None


# ---------------------------------------------------------------------------
# PR-1a: synthetic prospect_merchant_id helper (in routes/agent_center_bd_routes)
# ---------------------------------------------------------------------------

from routes.agent_center_bd_routes import _prospect_merchant_id


def test_prospect_merchant_id_stable_for_same_domain():
    """Same domain → same id across calls. Determinism is what enables
    re-audit-in-30-days trend tracking."""
    assert _prospect_merchant_id("gruns.co") == _prospect_merchant_id("gruns.co")
    assert _prospect_merchant_id("https://gruns.co/") == _prospect_merchant_id("gruns.co")
    assert _prospect_merchant_id("HTTPS://Gruns.CO/products/x") == _prospect_merchant_id("gruns.co")


def test_prospect_merchant_id_strips_www_prefix():
    """www.gruns.co and gruns.co should produce the same id."""
    assert _prospect_merchant_id("www.gruns.co") == _prospect_merchant_id("gruns.co")
    assert _prospect_merchant_id("https://www.gruns.co/products/x") == _prospect_merchant_id("gruns.co")


def test_prospect_merchant_id_distinct_for_different_domains():
    assert _prospect_merchant_id("gruns.co") != _prospect_merchant_id("hiya.com")


def test_prospect_merchant_id_format():
    pid = _prospect_merchant_id("gruns.co")
    assert pid.startswith("prospect_")
    assert len(pid) == len("prospect_") + 12  # 12-char hex


def test_prospect_merchant_id_handles_empty_input():
    assert _prospect_merchant_id("") == _prospect_merchant_id("")  # deterministic
    assert _prospect_merchant_id(None) == _prospect_merchant_id(None)
