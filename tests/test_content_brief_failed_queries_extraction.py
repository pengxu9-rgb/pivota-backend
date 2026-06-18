"""Q-P1-7: _extract_failed_category_queries shape-defensive regression.

Pre-fix the function assumed `per_product[i].category_visibility` was
always a dict and crashed with AttributeError when production probes
emitted the legacy list shape:

  category_visibility: [
    {"query": "best serum under $50", "matched": False, ...},
    ...
  ]

Post-fix the function handles both shapes:
  - dict: {"queries": [...], "match_details": [...]}
  - list: [{"query": ..., "matched": ...}, ...]
"""

from __future__ import annotations

import json

from services.executor_agents.content_brief import (
    _derive_brand_from_audit,
    _extract_failed_category_queries,
)


# =========================================================================
# Dict shape (canonical)
# =========================================================================


def test_dict_shape_returns_failed_queries():
    """Canonical probe output: queries + match_details under cv dict."""
    audit_report = {
        "per_product": [{
            "category_visibility": {
                "queries": [
                    {"query": "best serum under $50", "cited_urls_count": 3},
                    {"query": "vitamin c face wash", "cited_urls_count": 0},
                ],
                "match_details": [
                    {"query": "best serum under $50", "matched": True},
                    {"query": "vitamin c face wash", "matched": False},
                ],
            },
        }],
    }
    out = _extract_failed_category_queries(audit_report, cap=10)
    assert out == ["vitamin c face wash"]


def test_dict_shape_no_match_details_falls_back_to_cited_urls_count():
    """When match_details is missing, fall back to cited_urls_count==0
    as the failure signal."""
    audit_report = {
        "per_product": [{
            "category_visibility": {
                "queries": [
                    {"query": "with grounding", "cited_urls_count": 5},
                    {"query": "no grounding", "cited_urls_count": 0},
                ],
                # match_details intentionally absent
            },
        }],
    }
    out = _extract_failed_category_queries(audit_report, cap=10)
    assert out == ["no grounding"]


def test_dict_shape_dedupes_across_products():
    """Same failed query across two products → one entry."""
    audit_report = {
        "per_product": [
            {"category_visibility": {
                "queries": [{"query": "shared failure", "cited_urls_count": 0}],
            }},
            {"category_visibility": {
                "queries": [{"query": "shared failure", "cited_urls_count": 0}],
            }},
        ],
    }
    assert _extract_failed_category_queries(audit_report, cap=10) == ["shared failure"]


def test_dict_shape_respects_cap():
    """Cap caps results, processing in walk order."""
    audit_report = {
        "per_product": [{
            "category_visibility": {
                "queries": [
                    {"query": f"q{i}", "cited_urls_count": 0}
                    for i in range(10)
                ],
            },
        }],
    }
    out = _extract_failed_category_queries(audit_report, cap=3)
    assert out == ["q0", "q1", "q2"]


# =========================================================================
# List shape (legacy / partial-probe — pre-fix crash case)
# =========================================================================


def test_list_shape_returns_failed_queries():
    """The bug-reproducer: category_visibility is a list, not a dict.
    Pre-fix this crashed with AttributeError; post-fix it surfaces
    the failed queries with the same semantics as the dict shape."""
    audit_report = {
        "per_product": [{
            "category_visibility": [
                {"query": "matched query", "matched": True},
                {"query": "missing query", "matched": False},
                {"query": "another missing", "matched": False},
            ],
        }],
    }
    out = _extract_failed_category_queries(audit_report, cap=10)
    assert out == ["missing query", "another missing"]


def test_list_shape_dedupes_within_product():
    """List shape duplicate failures collapse to one entry."""
    audit_report = {
        "per_product": [{
            "category_visibility": [
                {"query": "fail", "matched": False},
                {"query": "fail", "matched": False},  # dup
                {"query": "pass", "matched": True},
            ],
        }],
    }
    assert _extract_failed_category_queries(audit_report, cap=10) == ["fail"]


def test_list_shape_skips_non_dict_entries():
    """Mixed-junk list shape: strings/None mid-list don't crash;
    only dict entries with `query` get processed."""
    audit_report = {
        "per_product": [{
            "category_visibility": [
                "garbage string",
                None,
                {"query": "real fail", "matched": False},
                42,
            ],
        }],
    }
    assert _extract_failed_category_queries(audit_report, cap=10) == ["real fail"]


# =========================================================================
# Edge cases / null inputs
# =========================================================================


def test_none_audit_report_returns_empty():
    assert _extract_failed_category_queries(None, cap=10) == []


def test_non_dict_audit_report_returns_empty():
    assert _extract_failed_category_queries("garbage", cap=10) == []
    assert _extract_failed_category_queries([], cap=10) == []


def test_empty_per_product_returns_empty():
    assert _extract_failed_category_queries({"per_product": []}, cap=10) == []


def test_missing_category_visibility_returns_empty():
    audit_report = {"per_product": [{"verdict": {"label": "GOOD"}}]}
    assert _extract_failed_category_queries(audit_report, cap=10) == []


def test_category_visibility_unexpected_type_skips_product():
    """If category_visibility is neither dict nor list (e.g. string,
    int), skip the product rather than crashing."""
    audit_report = {"per_product": [{"category_visibility": "garbage"}]}
    assert _extract_failed_category_queries(audit_report, cap=10) == []


def test_no_failures_returns_empty():
    """All queries matched → empty result."""
    audit_report = {
        "per_product": [{
            "category_visibility": [
                {"query": "q1", "matched": True},
                {"query": "q2", "matched": True},
            ],
        }],
    }
    assert _extract_failed_category_queries(audit_report, cap=10) == []


# =========================================================================
# JSONB-as-string regression (the prod AttributeError class)
# =========================================================================
#
# asyncpg / the `databases` lib can hand back a JSONB column — or a JSONB
# value nested inside one — as a raw JSON *string* instead of a parsed
# dict/list when the JSONB codec isn't registered. Calling `.get()` on
# that string raised AttributeError("'str' object has no attribute 'get'")
# in the content-brief executor (FAILED rows in the merchant "Pivota Agent
# Activity" panel). After #869's shape guards the crash became a *silent*
# no-op: the string fell through every isinstance() check, so the merchant
# got zero briefs and the data was never read. The fix decodes JSON-string
# values at each JSONB boundary before .get()/iteration — mirroring
# db.merchant_audit_runs._decode_jsonb_field.
#
# The unit-test-masks-prod-bug pattern: pre-existing tests above all pass
# the report (and nested fields) as parsed dicts/lists, which is exactly
# what prod did NOT do. These tests feed them as JSON strings.


_BASE_REPORT = {
    "merchant_name": "Acme Co",
    "per_product": [{
        "category_visibility": {
            "queries": [
                {"query": "best serum under $50", "cited_urls_count": 3},
                {"query": "vitamin c face wash", "cited_urls_count": 0},
            ],
            "match_details": [
                {"query": "best serum under $50", "matched": True},
                {"query": "vitamin c face wash", "matched": False},
            ],
        },
    }],
}


def test_whole_report_as_json_string_is_decoded():
    """report_jsonb arrives as a JSON string → decode, don't crash or skip."""
    out = _extract_failed_category_queries(json.dumps(_BASE_REPORT), cap=10)
    assert out == ["vitamin c face wash"]


def test_per_product_as_json_string_is_decoded():
    """The per_product array nested as a JSON string is decoded."""
    report = {
        **_BASE_REPORT,
        "per_product": json.dumps(_BASE_REPORT["per_product"]),
    }
    assert _extract_failed_category_queries(report, cap=10) == [
        "vitamin c face wash",
    ]


def test_category_visibility_as_json_string_is_decoded():
    """The prime suspect: the nested per-SKU category_visibility JSONB
    arrives as a JSON string. Pre-fix this crashed (`str`.get); post-#869
    it silently yielded []; now it decodes."""
    cv = _BASE_REPORT["per_product"][0]["category_visibility"]
    report = {"per_product": [{"category_visibility": json.dumps(cv)}]}
    assert _extract_failed_category_queries(report, cap=10) == [
        "vitamin c face wash",
    ]


def test_queries_and_match_details_as_json_strings_are_decoded():
    """Even the innermost arrays can be string-encoded."""
    report = {
        "per_product": [{
            "category_visibility": {
                "queries": json.dumps([{"query": "q1", "cited_urls_count": 0}]),
                "match_details": json.dumps([{"query": "q1", "matched": False}]),
            },
        }],
    }
    assert _extract_failed_category_queries(report, cap=10) == ["q1"]


def test_list_shape_category_visibility_as_json_string_is_decoded():
    """Legacy list-shape category_visibility, string-encoded."""
    report = {
        "per_product": [{
            "category_visibility": json.dumps([
                {"query": "qx", "matched": False},
            ]),
        }],
    }
    assert _extract_failed_category_queries(report, cap=10) == ["qx"]


def test_non_json_string_audit_report_still_returns_empty():
    """A non-JSON sentinel string must NOT raise — it stays a string,
    fails the dict guard, and returns []. (Guards db invariants the
    existing test_non_dict_audit_report_returns_empty relies on.)"""
    assert _extract_failed_category_queries("garbage", cap=10) == []


def test_derive_brand_from_json_string_report():
    """Brand derivation also decodes a string report (and tolerates junk)."""
    assert _derive_brand_from_audit(json.dumps(_BASE_REPORT)) == "Acme Co"
    assert _derive_brand_from_audit("garbage") == "(brand)"
    assert _derive_brand_from_audit(None) == "(brand)"
