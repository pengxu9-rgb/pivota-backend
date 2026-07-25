"""
Unit tests for `scripts/aeo_phase0_citation_baseline.py`.

Scope mirrors `test_agent_center_pivota_pdp_baseline.py`: only the PURE
detection / aggregation / rendering layer. The probe HTTP path is the
production path the script exists to exercise, so mocking it would defeat
the purpose.

The detection tests are the load-bearing ones. The whole baseline number is
"was a Pivota surface cited", and the two ways to get that wrong are both
represented here:

  * FALSE POSITIVE — counting a catalog brand's own site (cosrx.com) or a
    retailer as a Pivota citation. This is not hypothetical: the probe's own
    `groundingContainsUrl` matches `merchantBrand` against grounding-chunk
    titles, which is why this script recomputes the hit itself.
  * FALSE NEGATIVE — missing a real citation because Gemini wraps every
    cited URI in an opaque `vertexaisearch` redirect and only the chunk
    `title` carries the publisher domain.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

_HERE = os.path.dirname(__file__)
_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import aeo_phase0_citation_baseline as aeo  # noqa: E402


# ---------------------------------------------------------------------------
# _is_pivota_ref — the headline metric's detector
# ---------------------------------------------------------------------------
def test_pivota_ref_matches_real_pdp_host():
    src = {"uri": "https://agent.pivota.cc/products/sig_abc", "title": "whatever"}
    assert aeo._is_pivota_ref(src) is True


def test_pivota_ref_matches_gemini_redirect_via_title_domain():
    # Gemini: URI is an opaque redirect, `title` is the bare domain.
    src = {
        "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIY",
        "title": "agent.pivota.cc",
    }
    assert aeo._is_pivota_ref(src) is True


def test_pivota_ref_rejects_catalog_brand_domain():
    # The exact false positive the probe's brand-title match would produce.
    for domain in ("cosrx.com", "anua.com", "manyo.us", "roundlab.com"):
        assert aeo._is_pivota_ref({"uri": "https://x/y", "title": domain}) is False


def test_pivota_ref_rejects_retailers():
    for domain in ("target.com", "ulta.com", "iherb.com", "sephora.com"):
        assert aeo._is_pivota_ref({"uri": f"https://www.{domain}/p/x", "title": domain}) is False


def test_pivota_ref_rejects_lookalike_host():
    # A third-party domain that merely CONTAINS our name is not our surface.
    src = {"uri": "https://pivota-reviews.example.com/post", "title": "pivota review blog"}
    assert aeo._is_pivota_ref(src) is False


def test_pivota_ref_matches_subdomain_of_pivota_host():
    src = {"uri": "https://cdn.pivota.cc/asset", "title": ""}
    assert aeo._is_pivota_ref(src) is True


def test_pivota_ref_handles_missing_fields():
    assert aeo._is_pivota_ref({}) is False
    assert aeo._is_pivota_ref({"uri": None, "title": None}) is False


# ---------------------------------------------------------------------------
# _cited_domain — the "who is cited instead" harvester
# ---------------------------------------------------------------------------
def test_cited_domain_prefers_real_host_for_openai():
    src = {"uri": "https://www.ulta.com/p/abc", "title": "COSRX Essence | Ulta Beauty"}
    assert aeo._cited_domain(src) == "ulta.com"


def test_cited_domain_falls_back_to_title_for_gemini_redirect():
    src = {
        "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIY",
        "title": "target.com",
    }
    assert aeo._cited_domain(src) == "target.com"


def test_cited_domain_never_reports_the_redirector_itself():
    # A redirect URI with a prose title yields no attributable publisher --
    # better to report nothing than to credit vertexaisearch.cloud.google.com.
    src = {
        "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIY",
        "title": "Some Long Page Title With Spaces",
    }
    assert aeo._cited_domain(src) is None


def test_cited_domain_strips_www():
    assert aeo._cited_domain({"uri": "https://www.sephora.com/x", "title": ""}) == "sephora.com"


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------
def _row(tier: str, provider: str = "gemini", *, cited: bool = False,
         exact: bool = False, sources: int = 3, domains: List[str] | None = None,
         error: Any = None) -> Dict[str, Any]:
    return {
        "tier": tier,
        "provider": provider,
        "anchor": "a",
        "query": f"q-{tier}-{provider}-{cited}",
        "pivota_cited": cited,
        "exact_pdp_in_grounding": exact,
        "grounding_source_count": sources,
        "cited_domains": domains if domains is not None else ["target.com"],
        "run_error": error,
    }


def test_aggregate_computes_rate_per_tier():
    rows = [
        _row("sku", cited=True),
        _row("sku", cited=False),
        _row("brand", cited=False),
        _row("brand", cited=False),
    ]
    agg = aeo.aggregate(rows)
    assert agg["by_tier"]["sku"]["citation_rate_pct"] == 50.0
    assert agg["by_tier"]["brand"]["citation_rate_pct"] == 0.0
    assert agg["overall"]["cited"] == 1
    assert agg["overall"]["graded"] == 4


def test_aggregate_excludes_errored_runs_from_denominator():
    # An errored provider lane must not dilute the rate -- the deep-tier
    # rollup shipped this exact bug (errored gemini runs halved every rate).
    rows = [_row("sku", cited=True), _row("sku", error="__error__:timeout")]
    agg = aeo.aggregate(rows)
    assert agg["by_tier"]["sku"]["graded"] == 1
    assert agg["by_tier"]["sku"]["errored"] == 1
    assert agg["by_tier"]["sku"]["citation_rate_pct"] == 100.0


def test_aggregate_errored_rows_do_not_contribute_domains():
    rows = [_row("sku", error="boom", domains=["ghost.com"])]
    agg = aeo.aggregate(rows)
    assert agg["top_cited_domains"] == []


def test_aggregate_handles_all_errored_without_zero_division():
    agg = aeo.aggregate([_row("sku", error="boom")])
    assert agg["by_tier"]["sku"]["citation_rate_pct"] is None
    assert agg["overall"]["citation_rate_pct"] is None


def test_aggregate_counts_zero_grounding_runs():
    rows = [_row("sku", sources=0), _row("sku", sources=5)]
    agg = aeo.aggregate(rows)
    assert agg["by_tier"]["sku"]["runs_with_zero_grounding"] == 1


def test_aggregate_splits_by_provider():
    rows = [_row("sku", provider="gemini", cited=True),
            _row("sku", provider="chatgpt", cited=False)]
    agg = aeo.aggregate(rows)
    assert agg["by_provider"]["gemini"]["citation_rate_pct"] == 100.0
    assert agg["by_provider"]["chatgpt"]["citation_rate_pct"] == 0.0


def test_aggregate_ranks_cited_domains():
    rows = [
        _row("sku", domains=["ulta.com", "target.com"]),
        _row("brand", domains=["ulta.com"]),
    ]
    agg = aeo.aggregate(rows)
    assert agg["top_cited_domains"][0] == ("ulta.com", 2)


# ---------------------------------------------------------------------------
# portfolio integrity — guards the run-to-run comparability contract
# ---------------------------------------------------------------------------
def test_every_portfolio_prompt_has_a_known_anchor():
    for prompt in aeo.PORTFOLIO:
        assert prompt["anchor"] in aeo.ANCHORS, prompt


def test_every_portfolio_tier_is_declared():
    for prompt in aeo.PORTFOLIO:
        assert prompt["tier"] in aeo.TIER_ORDER, prompt


def test_portfolio_queries_are_unique():
    queries = [p["query"] for p in aeo.PORTFOLIO]
    assert len(queries) == len(set(queries))


def test_every_anchor_sig_is_well_formed():
    for key, anchor in aeo.ANCHORS.items():
        assert anchor["sig"].startswith("sig_"), key
        assert len(anchor["sig"]) == len("sig_") + 32, key


def test_scoring_vendor_is_not_a_catalog_brand():
    # Passing a catalog brand as `vendor` re-enables the probe's brand-title
    # match and silently inflates the citation rate.
    brands = {a["brand"].lower() for a in aeo.ANCHORS.values()}
    assert aeo.SCORING_VENDOR.lower() not in brands


def test_batch_default_stays_within_probe_hard_cap():
    assert 1 <= aeo.DEFAULT_BATCH_SIZE <= aeo.PROBE_HARD_MAX_RUNS


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _meta() -> Dict[str, Any]:
    return {
        "captured_at": "2026-07-25T00:00:00+00:00",
        "providers": ["gemini"],
        "providers_unavailable": ["claude (no key)"],
        "surface_note": "3,326 sitemap URLs",
    }


def test_render_markdown_includes_headline_and_tiers():
    rows = [_row("sku", cited=True), _row("brand")]
    payload = {"rows": rows, "request_errors": [], "requests_made": 2,
               "prompts_selected": 2}
    md = aeo.render_markdown(payload, aeo.aggregate(rows), _meta())
    assert "AEO Phase 0" in md
    assert "3,326 sitemap URLs" in md
    assert "claude (no key)" in md
    assert "sku" in md and "brand" in md


def test_render_markdown_escapes_pipes_in_queries():
    rows = [_row("sku")]
    rows[0]["query"] = "a | b"
    payload = {"rows": rows, "request_errors": [], "requests_made": 1,
               "prompts_selected": 1}
    md = aeo.render_markdown(payload, aeo.aggregate(rows), _meta())
    assert "a \\| b" in md


def test_render_markdown_lists_failed_requests():
    rows = [_row("sku")]
    payload = {
        "rows": rows,
        "request_errors": [{"provider": "gemini", "anchor": "a", "error": "RemoteDisconnected"}],
        "requests_made": 2,
        "prompts_selected": 1,
    }
    md = aeo.render_markdown(payload, aeo.aggregate(rows), _meta())
    assert "Failed probe requests" in md
    assert "RemoteDisconnected" in md


def test_render_markdown_marks_no_cited_sources():
    rows = [_row("sku", domains=[])]
    payload = {"rows": rows, "request_errors": [], "requests_made": 1,
               "prompts_selected": 1}
    md = aeo.render_markdown(payload, aeo.aggregate(rows), _meta())
    assert "(no cited sources)" in md
