"""Numeric <sup>/<sub> become Unicode sub/superscripts instead of digits glued to their word.

Flattened to plain digits, a footnote marker read as part of its word ("Ceramide<sup>1</sup>" ->
"Ceramide1", "week<sup>3</sup>." -> "week3.") and a formula lost its subscript. Digits (with + - = ( )
and comma lists) now render as their Unicode characters, still attached; every other sub/sup -- an
ordinal, a mark, a symbol footnote, a letter -- is attached exactly as before. Run through the real
producer.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import services.curated_brand_feed as cbf
from services.curated_brand_feed import (body_html_sentences, body_html_to_text, boilerplate_class,
                                         drop_repeated_store_sentences, is_safety_text)


@pytest.mark.parametrize("body,expected", [
    ("<p>5x the Ceramide<sup>1</sup> to restore.</p>", "5x the Ceramide¹ to restore."),
    ("<p>Results in just 1 week<sup>3</sup>.</p>", "Results in just 1 week³."),
    ("<p>Extract<sup>2</sup> for glow.</p>", "Extract² for glow."),
    ("<p>Glass jar<sup>4</sup> and cap<sup>5</sup>.</p>", "Glass jar⁴ and cap⁵."),
    ("<p>H<sub>2</sub>O and CO<sub>2</sub>.</p>", "H₂O and CO₂."),
    ("<p>Covers 50 m<sup>2</sup>.</p>", "Covers 50 m²."),
    ("<p>10<sup>6</sup> cells.</p>", "10⁶ cells."),
    ("<p>10<sup>-6</sup> and 10<sup>−6</sup>.</p>", "10⁻⁶ and 10⁻⁶."),
    ("<p>Ca<sup>2+</sup> ions.</p>", "Ca²⁺ ions."),
    ("<p>Peaks<sup>(1)</sup>.</p>", "Peaks⁽¹⁾."),
    ("<p>Ratio<sub>1=2</sub>.</p>", "Ratio₁₌₂."),
    ("<p>Ceramide<sup> 1 </sup> spaced inside.</p>", "Ceramide¹ spaced inside."),
    ("<p>Ceramide<sup><span>1</span></sup> nested.</p>", "Ceramide¹ nested."),
    ('<p>Ceramide<sup class="fn">1</sup> attributes.</p>', "Ceramide¹ attributes."),
    ("<p>Ceramide<SUP>1</SUP> upper case.</p>", "Ceramide¹ upper case."),
    ("&lt;p&gt;Ceramide&lt;sup&gt;1&lt;/sup&gt; escaped.&lt;/p&gt;", "Ceramide¹ escaped."),
    ("<p>Ceramide<sup>&#49;</sup> entity.</p>", "Ceramide¹ entity."),
    ("&amp;amp;lt;p&amp;amp;gt;Ceramide&amp;amp;lt;sup&amp;amp;gt;1&amp;amp;lt;/sup&amp;amp;gt; deep.&amp;amp;lt;/p&amp;amp;gt;",
     "Ceramide¹ deep."),
    ("<p><b>Ceramide</b><sup>1</sup> bold.</p>", "Ceramide¹ bold."),
])
def test_a_numeric_sub_or_superscript_is_rendered_as_unicode(body, expected):
    assert body_html_to_text(body) == expected


@pytest.mark.parametrize("body,expected", [
    # A COMMA LIST keeps its plain comma between the marks; a range keeps its hyphen as a superscript minus.
    ("<p>Peptides<sup>1,2</sup> work.</p>", "Peptides¹,² work."),
    ("<p>Peptides<sup>1, 2</sup> work.</p>", "Peptides¹,² work."),
    ("<p>Acids<sup>1-3</sup> work.</p>", "Acids¹⁻³ work."),
    ("<p>Oils<sup>1,2,10</sup> work.</p>", "Oils¹,²,¹⁰ work."),
])
def test_a_footnote_list_is_rendered_as_unicode(body, expected):
    assert body_html_to_text(body) == expected


@pytest.mark.parametrize("body,expected", [
    # Anything that is not digits is attached exactly as before.
    ("<p>The 10<sup>th</sup> serum.</p>", "The 10th serum."),
    ("<p>Brand<sup>®</sup> serum.</p>", "Brand® serum."),
    ("<p>Name<sup>TM</sup> serum.</p>", "NameTM serum."),
    ("<p>Retinol<sup>*</sup> serum.</p>", "Retinol* serum."),
    ("<p>Retinol<sup>†</sup> and peptide<sup>‡</sup>.</p>", "Retinol† and peptide‡."),
    ("<p>Extract<sup>a</sup> serum.</p>", "Extracta serum."),
    ("<p>Note<sup>1a</sup> serum.</p>", "Note1a serum."),
    ("<p>Sign<sup>+</sup> alone.</p>", "Sign+ alone."),
    ("<p>Empty<sup></sup> tag.</p>", "Empty tag."),
    ("<p>Comma<sup>,</sup> only.</p>", "Comma, only."),
])
def test_any_other_sub_or_superscript_is_attached_as_before(body, expected):
    assert body_html_to_text(body) == expected


def _fixture_bodies():
    """Every HTML-ish string literal in the description test modules."""
    out = set()
    for path in sorted(Path(__file__).parent.glob("test_curated_brand_feed_description*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                    "<" in node.value or "&lt;" in node.value):
                out.add(node.value)
    return sorted(out)


def test_text_without_a_numeric_sub_or_superscript_is_unchanged(monkeypatch):
    """Across every fixture of the description tests: with the numeral pass switched off, a body
    without a numeric sup/sub gives byte-identical output."""
    bodies = _fixture_bodies()
    assert len(bodies) > 50
    with_pass = {b: (body_html_to_text(b), body_html_sentences(b)) for b in bodies}
    monkeypatch.setattr(cbf, "_script_numeral", lambda m: m.group(0))
    changed = [b for b in bodies if with_pass[b] != (body_html_to_text(b), body_html_sentences(b))]
    assert all(re.search(r"(?i)(?:<|&lt;)\s*su[pb]\b", b) for b in changed), changed
    assert changed, "the fixtures carry at least one numeric sub/sup"


def test_a_safety_cue_before_a_superscript_is_still_a_cue():
    assert is_safety_text("Avoid contact with the eyes¹.")
    assert is_safety_text("Keep out of reach of children².")
    assert is_safety_text("Patch test₂ first.")
    assert not is_safety_text("Ceramide¹ restores the barrier.")


def test_a_store_wide_warning_with_a_footnote_is_never_dropped_by_the_repetition_pass():
    warning = body_html_to_text("<p>Avoid contact with eyes<sup>1</sup>.</p>")
    rows = [[f"Product {i} is a unique formula number {i} with its own long description of texture.",
             f"Second unique sentence {i} about its benefits and daily use for everyone.", warning]
            for i in range(12)]
    out, _ = drop_repeated_store_sentences(rows, kinds=[str(i) for i in range(12)])
    assert all(warning in row for row in out)


def test_sentences_still_split_after_a_superscript():
    assert body_html_sentences("<p>Results in one week<sup>3</sup>. Then more.</p>") == [
        "Results in one week³.", "Then more."]


def test_a_superscript_reads_like_the_digit_it_replaces_to_the_other_passes():
    """Every word-based pass (repetition keys, boilerplate classes) sees a superscript digit as a word
    character, exactly as it saw the glued ASCII digit: no sentence is judged differently."""
    for plain, script in (("Free shipping on orders over $501.", "Free shipping on orders over $50¹."),
                          ("Colours may vary due to your monitor settings1.",
                           "Colours may vary due to your monitor settings¹.")):
        assert boilerplate_class(plain) == boilerplate_class(script)
        assert len(cbf._sentence_key(plain).split()) == len(cbf._sentence_key(script).split())


def test_the_producer_is_deterministic():
    """A re-crawl of an unchanged body writes the same text: candidate_attribute_summary changes once."""
    body = "<p>5x the Ceramide<sup>1</sup> to restore. H<sub>2</sub>O based. Avoid eyes<sup>2</sup>.</p>"
    first = body_html_to_text(body)
    assert all(body_html_to_text(body) == first for _ in range(3))
    assert body_html_to_text(f"<p>{first}</p>") == first, "already-converted text passes through unchanged"
