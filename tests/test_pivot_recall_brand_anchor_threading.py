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
    # The boost must actually be worth something, and must reach the field that orders the page.
    assert "THEN 180" in captured_sql["sql"]


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
async def test_a_single_token_anchor_never_matches_the_TITLE(captured_sql):
    """The guards that approve a token are exact-span equality tests; the boost consumes the
    token as an UNANCHORED substring. For a 4-char brand those are different questions.

    `lush` is a real catalog brand, and `category_path_prefix_for_query` correctly refuses
    `blush` — then `%lush%` matches "Soft Pinch Liquid Blush", "Orgasm Powder Blush", "Baked
    Blush Luminoso". At +180 that outranks exact-title (100) plus title-LIKE (90), so a
    coincidental substring would lead the page. Identity fields only for a single token — which
    is also exactly what the gateway post-filter matches on.
    """
    await svc._fetch_canonical_search_rows(
        query="lush blush", merchant_id=None, limit=20, brand_anchor_terms=["lush"]
    )
    sql = captured_sql["sql"]
    anchor_clause = sql[sql.index("brand_anchor_0") - 400 : sql.index("brand_anchor_0") + 200]
    assert "p.brand" in anchor_clause
    assert "p.title" not in anchor_clause, "a single-token anchor must never match the title"


@pytest.mark.asyncio
async def test_a_multi_token_anchor_KEEPS_the_title_clause(captured_sql):
    """Unchanged behaviour. ANDed terms are far more selective, and the title is where a
    two-word brand survives a missing `brand` column."""
    await svc._fetch_canonical_search_rows(
        query="knight unicorn blush", merchant_id=None, limit=20
    )
    assert captured_sql["params"].get("brand_anchor_1") == "%unicorn%"
    # Scoped to the ANCHOR clause: `p.title` appears elsewhere in the scoring SQL, so an
    # unscoped `"p.title" in sql` passes even when the anchor drops it — it did, and the
    # over-correction mutant survived until this was narrowed.
    sql = captured_sql["sql"]
    anchor_clause = sql[sql.index("brand_anchor_0") - 400 : sql.index("brand_anchor_1") + 200]
    assert "p.title" in anchor_clause


@pytest.mark.asyncio
async def test_wildcards_and_junk_terms_are_refused(captured_sql):
    """`%` alone becomes LIKE '%%%' — true for every row — handing the whole candidate set +180
    and flattening the ranking. Reachable from POST /v1/pivot/query, which binds the model."""
    for junk in ["%", "_a_", "%murad%", "", "a b"]:
        captured_sql.clear()
        await svc._fetch_canonical_search_rows(
            query="anything", merchant_id=None, limit=20, brand_anchor_terms=[junk]
        )
        assert "brand_anchor_0" not in captured_sql["params"], f"{junk!r} must be refused"


@pytest.mark.asyncio
async def test_the_term_list_is_bounded(captured_sql):
    """Unbounded, 2000 terms produced a 719KB statement with 12k LIKE predicates over the
    catalog join — a statement-timeout shaped like this service's known pool incidents."""
    await svc._fetch_canonical_search_rows(
        query="x", merchant_id=None, limit=20, brand_anchor_terms=[f"brand{i}" for i in range(50)]
    )
    bound = [k for k in captured_sql["params"] if k.startswith("brand_anchor_")]
    assert len(bound) <= 8
    await svc._fetch_canonical_search_rows(
        query="x", merchant_id=None, limit=20, brand_anchor_terms=["a" * 5000]
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
    assert "brand_anchor_terms=brand_anchor_terms," in source, ( "the resolved anchor must be threaded into the recall request AS RESOLVED — `or None` turns [] (no brand) into None (derive it yourself), the opposite of the documented contract"
    )


# ---------------------------------------------------------------------------------------------
# ADMIT, don't merely re-rank. `brand_anchor_score` is a SCORE term — it never appears in the
# WHERE — so it reorders the candidate set but cannot admit a row the WHERE excluded. Measured in
# prod with the boost live (web-00278-tor): "I am looking for a Murad cleanser" returned 5 Murad
# products (because `cleanser` yields a category prefix and category recall admits them), while
# "show me Murad products" still returned one LIZUSH bath bomb — the phrase predicate looks for
# "%show me murad products%" and no Murad row was ever a candidate.


@pytest.mark.asyncio
async def test_a_brand_anchor_ADMITS_rows_not_just_boosts_them(captured_sql):
    await svc._fetch_canonical_search_rows(
        query="show me Murad products",
        merchant_id=None,
        limit=20,
        brand_anchor_terms=["murad"],
    )
    sql = captured_sql["sql"]
    # The admit predicate is bound and lives in the candidate WHERE, not only in the scoring.
    assert captured_sql["params"].get("brand_admit_0") == "% murad %"
    where = sql[sql.index("WHERE (") : sql.index("ORDER BY rank_score")]
    assert "brand_admit_0" in where, "the anchor must widen the candidate set, not only re-rank it"


@pytest.mark.asyncio
async def test_the_admit_branch_is_whole_word_and_identity_only(captured_sql):
    """It ADMITS rows, so a substring hit costs far more here than in the scoring: `%lush%` inside
    "Blush" would pull every blush in the catalog into the candidate window. Comparing against a
    space-padded field makes the match word-delimited with plain LIKE and a bound parameter."""
    await svc._fetch_canonical_search_rows(
        query="lush blush", merchant_id=None, limit=20, brand_anchor_terms=["lush"]
    )
    sql = captured_sql["sql"]
    assert captured_sql["params"]["brand_admit_0"] == "% lush %"
    admit = sql[sql.index("brand_admit_0") - 300 : sql.index("brand_admit_0") + 200]
    assert "p.brand" in admit and "m.merchant_name" in admit
    assert "p.title" not in admit, "admitting on title would flood the candidate window"


@pytest.mark.parametrize(
    "brand,term,admitted",
    [
        ("Murad", "murad", True),
        ("Murad Skin Care", "murad", True),   # the brand is one word among several
        ("LUSH", "lush", True),
        ("Blush", "lush", False),             # the substring hazard, refused
        ("Plush Beauty", "lush", False),
        ("Four Sigmatic", "sigma", False),
        ("Sigma Beauty", "sigma", True),
    ],
)
def test_the_word_boundary_property_the_admit_branch_relies_on(brand, term, admitted):
    """`(' ' || brand || ' ') LIKE '% term %'` in SQL, expressed here as the string property it is."""
    assert (f" {term} " in f" {brand.lower()} ") is admitted


@pytest.mark.asyncio
async def test_no_anchor_means_no_admit_branch(captured_sql):
    await svc._fetch_canonical_search_rows(
        query="hydrating cleanser", merchant_id=None, limit=20, brand_anchor_terms=[]
    )
    assert not [k for k in captured_sql["params"] if k.startswith("brand_admit_")]
