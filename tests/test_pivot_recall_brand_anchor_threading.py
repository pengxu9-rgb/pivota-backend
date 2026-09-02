"""Recall must boost the brand the caller resolved, not re-derive it.

`_fetch_canonical_search_rows` gives brand-matching rows a +180 score so a real
brand match is not truncated behind a large set of same-category rows. It derived
that anchor from `_category_brand_anchor_terms`, which needs >= 2 residual tokens
— so a SINGLE-WORD brand (Murad, CeraVe, NARS) was never boosted.

The gateway's post-filter can only keep what recall already returned. Measured in
prod on 2026-09-02, after the post-filter alone was fixed: "show me Murad products"
resolved `brand_category_anchor_terms: ["murad"]` and still reported
`brand_category_anchor_matched: false` with a LIZUSH bath bomb as the only result —
no Murad row survived recall to anchor onto.

The caller now resolves once, against the catalog brand dictionary, and threads the
answer down so both halves agree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.pivot_query_service as svc  # noqa: E402


@pytest.fixture
def captured_sql(monkeypatch: pytest.MonkeyPatch):
    """Capture the recall SQL + params instead of hitting Postgres."""
    seen: dict = {}

    class _FakeDb:
        async def fetch_all(self, sql, params=None):
            seen["sql"] = str(sql)
            seen["params"] = dict(params or {})
            return []

    monkeypatch.setattr(svc, "database", _FakeDb())
    return seen


@pytest.mark.asyncio
async def test_a_caller_supplied_anchor_is_what_recall_boosts(captured_sql):
    # "murad" alone can never come from the residual heuristic...
    assert svc._category_brand_anchor_terms("show me Murad products") == []
    await svc._fetch_canonical_search_rows(
        query="show me Murad products",
        merchant_id=None,
        limit=20,
        brand_anchor_terms=["murad"],
    )
    # ...but the caller's anchor reaches the scoring SQL as a bound parameter.
    assert captured_sql["params"].get("brand_anchor_0") == "%murad%"
    assert "brand_anchor_0" in captured_sql["sql"]


@pytest.mark.asyncio
async def test_no_opinion_from_the_caller_preserves_the_old_behaviour(captured_sql):
    """None means "decide it yourself" — every other caller is unchanged."""
    await svc._fetch_canonical_search_rows(
        query="knight unicorn blush", merchant_id=None, limit=20
    )
    assert captured_sql["params"].get("brand_anchor_0") == "%knight%"
    assert captured_sql["params"].get("brand_anchor_1") == "%unicorn%"


@pytest.mark.asyncio
async def test_an_empty_caller_anchor_is_not_a_missing_one(captured_sql):
    """[] is a decision — "this query has no brand" — and must NOT silently fall
    back to the residual heuristic, or the two halves disagree again."""
    await svc._fetch_canonical_search_rows(
        query="knight unicorn blush",
        merchant_id=None,
        limit=20,
        brand_anchor_terms=[],
    )
    assert "brand_anchor_0" not in captured_sql["params"]


@pytest.mark.asyncio
async def test_a_descriptor_still_never_boosts(captured_sql):
    await svc._fetch_canonical_search_rows(
        query="brightening blush", merchant_id=None, limit=20
    )
    assert "brand_anchor_0" not in captured_sql["params"]


def test_the_gateway_resolves_the_anchor_BEFORE_the_search() -> None:
    """The delivery line, and the ordering is the whole point.

    Resolving after the search is what the previous fix did, and it left recall
    unboosted. Pinned by source because the handler needs a live catalog to invoke.
    """
    import routes.agent_shop_gateway as gw

    source = Path(gw.__file__).read_text(encoding="utf-8")
    resolve_at = source.index("brand_anchor_terms, brand_anchor_source = await _resolve_brand_anchor_terms(query)")
    search_at = source.index("pivot_result = await search_pivot_catalog(")
    assert resolve_at < search_at, "the anchor must be resolved before the search that needs it"
    assert "brand_anchor_terms=brand_anchor_terms or None," in source, (
        "the resolved anchor must be threaded into the recall request"
    )
