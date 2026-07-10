"""Sibling-SKU citation conflation: brand-level vs strict SKU-level split.

Found reading live Mojawa run 7420c2b5 as an operator: a Purra Swim probe was
answered with Run Plus content (a sibling Mojawa product) and still counted as
"merchant cited" (merchant_cited_runs=1) — inflating THIS SKU's citation rate
and burying the real insight ("AI answers with your older product, not this
SKU").

The fix is ADDITIVE (merchant_cited_runs stays brand-level and parity-locked to
the RunFacts verdict path): a strict `sku_identified` run signal (probe
self-report correct_sku/sku_mentioned/product_visible or the SKU's own URL in
grounding — NO brand-text fallback) feeds `source_summary.sku_cited_runs`, a
per-prompt `brand_cited_sku_absent` flag, additive `sku_cited`/`sku_rate` in
citation_by_intent, and a per-SKU `brand_vs_sku_citation` summary block.
"""

from typing import Any, Dict, List

from services.sku_opportunity import build_sku_opportunity
from services.agent_center_bd_report_service import (
    _brand_vs_sku_citation,
    _citation_by_intent,
)


def _sku_ctx() -> Dict[str, Any]:
    return {
        "sku_key": "sku-swim",
        "product_key": "prod-swim",
        "sku_title": "Purra Swim",
        "product": {
            "title": "Purra Swim IP68 Waterproof Swimming Headphones",
            "brand": "Mojawa",
            "vendor": "Mojawa",
            "product_type": "headphones",
            "canonical_url": "https://mojawa.com/products/purra-swim",
        },
        "sku": {"title": "Black"},
    }


def _run(
    *,
    query: str,
    provider: str = "gemini",
    parsed: Dict[str, Any],
    raw: str,
    sources: List[Dict[str, str]],
    in_grounding: bool = False,
) -> Dict[str, Any]:
    return {
        "query": query,
        "_provider": provider,
        "raw": raw,
        "parsed": parsed,
        "grounding_sources": sources,
        "grounding_chunks": [s["uri"] for s in sources],
        "url_match": {
            "in_grounding": in_grounding,
            "llm_self_report": {
                k: v
                for k, v in parsed.items()
                if k in {"product_visible", "correct_sku", "sku_mentioned"}
            },
        },
        "axis_metadata": {"axis": "category", "sku_key": "sku-swim"},
    }


def _sibling_conflated_run(query: str) -> Dict[str, Any]:
    """The live conflation shape: answer names the BRAND (via a sibling
    product, Run Plus) but the probe verified this SKU is NOT in the answer."""
    return _run(
        query=query,
        parsed={
            "product_visible": False,
            "correct_sku": False,
            "sku_mentioned": False,
            "competitors_listed": ["Shokz"],
        },
        raw=(
            "Mojawa Run Plus is a pair of headphones designed for swimming "
            "with an IP68 rating. Shokz OpenSwim is another option."
        ),
        sources=[
            {"uri": "https://ebay.com/mojawa-run-plus", "title": "Mojawa Run Plus"},
        ],
    )


def _sku_verified_run(query: str) -> Dict[str, Any]:
    return _run(
        query=query,
        parsed={"product_visible": True, "correct_sku": True, "sku_mentioned": True},
        raw="The Mojawa Purra Swim IP68 Waterproof Swimming Headphones are available.",
        sources=[
            {
                "uri": "https://mojawa.com/products/purra-swim",
                # brand token in the source label: RunFacts brand_mentioned_runs
                # (the brand-level counter) matches cited-source LABELS, so the
                # brand must appear there for merchant_cited_runs to count it.
                "title": "Mojawa Purra Swim | Official Store",
            },
        ],
        in_grounding=True,
    )


def _absent_run(query: str) -> Dict[str, Any]:
    return _run(
        query=query,
        parsed={
            "product_visible": False,
            "correct_sku": False,
            "sku_mentioned": False,
            "competitors_listed": ["Shokz", "H2O Audio"],
        },
        raw="Shokz OpenSwim Pro and H2O Audio Tri are the top swim headphones.",
        sources=[{"uri": "https://rtings.com/swim", "title": "Best swim headphones"}],
    )


def _opportunity():
    runs = [
        _sibling_conflated_run("ip68 waterproof swim headphones"),
        _sku_verified_run("where can I buy Mojawa Purra Swim"),
        _absent_run("best headphones"),
    ]
    return build_sku_opportunity(_sku_ctx(), runs)


def _rows_by_query(opportunity):
    return {
        r["normalized_query"]: r
        for r in opportunity["per_prompt"]
    }


# --- per-prompt split -------------------------------------------------------

def test_sibling_conflated_run_splits_brand_vs_sku():
    rows = _rows_by_query(_opportunity())
    row = rows["ip68 waterproof swim headphones"]
    summary = row["source_summary"]
    # Brand-level counting unchanged: the Run Plus mention IS a brand citation.
    assert summary["merchant_cited_runs"] >= 1
    # Strict SKU-level: this SKU was verified absent.
    assert summary["sku_cited_runs"] == 0
    assert row["brand_cited_sku_absent"] is True


def test_sku_verified_run_counts_both_levels():
    rows = _rows_by_query(_opportunity())
    row = rows["where can i buy mojawa purra swim"]
    summary = row["source_summary"]
    assert summary["merchant_cited_runs"] >= 1
    assert summary["sku_cited_runs"] >= 1
    assert row["brand_cited_sku_absent"] is False


def test_fully_absent_run_flags_nothing():
    rows = _rows_by_query(_opportunity())
    row = rows["best headphones"]
    summary = row["source_summary"]
    assert summary["sku_cited_runs"] == 0
    # No brand citation either -> not a sibling-conflation case.
    assert row["brand_cited_sku_absent"] is False


def test_negative_verdict_defeats_sku_identification():
    # correct_sku=False must not be overridden by a stray positive flag.
    runs = [
        _run(
            query="q1",
            parsed={"product_visible": True, "correct_sku": False},
            raw="Similar product from the brand, not this one.",
            sources=[{"uri": "https://x.example/a", "title": "a"}],
        )
    ]
    opp = build_sku_opportunity(_sku_ctx(), runs)
    row = opp["per_prompt"][0]
    assert row["source_summary"]["sku_cited_runs"] == 0


# --- citation_by_intent additive keys --------------------------------------

def test_citation_by_intent_carries_sku_split():
    opp = _opportunity()
    buckets = _citation_by_intent(opp["per_prompt"])
    # every bucket has both level counts and both rates
    for bucket in buckets.values():
        assert {"cited", "sku_cited", "total", "rate", "sku_rate"} <= set(bucket)
        assert bucket["sku_cited"] <= bucket["cited"] or bucket["cited"] == 0
    # the conflated discovery query inflates brand-cited but not sku-cited
    all_cited = sum(b["cited"] for b in buckets.values())
    all_sku = sum(b["sku_cited"] for b in buckets.values())
    assert all_cited == 2  # conflated + verified
    assert all_sku == 1    # only the verified one


# --- per-SKU summary block ---------------------------------------------------

def test_brand_vs_sku_citation_summary():
    opp = _opportunity()
    block = _brand_vs_sku_citation(opp["per_prompt"])
    assert block["detected"] is True
    assert block["count"] == 1
    assert block["brand_only_queries"] == ["ip68 waterproof swim headphones"]
    assert block["brand_cited_queries"] == 2
    assert block["sku_cited_queries"] == 1
    assert "brand" in (block["note"] or "").lower()


def test_brand_vs_sku_citation_summary_clean_when_no_conflation():
    runs = [_sku_verified_run("where can I buy Mojawa Purra Swim")]
    opp = build_sku_opportunity(_sku_ctx(), runs)
    block = _brand_vs_sku_citation(opp["per_prompt"])
    assert block["detected"] is False
    assert block["count"] == 0
    assert block["note"] is None
