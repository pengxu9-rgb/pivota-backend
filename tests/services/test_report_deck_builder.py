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
