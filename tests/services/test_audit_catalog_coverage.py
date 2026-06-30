"""Tests for the Stage-1 audit -> Path-C candidate transform."""

from services.audit_catalog_coverage import audit_to_candidates


def _report(*name_groups):
    return {
        "authority_map": {
            "skus": [
                {"authority_hosts": [{"competitors_named": list(g)} for g in name_groups]}
            ]
        }
    }


def test_extracts_dedupes_and_stamps():
    report = _report(
        ["OLLY Collagen", "MERIT", "olly  collagen"],  # 3rd is a case/space dup of 1st
        ["Garden of Life Collagen"],
    )
    cands = audit_to_candidates(report, category_path="beauty/collagen")
    names = [c["product_name"] for c in cands]
    assert names == ["OLLY Collagen", "MERIT", "Garden of Life Collagen"]  # dup dropped, order kept
    assert all(c["category_path"] == "beauty/collagen" for c in cands)
    assert all(c["source"] == "audit_competitor_discovery" for c in cands)
    # competitor string feeds both brand + product_name; validator resolves the PDP
    assert cands[0]["brand"] == "OLLY Collagen" and cands[0]["product_name"] == "OLLY Collagen"


def test_expected_domains_hint_passthrough():
    cands = audit_to_candidates(
        _report(["Foo Serum"]),
        category_path="beauty",
        expected_url_domains=["foo.com", ""],  # blanks filtered
    )
    assert cands[0]["expected_url_domains"] == ["foo.com"]


def test_max_candidates_cap():
    report = _report([f"Brand {i}" for i in range(50)])
    assert len(audit_to_candidates(report, category_path="x", max_candidates=10)) == 10


def test_empty_and_malformed_safe():
    assert audit_to_candidates({}, category_path="x") == []
    assert audit_to_candidates({"authority_map": {}}, category_path="x") == []
    assert audit_to_candidates({"authority_map": {"skus": [None]}}, category_path="x") == []
    assert audit_to_candidates(None, category_path="x") == []
