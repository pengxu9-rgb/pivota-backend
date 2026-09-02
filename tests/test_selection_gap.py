"""C1 — the selection gap: products you sell x queries you lose.

The output is a LIST of named won/lost queries, never a rate: at temperature 0
every neutral unbranded query resolved 3/3 or 0/3, so there is no meaningful
fraction to report and a fraction is not actionable anyway.

The failure mode these tests exist to hold shut is the FALSE POSITIVE — naming a
product for a query the merchant has nothing for is a fabricated finding on a
merchant-facing surface, and much worse than a miss. So every "does match" case
below is paired with a "must not match" one.
"""

from unittest.mock import AsyncMock, patch

import pytest

from services.selection_gap import (
    SELECTION_GAP_VERSION,
    build_catalog_index,
    build_selection_gap,
    lost_queries_from_reports,
    match_products_for_query,
    per_prompt_evidence,
    won_queries_from_reports,
)

_MOD = "services.agent_center_bd_report_service"


def _catalog():
    """A small Anua-shaped catalog: the real 2026-09-01 cohort products.

    Titles LEAD with the brand, as merchant catalog titles overwhelmingly do —
    that is what makes the brand-word exclusion load-bearing rather than
    incidental.
    """
    return [
        {
            "product_key": "anua-niacinamide-serum",
            "title": "Anua Niacinamide 10 TXA 4 Serum",
            "brand": "Anua",
            "product_type": "Serum",
            "tags": ["brightening", "dark-spots"],
        },
        {
            "product_key": "anua-heartleaf-toner",
            "title": "Anua Heartleaf 77% Soothing Toner",
            "brand": "Anua",
            "product_type": "Toner",
            "tags": ["bha", "exfoliating"],
        },
        {
            "product_key": "anua-ceramide-cream",
            "title": "Anua Azelaic Acid 10 Ceramide Cream",
            "brand": "Anua",
            "product_type": "Cream",
            "tags": ["barrier"],
        },
    ]


def _failing(query, providers=("gemini", "openai")):
    """A `_failing_prompts` entry, in the shape that function emits."""
    return {
        "query": query,
        "axis": "category",
        "reason": "no first-party or correct-SKU grounded citation",
        "provider": providers[0] if providers else None,
        "providers": list(providers),
        "grounding_sources": [],
        "competitors_named": ["Beauty of Joseon"],
    }


def _per_prompt(query, *, grounded=3, sku_cited=0):
    return {
        "query": query,
        "normalized_query": query.lower(),
        "provider_verdicts": {"gemini": "loss", "openai": "loss"},
        "source_summary": {
            "runs_with_citations": grounded,
            "merchant_cited_runs": sku_cited,
            "sku_cited_runs": sku_cited,
        },
    }


def _report(*, failing=(), per_prompt=()):
    return {
        "failing_prompts": list(failing),
        "opportunity": {"per_prompt": list(per_prompt)},
    }


# ---------------------------------------------------------------------------
# the matching rule
# ---------------------------------------------------------------------------


def test_matches_the_product_that_answers_the_query():
    """The headline finding: Anua sells a niacinamide serum and is named in 0 of
    3 responses for "best affordable niacinamide serum". The query noise ("best",
    "affordable") and the product noise ("10", "TXA", "4") must both fall away."""
    index = build_catalog_index(_catalog(), merchant_name="Anua")
    hits = match_products_for_query("best affordable niacinamide serum", index)
    assert [h["product_key"] for h in hits] == ["anua-niacinamide-serum"]
    assert hits[0]["matched_form"] == "serum"
    assert hits[0]["matched_terms"] == ["niacinamide"]
    # Every match carries WHY it matched.
    assert "niacinamide" in hits[0]["match_reason"]
    assert "Anua Niacinamide 10 TXA 4 Serum" in hits[0]["match_reason"]


def test_form_word_alone_is_not_a_match():
    """The near-miss that must NOT match: "best retinol serum" shares the FORM
    word "serum" with the niacinamide serum and nothing else. Matching on it
    would tell the merchant they sell a retinol serum they do not stock — a
    fabricated finding. It must produce no product, and therefore no gap."""
    index = build_catalog_index(_catalog(), merchant_name="Anua")
    assert match_products_for_query("best retinol serum", index) == []
    # Positive counterpart in the same rule: the SAME form word DOES match once
    # a distinguishing term the product actually carries is also named.
    assert match_products_for_query("best niacinamide serum", index)


def test_distinguishing_word_alone_is_not_a_match():
    """"niacinamide products for beginners" names an ingredient the merchant
    carries but no form they sell — nothing says WHICH product answers it."""
    index = build_catalog_index(_catalog(), merchant_name="Anua")
    assert match_products_for_query("niacinamide products for beginners", index) == []
    # Positive counterpart: add the form and the same ingredient now matches.
    assert match_products_for_query("niacinamide serum for beginners", index)


def test_brand_word_is_never_a_distinguishing_term():
    """A branded query ("best anua serum") must not become a gap on the strength
    of the brand word. "Anua" is in every one of their titles, so matching on it
    would say nothing about WHICH product answers the query — and would turn
    every branded query into a gap against every product they sell."""
    index = build_catalog_index(_catalog(), merchant_name="Anua")
    # The brand word is stripped out of the distinguishing vocabulary...
    for entry in index:
        assert "anua" not in entry["distinctive"]
    # ...so a brand + form query has no distinguishing term left and cannot match.
    assert match_products_for_query("best anua serum", index) == []
    # Positive counterpart: the brand word alongside a real distinguishing term
    # still matches — the brand is ignored, not poisonous.
    assert match_products_for_query("best anua niacinamide serum", index)


def test_brand_word_is_stripped_from_the_brand_column_alone():
    """Merchants whose titles omit the brand still get it excluded — the `brand`
    column and the merchant name both feed the brand vocabulary."""
    rows = [{
        "product_key": "p1",
        "title": "Heartleaf Soothing Toner",
        "brand": "Heartleaf Labs",
        "product_type": "Toner",
        "tags": [],
    }]
    index = build_catalog_index(rows, merchant_name="Heartleaf Labs")
    assert "heartleaf" not in index[0]["distinctive"]
    assert match_products_for_query("best heartleaf toner", index) == []
    # Positive counterpart: a non-brand term on the same product still matches.
    assert match_products_for_query("best soothing toner", index)


def test_terms_match_is_exact_after_singularization_and_nothing_fuzzier():
    """Plurals bridge; different actives never do. "retinol"/"retinal" are
    DIFFERENT ingredients — any similarity threshold loose enough to bridge a
    typo also collapses those two, which is exactly the fabricated finding this
    module exists to avoid."""
    from services.selection_gap import _terms_match

    assert _terms_match("serums", "serum")
    assert _terms_match("toner", "toners")
    assert not _terms_match("retinol", "retinal")
    assert not _terms_match("niacinamide", "niacinimide")
    assert not _terms_match("cleanser", "cleansing")


def test_marketing_and_packaging_words_never_become_distinguishing_terms():
    """Merchant titles carry marketing and packaging noise ("Best Seller",
    "Value Set", "50ml"). If those entered the distinguishing vocabulary, the
    query filler "best" would match them and EVERY "best <form>" query would
    become a gap against this product — the exact false positive the module
    exists to prevent."""
    rows = [{
        "product_key": "p1",
        "title": "Anua Heartleaf Best Seller Toner Value Set 50ml 6 Pieces",
        "brand": "Anua",
        "product_type": "Toner",
        "tags": ["kit", "travel", "150ml", "per count"],
    }]
    index = build_catalog_index(rows, merchant_name="Anua")
    assert index[0]["distinctive"] == ["heartleaf"]
    assert match_products_for_query("best toner", index) == []
    assert match_products_for_query("toner value set", index) == []
    assert match_products_for_query("travel size toner 50ml", index) == []
    # Positive counterpart: the one real distinguishing word on the same product
    # still matches, so the noise filter is not simply blanking the row.
    assert match_products_for_query("best heartleaf toner", index)


def test_matching_is_case_and_plural_insensitive():
    index = build_catalog_index(_catalog(), merchant_name="Anua")
    hits = match_products_for_query("BEST NIACINAMIDE SERUMS", index)
    assert [h["product_key"] for h in hits] == ["anua-niacinamide-serum"]


def test_form_vocabulary_comes_from_the_catalog_not_a_hardcoded_list():
    """A merchant in a vertical with no skincare words at all still gets form
    words — they are read off their own product_type column and the word their
    titles end on."""
    rows = [{
        "product_key": "d1",
        "title": "Mavic Air 3 Obstacle Avoidance Drone",
        "brand": "DJI",
        "product_type": "Drone",
        "tags": ["4k"],
    }]
    index = build_catalog_index(rows, merchant_name="DJI")
    assert "drone" in index[0]["forms"]
    assert match_products_for_query("best obstacle avoidance drone", index)
    # And the same conservatism holds off-vocabulary.
    assert match_products_for_query("best waterproof drone", index) == []


def test_title_final_word_supplies_the_form_when_product_type_is_coarse():
    """Shopify product types are often a coarse "Skincare". The form is then
    recovered from the word the title ENDS on."""
    rows = [{
        "product_key": "p1",
        "title": "Anua Niacinamide 10 TXA 4 Serum",
        "brand": "Anua",
        "product_type": "Skincare",
        "tags": [],
    }]
    index = build_catalog_index(rows, merchant_name="Anua")
    assert "serum" in index[0]["forms"]
    assert match_products_for_query("best niacinamide serum", index)


def test_ranks_by_distinguishing_term_count_and_is_deterministic():
    rows = _catalog() + [{
        "product_key": "anua-second-toner",
        "title": "Anua Heartleaf Pore Control Toner",
        "brand": "Anua",
        "product_type": "Toner",
        "tags": ["pore"],
    }]
    index = build_catalog_index(rows, merchant_name="Anua")
    first = match_products_for_query("best heartleaf soothing toner", index)
    second = match_products_for_query("best heartleaf soothing toner", index)
    assert first == second  # deterministic — no LLM, no set ordering leak
    assert first[0]["product_key"] == "anua-heartleaf-toner"  # 2 terms beats 1
    assert [h["product_key"] for h in first] == [
        "anua-heartleaf-toner",
        "anua-second-toner",
    ]


# ---------------------------------------------------------------------------
# the section
# ---------------------------------------------------------------------------


def test_lost_query_with_no_product_yields_no_gap():
    """The false-positive guard at section level: a lost query the merchant has
    nothing for must NOT appear in `gaps`. It is reported honestly on the
    `lost_queries_without_product` list instead — a real measured loss with no
    product claim attached to it."""
    out = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=[
            _failing("best affordable niacinamide serum"),
            _failing("best vitamin c serum"),
            _failing("best hair growth shampoo"),
        ],
        merchant_name="Anua",
    )
    assert [g["query"] for g in out["gaps"]] == ["best affordable niacinamide serum"]
    assert [r["query"] for r in out["lost_queries_without_product"]] == [
        "best hair growth shampoo",
        "best vitamin c serum",
    ]
    for row in out["lost_queries_without_product"]:
        assert "matched_products" not in row


def test_gap_carries_the_products_and_the_observed_evidence():
    lost = _failing("best affordable niacinamide serum")
    out = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=[lost],
        query_evidence=per_prompt_evidence([
            _report(per_prompt=[_per_prompt("best affordable niacinamide serum")])
        ]),
        merchant_name="Anua",
    )
    gap = out["gaps"][0]
    assert gap["query"] == "best affordable niacinamide serum"
    assert gap["matched_products"][0]["product_key"] == "anua-niacinamide-serum"
    assert gap["matched_products"][0]["title"] == "Anua Niacinamide 10 TXA 4 Serum"
    assert gap["matched_products"][0]["match_reason"]
    # Observed evidence: 3 grounded responses, 0 of them cited the merchant.
    assert gap["evidence"]["grounded_responses"] == 3
    assert gap["evidence"]["responses_citing_your_product"] == 0
    assert gap["evidence"]["engines"] == ["gemini", "openai"]


def test_won_and_lost_land_on_their_own_sides():
    reports = [
        _report(
            failing=[_failing("best affordable niacinamide serum")],
            per_prompt=[
                _per_prompt("best affordable niacinamide serum", sku_cited=0),
                _per_prompt("best heartleaf toner", grounded=3, sku_cited=3),
            ],
        )
    ]
    out = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=lost_queries_from_reports(reports),
        won_queries=won_queries_from_reports(reports),
        query_evidence=per_prompt_evidence(reports),
        merchant_name="Anua",
    )
    lost_names = [g["query"] for g in out["gaps"]] + [
        r["query"] for r in out["lost_queries_without_product"]
    ]
    won_names = [w["query"] for w in out["won_queries"]]
    assert lost_names == ["best affordable niacinamide serum"]
    assert won_names == ["best heartleaf toner"]
    # Neither side leaks into the other.
    assert "best heartleaf toner" not in lost_names
    assert "best affordable niacinamide serum" not in won_names
    # And the won side carries its own evidence: 3 of 3 responses cited them.
    assert out["won_queries"][0]["evidence"]["responses_citing_your_product"] == 3


def test_a_query_some_sku_won_is_never_reported_as_a_gap():
    """Cross-SKU safety: SKU A failed the query but SKU B was cited for it.
    Telling the merchant they lose a query they win is the same fabrication
    class as a false product match, so the won side takes precedence."""
    reports = [
        _report(failing=[_failing("best niacinamide serum")]),
        _report(per_prompt=[_per_prompt("best niacinamide serum", sku_cited=2)]),
    ]
    out = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=lost_queries_from_reports(reports),
        won_queries=won_queries_from_reports(reports),
        query_evidence=per_prompt_evidence(reports),
        merchant_name="Anua",
    )
    assert out["gaps"] == []
    assert out["lost_queries_without_product"] == []
    assert [w["query"] for w in out["won_queries"]] == ["best niacinamide serum"]
    # Positive counterpart: with no winning SKU the SAME query IS a gap.
    lost_only = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=lost_queries_from_reports([reports[0]]),
        merchant_name="Anua",
    )
    assert [g["query"] for g in lost_only["gaps"]] == ["best niacinamide serum"]


def test_output_is_a_list_of_named_queries_not_a_rate():
    reports = [
        _report(
            failing=[
                _failing("best affordable niacinamide serum"),
                _failing("best bha exfoliating toner"),
                _failing("best hair growth shampoo"),
            ],
            per_prompt=[
                _per_prompt("best affordable niacinamide serum"),
                _per_prompt("best bha exfoliating toner"),
                _per_prompt("best hair growth shampoo"),
                _per_prompt("best ceramide cream", sku_cited=3),
            ],
        )
    ]
    out = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=lost_queries_from_reports(reports),
        won_queries=won_queries_from_reports(reports),
        query_evidence=per_prompt_evidence(reports),
        merchant_name="Anua",
    )
    # The queries are named, verbatim.
    assert [g["query"] for g in out["gaps"]] == [
        "best affordable niacinamide serum",
        "best bha exfoliating toner",
    ]
    assert [w["query"] for w in out["won_queries"]] == ["best ceramide cream"]

    # And nothing anywhere in the payload is a rate/percentage/score.
    banned = ("rate", "percent", "pct", "ratio", "share", "score", "coverage")

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                assert not any(b in str(key).lower() for b in banned), (
                    f"{path}.{key} looks like a rate"
                )
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
        else:
            assert not isinstance(node, float), f"{path} is a float ({node!r})"

    walk(out)


def test_output_carries_the_version():
    out = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=[_failing("best niacinamide serum")],
        merchant_name="Anua",
    )
    assert "version" in out
    assert out["version"] == SELECTION_GAP_VERSION
    assert isinstance(out["version"], int) and out["version"] >= 1


def test_counts_are_absolute_not_fractions():
    out = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=[
            _failing("best niacinamide serum"),
            _failing("best hair growth shampoo"),
        ],
        merchant_name="Anua",
    )
    assert out["counts"] == {
        "catalog_products_indexed": 3,
        "lost_queries": 2,
        "lost_queries_with_matched_product": 1,
        "won_queries": 0,
    }


def test_empty_inputs_are_unavailable_not_a_zero_gap_list():
    out = build_selection_gap(catalog_rows=[], lost_queries=[], merchant_name="Anua")
    assert out["available"] is False
    assert out["gaps"] == []
    assert out["version"] == SELECTION_GAP_VERSION
    # Positive counterpart: real inputs flip `available`.
    live = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=[_failing("best niacinamide serum")],
        merchant_name="Anua",
    )
    assert live["available"] is True


# ---------------------------------------------------------------------------
# adapters over the existing report shapes
# ---------------------------------------------------------------------------


def test_lost_queries_are_consumed_from_failing_prompts_and_deduped():
    """`_failing_prompts` already emits one entry per UNIQUE failing query per
    SKU; across SKUs the same query recurs. Union + dedupe, engines merged."""
    reports = [
        _report(failing=[_failing("best niacinamide serum", providers=("gemini",))]),
        _report(failing=[_failing("best niacinamide serum", providers=("openai",))]),
        _report(failing=[_failing("best bha toner", providers=("gemini",))]),
    ]
    rows = lost_queries_from_reports(reports)
    assert [r["query"] for r in rows] == ["best niacinamide serum", "best bha toner"]
    assert rows[0]["engines"] == ["gemini", "openai"]


def test_won_queries_need_an_actual_citation():
    reports = [
        _report(per_prompt=[
            _per_prompt("cited query", sku_cited=1),
            _per_prompt("grounded but uncited query", grounded=3, sku_cited=0),
        ])
    ]
    assert [r["query"] for r in won_queries_from_reports(reports)] == ["cited query"]


def test_malformed_report_rows_are_skipped_not_fatal():
    reports = [None, {"failing_prompts": [None, {"query": ""}]}, _report()]
    assert lost_queries_from_reports(reports) == []
    assert won_queries_from_reports(reports) == []
    # Positive counterpart: a well-formed sibling in the SAME list still lands.
    reports.append(_report(failing=[_failing("best niacinamide serum")]))
    assert [r["query"] for r in lost_queries_from_reports(reports)] == [
        "best niacinamide serum"
    ]


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_section_is_none_without_a_catalog():
    """No catalog => no section. A gap list is only honest when we know what the
    merchant sells; guessing is the fabrication this whole module avoids."""
    from services.agent_center_bd_report_service import _selection_gap_section

    with patch(f"{_MOD}._fetch_all_dicts", new=AsyncMock(return_value=[])):
        assert await _selection_gap_section("m1", [_report()]) is None


@pytest.mark.asyncio
async def test_section_builds_from_the_catalog_and_the_per_sku_reports():
    from services.agent_center_bd_report_service import _selection_gap_section

    reports = [
        _report(
            failing=[_failing("best affordable niacinamide serum")],
            per_prompt=[
                _per_prompt("best affordable niacinamide serum"),
                _per_prompt("best ceramide cream", sku_cited=3),
            ],
        )
    ]
    with patch(f"{_MOD}._fetch_all_dicts", new=AsyncMock(return_value=_catalog())):
        out = await _selection_gap_section("m1", reports, merchant_name="Anua")
    assert out is not None
    assert out["version"] == SELECTION_GAP_VERSION
    assert [g["query"] for g in out["gaps"]] == ["best affordable niacinamide serum"]
    assert out["gaps"][0]["matched_products"][0]["product_key"] == (
        "anua-niacinamide-serum"
    )
    assert [w["query"] for w in out["won_queries"]] == ["best ceramide cream"]


@pytest.mark.asyncio
async def test_section_reads_only_live_rows_for_the_merchant():
    from services.agent_center_bd_report_service import _selection_gap_section

    fetch = AsyncMock(return_value=_catalog())
    with patch(f"{_MOD}._fetch_all_dicts", new=fetch):
        await _selection_gap_section("m1", [_report()])
    sql, values = fetch.await_args.args
    assert "catalog_products" in sql
    assert "sync_status = 'live'" in sql
    assert values["merchant_id"] == "m1"


@pytest.mark.asyncio
async def test_section_does_not_query_without_a_merchant_id():
    from services.agent_center_bd_report_service import _selection_gap_section

    fetch = AsyncMock(return_value=_catalog())
    with patch(f"{_MOD}._fetch_all_dicts", new=fetch):
        assert await _selection_gap_section("", [_report()]) is None
    fetch.assert_not_awaited()


def test_version_is_not_part_of_the_audit_basis():
    """SELECTION_GAP_VERSION is a READ-TIME interpretation stamp, not a record of
    what the run was measured with. It must never reach `audit_basis` — bumping
    it must not make two runs look like they were probed differently."""
    import db.audit_basis as audit_basis
    import services.selection_gap as selection_gap

    source = open(audit_basis.__file__).read()
    assert "selection_gap" not in source.lower()
    assert "SELECTION_GAP_VERSION" not in source
    # Positive counterpart: the constant IS defined, and IS in the output.
    assert selection_gap.SELECTION_GAP_VERSION >= 1
    out = build_selection_gap(
        catalog_rows=_catalog(),
        lost_queries=[_failing("best niacinamide serum")],
        merchant_name="Anua",
    )
    assert out["version"] == selection_gap.SELECTION_GAP_VERSION


def test_no_llm_dependency():
    """Deterministic by construction — a fabricated gap is the failure mode."""
    import services.selection_gap as selection_gap

    source = open(selection_gap.__file__).read()
    assert "llm_match" not in source.replace(
        "services/pdp_matcher/llm_match.py", ""
    ), "selection_gap must not call the LLM matcher"
    assert "import" in source


# --- one product per size family ------------------------------------------
def test_the_same_product_at_two_sizes_is_listed_once():
    """Measured on the real Anua catalogue 2026-09-02: 8 products are carried at
    two sizes ("... Serum" and "... Serum (10ml)"). Listing both makes one gap
    read as two products the merchant already sells."""
    from services.selection_gap import build_catalog_index, match_products_for_query

    rows = [
        {"product_key": "a", "title": "Niacinamide 10 TXA 4 Serum",
         "brand": "Anua", "product_type": "Serum", "tags": []},
        {"product_key": "b", "title": "Niacinamide 10 TXA 4 Serum (10ml)",
         "brand": "Anua", "product_type": "Serum", "tags": []},
    ]
    matched = match_products_for_query(
        "best affordable niacinamide serum", build_catalog_index(rows)
    )
    assert len(matched) == 1, [m["title"] for m in matched]


def test_two_genuinely_different_products_are_both_listed():
    """Positive counterpart: de-duplication must not collapse distinct products
    that happen to share a form and a term."""
    from services.selection_gap import build_catalog_index, match_products_for_query

    rows = [
        {"product_key": "a", "title": "Niacinamide 10 TXA 4 Serum",
         "brand": "Anua", "product_type": "Serum", "tags": []},
        {"product_key": "b", "title": "Niacinamide Brightening Booster Serum",
         "brand": "Anua", "product_type": "Serum", "tags": []},
    ]
    matched = match_products_for_query(
        "best affordable niacinamide serum", build_catalog_index(rows)
    )
    assert len(matched) == 2


def test_a_bracketed_product_distinction_is_not_collapsed():
    """Only a trailing MEASUREMENT is stripped — "(Multichrome)" is a real
    product difference and must survive."""
    from services.selection_gap import _size_family

    assert _size_family("X Serum (30ml)") == _size_family("X Serum")
    assert _size_family("X Serum (Multichrome)") != _size_family("X Serum")
