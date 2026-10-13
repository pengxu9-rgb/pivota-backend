"""Gaps a read-only backfill dry run found in the description cleanup (2026-10-13).

1. A truncation cut that lands right after a section heading must not end the copy on the bare heading.
2. A "Disclaimer:" label glued to product copy without punctuation starts its own unit, so only the disclaimer goes.
3. A photo/lighting colour disclaimer ("the color in the image may vary") is boilerplate; colour copy without that
   context is kept.
4. Quantity-tier bulk pricing ("10 pcs and above - 12% off") is boilerplate.
5. "follow <Brand> Instagram" is a social line; "follow the instructions" is not.
Invented names and text only. Run through the real producer.
"""
import pytest

from services.curated_brand_feed import BODY_TEXT_MAX_LEN, body_html_to_text, join_description_sentences

INTRO = " ".join(f"Sentence {i} describes the hydrating formula and how it layers under makeup." for i in range(1, 26))


def test_a_cut_after_a_heading_does_not_end_on_the_heading():
    inci = ", ".join(f"Ingredient{i}" for i in range(400))  # one long list, no sentence boundary
    out = body_html_to_text(f"<p>{INTRO}</p><h3>Full Ingredients</h3><p>{inci}</p>")
    assert len(out) <= BODY_TEXT_MAX_LEN
    assert not out.rstrip().endswith("Full Ingredients"), out[-80:]
    assert out.endswith("under makeup."), out[-80:]


@pytest.mark.parametrize("line", ["For external use only", "Keep away from heat", "Avoid eyes"])
def test_a_short_safety_line_at_the_cut_is_kept(line):
    """Post-merge-review repro: a short unpunctuated SAFETY line before the cut is not a heading."""
    out = join_description_sentences(["A" * 60 + ".", line, "B" * 200 + "."], max_len=100)
    assert out.endswith(line), out


def test_a_short_unit_inside_the_copy_is_not_dropped():
    out = join_description_sentences(["Key Benefits", "Calms redness.", "How to use:", "Apply twice daily."])
    assert out == "Key Benefits Calms redness. How to use: Apply twice daily."


def test_a_glued_disclaimer_goes_and_the_product_words_stay():
    body = ("<p>Highly pigmented colours Made in Japan Disclaimer: Due to photographic lighting sources and monitor "
            "settings, the color in the image may vary slightly to the actual color of the product.</p>")
    assert body_html_to_text(body) == "Highly pigmented colours Made in Japan"


def test_a_lighting_only_colour_disclaimer_is_removed():
    """No monitor/screen word: photographic lighting alone is the context."""
    body = ("<p>A long-wear gel colour.</p><p>Due to photographic lighting sources, the color in the image may vary "
            "slightly from the actual product.</p>")
    assert body_html_to_text(body) == "A long-wear gel colour."


@pytest.mark.parametrize("sentence", [
    "The color in the image may vary depending on how many coats you apply.",
    "Shades vary on different skin tones; see the swatch photos for reference.",
    "Note: shake well before use.",
])
def test_colour_and_note_copy_without_boilerplate_context_is_kept(sentence):
    assert body_html_to_text(f"<p>A long-wear gel colour.</p><p>{sentence}</p>").endswith(sentence)


def test_quantity_tier_bulk_pricing_is_removed():
    body = ("<p>A long-wear gel colour.</p>"
            "<p>MIX &amp; MATCH BULK DISCOUNT 10 pcs and above - 12% off 30 pcs and above - 22% off</p>")
    assert body_html_to_text(body) == "A long-wear gel colour."


def test_please_note_stays_one_unit():
    """'note:' inside 'Please note:' is not a second label: the packaging note is still removed whole."""
    assert body_html_to_text("<p>A firming cream.</p><p>Please note: packaging may vary as we update our design.</p>") \
        == "A firming cream."


def test_a_count_that_is_not_pricing_is_kept():
    sentence = "Contains 12 pieces and more than 30 shades to mix."
    assert body_html_to_text(f"<p>{sentence}</p>") == sentence


def test_follow_brand_on_a_platform_is_removed_but_follow_instructions_is_kept():
    out = body_html_to_text("<p>A firming cream.</p><p>Join the campaign, and follow Example Brand Instagram and "
                            "Facebook.</p><p>Follow the instructions on the box.</p>")
    assert out == "A firming cream. Follow the instructions on the box."
