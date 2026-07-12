"""Pin the recall scope bonus introduced in Phase 6.

The canonical-recall SQL in services.pivot_query_service adds a rank
bonus for pdp_scope='multi_merchant_canonical' so the few canonical
PDPs aren't drowned out by a long-tail merchant's exclusive inventory
(today: 1216 MOYU brushes vs ~20 canonical industry rows).

This is a structural test on the SQL string — running the query needs
a real DB and is covered end-to-end by Phase 5 probe v9. If a future
change drops the bonus or shifts its weight, this fails fast.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import pivot_query_service  # noqa: E402


def _src(fn) -> str:
    return inspect.getsource(fn)


def test_canonical_search_includes_pdp_scope_bonus():
    """The 200-point bonus is what makes canonical PDPs rank above
    merchant_owned for any matched query. Anything smaller wouldn't
    survive a category_path bonus (90) or a brand exact match (80)."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    assert "p.pdp_scope = 'multi_merchant_canonical'" in src
    assert "THEN 200" in src, (
        "scope bonus must be ≥200 to dominate the existing rank terms — "
        "title-exact (100), source_product_id (105), category_path (90)"
    )


def test_canonical_search_selects_pdp_scope_for_consumers():
    """Downstream consumers (UI badging, observability, debugging) need
    pdp_scope on the response so the planner can tell canonical from
    merchant_owned in production traces."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    assert "p.pdp_scope" in src and "c.pdp_scope" in src, (
        "candidate_skus CTE must include p.pdp_scope and the outer "
        "SELECT must pass it through"
    )


# ---------------------------------------------------------------------------
# Phase O-5 — recall live-stage filter + lifecycle rank bonus
# ---------------------------------------------------------------------------


def test_canonical_search_filters_to_live_lifecycle_stages_for_global_queries():
    """The whole point of O-5: drafts and candidates (thin content,
    no taxonomy) must NOT surface in global recall. Pin both the
    enum and the IS NULL grandfather so a future "tighten the gate"
    PR is intentional, not accidental."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    assert "p.pdp_lifecycle_stage IN ('validated', 'published')" in src, (
        "global recall must hard-filter on live lifecycle stages "
        "(validated|published) — see PDP_ONBOARDING_PLAYBOOK.md O-5"
    )
    assert "p.pdp_lifecycle_stage IS NULL" in src, (
        "the NULL grandfather must remain until O-6b backfill confirms "
        "0 NULL rows in prod; remove only in a follow-up PR with that evidence"
    )


def test_canonical_search_lifecycle_filter_skips_merchant_scoped_queries():
    """A merchant calling find_products with their own merchant_id
    should always see their inventory regardless of stage — they
    haven't promised the gateway anything about taxonomy fill yet,
    and hiding their candidate-stage products from their own dashboard
    is wrong."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    # The lifecycle clause must be inside an `if not merchant_id`
    # branch so merchant-scoped queries skip it.
    assert "if not merchant_id" in src, (
        "lifecycle filter must be merchant_id-conditional to keep merchant-scoped "
        "recall returning all of a merchant's inventory"
    )
    assert "lifecycle_clause" in src, (
        "the merchant_id-conditional lifecycle SQL fragment must be a named variable "
        "for clarity at the SQL site"
    )


def test_canonical_search_excludes_deactivated_merchants_from_global_recall():
    """A deactivated merchant (catalog_merchants.status='inactive') — e.g. a
    decommissioned test rig whose stores are all retired — must NOT leak its
    catalog into cross-merchant recall (this is what surfaced a demo merchant's
    dog leashes for 'leather crossbody bag'). COALESCE keeps external seeds
    (no catalog_merchants row -> NULL -> 'active') and observed sellers serving."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    assert "COALESCE(m.status, 'active') <> 'inactive'" in src, (
        "global recall must exclude products from merchants marked inactive"
    )
    assert "merchant_status_clause" in src, (
        "the merchant-status filter must be a named SQL fragment for clarity"
    )
    # Must be merchant_id-conditional so a merchant still sees their own rows:
    # the COALESCE assignment is guarded by an `if not merchant_id:` branch.
    idx_assign = src.index('"AND COALESCE(m.status')
    assert "if not merchant_id" in src[max(0, idx_assign - 80):idx_assign], (
        "merchant_status_clause must be assigned inside an `if not merchant_id` "
        "branch so merchant-scoped queries keep returning all of a merchant's inventory"
    )


def test_canonical_search_includes_lifecycle_rank_bonus():
    """Within the live pool, published > validated as a tie-breaker.
    Pin both magnitudes — they must stay below brand-exact (80) and
    category-prefix (90) so lifecycle is a tie-breaker, not a
    dominating signal that overrides query-relevance."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    assert "p.pdp_lifecycle_stage = 'published' THEN 60" in src, (
        "published-stage rank bonus must be +60 (under brand/category)"
    )
    assert "p.pdp_lifecycle_stage = 'validated' THEN 20" in src, (
        "validated-stage rank bonus must be +20 (small tie-breaker)"
    )


def test_canonical_search_selects_lifecycle_stage_for_observability():
    """pdp_lifecycle_stage must reach the response so probe tools and
    dashboards can verify the new filter is doing what we expect
    in prod traces (e.g., when debugging "why is this row showing up
    at the top?")."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    assert "p.pdp_lifecycle_stage" in src and "c.pdp_lifecycle_stage" in src, (
        "candidate_skus CTE must include p.pdp_lifecycle_stage and the outer "
        "SELECT must pass it through"
    )


# ---------------------------------------------------------------------------
# RECALL_RELEVANCE_V2 — separate text-relevance from structural boosts
# (docs/recall-relevance-saturation-fix.md)
# ---------------------------------------------------------------------------


def test_canonical_search_emits_text_and_structure_split():
    """The candidate CTE must emit text_score (relevance only) and
    structure_score (scope/lifecycle/category) as separate columns, and the
    outer SELECT must pass both through — without changing rank_score."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    assert "AS text_score" in src and "AS structure_score" in src, (
        "v2 split columns must exist in the candidate CTE"
    )
    assert "c.text_score AS text_score" in src and "c.structure_score AS structure_score" in src, (
        "outer SELECT must pass through text_score + structure_score"
    )
    # text_score must carry partial-match (LIKE) credit, else a non-exact match
    # scores ~0 and v2 can't discriminate (the whole point of the fix).
    assert "LOWER(COALESCE(p.title, '')) LIKE :query_like THEN 90" in src
    # structure_score carries the scope boost; rank_score (unchanged) still has it too.
    assert "AS rank_score" in src


def test_canonical_match_reason_v2_ranks_on_text_not_structure(monkeypatch):
    """With v2 ON, candidate_score derives from text_score (so the +200
    structural boost can't saturate it); with v2 OFF it derives from rank_score
    exactly as before. structure_score is always surfaced for the tie-break."""
    # A junk same-category row: low text, high structure (the +200 case).
    junk = {"rank_score": 290, "text_score": 0, "structure_score": 290,
            "product_title": "Kids Hair Clips", "brand": "X", "merchant_name": "X"}
    # A precise match: high text, no structural boost.
    precise = {"rank_score": 90, "text_score": 90, "structure_score": 0,
               "product_title": "Anuko Hair Butter", "brand": "Anuko", "merchant_name": "Anuko"}

    monkeypatch.delenv("RECALL_RELEVANCE_V2", raising=False)
    junk_off = pivot_query_service._canonical_match_reason(junk, "anuko hair butter")
    precise_off = pivot_query_service._canonical_match_reason(precise, "anuko hair butter")
    # OFF: junk's rank_score (290 -> capped 1.4) beats precise (0.90) — the bug.
    assert junk_off["candidate_score"] > precise_off["candidate_score"]

    monkeypatch.setenv("RECALL_RELEVANCE_V2", "1")
    junk_on = pivot_query_service._canonical_match_reason(junk, "anuko hair butter")
    precise_on = pivot_query_service._canonical_match_reason(precise, "anuko hair butter")
    # ON: ranked by text_score — precise (0.90) now beats junk (text 0 -> 0.12 floor).
    assert precise_on["candidate_score"] > junk_on["candidate_score"]
    assert junk_on["candidate_score"] == 0.12
    # structure_score is surfaced for the secondary tie-break.
    assert junk_on["structure_score"] == 290.0 and precise_on["structure_score"] == 0.0


def test_canonical_match_reason_v2_falls_back_to_rank_score_when_no_split(monkeypatch):
    """The citable lane emits no text_score (it has no structural boost), so v2
    must fall back to rank_score for candidate_score rather than flooring to 0.12."""
    monkeypatch.setenv("RECALL_RELEVANCE_V2", "1")
    citable_row = {"rank_score": 90, "product_title": "Anuko Hair Butter"}  # no text_score key
    reason = pivot_query_service._canonical_match_reason(citable_row, "anuko hair butter")
    assert reason["candidate_score"] == 0.9
    assert reason["structure_score"] == 0.0
