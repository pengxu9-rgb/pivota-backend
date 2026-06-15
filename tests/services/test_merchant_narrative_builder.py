from __future__ import annotations

from typing import Any, Dict, List

from services.agent_center_bd_report_service import build_authority_map
from services.merchant_narrative_builder import build_merchant_narrative

REDIR = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZI"


def _run(query: str, title: str, axis: str = "intent", *, excerpt: str = None, comps: List[str] = None) -> Dict[str, Any]:
    run: Dict[str, Any] = {
        "query": query,
        "axis_metadata": {"axis": axis},
        "parsed": {"product_visible": True, "correct_sku": True},
        "grounding_sources": [{"uri": REDIR + title.replace(".", ""), "title": title}],
        "url_match": {"in_grounding": False},
    }
    if excerpt:
        run["evidence_excerpt"] = excerpt
    if comps:
        run["parsed"]["competitors_listed"] = comps
    return run


def _authority_map(raw_runs: List[Dict[str, Any]], *, host: str, brand: str) -> Dict[str, Any]:
    probe_runs = [{"provider": "gemini", "probe_run_id": "p", "raw_runs": raw_runs}]
    return build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
        merchant_host=host,
        merchant_brand=brand,
    )


def test_narrative_own_listing_only_is_not_reported_as_recommended():
    """No inflation: a SKU found only via own site + a marketplace listing reads
    as findable, explicitly NOT independently recommended."""
    am = _authority_map(
        [_run("buy Ownist", "ownist.com"), _run("Ownist on ebay", "ebay.com")],
        host="ownist.com",
        brand="Ownist",
    )
    per_sku = [{
        "sku_key": "sku-1",
        "sku_title": "Ownist Triple Shine",
        "band": "partial",
        "band_display": {"label": "Partial", "meaning": "Listed, not recommended"},
        "primary_gaps": [{"dimension": "citation"}],
        "verbatim_grounding_evidence": [],
        "query_class_coverage": {"branded_navigational": 2, "category_discovery": 0},
        "next_best_action": {"primary_gap": "citation", "headline": "Earn a category citation", "first_move": "Pitch a roundup"},
    }]
    narr = build_merchant_narrative(
        merchant_name="Ownist", per_sku_reports=per_sku, authority_map=am,
        providers=["gemini"], verify_providers=["deepseek"],
    )
    assert "find you" in narr["headline_story"] or "find" in narr["headline_story"].lower()
    assert "recommend" in narr["headline_story"].lower()
    assert narr["where_youre_losing"]["independently_recommended_for_category"] is False
    assert "ownist.com" in narr["whats_working"]["findability_hosts"]
    assert narr["per_sku_scorecard"][0]["surfaced_only_via_own_listing"] is True
    # Findable-but-not-endorsed: this IS the "only your own/retail listings
    # appear" case (the branch that must NOT fire for an invisible brand).
    assert "only your own" in narr["where_youre_losing"]["summary"].lower()


def test_narrative_category_endorsement_and_named_competitors():
    """Endorsement-driven: an editorial that cites on a category query makes the
    story 'independently recommended', and real grounded competitor names + cited
    hosts populate 'who AI cites instead'."""
    am = _authority_map(
        [
            _run("buy Aruen", "aruen.com"),
            _run("Aruen for sale", "ebay.com"),
            _run("best collagen supplement", "goodhousekeeping.com", "category",
                 comps=["Vital Proteins", "Ancient Nutrition"]),
        ],
        host="aruen.com",
        brand="Aruen",
    )
    narr = build_merchant_narrative(
        merchant_name="Aruen", per_sku_reports=[], authority_map=am,
        providers=["gemini"], verify_providers=["deepseek"],
    )
    assert narr["where_youre_losing"]["independently_recommended_for_category"] is True
    assert "goodhousekeeping.com" in narr["where_youre_losing"]["endorsement_hosts"]
    who = narr["where_youre_losing"]["who_ai_cites_instead"]
    assert who["available"] is True
    names = {c["name"] for c in who["competitors"]}
    assert names == {"Vital Proteins", "Ancient Nutrition"}
    cited = {h["host"] for h in who["cited_hosts"]}
    # The merchant's OWN findability never appears under "who AI cites instead":
    # not the own site (aruen.com, own_domain) and not the own marketplace
    # listing (ebay.com, marketplace_self_listing) — both are findability.
    assert "aruen.com" not in cited
    assert "ebay.com" not in cited
    # The independent editorial that named competitors IS surfaced.
    assert "goodhousekeeping.com" in cited


def test_narrative_states_limit_when_landscape_unavailable():
    """No fabrication: when no third-party host or competitor surfaced, say so —
    don't invent a landscape."""
    am = _authority_map([_run("buy Ownist", "ownist.com")], host="ownist.com", brand="Ownist")
    narr = build_merchant_narrative(
        merchant_name="Ownist", per_sku_reports=[], authority_map=am,
        providers=["gemini"],
    )
    who = narr["where_youre_losing"]["who_ai_cites_instead"]
    assert who["available"] is False
    assert who["competitors"] == []
    assert who["note"]
    assert any("not available" in lim.lower() for lim in narr["honest_limits"])


def test_narrative_invisible_when_no_hosts():
    narr = build_merchant_narrative(
        merchant_name="Ghost", per_sku_reports=[],
        authority_map={"skus": [], "hosts": [], "host_attribution_summary": {}},
    )
    assert "invisible" in narr["headline_story"].lower()
    assert narr["whats_working"]["findability_hosts"] == []
    assert narr["where_youre_losing"]["independently_recommended_for_category"] is False
    # No contradiction with the 'invisible' headline: must NOT claim own/retail
    # listings appear when nothing surfaced at all.
    where = narr["where_youre_losing"]["summary"].lower()
    assert "only your own" not in where
    assert "surface at all" in where


def test_verify_summary_plain_language():
    am = {"skus": [], "hosts": [], "host_attribution_summary": {}}
    completed = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={"status": "completed", "verified": 8, "flagged": 2, "citation_positive_candidates": 12},
    )["verify_summary_plain"]
    # `verified` (8) is the number actually checked; `flagged` (2) is a subset of
    # those, NOT additional. So checked=8, held=6 — never checked=10 (which would
    # double-count) and never checked > candidates.
    assert completed["checked"] == 8
    assert completed["checked"] <= completed["candidates"]
    assert "checked 8 of 12" in completed["text"]
    assert "6 held up" in completed["text"]
    assert "2 flagged" in completed["text"]

    skipped = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={"status": "skipped", "reason": "no_positive_candidates"},
    )["verify_summary_plain"]
    assert skipped["checked"] == 0
    assert "did not run" in skipped["text"]


def test_prioritized_actions_mapped_to_growth_phases():
    per_sku = [
        {"sku_key": "a", "sku_title": "A", "next_best_action": {"primary_gap": "content", "headline": "Substantiate INCI", "first_move": "Upload COA"}},
        {"sku_key": "b", "sku_title": "B", "next_best_action": {"primary_gap": "citation", "headline": "Earn a citation", "first_move": "Pitch editorial"}},
    ]
    actions = build_merchant_narrative(
        merchant_name="X", per_sku_reports=per_sku,
        authority_map={"skus": [], "hosts": [], "host_attribution_summary": {}},
    )["prioritized_actions"]
    phases = [a["growth_phase"] for a in actions]
    # create_and_distribute (citation) is ordered before evidence_intake (content)
    assert phases == ["create_and_distribute", "evidence_intake"]
    assert {a["headline"] for a in actions} == {"Earn a citation", "Substantiate INCI"}


def _whats_working_excerpt(evidence_items):
    narr = build_merchant_narrative(
        merchant_name="A",
        per_sku_reports=[{"sku_key": "a", "sku_title": "A",
                          "verbatim_grounding_evidence": evidence_items}],
        authority_map={"skus": [], "hosts": [], "host_attribution_summary": {}},
    )
    return narr["whats_working"]["evidence_excerpt"]


def test_evidence_excerpt_is_real_or_none():
    """The 'what's working' excerpt is a real branded grounded excerpt where the
    SKU was actually found — or None. Never fabricated, never a category-query
    excerpt, and never a 'couldn't find it' line framed as success."""
    src = [{"title": "aruen.us"}]
    # Category-axis excerpt -> None (branded only).
    assert _whats_working_excerpt([{
        "query": "best collagen", "axis_metadata": {"axis": "category"},
        "evidence_excerpt": "Top picks include several brands.",
        "grounding_sources": src, "product_visible": True,
    }]) is None
    # Branded but the SKU was NOT found -> None (no inflation: a 'couldn't find
    # it' excerpt must not be presented as what's working).
    assert _whats_working_excerpt([{
        "query": "buy Acme Glow", "axis_metadata": {"axis": "intent"},
        "evidence_excerpt": "I could not find Acme Glow; consider other serums.",
        "grounding_sources": src, "product_visible": False,
    }]) is None
    # Missing the signal (older runs that predate it) -> None, not guessed.
    assert _whats_working_excerpt([{
        "query": "buy Acme Glow", "axis_metadata": {"axis": "intent"},
        "evidence_excerpt": "Acme Glow Serum, $29.", "grounding_sources": src,
    }]) is None
    # Branded AND found -> the real excerpt is used.
    ex = _whats_working_excerpt([{
        "query": "buy Acme Glow", "axis_metadata": {"axis": "intent"},
        "evidence_excerpt": "Acme Glow Serum, $29. In stock.",
        "grounding_sources": src, "product_visible": True,
    }])
    assert ex is not None and "Acme Glow Serum" in ex["excerpt"]


def test_competitor_times_named_not_inflated_by_host_fanout():
    """A competitor named in one answer that cites several hosts is counted once
    per SKU — not once per cited host (the host rollup fans `competitors_named`
    onto every host in the answer, which would otherwise multiply the count)."""
    run = {
        "query": "best collagen supplement",
        "axis_metadata": {"axis": "category"},
        "parsed": {"product_visible": True, "correct_sku": True,
                   "competitors_listed": ["Vital Proteins"]},
        "grounding_sources": [
            {"uri": REDIR + "h1", "title": "goodhousekeeping.com"},
            {"uri": REDIR + "h2", "title": "byrdie.com"},
        ],
    }
    am = _authority_map([run], host="aruen.com", brand="Aruen")
    who = build_merchant_narrative(
        merchant_name="Aruen", per_sku_reports=[], authority_map=am,
    )["where_youre_losing"]["who_ai_cites_instead"]
    vp = [c for c in who["competitors"] if c["name"] == "Vital Proteins"]
    assert vp and vp[0]["times_named"] == 1


def test_builder_degrades_on_malformed_per_sku_reports():
    """Defensive: non-dict / missing-field per-SKU entries must not raise — the
    builder degrades rather than crashing the whole brand report."""
    narr = build_merchant_narrative(
        merchant_name="X",
        per_sku_reports=[None, "garbage", {}, {"sku_key": "a", "next_best_action": None}],
        authority_map={"skus": [], "hosts": [], "host_attribution_summary": {}},
        verify_summary=None,
    )
    assert isinstance(narr, dict)
    assert "headline_story" in narr
    assert isinstance(narr["per_sku_scorecard"], list)
    assert isinstance(narr["prioritized_actions"], list)
