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
