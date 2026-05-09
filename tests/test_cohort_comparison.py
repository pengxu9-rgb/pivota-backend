"""PR-2b: cohort comparison helper tests.

Pure-function coverage of services/cohort_comparison:
  - extract_per_query_breakdown — handles missing fields, redirector
    URLs, multiple products
  - extract_brand_mentions — sums times_cited, lowercase keys
  - build_brand_mention_matrix — cross-cuts N audits, sorts by total
  - build_cohort_comparison — end-to-end joining parent + cohort
"""

from __future__ import annotations

from typing import Any, Dict, List

from services.cohort_comparison import (
    build_brand_mention_matrix,
    build_cohort_comparison,
    extract_brand_mentions,
    extract_per_query_breakdown,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_audit_report(
    *,
    merchant_name: str,
    products_with_categories: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a minimal report_jsonb shape matching what the audit
    pipeline produces. Each product entry: {title, queries: [...],
    competitor_brands: [...]}."""
    per_product = []
    for p in products_with_categories:
        per_product.append({
            "product": {"title": p["title"]},
            "category_visibility": {
                "queries": p.get("queries") or [],
                "match_details": p.get("match_details") or [],
                "competitor_brands": p.get("competitor_brands") or [],
            },
        })
    return {
        "merchant_name": merchant_name,
        "per_product": per_product,
    }


# ---------------------------------------------------------------------------
# extract_per_query_breakdown
# ---------------------------------------------------------------------------


def test_extract_per_query_breakdown_basic():
    report = _make_audit_report(
        merchant_name="Grüns",
        products_with_categories=[{
            "title": "Daily Gummies",
            "queries": [
                {"query": "best gummy vitamins", "self_report_yes": True,
                 "top_cited_url": "https://healthline.com/x", "cited_urls_count": 3},
                {"query": "top vitamins for kids", "self_report_yes": False,
                 "top_cited_url": None, "cited_urls_count": 0},
            ],
            "match_details": [
                {"query": "best gummy vitamins", "matched": False, "in_grounding": False},
                {"query": "top vitamins for kids", "matched": True, "in_grounding": True},
            ],
        }],
    )
    out = extract_per_query_breakdown(report, brand_label="Grüns")
    assert len(out) == 2
    assert out[0]["brand"] == "Grüns"
    assert out[0]["query"] == "best gummy vitamins"
    assert out[0]["top_cited_url"] == "https://healthline.com/x"
    assert out[0]["matched_in_grounding"] is False
    assert out[1]["matched_in_grounding"] is True


def test_extract_per_query_breakdown_strips_vertex_redirector():
    """Vertex AI redirector URLs hide the actual host — surface
    `top_cited_url=None` + `top_cited_url_was_redirector=True` so
    the renderer can fall back to cited_urls_count."""
    redirector = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
    report = _make_audit_report(
        merchant_name="Test",
        products_with_categories=[{
            "title": "X",
            "queries": [{"query": "q1", "top_cited_url": redirector, "cited_urls_count": 1}],
            "match_details": [{"query": "q1", "matched": False}],
        }],
    )
    out = extract_per_query_breakdown(report, brand_label="Test")
    assert out[0]["top_cited_url"] is None
    assert out[0]["top_cited_url_was_redirector"] is True
    assert out[0]["cited_urls_count"] == 1


def test_extract_per_query_breakdown_handles_missing_match_details():
    """category_visibility is sometimes None (product_type missing)
    or missing match_details — don't crash."""
    report = _make_audit_report(
        merchant_name="Test",
        products_with_categories=[{
            "title": "X",
            "queries": [{"query": "q1", "top_cited_url": "https://x.com", "cited_urls_count": 1}],
            "match_details": [],  # missing
        }],
    )
    out = extract_per_query_breakdown(report, brand_label="Test")
    assert out[0]["matched_in_grounding"] is False
    assert out[0]["query"] == "q1"


def test_extract_per_query_breakdown_returns_empty_for_no_per_product():
    assert extract_per_query_breakdown({"per_product": []}, brand_label="X") == []
    assert extract_per_query_breakdown({}, brand_label="X") == []
    assert extract_per_query_breakdown(None, brand_label="X") == []


def test_extract_per_query_breakdown_multiple_products():
    report = _make_audit_report(
        merchant_name="Test",
        products_with_categories=[
            {"title": "P1", "queries": [{"query": "q1"}], "match_details": []},
            {"title": "P2", "queries": [{"query": "q2"}], "match_details": []},
        ],
    )
    out = extract_per_query_breakdown(report, brand_label="Test")
    assert len(out) == 2
    titles = [r["product_title"] for r in out]
    assert "P1" in titles and "P2" in titles


# ---------------------------------------------------------------------------
# extract_brand_mentions
# ---------------------------------------------------------------------------


def test_extract_brand_mentions_sums_across_products():
    report = _make_audit_report(
        merchant_name="Grüns",
        products_with_categories=[
            {"title": "P1", "competitor_brands": [
                {"name": "SmartyPants", "times_cited": 3},
                {"name": "Centrum", "times_cited": 1},
            ]},
            {"title": "P2", "competitor_brands": [
                {"name": "SmartyPants", "times_cited": 2},
                {"name": "Olly", "times_cited": 1},
            ]},
        ],
    )
    out = extract_brand_mentions(report)
    assert out["smartypants"] == 5
    assert out["centrum"] == 1
    assert out["olly"] == 1


def test_extract_brand_mentions_lowercase_keys():
    """Keys must be lowercase for cross-audit join (different audits
    may casing the same brand differently)."""
    report = _make_audit_report(
        merchant_name="Test",
        products_with_categories=[{
            "title": "P", "competitor_brands": [
                {"name": "SmartyPants", "times_cited": 1},
                {"name": "smartypants", "times_cited": 2},  # same brand, different casing
            ],
        }],
    )
    out = extract_brand_mentions(report)
    assert out["smartypants"] == 3  # merged


def test_extract_brand_mentions_skips_garbage():
    report = _make_audit_report(
        merchant_name="Test",
        products_with_categories=[{
            "title": "P", "competitor_brands": [
                {"name": "Valid", "times_cited": 1},
                {"name": "", "times_cited": 5},        # empty name
                {"times_cited": 3},                    # missing name
                "garbage string",                       # not dict
                {"name": "Untyped", "times_cited": "not a number"},  # bad cited
            ],
        }],
    )
    out = extract_brand_mentions(report)
    assert out["valid"] == 1
    assert out.get("untyped") == 0  # bad times_cited → 0 contribution
    assert "" not in out


def test_extract_brand_mentions_handles_garbage_input():
    assert extract_brand_mentions(None) == {}
    assert extract_brand_mentions({}) == {}
    assert extract_brand_mentions("string") == {}


# ---------------------------------------------------------------------------
# build_brand_mention_matrix
# ---------------------------------------------------------------------------


def test_brand_mention_matrix_cross_cuts_audits():
    """Three audits, each citing different brand sets — matrix shows
    the union with per-audit counts."""
    audits = [
        {"label": "Grüns", "mentions": {"smartypants": 3, "centrum": 1}},
        {"label": "SmartyPants", "mentions": {"centrum": 4, "olly": 2}},
        {"label": "Centrum", "mentions": {"smartypants": 1, "olly": 1}},
    ]
    out = build_brand_mention_matrix(audits)
    assert out["audits"] == ["Grüns", "SmartyPants", "Centrum"]
    by_brand = {r["brand_lower"]: r for r in out["matrix"]}
    # SmartyPants: 3 (in Grüns audit) + 0 (in own) + 1 (in Centrum) = 4
    sp = by_brand["smartypants"]
    assert sp["total_mentions"] == 4
    assert sp["by_audit"] == {"Grüns": 3, "SmartyPants": 0, "Centrum": 1}
    assert sp["audit_count"] == 2  # mentioned in 2 of 3
    # Centrum: 1 (in Grüns) + 4 (in SmartyPants) + 0 (in own) = 5
    assert by_brand["centrum"]["total_mentions"] == 5
    # Olly: 0 + 2 + 1 = 3
    assert by_brand["olly"]["total_mentions"] == 3


def test_brand_mention_matrix_sorted_by_total_descending():
    audits = [{"label": "A", "mentions": {"low": 1, "high": 10, "mid": 5}}]
    out = build_brand_mention_matrix(audits)
    rows = out["matrix"]
    totals = [r["total_mentions"] for r in rows]
    assert totals == sorted(totals, reverse=True)


def test_brand_mention_matrix_empty_audits():
    out = build_brand_mention_matrix([])
    assert out["audits"] == []
    assert out["matrix"] == []


# ---------------------------------------------------------------------------
# build_cohort_comparison — end-to-end
# ---------------------------------------------------------------------------


def test_build_cohort_comparison_full_shape():
    parent = _make_audit_report(
        merchant_name="Grüns",
        products_with_categories=[{
            "title": "Daily Gummies",
            "queries": [{"query": "best gummy vitamins", "top_cited_url": "https://healthline.com", "cited_urls_count": 1}],
            "match_details": [{"query": "best gummy vitamins", "matched": False}],
            "competitor_brands": [
                {"name": "SmartyPants", "times_cited": 3},
                {"name": "Olly", "times_cited": 1},
            ],
        }],
    )
    competitor_report = _make_audit_report(
        merchant_name="SmartyPants",
        products_with_categories=[{
            "title": "Adult Multi",
            "queries": [{"query": "best multivitamins", "top_cited_url": "https://nymag.com", "cited_urls_count": 2}],
            "match_details": [{"query": "best multivitamins", "matched": True}],
            "competitor_brands": [
                {"name": "Centrum", "times_cited": 4},
                {"name": "Olly", "times_cited": 1},
            ],
        }],
    )
    cohort_runs = [
        {
            "competitor_brand": "SmartyPants",
            "status": "succeeded",
            "report_jsonb": competitor_report,
        },
        {
            "competitor_brand": "Failed Brand",
            "status": "failed",
            "report_jsonb": None,  # failed runs have no report
        },
    ]
    out = build_cohort_comparison(
        parent_report=parent,
        parent_label="Grüns",
        cohort_runs=cohort_runs,
    )
    # summary
    assert out["summary"]["parent_brand"] == "Grüns"
    assert out["summary"]["competitors_audited"] == 1  # only succeeded count
    assert out["summary"]["queries_total"] == 2  # 1 parent + 1 competitor query
    # per-query
    queries_by_brand = {r["brand"] for r in out["per_query_breakdown"]}
    assert queries_by_brand == {"Grüns", "SmartyPants"}
    # brand mention matrix
    matrix = out["brand_mention_matrix"]
    assert "Grüns" in matrix["audits"]
    assert "SmartyPants" in matrix["audits"]
    # SmartyPants was named in Grüns audit (3x), not in own audit → total 3
    sp = next((r for r in matrix["matrix"] if r["brand_lower"] == "smartypants"), None)
    assert sp is not None
    assert sp["total_mentions"] == 3
    # Centrum was only named in SmartyPants' audit (4x) → total 4
    centrum = next((r for r in matrix["matrix"] if r["brand_lower"] == "centrum"), None)
    assert centrum is not None
    assert centrum["total_mentions"] == 4
    # Olly was named in BOTH audits (1+1=2) → audit_count=2
    olly = next((r for r in matrix["matrix"] if r["brand_lower"] == "olly"), None)
    assert olly["total_mentions"] == 2
    assert olly["audit_count"] == 2
    # caveat surfaced
    assert "auto-generated" in out["caveat"].lower()


def test_build_cohort_comparison_handles_no_parent_report():
    """When parent_report is None (parent audit row missing), still
    returns a comparison from cohort runs alone."""
    cohort_report = _make_audit_report(
        merchant_name="C1",
        products_with_categories=[{
            "title": "X",
            "queries": [{"query": "q1"}],
            "match_details": [],
            "competitor_brands": [{"name": "A", "times_cited": 1}],
        }],
    )
    cohort_runs = [{
        "competitor_brand": "C1",
        "status": "succeeded",
        "report_jsonb": cohort_report,
    }]
    out = build_cohort_comparison(
        parent_report=None,
        parent_label="(parent unknown)",
        cohort_runs=cohort_runs,
    )
    assert out["summary"]["competitors_audited"] == 1
    assert out["summary"]["queries_total"] == 1


def test_build_cohort_comparison_no_succeeded_competitors():
    """All cohort runs failed — comparison still returns valid empty
    shape rather than crashing."""
    cohort_runs = [
        {"competitor_brand": "C1", "status": "failed", "report_jsonb": None},
        {"competitor_brand": "C2", "status": "failed", "report_jsonb": None},
    ]
    out = build_cohort_comparison(
        parent_report=_make_audit_report(merchant_name="P", products_with_categories=[]),
        parent_label="P",
        cohort_runs=cohort_runs,
    )
    assert out["summary"]["competitors_audited"] == 0
    assert out["per_query_breakdown"] == []
