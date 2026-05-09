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
    no taxonomy) must NOT surface in global recall."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    assert "p.pdp_lifecycle_stage IN ('validated', 'published')" in src, (
        "global recall must hard-filter on live lifecycle stages "
        "(validated|published) — see PDP_ONBOARDING_PLAYBOOK.md O-5"
    )


def test_canonical_search_no_longer_grandfathers_null_lifecycle_stage():
    """Phase O-5b: after O-6b backfill confirmed 0 NULL rows in prod,
    the original O-5 NULL grandfather (`OR pdp_lifecycle_stage IS NULL`)
    was removed. Any NULL stage now is a bug — a row that escaped the
    3 ingest paths' write-time compute. Pin the absence so a future
    refactor doesn't accidentally re-introduce a fallback that masks
    the bug by silently surfacing the row.

    Asserting on the SQL form specifically (with the `p.` table alias)
    so the post-removal comment that explains *what was removed* doesn't
    trip the pin."""
    src = _src(pivot_query_service._fetch_canonical_search_rows)
    assert "p.pdp_lifecycle_stage IS NULL" not in src, (
        "the NULL grandfather (SQL `p.pdp_lifecycle_stage IS NULL`) was "
        "removed in O-5b — re-introducing it would silently mask ingest-path "
        "bugs by surfacing un-staged rows"
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
