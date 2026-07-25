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
         error: Any = None, unattributable: int = 0) -> Dict[str, Any]:
    return {
        "tier": tier,
        "provider": provider,
        "anchor": "a",
        "query": f"q-{tier}-{provider}-{cited}",
        "pivota_cited": cited,
        "probe_url_match_in_grounding": exact,
        "grounding_source_count": sources,
        "unattributable_source_count": unattributable,
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


# ---------------------------------------------------------------------------
# run_baseline + the raw_runs -> row mapping.
#
# This layer had ZERO coverage in the first cut, and every blocker the review
# found lived here. `post_probe` is the only thing stubbed; the mapping,
# error detection, mock-fallback guard and blind-tier logic are all real.
# ---------------------------------------------------------------------------
class _Args:
    def __init__(self, **kw):
        self.agent_url = "https://probe.invalid"
        self.providers = ["gemini"]
        self.tiers = ["sku"]
        self.batch_size = 1
        self.timeout = 5
        self.retries = 1
        for k, v in kw.items():
            setattr(self, k, v)


def _run_with(monkey_payloads, **argkw):
    """Drive run_baseline with a canned sequence of probe responses."""
    calls = []
    seq = list(monkey_payloads)

    def fake_post(agent_url, key, **kw):
        calls.append(kw)
        return seq.pop(0) if seq else {"_error": "exhausted"}

    orig = aeo.post_probe
    aeo.post_probe = fake_post
    try:
        return aeo.run_baseline(_Args(**argkw), "k"), calls
    finally:
        aeo.post_probe = orig


def _probe_ok(query, *, sources=None, raw="answer text", provider="gemini",
              in_grounding=False):
    return {
        "result": {
            "provider": provider,
            "raw_runs": [{
                "query": query,
                "raw": raw,
                "parsed": {},
                "grounding_sources": sources if sources is not None else [
                    {"uri": "https://www.ulta.com/p/x", "title": "Ulta"}],
                "url_match": {"in_grounding": in_grounding},
            }],
        }
    }


def test_run_baseline_detects_error_encoded_in_raw():
    """🚨 B1: the prober signals failure via raw='__error__:...', NOT an
    'error' key. Grading those as honest 0% citations is the exact dilution
    the script claims to avoid."""
    q = aeo.PORTFOLIO[10]["query"]  # a sku-tier prompt
    payload, _ = _run_with([_probe_ok(q, raw="__error__:LLM_PROBE_TIMEOUT",
                                     sources=[])], tiers=["sku"], batch_size=1)
    errored = [r for r in payload["rows"] if r["run_error"]]
    assert len(errored) == 1, payload["rows"]
    assert "TIMEOUT" in errored[0]["run_error"]
    # and it must be EXCLUDED from the graded denominator
    agg = aeo.aggregate(payload["rows"])
    assert agg["overall"]["graded"] == 0
    assert agg["overall"]["errored"] == 1
    assert agg["overall"]["citation_rate_pct"] is None


def test_run_baseline_treats_mock_fallback_as_unmeasured_not_zero():
    """S6: a missing API key returns HTTP 200 with provider='mock_fallback_
    no_openai_key' and empty raw_runs. That must never read as a measured 0%."""
    q = aeo.PORTFOLIO[10]["query"]
    resp = {"result": {"provider": "mock_fallback_no_openai_key", "raw_runs": []}}
    payload, _ = _run_with([resp], tiers=["sku"], batch_size=1)
    # No graded rows, and the fallback is recorded as a gap (the remaining
    # canned responses are exhausted, which is irrelevant to the assertion).
    assert payload["rows"] == []
    first = payload["request_errors"][0]
    assert first["error"] == "provider_fallback:mock_fallback_no_openai_key"
    assert first["queries"]  # the lost prompts are named


def test_run_baseline_records_empty_raw_runs_as_a_gap():
    q = aeo.PORTFOLIO[10]["query"]
    payload, _ = _run_with([{"result": {"provider": "gemini", "raw_runs": []}}],
                           tiers=["sku"], batch_size=1)
    assert payload["rows"] == []
    assert "no_raw_runs" in payload["request_errors"][0]["error"]


def test_run_baseline_maps_pivota_citation_from_real_sources():
    q = aeo.PORTFOLIO[10]["query"]
    srcs = [{"uri": "https://agent.pivota.cc/products/sig_x", "title": "Pivota"}]
    payload, _ = _run_with([_probe_ok(q, sources=srcs)], tiers=["sku"])
    row = payload["rows"][0]
    assert row["pivota_cited"] is True
    assert row["grounding_source_count"] == 1


def test_run_baseline_counts_unattributable_sources():
    q = aeo.PORTFOLIO[10]["query"]
    srcs = [{"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/A",
             "title": "Olive Young Global"}]  # human-readable, not a domain
    payload, _ = _run_with([_probe_ok(q, sources=srcs)], tiers=["sku"])
    row = payload["rows"][0]
    assert row["cited_domains"] == []
    assert row["unattributable_source_count"] == 1


def test_run_baseline_blinds_open_tier_prompts():
    """🚨 B3: the scan mode always injects `Product: <title>` into the prompt.
    For an open category/concern query, sending the branded SKU name tells the
    model the answer. Blind tiers must send the category descriptor instead."""
    cat = next(p for p in aeo.PORTFOLIO if p["tier"] == "category")
    _, calls = _run_with([_probe_ok(cat["query"])], tiers=["category"])
    assert calls[0]["blind"] is True

    sku = next(p for p in aeo.PORTFOLIO if p["tier"] == "sku")
    _, calls2 = _run_with([_probe_ok(sku["query"])], tiers=["sku"])
    assert calls2[0]["blind"] is False


def test_blind_tiers_are_exactly_the_open_intent_tiers():
    assert aeo.BLIND_TIERS == frozenset({"category", "ingredient_concern"})
    for t in aeo.BLIND_TIERS:
        assert t in aeo.TIER_ORDER


def test_run_baseline_reports_expected_units():
    """S5: without an expected count, a run that lost half its units renders
    as a complete baseline."""
    payload, _ = _run_with([], tiers=["sku"], providers=["gemini", "chatgpt"])
    assert payload["expected_units"] == 5 * 2  # 5 sku prompts x 2 providers
    assert payload["expected_by_tier"]["sku"] == 10


def test_aggregate_keeps_a_wholly_lost_tier_visible():
    # The wedge tier vanishing from the report is the worst case.
    agg = aeo.aggregate([_row("sku")], {"sku": 2, "ingredient_concern": 10})
    assert "ingredient_concern" in agg["by_tier"]
    assert agg["by_tier"]["ingredient_concern"]["missing"] == 10
    assert agg["by_tier"]["ingredient_concern"]["graded"] == 0
    assert agg["by_tier"]["sku"]["missing"] == 1


def test_render_markdown_warns_loudly_on_incomplete_run():
    rows = [_row("sku")]
    payload = {"rows": rows, "request_errors": [
        {"provider": "gemini", "anchor": "a", "queries": ["lost prompt one"],
         "error": "RemoteDisconnected"}],
        "requests_made": 2, "prompts_selected": 5, "expected_units": 10,
        "expected_by_tier": {"sku": 10}}
    md = aeo.render_markdown(payload, aeo.aggregate(rows, {"sku": 10}), _meta())
    assert "INCOMPLETE RUN" in md
    assert "lost prompt one" in md  # the missing prompts must be NAMED


# ---------------------------------------------------------------------------
# _run_errored
# ---------------------------------------------------------------------------
def test_run_errored_reads_both_encodings():
    assert aeo._run_errored({"raw": "__error__:boom"}) == "boom"
    assert aeo._run_errored({"error": "explicit"}) == "explicit"
    assert aeo._run_errored({"raw": "a normal answer"}) is None
    assert aeo._run_errored({}) is None
    assert aeo._run_errored({"raw": "__error__:"}) == "unknown"


# ---------------------------------------------------------------------------
# redirector handling (S4)
# ---------------------------------------------------------------------------
def test_both_vertex_redirector_hosts_are_unattributable():
    for host in ("vertexaisearch.cloud.google.com",
                 "vertex-ai-search.cloud.google.com"):
        src = {"uri": f"https://{host}/grounding-api-redirect/x",
               "title": "Some Long Page Title With Spaces"}
        assert aeo._cited_domain(src) is None, host


def test_pivota_ref_rejects_third_party_page_about_pivota():
    """S10: a review OF pivota.cc hosted on trustpilot is not our surface
    being cited."""
    src = {"uri": "https://www.trustpilot.com/review/pivota.cc",
           "title": "pivota.cc Reviews | Read Customer Reviews"}
    assert aeo._is_pivota_ref(src) is False


def test_pivota_ref_rejects_pivotal_substring_in_title():
    src = {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/x",
           "title": "Pivotal role of niacinamide - healthline.com"}
    assert aeo._is_pivota_ref(src) is False


def test_render_markdown_distinguishes_nothing_cited_from_unresolvable():
    # S11: an empty domain list means two very different things, and the
    # headline's zero-grounding count only covers one of them.
    nothing = _row("sku", domains=[], sources=0)
    unresolvable = _row("brand", domains=[], sources=6)
    payload = {"rows": [nothing, unresolvable], "request_errors": [],
               "requests_made": 2, "prompts_selected": 2}
    md = aeo.render_markdown(payload, aeo.aggregate([nothing, unresolvable]), _meta())
    assert "(model cited nothing)" in md
    assert "publisher unresolvable" in md
