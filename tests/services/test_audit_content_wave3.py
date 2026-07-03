"""Wave-3 fixes from the 2026-07-03 audit review (DamDam Shiso shampoo+conditioner
run, Jul 3 8:59 PM):

  1) the 'category winner' panel leaked a raw ```json {...} envelope when the
     winner-profile LLM response failed to parse (unterminated fence);
  2) the STRONG verdict headline claimed "cite your URL ... at goal state" off a
     brand-MENTION count while the merchant's own page was cited 0 times;
  3) the content-gap next-best-action was generic ("fill the gaps") instead of
     targeting the actual category winner + the decision factors AI credits them
     with.
"""
from __future__ import annotations

from services.agent_center_bd_report_service import (
    VERDICT_STRONG,
    _competitor_attribute_run_text,
    _explain_verdict,
    _own_url_cited_runs,
    _salvage_competitor_prose,
)
from services.next_best_action import (
    PRIMARY_SKU_CONTENT_REVISION_GAP,
    PRIMARY_SKU_SUBSTITUTION_LEAK,
    build_sku_next_best_action,
)


# ---- Bug 1: category-winner must never leak a raw ```json envelope --------

# The exact string that leaked into prod: a ```json fence whose evidence_excerpt
# is unterminated (no closing quote/brace), so upstream json parse returned None.
_LEAKED_RAHUA = (
    '```json { "product_visible": true, "competitors_listed": [], '
    '"evidence_excerpt": "Rahua is known for its plant-powered hair care, '
    'body care, and wellness products, emphasizing clean formulas and '
    'salon-quality results. The brand is certified vegan and cruelty-free, '
    'and its product'
)


def test_salvage_extracts_prose_from_unterminated_json_fence():
    out = _salvage_competitor_prose(_LEAKED_RAHUA)
    assert out.startswith("Rahua is known for its plant-powered hair care")
    assert "```" not in out
    assert "product_visible" not in out
    assert "evidence_excerpt" not in out


def test_salvage_parses_well_formed_fenced_json():
    raw = (
        '```json\n{"product_visible": true, "evidence_excerpt": '
        '"Biolage is a professional salon haircare brand."}\n```'
    )
    assert _salvage_competitor_prose(raw) == (
        "Biolage is a professional salon haircare brand."
    )


def test_salvage_passes_clean_prose_through():
    prose = "Olaplex is known for its patented bond-building technology."
    assert _salvage_competitor_prose(prose) == prose


def test_salvage_hides_json_without_prose_field():
    # Structured envelope but no prose key -> return "" so the section HIDES
    # rather than dumping JSON into merchant copy.
    assert _salvage_competitor_prose('```json {"product_visible": false}') == ""
    assert _salvage_competitor_prose('{"competitors_listed": ["A", "B"]}') == ""


def test_run_text_salvages_raw_when_parse_empty():
    # parsed is empty (upstream parse failed); raw holds the leaked fence.
    run = {"parsed": None, "raw": _LEAKED_RAHUA}
    text = _competitor_attribute_run_text(run)
    assert text.startswith("Rahua is known for")
    assert "```json" not in text


def test_run_text_prefers_parsed_when_present():
    run = {
        "parsed": {"evidence_excerpt": "Clean parsed prose."},
        "raw": _LEAKED_RAHUA,
    }
    assert _competitor_attribute_run_text(run) == "Clean parsed prose."


# ---- Bug 2: STRONG headline must not claim URL citation off brand-mentions --

def _strong_evidence(*, cited: int, total: int, own_cited):
    return {
        "attribution_runs_total": total,
        "merchant_cited_runs": cited,
        "own_url_cited_runs": own_cited,
    }


def test_strong_mention_only_does_not_claim_url_citation_or_goal_state():
    # The DamDam case: brand named in 24/28, own page cited in 0.
    text = _explain_verdict(
        VERDICT_STRONG, 62, 62,
        _strong_evidence(cited=24, total=28, own_cited=0),
    )
    assert "names your brand in 24 of 28" in text
    assert "cite your URL" not in text
    assert "goal state" not in text
    assert "third-party listings" in text


def test_strong_with_real_url_citation_reports_own_count_and_goal_state():
    text = _explain_verdict(
        VERDICT_STRONG, 82, 80,
        _strong_evidence(cited=26, total=28, own_cited=20),
    )
    # honest own-URL count, not the softer brand-mention count
    assert "cite your URL in 20 of 28" in text
    assert "at goal state" in text


def test_strong_legacy_without_own_field_keeps_prior_wording():
    # own_url_cited_runs absent (legacy callers) -> unchanged behavior.
    text = _explain_verdict(
        VERDICT_STRONG, 70, 70,
        {"attribution_runs_total": 28, "merchant_cited_runs": 24},
    )
    assert "cite your URL in 24 of 28" in text
    assert "at goal state" in text


def test_own_url_cited_runs_counts_only_own_domain():
    runs = [
        {"grounding_sources": [{"uri": "https://damdamtokyo.com/p", "title": "DAMDAM"}]},
        {"grounding_sources": [{"uri": "https://sephora.com/x", "title": "Sephora"}]},
        {"grounding_sources": [
            {"uri": "https://rahua.com/a", "title": "Rahua"},
            {"uri": "https://damdamtokyo.com/q", "title": "DAMDAM"},
        ]},
    ]
    assert _own_url_cited_runs(runs, merchant_host="damdamtokyo.com") == 2
    assert _own_url_cited_runs(runs, merchant_host=None) is None


# ---- Bug 3: content-gap NBA must target the category winner -----------------

def _thin_scores():
    return {
        "identity": {"score": 23},
        "content_richness": {"score": 14},  # blocked band -> content_revision_gap
        "routability": {"score": 6},
        "citation": {"score": 21},
    }


def _thin_opportunity():
    return {
        "per_prompt": [],
        "top_open_lanes": [],
        "substitution_alert": {"present": False},
        "demand_state_summary": "tested",
        "intent_ladder": {},
        "confidence": {"prompt_count": 4, "prompts_with_demand": 2},
    }


_RAHUA_INTEL = {
    "status": "assessed",
    "competitor": "Rahua",
    "attributes_present": ["vegan", "cruelty_free"],
    "known_for": "Rahua is known for plant-powered hair care.",
}


def test_content_gap_nba_names_winner_and_attributes():
    nba = build_sku_next_best_action(
        opportunity=_thin_opportunity(),
        scores=_thin_scores(),
        identity={"name": "DAMDAM Classic Shampoo - Shiso"},
        sku_title="DAMDAM CLASSIC SHAMPOO - Shiso",
        catalog_unavailable=True,
        competitor_intel=_RAHUA_INTEL,
    )
    assert nba["primary_gap"] == PRIMARY_SKU_CONTENT_REVISION_GAP
    blob = " ".join(
        [nba["headline"], nba["why_this_first"], nba["first_move"]]
        + list(nba["self_serve_actions"])
    )
    assert "Rahua" in blob
    # humanized attributes (underscore -> space) appear in the guidance
    assert "cruelty free" in blob
    assert "vegan" in blob
    # still a content-first move (not a comparison for an unreadable page)
    assert "comparison" not in nba["first_move"].lower()


def test_content_gap_nba_stays_generic_without_winner():
    nba = build_sku_next_best_action(
        opportunity=_thin_opportunity(),
        scores=_thin_scores(),
        identity={"name": "DAMDAM Classic Shampoo - Shiso"},
        sku_title="DAMDAM CLASSIC SHAMPOO - Shiso",
        catalog_unavailable=True,
        competitor_intel=None,
    )
    assert nba["primary_gap"] == PRIMARY_SKU_CONTENT_REVISION_GAP
    assert "Fill the gaps" in nba["headline"]
    # no fabricated competitor when none was assessed
    assert "Rahua" not in nba["why_this_first"]


def test_content_gap_nba_never_asserts_merchant_has_attributes():
    # Anti-fabrication: the winner's factors are framed as "where true of you",
    # never asserted about the merchant's own product.
    nba = build_sku_next_best_action(
        opportunity=_thin_opportunity(),
        scores=_thin_scores(),
        identity={"name": "DAMDAM Classic Shampoo - Shiso"},
        sku_title="DAMDAM CLASSIC SHAMPOO - Shiso",
        catalog_unavailable=True,
        competitor_intel=_RAHUA_INTEL,
    )
    guidance = " ".join(nba["self_serve_actions"]) + nba["why_this_first"]
    assert ("where they're true" in guidance) or ("genuinely true" in guidance)


def test_substitution_nba_folds_winner_factors_when_names_match():
    opp = _thin_opportunity()
    opp["substitution_alert"] = {
        "present": True,
        "prompt": "best shampoo alternatives",
        "substituted_by": "Rahua",
        "engines": ["gemini"],
    }
    # readable content so the substitution branch (not content-gap) fires
    scores = {
        "identity": {"score": 60},
        "content_richness": {"score": 60},
        "routability": {"score": 60},
        "citation": {"score": 60},
    }
    nba = build_sku_next_best_action(
        opportunity=opp,
        scores=scores,
        identity={"name": "DAMDAM Classic Shampoo - Shiso"},
        sku_title="DAMDAM CLASSIC SHAMPOO - Shiso",
        catalog_unavailable=True,
        competitor_intel=_RAHUA_INTEL,
    )
    assert nba["primary_gap"] == PRIMARY_SKU_SUBSTITUTION_LEAK
    compare = nba["self_serve_actions"][0]
    assert "Rahua" in compare
    assert "vegan" in compare or "cruelty free" in compare
