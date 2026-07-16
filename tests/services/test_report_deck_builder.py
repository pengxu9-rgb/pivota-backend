from __future__ import annotations

import io
import zipfile
from decimal import Decimal

import pytest

from services.provider_credit_rates import credits_for_tokens, token_cost_usd
from services.report_deck_builder import (
    DECK_TOKEN_PRICE_MULTIPLE,
    build_report_deck,
)
from tests.services.test_report_summary_builder import _brand_report
from services.report_summary_builder import build_report_summary


def _summary():
    return build_report_summary(_brand_report())


# ── Pricing: actual tokens x 1.6 ─────────────────────────────────────────────


def test_token_cost_usd_uses_measured_tokens():
    # deepseek: $0.14/1M in, $0.28/1M out
    usd = token_cost_usd("deepseek", input_tokens=1_000_000, output_tokens=0)
    assert usd == Decimal("0.14")
    usd = token_cost_usd("deepseek", input_tokens=0, output_tokens=1_000_000)
    assert usd == Decimal("0.28")
    assert token_cost_usd("deepseek", input_tokens=0, output_tokens=0) == 0
    # negative counts are clamped, never a credit refund
    assert token_cost_usd("deepseek", input_tokens=-5, output_tokens=0) == 0


def test_credits_for_tokens_applies_1_6_multiple_and_ceils():
    # 1M in + 1M out = $0.42 COGS x 1.6 = $0.672 -> / credit_to_usd(0.01)
    # = 67.2 -> ceil = 68 credits.
    credits, usd = credits_for_tokens(
        "deepseek",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        multiple=DECK_TOKEN_PRICE_MULTIPLE,
    )
    assert usd == Decimal("0.42")
    assert credits == 68


def test_credits_for_tokens_minimum_one_credit_for_any_real_usage():
    # A typical exec-summary call (3k in / 500 out) costs well under a credit
    # raw — but any non-zero usage bills at least 1 credit.
    credits, usd = credits_for_tokens(
        "deepseek",
        input_tokens=3_000,
        output_tokens=500,
        multiple=DECK_TOKEN_PRICE_MULTIPLE,
    )
    assert usd > 0
    assert credits == 1


def test_credits_for_tokens_zero_usage_bills_nothing():
    credits, usd = credits_for_tokens(
        "deepseek", input_tokens=0, output_tokens=0, multiple=Decimal("1.6")
    )
    assert credits == 0
    assert usd == 0


def test_credits_for_tokens_rejects_nonpositive_multiple():
    with pytest.raises(ValueError):
        credits_for_tokens(
            "deepseek", input_tokens=100, output_tokens=100, multiple=Decimal("0")
        )


def test_deck_multiple_is_1_6():
    # The user-set price point: 1.6x actual token consumption. A change here
    # is a pricing decision, not a refactor.
    assert DECK_TOKEN_PRICE_MULTIPLE == Decimal("1.6")


# ── Deck rendering ───────────────────────────────────────────────────────────


def _slide_texts(deck_bytes: bytes):
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation(io.BytesIO(deck_bytes))
    texts = []
    for slide in prs.slides:
        chunks = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                chunks.append(shape.text_frame.text)
        texts.append("\n".join(chunks))
    return texts


def test_full_deck_renders_contract_verbatim():
    deck = build_report_deck(
        _summary(),
        executive_bullets=["AI does not recommend the brand today."],
    )
    assert deck is not None
    assert zipfile.is_zipfile(io.BytesIO(deck))
    slides = _slide_texts(deck)
    # cover + findings + exec summary + 1 action + methodology
    assert len(slides) == 5
    cover = slides[0]
    assert "4.2 / 10" in cover
    assert "Needs work" in cover
    assert "GlowLab is invisible to AI shopping agents today." in cover
    assert "data as of 2026-07-15" in cover
    assert "Who AI cites instead" in slides[1]
    assert "byrdie.com" in slides[1]
    assert "Executive summary" in slides[2]
    assert "Get Hydra Serum indexed so AI can find it." in slides[3]
    assert "best hydrating serum for dry skin" in slides[3]
    assert "How this was measured" in slides[4]
    assert "Provider coverage: grounded on gemini, openai." in slides[4]


def test_preview_deck_is_single_watermarked_slide():
    deck = build_report_deck(_summary(), preview_only=True)
    assert deck is not None
    slides = _slide_texts(deck)
    assert len(slides) == 1
    assert "PREVIEW" in slides[0]
    assert "4.2 / 10" in slides[0]


def test_deck_without_exec_bullets_skips_that_slide():
    deck = build_report_deck(_summary())
    slides = _slide_texts(deck)
    assert not any("Executive summary" in s for s in slides)
    assert len(slides) == 4


def test_deck_degrades_on_empty_summary():
    deck = build_report_deck({})
    assert deck is not None
    slides = _slide_texts(deck)
    assert len(slides) >= 2  # cover + methodology bookend, no crash
    assert "— / 10" in slides[0]


# ── Deck-quality round: the highlights leadership actually needs ─────────────

def _rich_summary():
    """A summary carrying share-of-voice, subscores, real trend, curated
    actions, and multiple SKUs — the fields the early deck ignored."""
    return {
        "generated_at": "2026-07-16",
        "subject": {"merchant_name": "Mojawa"},
        "score": {
            "display": 2.3, "scale_max": 10, "band": "needs_work",
            "subscores": [
                {"key": "visibility", "display": 0.6},
                {"key": "attribution", "display": 4.6},
            ],
            "weakest_dimension": {"key": "identity", "label": "product identity", "display": 2.3},
            "delta": None,  # the field the old cover read — must NOT gate trend
        },
        "verdict": {"headline": "Independently endorsed, but not agent-ready."},
        "since_last_audit": {
            "headline": "Material change since your last audit earlier today",
            "movements": [
                {"signal": "attribution", "label": "First-party citation",
                 "from": 39, "to": 46, "direction": "improved", "is_material": True},
                {"signal": "visibility", "label": "AI visibility",
                 "from": 6, "to": 6, "direction": "stable", "is_material": False},
            ],
        },
        "share_of_voice": {
            "available": True, "basis": "discovery_prompts", "prompts_probed": 11,
            "brand": {"name": "Mojawa", "prompts_cited": 8, "pct": 72.7},
            "competitors": [
                {"name": "Shokz", "pct": 100.0},
                {"name": "Suunto", "pct": 81.8},
                {"name": "Bose", "pct": 45.5},
            ],
        },
        "top_findings": [
            {"title": "Independent endorsement", "severity": "info",
             "evidence_summary": "wired.com already recommends Mojawa."},
        ],
        "top_actions": [
            {"headline": "Win the vs-Shokz swimming lane", "action_source": "primary",
             "why_this_first": "AI names Shokz on the specific ask.",
             "first_move": "Publish the comparison."},
            {"headline": "Re-test failed SKU prompt: bone conduction …",
             "action_source": "secondary",
             "why_this_first": "Named in the failing prompt evidence.",
             "first_move": "Revise the PDP, re-run the prompt."},
        ],
        "competitive_snapshot": {"available": True, "top_cited_hosts": ["wired.com"]},
        "sku_summaries": [
            {"sku_title": "Purra Swim Headphones", "score": {"display": 2.3, "scale_max": 10},
             "band_display": {"label": "Found by AI, but not agent-ready"}},
            {"sku_title": "Run Plus Open-Ear", "score": {"display": 5.1, "scale_max": 10},
             "band_display": {"label": "Recommended, but not agent-ready"}},
        ],
        "meta": {"providers": ["gemini", "chatgpt"], "products_audited": 2,
                 "honest_limits": ["Sample coverage."]},
    }


def test_deck_has_share_of_voice_slide_with_rank():
    slides = _slide_texts(build_report_deck(_rich_summary()))
    sov = next(s for s in slides if "Where you rank in AI answers" in s)
    assert "You rank #3 of 4 brands named." in sov  # Shokz, Suunto, Mojawa, Bose
    assert "Shokz" in sov and "100%" in sov
    assert "Mojawa  (you)" in sov and "73%" in sov


def test_deck_cover_shows_subscores_and_real_trend():
    cover = _slide_texts(build_report_deck(_rich_summary()))[0]
    assert "AI visibility 0.6" in cover
    assert "First-party citation 4.6" in cover
    # Real movement from since_last_audit, NOT the null score.delta.
    assert "First-party citation 39 → 46 ▲" in cover


def test_deck_leads_with_weakest_dimension_diagnosis():
    found = next(s for s in _slide_texts(build_report_deck(_rich_summary()))
                 if "What we found" in s)
    assert "Biggest drag on your score: Product identity (2.3/10)." in found


def test_deck_drops_secondary_qa_actions():
    slides = _slide_texts(build_report_deck(_rich_summary()))
    assert any("Win the vs-Shokz swimming lane" in s for s in slides)
    # The QA re-test secondary is a portal to-do, never a boardroom slide.
    assert not any("Re-test failed SKU prompt" in s for s in slides)


def test_deck_has_per_product_scorecard_when_multi_sku():
    slides = _slide_texts(build_report_deck(_rich_summary()))
    card = next(s for s in slides if "Product-by-product" in s)
    assert "Purra Swim Headphones" in card and "2.3/10" in card
    assert "Run Plus Open-Ear" in card and "5.1/10" in card
    assert "Recommended, but not agent-ready" in card


def test_deck_single_sku_has_no_scorecard():
    s = _rich_summary()
    s["sku_summaries"] = s["sku_summaries"][:1]
    slides = _slide_texts(build_report_deck(s))
    assert not any("Product-by-product" in x for x in slides)


# ── More moves: the full inventory reaches the deck, not just NBA actions ────

def _moves_summary():
    s = _rich_summary()
    s["get_cited_moves"] = [
        {"host": "swimswam.com", "headline": "Pitch swimswam.com",
         "first_move": "Earn a review here.", "realism": "reachable",
         "already_endorses_you": False,
         "for_questions": ["what headphones are good for lap swimming workouts?"]},
        {"host": "wired.com", "headline": "Build on wired.com",
         "first_move": "Extend the coverage.", "already_endorses_you": True,
         "for_questions": []},
    ]
    s["winnable_lanes"] = [
        {"query": "ip68 waterproof headphones for competitive swimmers",
         "win_path": "own_content", "win_condition": "Publish a spec page.", "target_hosts": []},
        {"query": "what headphones are good for lap swimming workouts?",
         "win_path": "publisher", "win_condition": "Get cited.", "target_hosts": ["swimswam.com"]},
    ]
    return s


def test_deck_has_where_to_earn_citations_slide():
    slides = _slide_texts(build_report_deck(_moves_summary()))
    s = next(x for x in slides if "Where to earn citations" in x)
    assert "Pitch swimswam.com" in s
    assert "what headphones are good for lap swimming workouts?" in s
    assert "Build on wired.com" in s  # already-endorses → "Build on", not "Pitch"


def test_deck_has_winnable_lanes_slide_with_paths():
    slides = _slide_texts(build_report_deck(_moves_summary()))
    s = next(x for x in slides if "Lanes you can win" in x)
    assert "ip68 waterproof headphones for competitive swimmers" in s
    assert "Win with your own page" in s
    assert "Get cited on a publisher" in s and "swimswam.com" in s


def test_deck_move_slides_absent_when_no_moves():
    s = _moves_summary()
    s["get_cited_moves"] = []
    s["winnable_lanes"] = []
    slides = _slide_texts(build_report_deck(s))
    assert not any("Where to earn citations" in x for x in slides)
    assert not any("Lanes you can win" in x for x in slides)


# ── Continuous-use: the outcome loop (what moved since last audit) ──────────

def test_deck_progress_slide_shows_wins():
    s = _rich_summary()
    s["progress"] = {
        "available": True, "is_first_audit": False,
        "note": "Observed re-audit facts; not proof of causation.",
        "summary": {"won": 2, "progress": 1, "no_change": 3, "no_longer_grounded": 0},
        "wins": [{"host": "swimswam.com", "what_changed": "swimswam.com now recommends you.", "query": None}],
        "in_progress": [{"host": "rtings.com", "what_changed": "rtings.com now names you.", "query": None}],
    }
    slides = _slide_texts(build_report_deck(s))
    p = next(x for x in slides if "What moved since last audit" in x)
    assert "2 hosts now recommend you" in p
    assert "swimswam.com now recommends you." in p
    assert "not proof of causation" in p  # honesty note carried


def test_deck_progress_slide_honest_when_nothing_moved():
    s = _rich_summary()
    s["progress"] = {
        "available": True, "is_first_audit": False, "note": None,
        "summary": {"won": 0, "progress": 0, "no_change": 3, "no_longer_grounded": 1},
        "wins": [], "in_progress": [],
    }
    p = next(x for x in _slide_texts(build_report_deck(s)) if "What moved since last audit" in x)
    assert "No new citations landed at your targets yet" in p
    assert "1 dropped off" in p  # regressions surfaced, not hidden
    assert "Working the moves below is what moves this" in p


def test_deck_no_progress_slide_on_first_audit():
    s = _rich_summary()
    s["progress"] = {"available": False, "is_first_audit": True, "note": "First audit.",
                     "summary": {"won": 0, "progress": 0, "no_change": 0, "no_longer_grounded": 0},
                     "wins": [], "in_progress": []}
    assert not any("What moved since last audit" in x for x in _slide_texts(build_report_deck(s)))
