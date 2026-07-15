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


def _editorial(query: str, host: str, brand: str, *, comps: List[str] = None) -> Dict[str, Any]:
    """A real-URI editorial grounding whose title NAMES the brand on a category
    query — the honest endorsement shape. Post W1 site-8 cutover, endorsement
    requires the source to name the brand (RunFacts T2), so a bare-host redirector
    title no longer implies endorsement."""
    run: Dict[str, Any] = {
        "query": query,
        "axis_metadata": {"axis": "category"},
        "parsed": {"product_visible": True, "correct_sku": True},
        "grounding_sources": [
            {
                "uri": f"https://www.{host}/reviews/{brand.lower()}-collagen",
                "title": f"{brand} Collagen Review | {host}",
            }
        ],
        "url_match": {"in_grounding": False},
    }
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
            _editorial("best collagen supplement", "goodhousekeeping.com", "Aruen",
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


def test_competitor_storefront_not_recommended_as_outreach():
    """A competitor's own storefront the engine cited (e.g. asiamnaturally.com,
    the 'As I Am' brand store) must be flagged a competitor and dropped from
    'get cited on' outreach — you can't pitch a rival's store. The registry has
    never seen the host (classify_host -> unclassified); detection comes from the
    competitor brand name the engine itself listed in the same run. A genuine
    independent source named alongside it is NOT suppressed."""
    am = _authority_map(
        [
            _run("buy Anuko bond oil", "anuko.com"),
            # prefix-match path: "As I Am" -> alias "asiam" prefixes the host.
            _run("best bond repair hair oil", "asiamnaturally.com", "category",
                 comps=["As I Am", "Olaplex"]),
            # exact-match path: "Ouidad" -> alias "ouidad" == registrable label.
            _run("best bond repair hair oil", "ouidad.com", "category",
                 comps=["Ouidad"]),
            # unclassified host that matches NO competitor — must survive (proves
            # name-match precision, not just the editorial/host_type guard).
            _run("best bond repair hair oil", "myhairnotes.com", "category",
                 comps=["As I Am", "Olaplex"]),
            # editorial host (registry-classified) — survives via host_type guard.
            _run("best bond repair hair oil", "stylecraze.com", "category",
                 comps=["As I Am", "Olaplex"]),
        ],
        host="anuko.com",
        brand="Anuko",
    )
    # 1) The brand-level rollup flags competitor storefronts, even though the
    #    cited-host registry returns 'unclassified' for them — via both the
    #    prefix path (asiamnaturally) and the exact path (ouidad).
    hosts = {h["host"]: h for h in am["hosts"]}
    assert hosts["asiamnaturally.com"]["is_competitor"] is True
    assert hosts["asiamnaturally.com"]["citation_role"] == "competitor"
    assert hosts["ouidad.com"]["is_competitor"] is True
    # A non-competitor UNCLASSIFIED source is left alone: the precision guard,
    # not the registry/host_type guard, is what protects it.
    assert hosts["myhairnotes.com"]["host_type"] == "unclassified"
    assert hosts["myhairnotes.com"]["is_competitor"] is False
    assert hosts["stylecraze.com"]["is_competitor"] is False

    narr = build_merchant_narrative(
        merchant_name="Anuko", per_sku_reports=[], authority_map=am,
        providers=["gemini"], verify_providers=["deepseek"],
    )
    who = narr["where_youre_losing"]["who_ai_cites_instead"]
    cited = {h["host"] for h in who["cited_hosts"]}
    # 2) Both competitor stores are gone from outreach targets...
    assert "asiamnaturally.com" not in cited
    assert "ouidad.com" not in cited
    # ...but the genuine independent sources named alongside them survive.
    assert "myhairnotes.com" in cited
    assert "stylecraze.com" in cited
    # 3) The competitors are still surfaced as competitive intel (by name).
    assert {"As I Am", "Ouidad"} <= {c["name"] for c in who["competitors"]}
    # 4) No outreach move points the merchant at a competitor's store.
    move_hosts = {m.get("host") for m in narr["where_youre_losing"].get("outreach_moves") or []}
    assert "asiamnaturally.com" not in move_hosts
    assert "ouidad.com" not in move_hosts


def test_accented_competitor_storefront_not_recommended_as_outreach():
    """Diacritics: a competitor named with accents ('Kérastase') must flag the
    rival's own ASCII storefront (kerastase-usa.com, unknown to the registry →
    unclassified) exactly like an ASCII name would. Pre-fix,
    brand_alias._normalize turned the accented letter into a token break
    ('Kérastase' → alias 'krastase'), the alias never matched the registrable
    label 'kerastase-usa', and the rival's store served as a 'Get cited on'
    outreach move (prod run 83e8fcb4-5cd8-45a1-9067-f46b86a56336, merchant
    merch_924da2be8503e5f7 / Anuko)."""
    am = _authority_map(
        [
            _run("buy Anuko bond oil", "anuko.com"),
            _run("best bond repair hair oil", "kerastase-usa.com", "category",
                 comps=["Kérastase", "Olaplex"]),
            # An unclassified host that does NOT belong to the accented rival —
            # must survive (registrable-label match, not substring-anywhere).
            _run("best bond repair hair oil", "myhairnotes.com", "category",
                 comps=["Kérastase", "Olaplex"]),
        ],
        host="anuko.com",
        brand="Anuko",
    )
    hosts = {h["host"]: h for h in am["hosts"]}
    assert hosts["kerastase-usa.com"]["is_competitor"] is True
    assert hosts["kerastase-usa.com"]["citation_role"] == "competitor"
    assert hosts["myhairnotes.com"]["is_competitor"] is False

    narr = build_merchant_narrative(
        merchant_name="Anuko", per_sku_reports=[], authority_map=am,
        providers=["gemini"], verify_providers=["deepseek"],
    )
    who = narr["where_youre_losing"]["who_ai_cites_instead"]
    cited = {h["host"] for h in who["cited_hosts"]}
    assert "kerastase-usa.com" not in cited
    assert "myhairnotes.com" in cited
    # The rival stays in competitive intel under its display name.
    assert "Kérastase" in {c["name"] for c in who["competitors"]}
    move_hosts = {m.get("host") for m in narr["where_youre_losing"].get("outreach_moves") or []}
    assert "kerastase-usa.com" not in move_hosts


def test_ingredient_type_named_competitor_does_not_flag_host():
    """No over-suppression: on category queries the engine lists ingredient /
    category TYPES (e.g. 'Argan Oil') as 'competitors'. Those must not turn an
    unclassified host that merely leads with the term (arganoilworld.com) into a
    competitor and strip it from outreach — same generic-type guard the
    named-competitor list uses."""
    am = _authority_map(
        [
            _run("buy Anuko bond oil", "anuko.com"),
            _run("best hair oil", "arganoilworld.com", "category",
                 comps=["Argan Oil", "Coconut Oil"]),
        ],
        host="anuko.com",
        brand="Anuko",
    )
    hosts = {h["host"]: h for h in am["hosts"]}
    assert hosts["arganoilworld.com"]["is_competitor"] is False
    narr = build_merchant_narrative(
        merchant_name="Anuko", per_sku_reports=[], authority_map=am,
        providers=["gemini"], verify_providers=["deepseek"],
    )
    cited = {h["host"] for h in narr["where_youre_losing"]["who_ai_cites_instead"]["cited_hosts"]}
    assert "arganoilworld.com" in cited


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


def test_verify_coverage_and_branded_scope_disclosed_when_run():
    """When verify RAN, 'what we didn't measure' must disclose that only a SAMPLE
    of citation-positive (branded) answers was checked, and that the discovery/
    category queries were not — so a partial, branded-only check can't masquerade
    as full accuracy coverage (the holistic-verdict honesty seam)."""
    am = {"skus": [], "hosts": [], "host_attribution_summary": {}}
    narr = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={"status": "completed", "verified": 8, "flagged": 2,
                        "citation_positive_candidates": 12},
    )
    limit = next((l for l in narr["honest_limits"] if "citation-positive" in l), None)
    assert limit is not None
    assert "8 of 12" in limit                      # actual checked / candidates
    assert "discovery/category queries" in limit   # the branded-only scope caveat
    assert "unmeasured" in limit
    # the did-not-run bullet must NOT appear when verify ran
    assert not any("did not run" in l for l in narr["honest_limits"])


def test_verify_ran_without_positives_discloses_nothing_checked():
    """Verify ran but had no cited answers to check -> honest-limits states accuracy
    is unmeasured this run (not a misleading 'did not run')."""
    am = {"skus": [], "hosts": [], "host_attribution_summary": {}}
    narr = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={"status": "completed", "verified": 0, "flagged": 0,
                        "citation_positive_candidates": 0},
    )
    limit = next((l for l in narr["honest_limits"] if "no cited answers to" in l), None)
    assert limit is not None
    assert "unmeasured" in limit
    assert not any("did not run" in l for l in narr["honest_limits"])


def test_verify_skip_reason_surfaced_in_both_places():
    """The verify skip REASON must be human-readable in both the plain summary
    and the 'what we didn't measure' honest-limits bullet — so a merchant can
    tell WHY verify didn't run (config gap vs. simply not cited yet), not just
    that it didn't. Regression for the misleading bare 'did not run' bullet."""
    am = {"skus": [], "hosts": [], "host_attribution_summary": {}}

    # Data reason — uncited merchant: nothing to fact-check (expected, not a bug).
    narr = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={"status": "skipped", "reason": "no_citation_positive_probes"},
    )
    plain = narr["verify_summary_plain"]
    assert plain["reason"] == "no_citation_positive_probes"
    assert "nothing to fact-check" in plain["text"]
    limit = next(l for l in narr["honest_limits"] if "verification did not run" in l)
    assert "nothing to fact-check" in limit
    # The bare, reasonless sentence must be gone.
    assert limit.rstrip() != "Answer-quality (DeepSeek) verification did not run for this audit."

    # Config reason — no verifier resolved: actionable on our side.
    narr2 = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={"status": "skipped", "reason": "no_verify_providers_resolved"},
    )
    limit2 = next(l for l in narr2["honest_limits"] if "verification did not run" in l)
    assert "no answer-quality verifier was configured" in limit2
    assert "no answer-quality verifier was configured" in narr2["verify_summary_plain"]["text"]

    # Unknown reason code still degrades gracefully (keeps the code, no crash).
    narr3 = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={"status": "skipped", "reason": "brand_new_reason"},
    )
    assert "brand_new_reason" in narr3["verify_summary_plain"]["text"]


def test_verify_skip_reason_read_from_brand_rollup_reasons_plural():
    """The brand-level verify_summary is a ROLLUP that emits `reasons` (plural
    list), NOT `reason` (singular). The narrative must read it — else the
    report shows 'reason unavailable' while the real cause sits in `reasons`.
    Regression for the live #958 bug ('did not run — reason unavailable')."""
    am = {"skus": [], "hosts": [], "host_attribution_summary": {}}

    narr = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={"status": "skipped", "reasons": ["no_verify_providers_resolved"]},
    )
    plain = narr["verify_summary_plain"]
    assert plain["reason"] == "no_verify_providers_resolved"
    assert "no answer-quality verifier was configured" in plain["text"]
    assert "reason unavailable" not in plain["text"]
    limit = next(l for l in narr["honest_limits"] if "verification did not run" in l)
    assert "no answer-quality verifier was configured" in limit
    assert "reason unavailable" not in limit

    # Multiple distinct reasons across SKUs → each surfaced, deduped, joined.
    narr2 = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={
            "status": "skipped",
            "reasons": ["missing_deepseek_api_key", "no_citation_positive_probes"],
        },
    )
    t2 = narr2["verify_summary_plain"]["text"]
    assert "DeepSeek verifier wasn't available" in t2
    assert "nothing to fact-check" in t2


def test_verify_flagged_examples_surfaced():
    """'N flagged for accuracy' must be actionable: the actual flagged queries +
    WHY (misstates facts / unsupported recommendation + DeepSeek's note) surface
    in verify_summary_plain.flagged_examples, from verify_summary.flagged_probes."""
    am = {"skus": [], "hosts": [], "host_attribution_summary": {}}
    narr = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={
            "status": "completed", "verified": 4, "flagged": 2,
            "citation_positive_candidates": 6,
            "flagged_probes": [
                {"query": "best low molecular weight collagen", "misstates_facts": True,
                 "supports_recommendation": True, "note": "Overstates absorption claims."},
                {"query": "collagen for sleep", "misstates_facts": False,
                 "supports_recommendation": False, "note": "Recommends a competitor instead."},
                {"query": "", "note": "dropped — no query"},
            ],
        },
    )
    ex = narr["verify_summary_plain"]["flagged_examples"]
    assert len(ex) == 2  # the empty-query probe is dropped
    assert ex[0]["query"] == "best low molecular weight collagen"
    assert ex[0]["misstates_facts"] is True
    assert ex[0]["unsupported_recommendation"] is False
    assert "absorption" in ex[0]["note"]
    assert ex[1]["unsupported_recommendation"] is True
    assert ex[1]["misstates_facts"] is False

    # No flagged_probes (clean run) → empty list, never missing.
    clean = build_merchant_narrative(
        merchant_name="X", per_sku_reports=[], authority_map=am,
        verify_summary={"status": "completed", "verified": 5, "flagged": 0,
                        "citation_positive_candidates": 5},
    )["verify_summary_plain"]
    assert clean["flagged_examples"] == []


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


def test_ingredient_types_dropped_real_brand_kept_in_who_ai_cites():
    """Category questions ('best collagen') make the grounded answer name
    ingredient/supplement TYPES (Magnesium, Ashwagandha, Vitamin D), which the
    extractor captures as competitors. Those generic types must be filtered out;
    only the real brand (Thorne) survives in 'who AI cites instead'."""
    run = {
        "query": "best supplement for energy",
        "axis_metadata": {"axis": "category"},
        "parsed": {"product_visible": True, "correct_sku": True,
                   "competitors_listed": ["Magnesium", "Ashwagandha",
                                          "Vitamin D", "Thorne"]},
        "grounding_sources": [{"uri": REDIR + "gh", "title": "goodhousekeeping.com"}],
    }
    am = _authority_map([run], host="aruen.com", brand="Aruen")
    who = build_merchant_narrative(
        merchant_name="Aruen", per_sku_reports=[], authority_map=am,
    )["where_youre_losing"]["who_ai_cites_instead"]
    names = {c["name"] for c in who["competitors"]}
    assert names == {"Thorne"}


def test_who_ai_cites_competitors_empty_when_only_ingredient_types():
    """No fabrication: when every named 'competitor' is a generic ingredient
    type, the brand competitor list degrades to empty (the host is still cited)."""
    run = {
        "query": "best collagen",
        "axis_metadata": {"axis": "category"},
        "parsed": {"product_visible": True, "correct_sku": True,
                   "competitors_listed": ["Collagen", "Magnesium", "Probiotics"]},
        "grounding_sources": [{"uri": REDIR + "gh", "title": "goodhousekeeping.com"}],
    }
    am = _authority_map([run], host="aruen.com", brand="Aruen")
    who = build_merchant_narrative(
        merchant_name="Aruen", per_sku_reports=[], authority_map=am,
    )["where_youre_losing"]["who_ai_cites_instead"]
    assert who["competitors"] == []
    # The cited host is still surfaced, and the note explains no brand was named.
    assert any(h["host"] == "goodhousekeeping.com" for h in who["cited_hosts"])
    assert who["note"]


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


def test_where_youre_losing_carries_win_plan_summary():
    """Fix 4: the narrative's 'where you're losing' rolls up the win-plan so the
    merchant sees the path to winning, not just the loss."""
    win_plan = {
        "available": True,
        "rollup": {
            "losing_category_queries": 3,
            "independent_hosts_to_win": ["byrdie.com", "forbes.com", "goodhousekeeping.com"],
            "draft_ready_hosts": ["forbes.com"],
            "pitch_ready_hosts": ["forbes.com"],
        },
    }
    narr = build_merchant_narrative(
        merchant_name="Aruen", per_sku_reports=[], win_plan=win_plan,
    )
    wps = narr["where_youre_losing"]["win_plan_summary"]
    assert wps is not None
    assert wps["losing_category_queries"] == 3
    assert wps["independent_hosts_to_win"] == [
        "byrdie.com", "forbes.com", "goodhousekeeping.com",
    ]
    assert wps["pitch_ready_hosts"] == ["forbes.com"]
    assert "3 category queries" in wps["summary"]
    assert "ready-to-send pitch" in wps["summary"]


def test_win_plan_summary_absent_when_no_plan_no_fabrication():
    # No win_plan passed -> field present but None (never fabricated).
    narr = build_merchant_narrative(merchant_name="Aruen", per_sku_reports=[])
    assert narr["where_youre_losing"]["win_plan_summary"] is None
    # Unavailable win_plan -> also None.
    narr2 = build_merchant_narrative(
        merchant_name="Aruen", per_sku_reports=[],
        win_plan={"available": False, "rollup": {}},
    )
    assert narr2["where_youre_losing"]["win_plan_summary"] is None


def test_where_youre_losing_threads_vertical_profile_without_nameerror():
    """Regression for the #1243 NameError: `_where_youre_losing` referenced
    `vertical_profile` at its `_who_ai_cites_instead(...)` call site without
    receiving it as a parameter, so it raised on EVERY narrative build (best-
    effort-caught in run_brand_report, silently killing the section). The
    profile must thread from `build_merchant_narrative` down through
    `_where_youre_losing` into `_who_ai_cites_instead` for any vertical — not
    just the BEAUTY default that happened to be an in-scope module global."""
    from services.vertical_profiles import ELECTRONICS_AUDIO_PROFILE

    am = _authority_map(
        [
            _run("buy Anuko headphones", "anuko.com"),
            _editorial("best noise cancelling headphones", "rtings.com", "Anuko",
                       comps=["Bose", "Sony"]),
        ],
        host="anuko.com",
        brand="Anuko",
    )
    # A non-default vertical must not raise and must still produce the section.
    narr = build_merchant_narrative(
        merchant_name="Anuko", per_sku_reports=[], authority_map=am,
        providers=["gemini"], vertical_profile=ELECTRONICS_AUDIO_PROFILE,
    )
    where = narr["where_youre_losing"]
    assert isinstance(where, dict)
    assert "who_ai_cites_instead" in where
    assert where["who_ai_cites_instead"]["available"] is True

    # And the helper itself accepts the profile directly (the broken call site).
    from services.merchant_narrative_builder import _where_youre_losing
    direct = _where_youre_losing(
        "Anuko", am, {"findability_hosts": []}, None,
        vertical_profile=ELECTRONICS_AUDIO_PROFILE,
    )
    assert isinstance(direct, dict) and "who_ai_cites_instead" in direct


def test_narrative_cited_via_retailer_is_not_invisible():
    # Brand cited via RETAILERS on branded queries (no own-site first-party, no
    # category endorsement) but with real per-SKU citation must NOT read as
    # "invisible / doesn't surface at all" — that contradicts the citation data.
    # The real run's condition: findable=False (no recognized own/marketplace
    # host, e.g. merchant_domain was None) AND not endorsed, BUT the brand IS
    # cited (per-SKU citation > 0). Test the branch directly with a controlled
    # summary so it isn't masked by the classifier marking a retailer/editorial
    # host as findable/endorsed.
    from services.merchant_narrative_builder import _headline_story, _where_youre_losing
    summary = {
        "findability_hosts": [],
        "independently_recommended_for_category": False,
        "has_independent_endorsement": False,
        "cited_via_hosts": True,
    }
    hs = _headline_story("Anuko", summary).lower()
    assert "invisible" not in hs
    assert "retailer" in hs or "marketplace" in hs
    where = _where_youre_losing("Anuko", {"skus": [], "hosts": []}, summary)
    wyl = where["summary"].lower()
    assert "doesn't surface at all" not in wyl
    assert "surfaces via" in wyl
    # Inverse: a brand cited NOWHERE still reads as invisible.
    not_cited = {**summary, "cited_via_hosts": False}
    assert "invisible" in _headline_story("Anuko", not_cited).lower()


def test_who_ai_cites_ranks_recommends_class_over_unclassified_ties():
    """Grounding defect 2 (Anuko run 549ace84): the pre-cap sort broke
    1-citation ties alphabetically, so womenshealthmag.com (recommends-class
    editorial, cited on the category query, named in the win plan's
    independent_hosts_to_win) was truncated out of the [:8] cap while
    alphabetically-earlier unclassified 1-cite blogs survived — the outreach
    panel contradicted the win-plan panel. Ties must break by endorsement
    weight before the cap."""
    from services.merchant_narrative_builder import _who_ai_cites_instead
    blogs = [
        {"host": f"blog-{c}.com", "citation_role": "unclassified",
         "recommendation_class": "unknown", "prompts_cited_count": 1,
         "cited_on_category_query": False}
        for c in "abcdefgh"  # 8 unclassified 1-cite hosts, all sort before "w"
    ]
    editorial = {
        "host": "womenshealthmag.com", "citation_role": "editorial_review",
        "recommendation_class": "recommends", "prompts_cited_count": 1,
        "cited_on_category_query": True,
    }
    who = _who_ai_cites_instead({"hosts": blogs + [editorial], "skus": []})
    cited = [h["host"] for h in who["cited_hosts"]]
    assert len(cited) == 8  # cap still applies
    assert "womenshealthmag.com" in cited  # no longer truncated by the tie
    assert cited[0] == "womenshealthmag.com"  # endorsement weight wins the tie
    # Citation frequency still dominates classification: a 3-cite unclassified
    # host outranks the 1-cite editorial.
    frequent = {"host": "zzz-frequent.com", "citation_role": "unclassified",
                "recommendation_class": "unknown", "prompts_cited_count": 3,
                "cited_on_category_query": False}
    who2 = _who_ai_cites_instead({"hosts": blogs + [editorial, frequent], "skus": []})
    assert [h["host"] for h in who2["cited_hosts"]][0] == "zzz-frequent.com"


def test_who_ai_cites_ranks_host_with_grounded_rival_over_tie_without():
    """Next tie-break gap after the Anuko fix (#1382): among 1-citation,
    recommends-class, category-cited, classified peers that ALSO tie on those
    terms, a host that grounded a named rival ('you're losing this
    endorsement') must survive the [:8] cap ahead of an otherwise-identical
    peer that named none — instead of both falling to the alphabetical host
    key. The competitors_named term breaks only true ties: it sits below
    frequency and the higher-signal endorsement terms and above host."""
    from services.merchant_narrative_builder import _who_ai_cites_instead
    # 7 filler peers, all tied on the first four rank terms, no grounded rival.
    fillers = [
        {"host": f"tie-{c}.com", "citation_role": "editorial_review",
         "recommendation_class": "recommends", "prompts_cited_count": 1,
         "cited_on_category_query": True}
        for c in "abcdefg"
    ]
    # Two focal peers, identical on every rank term and alphabetically LAST (so
    # without the rival term they'd tie at the tail and sort by host). Only one
    # grounded a competitor.
    with_rival = {
        "host": "zeta-with-rival.com", "citation_role": "editorial_review",
        "recommendation_class": "recommends", "prompts_cited_count": 1,
        "cited_on_category_query": True,
    }
    no_rival = {
        "host": "zeta-no-rival.com", "citation_role": "editorial_review",
        "recommendation_class": "recommends", "prompts_cited_count": 1,
        "cited_on_category_query": True,
    }
    # The per-host grounded rival is derived from the SKU authority rows (a real
    # brand name, not an ingredient/category type), exactly as production does.
    skus = [{
        "sku_key": "sku-1",
        "authority_hosts": [
            {"host": "zeta-with-rival.com", "competitors_named": ["Vital Proteins"]},
        ],
    }]
    who = _who_ai_cites_instead(
        {"hosts": fillers + [no_rival, with_rival], "skus": skus}
    )
    cited = [h["host"] for h in who["cited_hosts"]]
    assert len(cited) == 8  # cap still applies (9 hosts in, 8 out)
    assert cited[0] == "zeta-with-rival.com"  # grounded rival wins the tie
    assert "zeta-with-rival.com" in cited  # survives the [:8] cap
    assert "zeta-no-rival.com" not in cited  # the no-rival tie-peer is truncated
    # Sanity: the rival term must NOT override citation frequency. A 2-cite peer
    # with no grounded rival still outranks the 1-cite host that has one.
    frequent = {"host": "aaa-frequent.com", "citation_role": "editorial_review",
                "recommendation_class": "recommends", "prompts_cited_count": 2,
                "cited_on_category_query": True}
    who2 = _who_ai_cites_instead(
        {"hosts": fillers + [no_rival, with_rival, frequent], "skus": skus}
    )
    assert [h["host"] for h in who2["cited_hosts"]][0] == "aaa-frequent.com"
    # ...and the higher-signal endorsement terms must still dominate the rival
    # term: a recommends-class host with NO grounded rival outranks a
    # non-recommends host that HAS one. Pins the tuple ordering (rival term sits
    # below recommends/category/classified) against a future reorder.
    non_recommends_with_rival = {
        "host": "bbb-plain-with-rival.com", "citation_role": "editorial_review",
        "recommendation_class": "unknown", "prompts_cited_count": 1,
        "cited_on_category_query": True,
    }
    skus3 = [{
        "sku_key": "sku-3",
        "authority_hosts": [
            {"host": "bbb-plain-with-rival.com", "competitors_named": ["Vital Proteins"]},
        ],
    }]
    who3 = _who_ai_cites_instead(
        {"hosts": [no_rival, non_recommends_with_rival], "skus": skus3}
    )
    ranked3 = [h["host"] for h in who3["cited_hosts"]]
    assert ranked3.index("zeta-no-rival.com") < ranked3.index("bbb-plain-with-rival.com")


def test_endorsing_host_outreach_move_not_framed_as_rival_recommendation():
    """Grounding defect 1 end-to-end: an editorial that independently endorses
    the merchant on a category query (endorsement_hosts) while also grounding
    answers that named competitors must NOT produce an outreach move claiming
    it 'recommends a competitor over you' — the move is reframed to extend the
    won coverage. Built through build_authority_map so the endorsement rollup,
    per-host competitor names, and the narrative all come from one real path."""
    am = _authority_map(
        [
            _run("buy Aruen", "aruen.com"),
            _editorial("best collagen supplement", "goodhousekeeping.com", "Aruen",
                       comps=["Vital Proteins", "Ancient Nutrition"]),
        ],
        host="aruen.com",
        brand="Aruen",
    )
    narr = build_merchant_narrative(
        merchant_name="Aruen", per_sku_reports=[], authority_map=am,
        providers=["gemini"], verify_providers=["deepseek"],
    )
    where = narr["where_youre_losing"]
    assert "goodhousekeeping.com" in where["endorsement_hosts"]
    # The cited-host row now carries the grounded per-host competitor names.
    gh_row = next(
        h for h in where["who_ai_cites_instead"]["cited_hosts"]
        if h["host"] == "goodhousekeeping.com"
    )
    assert set(gh_row["competitors_named"]) == {"Vital Proteins", "Ancient Nutrition"}
    moves = {m["host"]: m for m in where["outreach_moves"]}
    gh = moves["goodhousekeeping.com"]
    assert gh["already_endorses_you"] is True
    assert "recommend competitors over you" not in gh["why"]
    assert "already recommends you" in gh["why"]
    assert gh["headline"] == "Build on goodhousekeeping.com"


def test_cocited_rival_host_copy_is_grounding_not_personal_endorsement():
    """Follow-up to #1382 — over-attribution guard. build_authority_map fans a
    run's competitors onto EVERY grounding source of that run, so a recommends-
    class editorial merely co-cited alongside another source inherits
    `competitors_named` even though the rival's name came from the answer, not
    that host's own content. The outreach copy must therefore say the host
    GROUNDS answers that recommend competitors (co-citation) — it must never
    assert this specific host personally 'recommends a competitor over you'."""
    # One category run, one answer that named a rival, TWO editorial grounding
    # sources — neither names the merchant (so neither is an endorser), and
    # neither individually named the rival; the ANSWER did. Both inherit the
    # fanned competitor via build_authority_map.
    cocited_run = {
        "query": "best collagen supplement",
        "axis_metadata": {"axis": "category"},
        "parsed": {
            "product_visible": True,
            "correct_sku": False,
            "competitors_listed": ["Vital Proteins"],
        },
        "grounding_sources": [
            {"uri": "https://www.stylecraze.com/articles/best-collagen",
             "title": "Best Collagen Supplements 2026"},
            {"uri": "https://www.byrdie.com/best-collagen-5munit",
             "title": "The Best Collagen, Tested"},
        ],
        "url_match": {"in_grounding": False},
    }
    am = _authority_map(
        [_run("buy Aruen", "aruen.com"), cocited_run],
        host="aruen.com",
        brand="Aruen",
    )
    narr = build_merchant_narrative(
        merchant_name="Aruen", per_sku_reports=[], authority_map=am,
        providers=["gemini"], verify_providers=["deepseek"],
    )
    where = narr["where_youre_losing"]
    # Neither co-cited editorial is an endorser (neither named the brand).
    assert "stylecraze.com" not in where["endorsement_hosts"]
    assert "byrdie.com" not in where["endorsement_hosts"]
    moves = {m["host"]: m for m in where["outreach_moves"]}
    # At least one co-cited editorial surfaces as a rival-grounding move.
    rival_moves = [
        m for m in moves.values()
        if "recommend competitors over you" in m["why"]
    ]
    assert rival_moves, "expected a co-citation rival-grounding move"
    for m in rival_moves:
        assert m["host"] in {"stylecraze.com", "byrdie.com"}
        # The fanned competitor drives the move — but the copy is co-citation
        # ("grounds answers that recommend competitors"), never a personal
        # endorsement claim about this specific host.
        assert "grounds answers that recommend competitors over you" in m["why"]
        assert "it recommends a competitor over you" not in m["why"]
        assert m["already_endorses_you"] is False


def test_branded_only_endorser_not_framed_as_rival_recommendation():
    """Grounding defect 1c (#1382 follow-up): a host that endorsed the merchant
    ONLY on branded queries lives in `endorsement_hosts` but not
    `endorsement_category_hosts`. When ANY category endorsement also exists, the
    category-preferred selection used for the summary copy would drop the
    branded-only endorser from the outreach-suppression set — so it could still
    be framed as 'recommends a competitor over you' despite already endorsing the
    merchant. The suppression set must UNION both lists.

    Also pins the intended split: the category-recommendation summary copy (and
    the `endorsement_hosts` output field paired with it) stay CATEGORY-scoped —
    the branded-only host is deliberately absent there, only present in the
    suppression path.
    """
    from services.merchant_narrative_builder import _where_youre_losing

    # A category endorser (drives the summary copy) AND a branded-only endorser
    # that also grounded a rival — the union must protect the latter.
    summary = {
        "independently_recommended_for_category": True,
        "endorsement_category_hosts": ["allure.com"],
        "endorsement_hosts": ["allure.com", "byrdie.com"],
        "findability_hosts": ["merchant.com"],
    }
    authority_map = {
        "hosts": [
            {
                "host": "byrdie.com",
                "citation_role": "independent_editorial",
                "recommendation_class": "recommends",
                "prompts_cited_count": 2,
                "cited_on_category_query": False,
            },
        ],
        "skus": [
            {
                "sku_key": "sku-1",
                "authority_hosts": [
                    {"host": "byrdie.com", "competitors_named": ["Rival Beauty"]},
                ],
            },
        ],
    }
    where = _where_youre_losing("Aruen", authority_map, summary)

    # The branded-only endorser's outreach move is reframed as extend-the-win,
    # never "recommends a competitor over you".
    moves = {m["host"]: m for m in where["outreach_moves"]}
    byrdie = moves["byrdie.com"]
    assert byrdie["already_endorses_you"] is True
    assert "recommends a competitor" not in byrdie["why"]
    assert "already recommends you" in byrdie["why"]

    # Intended split is preserved: the category-recommendation summary + the
    # paired output field stay category-scoped (branded-only host absent there).
    assert "allure.com" in where["summary"]
    assert "byrdie.com" not in where["summary"]
    assert where["endorsement_hosts"] == ["allure.com"]


def test_outreach_and_pitch_targets_agree_on_endorser_status():
    """#1382 follow-up, cross-panel consistency: outreach_moves and pitch_targets
    render in the same portal panel and both stamp 'already endorses you'. They
    MUST use the same full endorser set, or the same host reads 'already
    recommends you' in one and 'not yet observed' in the other.

    Also guards BOTH union operands with the RunFacts overlay's independent
    re-sourcing (neither list is a guaranteed subset of the other):
      - wirecutter.com — endorser on BRANDED queries only (in endorsement_hosts,
        not endorsement_category_hosts).
      - rtings.com — endorser on CATEGORY queries only (in
        endorsement_category_hosts, not endorsement_hosts).
    Both must be suppressed from the rival framing and stamped already-endorses in
    both panels. Uses the electronics profile so pitch_targets is non-empty (its
    authority_hosts include both hosts); beauty returns [] and can't catch this.
    """
    from services.merchant_narrative_builder import _where_youre_losing
    from services.vertical_profiles import ELECTRONICS_AUDIO_PROFILE

    summary = {
        "independently_recommended_for_category": True,
        "endorsement_category_hosts": ["rtings.com"],   # category-only endorser
        "endorsement_hosts": ["wirecutter.com"],        # branded-only endorser
        "findability_hosts": ["merchant.com"],
    }
    authority_map = {
        "hosts": [
            {"host": "wirecutter.com", "citation_role": "independent_editorial",
             "recommendation_class": "recommends", "prompts_cited_count": 3,
             "cited_on_category_query": False},
            {"host": "rtings.com", "citation_role": "independent_editorial",
             "recommendation_class": "recommends", "prompts_cited_count": 2,
             "cited_on_category_query": True},
        ],
        "skus": [
            {"sku_key": "sku-1", "authority_hosts": [
                {"host": "wirecutter.com", "competitors_named": ["Rivaltone"]},
                {"host": "rtings.com", "competitors_named": ["Rivaltone"]},
            ]},
        ],
    }
    where = _where_youre_losing(
        "Aruen", authority_map, summary,
        vertical_profile=ELECTRONICS_AUDIO_PROFILE,
    )

    # Neither endorser is framed as recommending a rival (both union operands).
    moves = {m["host"]: m for m in where["outreach_moves"]}
    for host in ("wirecutter.com", "rtings.com"):
        assert moves[host]["already_endorses_you"] is True, host
        assert "recommends a competitor" not in moves[host]["why"], host

    # The pitch-targets panel agrees — same already-endorses status, no
    # contradictory "not yet observed" / "cited in your category" badge.
    pitch = {t["host"]: t for t in where["pitch_targets"]}
    for host in ("wirecutter.com", "rtings.com"):
        assert pitch[host]["status"] == "already_endorses_you", host


def test_outreach_moves_carry_losing_query_evidence():
    """Get-cited moves carry the MEASURED reason to work each host: the losing
    category queries whose grounded answers cited it (win-plan join, inverted).
    Hosts the win plan never grounded get [] — never an inferred list."""
    from services.merchant_narrative_builder import (
        _losing_queries_by_host,
        _outreach_moves,
    )

    win_plan = {
        "available": True,
        "sku_plans": [
            {
                "losing_queries": [
                    {
                        "query": "waterproof mp3 headphones for lap swimming",
                        "grounds_in": [{"host": "Rtings.com"}, {"host": "techradar.com"}],
                    },
                    {
                        "query": "open ear headphones for triathlon training",
                        "grounds_in": [{"host": "rtings.com"}],
                    },
                ]
            }
        ],
    }
    by_host = _losing_queries_by_host(win_plan)
    assert by_host["rtings.com"] == [
        "waterproof mp3 headphones for lap swimming",
        "open ear headphones for triathlon training",
    ]

    who = {
        "cited_hosts": [
            {
                "host": "rtings.com",
                "citation_role": "editorial_review",
                "recommendation_class": "recommends",
                "prompts_cited_count": 3,
                "cited_on_category_query": True,
                "competitors_named": ["Shokz"],
            },
            {
                "host": "coachweb.com",
                "citation_role": "editorial_review",
                "recommendation_class": "recommends",
                "prompts_cited_count": 1,
                "cited_on_category_query": False,
                "competitors_named": [],
            },
        ],
        "competitors": [],
        "available": True,
    }
    moves = _outreach_moves(who, losing_queries_by_host=by_host)
    by_move_host = {m["host"]: m for m in moves}
    assert by_move_host["rtings.com"]["losing_queries"] == [
        "waterproof mp3 headphones for lap swimming",
        "open ear headphones for triathlon training",
    ]
    assert by_move_host["coachweb.com"]["losing_queries"] == []


def test_annotate_outreach_moves_with_pitch_paths(monkeypatch):
    from services import merchant_narrative_builder as mnb

    def fake_classify(host, **kw):
        if host == "healthline.com":
            return {"pitch_recipient": {"email": "tips@healthline.com"}}
        if host == "byrdie.com":
            return {"pitch_recipient": {"submission_url": "https://byrdie.com/write-for-us"}}
        return {}

    import services.cited_host_classifier as chc
    monkeypatch.setattr(chc, "classify_host", fake_classify)
    moves = [
        {"host": "healthline.com"},
        {"host": "byrdie.com"},
        {"host": "unknown.example"},
        "junk",
    ]
    out = mnb.annotate_outreach_moves_with_pitch_paths(moves)
    assert out[0]["pitch_state"] == "draft_ready"
    assert out[0]["pitch_email"] == "tips@healthline.com"
    assert out[1]["pitch_state"] == "submission_only"
    assert out[1]["pitch_submission_url"] == "https://byrdie.com/write-for-us"
    assert out[2]["pitch_state"] == "target_only"
    assert out[2]["pitch_email"] is None
    # non-dict entries and non-list inputs degrade silently
    assert mnb.annotate_outreach_moves_with_pitch_paths(None) is None
