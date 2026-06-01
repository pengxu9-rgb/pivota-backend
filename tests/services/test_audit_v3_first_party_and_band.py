"""Regression tests for two scoring/reporting bugs found in the 4th Ownist run.

1. first_party_rate = 0 despite the merchant's own site being a grounding source.
   Gemini grounding returns sources as Vertex redirector URIs
   (`vertexaisearch.cloud.google.com/grounding-api-redirect/...`) with the REAL
   publisher domain in `title`. Source matching read only the URI, so the
   merchant domain was never matched. Fix: `_source_urls` is now
   redirector-aware (reuses `_identify_run_sources`).

2. band/blocked_skus inconsistency. Per-SKU `band` is the band of the lowest
   dimension; `brand_rollup.blocked_skus` used a different ad-hoc rule
   (identity/routability < 40 OR citation == 0) that ignored content_richness,
   so band="blocked" SKUs were missing from blocked_skus. Fix: blocked_skus is
   now derived from `band == "blocked"`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import services.agent_center_bd_report_service as bd

MERCHANT_URL = "https://ownist.com/products/triple-shine-1-box"


def _redirector_run(query: str) -> Dict[str, Any]:
    # A Gemini grounding run whose only source is the merchant's own site,
    # delivered as a Vertex redirector URI + the real domain in `title`.
    return {
        "query": query,
        "provider": "gemini",
        "grounding_sources": [{
            "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIabc123",
            "title": "ownist.com",
        }],
        "parsed": {"product_visible": True, "answer": f"{query}: yes, on ownist.com"},
        "url_match": {"target_url": MERCHANT_URL, "in_grounding": False, "llm_self_report": {}},
    }


def test_source_urls_resolves_redirector_title_to_real_domain() -> None:
    run = _redirector_run("where can I buy Ownist Triple Shine Grape")
    urls = bd._source_urls(run)
    assert "ownist.com" in urls, "redirector title (real domain) not surfaced"
    # And first-party matching now sees the merchant domain.
    assert bd._url_in_sources(run, [MERCHANT_URL]) is True


def test_first_party_rate_counts_merchant_grounding() -> None:
    sku_ctx = {
        "product": {"title": "Triple Shine Grape", "brand": "Ownist",
                    "canonical_url": MERCHANT_URL},
        "sku": {"title": "14 Servings"},
        "sku_key": "p1::v::var1", "product_key": "p1",
    }
    runs = [_redirector_run(f"shop Ownist Triple Shine Grape online {i}") for i in range(8)]
    _score, breakdown = bd.compute_citation_score(sku_ctx, runs)
    fp = breakdown["first_party_rate"]
    # Every run grounded on ownist.com -> first-party numerator must be all of
    # them (was 0 before the redirector-aware fix).
    assert fp["numerator"] == len(runs), f"first_party undercounted: {fp}"
    assert fp["points"] > 0


def _report(sku_key: str, band: str, *, identity=80, routability=58, citation=30, content=18) -> Dict[str, Any]:
    return {
        "sku_key": sku_key, "product_key": sku_key.split("::")[0], "band": band,
        "scores": {
            "identity": {"score": identity}, "routability": {"score": routability},
            "citation": {"score": citation}, "content_richness": {"score": content},
        },
        "primary_gaps": [], "impact_proxy": 1.0,
    }


def test_blocked_skus_matches_per_sku_band() -> None:
    reports: List[Dict[str, Any]] = [
        # band=blocked but identity/routability OK and citation>0 — the OLD
        # ad-hoc rule MISSED this (it ignored content_richness). Must be listed.
        _report("p1::v::a", "blocked", content=18),
        # not blocked — must NOT be listed.
        _report("p2::v::b", "ready", content=80, citation=72),
    ]
    rollup = bd.build_brand_rollup(reports, "merch_test")
    blocked_keys = {b["sku_key"] for b in rollup["blocked_skus"]}
    assert blocked_keys == {"p1::v::a"}, f"blocked_skus disagrees with band: {blocked_keys}"
    # Self-consistency: blocked_skus == exactly the band=="blocked" reports.
    band_blocked = {r["sku_key"] for r in reports if r["band"] == "blocked"}
    assert blocked_keys == band_blocked
