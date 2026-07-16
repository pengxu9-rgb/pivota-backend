"""Unit tests for build_product_competitiveness — product-first view: discovery
(non-branded, winnable) appearance + who AI recommends instead, with branded
name queries reported separately as low-value."""
from __future__ import annotations

from services.agent_center_bd_report_service import build_product_competitiveness


def _row(query, axis, merchant_cited_runs=0, competitors=None, grounded=True):
    # Appearance is now VERDICT-based (provider_verdicts). Map the legacy params:
    # grounded + cited -> "win" (product appears), grounded + not cited -> "loss"
    # (graded but didn't appear), not grounded -> "absent" (inconclusive).
    if not grounded:
        verdict = "absent"
    elif merchant_cited_runs > 0:
        verdict = "win"
    else:
        verdict = "loss"
    return {
        "query": query,
        "normalized_query": query,
        "axis": axis,
        "provider_verdicts": {"gemini": verdict},
        "competitors": competitors or [],
    }


def test_splits_discovery_from_branded_and_counts_appearance():
    per_prompt = [
        # discovery (axis=category): "best X" -> category_head, "best X for Y" -> problem_jtbd
        _row("best hair oil", "category", merchant_cited_runs=0,
             competitors=["Cantu Shea Butter for Natural Hair", "&honey Moist Oil"]),
        _row("best hair oil for damaged hair", "category", merchant_cited_runs=1,
             competitors=["Cantu, Shea Butter, Coconut Cream"]),
        _row("hair oil for sensitive scalp", "attribute", merchant_cited_runs=0,
             competitors=["MUCOTA Adllura"]),
        # branded (axis=intent -> navigational, axis=review -> trust)
        _row("where can I buy Anuko Hair Oil", "intent", merchant_cited_runs=1),
        _row("is Anuko legit", "review", merchant_cited_runs=1),
    ]
    pc = build_product_competitiveness(per_prompt)

    assert pc["has_discovery"] is True
    # 3 discovery queries, appeared on 1 (the problem_jtbd one).
    assert pc["discovery"]["total"] == 3
    assert pc["discovery"]["appeared"] == 1
    assert pc["discovery"]["rate"] == round(1 / 3, 3)
    # branded counted separately, appeared on both.
    assert pc["branded"]["total"] == 2
    assert pc["branded"]["appeared"] == 2


def test_competitors_grouped_by_brand_on_discovery_only():
    per_prompt = [
        _row("best hair oil", "category",
             competitors=["Cantu Shea Butter for Natural Hair",
                          "Cantu, Shea Butter, Coconut Cream"]),
        _row("best hair oil for frizz", "category",
             competitors=["Cantu, Leave-In Repair", "&honey Oil"]),
        # branded query competitors must NOT pollute the discovery competitor set
        _row("Anuko Hair Oil reviews", "review",
             competitors=["SomeBrandedOnlyComp"]),
    ]
    pc = build_product_competitiveness(per_prompt)
    names = {c["name"] for c in pc["discovery"]["top_competitors"]}
    # three Cantu SKU strings collapse into one "Cantu".
    assert "Cantu" in names
    assert "&honey" in names
    assert "SomeBrandedOnlyComp" not in names
    cantu = next(c for c in pc["discovery"]["top_competitors"] if c["name"] == "Cantu")
    assert cantu["query_count"] == 2  # cited on both discovery queries


def test_no_discovery_queries_flags_has_discovery_false():
    per_prompt = [
        _row("where can I buy Anuko Hair Oil", "intent", merchant_cited_runs=1),
        _row("is Anuko legit", "review", merchant_cited_runs=1),
    ]
    pc = build_product_competitiveness(per_prompt)
    assert pc["has_discovery"] is False
    assert pc["discovery"]["total"] == 0
    assert pc["branded"]["total"] == 2


def test_ungrounded_discovery_excluded_from_denominator():
    # A discovery query the AI didn't ground is inconclusive — not "appeared",
    # not "missed", and not in the total. Honest rate over grounded only.
    per_prompt = [
        _row("best hair oil for damaged hair", "category", merchant_cited_runs=1),
        _row("best hair oil for split ends", "category", merchant_cited_runs=0,
             competitors=["Olaplex"]),
        _row("best hair oil for frizz", "category", grounded=False),  # ungrounded
    ]
    pc = build_product_competitiveness(per_prompt)
    assert pc["discovery"]["total"] == 2          # ungrounded one excluded
    assert pc["discovery"]["appeared"] == 1
    assert pc["discovery"]["ungrounded"] == 1
    assert pc["grounding_unavailable"] is False


def test_all_discovery_ungrounded_flags_couldnt_measure():
    # Discovery queries ran but the AI grounded NONE -> couldn't measure this
    # run; must NOT report a false "appears in 0 of N".
    per_prompt = [
        _row("best hair oil for damaged hair", "category", grounded=False),
        _row("bond repair hair oil for breakage", "category", grounded=False),
    ]
    pc = build_product_competitiveness(per_prompt)
    assert pc["has_discovery"] is False
    assert pc["grounding_unavailable"] is True
    assert pc["discovery"]["total"] == 0
    assert pc["discovery"]["ungrounded"] == 2


def _vrow(query, axis, verdicts):
    # row with per-model provider_verdicts (win/loss/absent) + grounding so the
    # aggregate path counts it.
    return {
        "query": query,
        "normalized_query": query,
        "axis": axis,
        "source_summary": {"runs_with_citations": 1,
                           "top_cited_hosts": [{"host": "x.com", "times_cited": 1}]},
        "provider_verdicts": verdicts,
    }


def test_per_model_discovery_split_and_divergence():
    per_prompt = [
        _vrow("best hair oil for damaged hair", "category",
              {"gemini": "win", "chatgpt": "loss", "deepseek": "win"}),
        _vrow("bond repair hair oil for breakage", "category",
              {"gemini": "win", "chatgpt": "win"}),
        _vrow("hair oil for split ends", "category",
              {"gemini": "loss", "chatgpt": "absent"}),  # chatgpt ungrounded->skip
    ]
    pc = build_product_competitiveness(per_prompt)
    bm = pc["by_model"]
    # gemini graded all 3 (win,win,loss) -> 2/3; chatgpt graded 2 (loss,win) -> 1/2
    assert bm["gemini"] == {"appeared": 2, "total": 3, "rate": round(2 / 3, 3)}
    assert bm["chatgpt"] == {"appeared": 1, "total": 2, "rate": 0.5}
    # divergence: query 1 (gemini win, chatgpt loss) — deepseek excluded (verify)
    div_qs = [d["query"] for d in pc["model_divergence"]]
    assert "best hair oil for damaged hair" in div_qs
    d0 = next(d for d in pc["model_divergence"] if d["query"] == "best hair oil for damaged hair")
    assert d0["won"] == ["gemini"] and d0["lost"] == ["chatgpt"]
    # query 2 both win -> not divergent
    assert "bond repair hair oil for breakage" not in div_qs


def test_per_model_excludes_branded_and_deepseek():
    per_prompt = [
        _vrow("is anuko legit", "review", {"gemini": "win", "chatgpt": "win"}),  # branded
    ]
    pc = build_product_competitiveness(per_prompt)
    assert pc["by_model"] == {}          # branded not in per-model discovery
    assert pc["model_divergence"] == []


def test_competitor_brand_label_keeps_two_word_brands():
    """Regression: the old first-word-only rule mangled multi-word brands
    (Wonder Curl -> Wonder). Keep them whole; still collapse product descriptors."""
    from services.agent_center_bd_report_service import _competitor_brand_label
    # Two-word brands stay intact.
    assert _competitor_brand_label("Wonder Curl") == "Wonder Curl"
    assert _competitor_brand_label("Camille Rose Curl Maker") == "Camille Rose"
    assert _competitor_brand_label("Aunt Jackie's Curling Custard") == "Aunt Jackie's"
    assert _competitor_brand_label("Maui Moisture Shampoo") == "Maui Moisture"
    # Product/ingredient 2nd word is dropped so a brand's SKUs group into one.
    assert _competitor_brand_label("Cantu Shea Butter for Natural Hair") == "Cantu"
    assert _competitor_brand_label("Cantu, Shea Butter, Coconut Curling Cream") == "Cantu"
    assert _competitor_brand_label("&honey Moist Shampoo") == "&honey"
    # Single-token names unchanged.
    assert _competitor_brand_label("SheaMoisture") == "SheaMoisture"
    assert _competitor_brand_label("d'Alba") == "d'Alba"
    assert _competitor_brand_label("") == ""


def test_competitor_brand_label_keeps_connector_word_brands():
    """Regression (live run a51ae093): the 2-token rule clipped brands whose 2nd
    token is a connector ("As I Am" -> "As I", "Creme of Nature" -> "Creme of").
    A brand spanning a connector must survive whole; genuine product descriptors
    must still collapse."""
    from services.agent_center_bd_report_service import _competitor_brand_label
    # Connector-spanning brands stay whole, even with trailing product words.
    assert _competitor_brand_label("As I Am Coconut CoWash") == "As I Am"
    assert _competitor_brand_label("Creme of Nature Argan Oil") == "Creme of Nature"
    assert _competitor_brand_label("Bumble and bumble Surf Spray") == "Bumble and bumble"
    assert _competitor_brand_label("Carol's Daughter") == "Carol's Daughter"
    # Bare "&" connector (with surrounding spaces) as the 2nd token is held together.
    assert _competitor_brand_label("Lock & Mane Detangler") == "Lock & Mane"
    # Non-connector 2nd words are unaffected: real two-word brand kept...
    assert _competitor_brand_label("Maui Moisture Curl Quench") == "Maui Moisture"
    # ...and a product-form 2nd word still collapses to the lead brand token.
    assert _competitor_brand_label("Cantu Shea Butter Leave-In") == "Cantu"


def test_discovery_appearance_splits_endorsement_from_own_listing():
    """A category 'win' where the brand only appears via its OWN listing
    (appearance_via_listing=True) is findability, not endorsement; it must be
    counted separately so "appears N/M" doesn't overstate competitiveness."""
    per_prompt = [
        {  # own Hwahae listing retrieved — findability
            "query": "best hair butter", "normalized_query": "best hair butter",
            "axis": "category", "provider_verdicts": {"chatgpt": "win"},
            "appearance_via_listing": True, "competitors": ["Cantu"],
        },
        {  # brand surfaced via an independent source — endorsement
            "query": "top hair butter", "normalized_query": "top hair butter",
            "axis": "category", "provider_verdicts": {"gemini": "win"},
            "appearance_via_listing": False, "competitors": [],
        },
    ]
    d = build_product_competitiveness(per_prompt)["discovery"]
    assert d["appeared"] == 2
    assert d["appeared_listing"] == 1
    assert d["appeared_recommended"] == 1


def test_evidence_play_recommends_supplying_proof_to_commerce_index():
    """Build C: the Pivota-moat lever. A product making efficacy/cert claims with
    no supplied evidence (and AI-flagged unsupported answers) gets a 'supply lab
    reports/certifications -> Pivota publishes them as grounded claims' action."""
    from services.agent_center_bd_report_service import build_evidence_play
    product = {
        "title": "Anuko Bond & Repair Hair Oil",
        "description": "Bond technology repairs disulfide bonds. Clinically shown. Vegan, cruelty-free.",
    }
    ep = build_evidence_play(product=product, sku_ctx={}, verify_summary={"flagged": 3})
    assert ep["present"] is True
    assert ep["already_substantiated"] is False
    assert "repair" in ep["claims_to_substantiate"] and "vegan" in ep["claims_to_substantiate"]
    assert any("Pivota publishes" in m for m in ep["moves"])
    # verify gap -> evidence action (factual-only copy: names wrong facts)
    assert any("wrong facts" in m and "3" in m for m in ep["moves"])


def test_evidence_play_silent_when_substantiated_or_no_claims():
    from services.agent_center_bd_report_service import build_evidence_play
    product = {"title": "Anuko Bond & Repair Hair Oil", "description": "repairs, clinically shown"}
    # Merchant already supplied evidence -> no nag.
    assert build_evidence_play(
        product=product, sku_ctx={"has_substantiated_evidence": True},
        verify_summary={"flagged": 3})["present"] is False
    # No substantiation-worthy claims and nothing flagged -> not present.
    assert build_evidence_play(
        product={"title": "Plain Cotton Tote Bag"}, sku_ctx={},
        verify_summary={"flagged": 0})["present"] is False


def test_engine_playbook_is_per_engine_and_names_real_sources():
    """Build A: per-engine ops. Gemini (Google index) and ChatGPT (Bing +
    Reddit/community) get DIFFERENT moves grounded in how each cites; the weaker
    engine is flagged as the primary gap and divergence is surfaced."""
    from services.agent_center_bd_report_service import build_engine_playbook
    per_prompt = [
        {"query": "best hair oil", "normalized_query": "best hair oil",
         "axis": "category", "provider_verdicts": {"gemini": "loss", "chatgpt": "win"}},
        {"query": "repairing hair oil for damaged hair",
         "normalized_query": "repairing hair oil for damaged hair",
         "axis": "category", "provider_verdicts": {"gemini": "loss", "chatgpt": "win"}},
    ]
    channel = {"channels": [
        {"host": "hwahae.com", "type": "editorial", "is_own_site": False},
        {"host": "reddit.com", "type": "community", "is_own_site": False},
    ]}
    pb = build_engine_playbook(per_prompt=per_prompt, channel_appearance=channel)
    assert pb["has_signal"] is True
    assert pb["primary_gap"] == "gemini"          # invisible on Gemini, present on ChatGPT
    assert pb["engines"]["gemini"]["status"] == "invisible"
    assert pb["engines"]["chatgpt"]["status"] == "present"
    # Per-engine moves differ and name the real cited sources.
    gemini_moves = " ".join(pb["engines"]["gemini"]["moves"]).lower()
    chatgpt_moves = " ".join(pb["engines"]["chatgpt"]["moves"]).lower()
    assert "google" in gemini_moves and "hwahae.com" in gemini_moves
    assert "reddit" in chatgpt_moves and "reddit.com" in chatgpt_moves
    assert "google" not in chatgpt_moves  # engine-specific, not copy-paste
    assert "gemini" in (pb["divergence_note"] or "").lower()


def test_engine_playbook_no_signal_when_ungrounded():
    """No graded discovery rows -> no per-engine signal (don't fabricate ops)."""
    from services.agent_center_bd_report_service import build_engine_playbook
    pb = build_engine_playbook(per_prompt=[
        {"query": "best hair oil", "axis": "category",
         "provider_verdicts": {"gemini": "absent", "chatgpt": "absent"}},
    ], channel_appearance={})
    assert pb["has_signal"] is False
    assert pb["primary_gap"] is None
