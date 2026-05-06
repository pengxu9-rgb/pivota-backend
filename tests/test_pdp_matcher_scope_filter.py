"""Pin the candidate-query scope filter introduced in Phase 6.

The three candidate-fetching helpers in services.pdp_matcher.runner all
filter to pdp_scope='multi_merchant_canonical' so a cross-merchant seed
can never silently be attached to a single-merchant private PDP via
trigram match. This is a structural test: it inspects the source of
the helpers to confirm the clause is present, since the helpers are
SQL-bound and require a real DB to exercise end-to-end. If a future
change drops the filter, this test fails fast.

(End-to-end behavior is exercised by the runner's --dry-run on
staging; see Phase 6 verification in the plan file.)
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pdp_matcher import runner  # noqa: E402


_REQUIRED_CLAUSE = "pdp_scope = 'multi_merchant_canonical'"


def _src(fn) -> str:
    return inspect.getsource(fn)


def test_source_product_id_query_filters_canonical_only():
    src = _src(runner._candidates_by_source_product_id)
    assert _REQUIRED_CLAUSE in src, "source_product_id candidate query must filter to canonical PDPs"


def test_canonical_url_query_filters_canonical_only():
    src = _src(runner._candidates_by_canonical_url)
    assert _REQUIRED_CLAUSE in src, "canonical_url candidate query must filter to canonical PDPs"


def test_title_trigram_query_filters_canonical_only():
    src = _src(runner._candidates_by_title_trigram)
    assert _REQUIRED_CLAUSE in src, (
        "title-trigram candidate query must filter to canonical PDPs — "
        "without this filter, a cross-merchant seed could silently match "
        "a single-merchant private PDP via title trigram (e.g. Sigma seed "
        "→ MOYU brush PDP). This is the load-bearing assertion of Phase 6."
    )
