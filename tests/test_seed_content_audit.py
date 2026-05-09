"""Tests for services/seed_content_audit.py.

Anchor cases come from the user-reported PDP on 2026-05-09:
  https://agent.pivota.cc/products/sig_7259d7590d30998998b8db36c3c214d6

Description had `&rsquo;` HTML entities; pdp_ingredients_raw had Fenty
shade names ("TWO'LIP KISS, SORTA $ELFISH, ...") concatenated with
the actual INCI list. The auditor must detect + fix both.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.seed_content_audit import (  # noqa: E402
    AUDITOR_VERSION,
    audit_seed_data,
    has_html_entities,
    has_shade_name_prefix,
    html_entity_decode,
    strip_shade_name_prefix,
)


# ---------------------------------------------------------------------------
# html_entity_decode
# ---------------------------------------------------------------------------


def test_html_entity_decode_handles_apostrophe_entity() -> None:
    """The trigger from the user's PDP — `&rsquo;` everywhere."""
    raw = "LIP COLOR + CARE LINK&rsquo;D UP\n\nSTRAIGHT UP: \nIt&rsquo;s about to get juicy"
    out = html_entity_decode(raw)
    assert "&rsquo;" not in out
    assert "LINK’D UP" in out
    assert "It’s about to get juicy" in out


def test_html_entity_decode_handles_nbsp_and_amp() -> None:
    raw = "BIS-DIGLYCERYL&nbsp;POLYACYLADIPATE-2 &amp; SQUALANE"
    out = html_entity_decode(raw)
    assert "&nbsp;" not in out
    assert "&amp;" not in out
    assert "BIS-DIGLYCERYL POLYACYLADIPATE-2 & SQUALANE" == out


def test_html_entity_decode_passes_through_clean_text() -> None:
    """Text without entities is returned unchanged."""
    assert html_entity_decode("clean description") == "clean description"


def test_html_entity_decode_handles_none_and_non_string() -> None:
    """Defensive — never crash on None / int / dict."""
    assert html_entity_decode(None) is None
    assert html_entity_decode(123) == 123
    assert html_entity_decode({"x": 1}) == {"x": 1}


def test_has_html_entities_correctly_distinguishes() -> None:
    assert has_html_entities("It&rsquo;s") is True
    assert has_html_entities("plain text") is False
    assert has_html_entities(None) is False
    assert has_html_entities(123) is False


# ---------------------------------------------------------------------------
# strip_shade_name_prefix
# ---------------------------------------------------------------------------


def test_strip_shade_prefix_user_reported_fenty_example() -> None:
    """Exact string from the user's failing PDP. Shade names like
    'TWO'LIP KISS' / 'SORTA $ELFISH' / 'BLAZ'D DONUT' precede the
    real INCI list separated by a colon."""
    raw = (
        "TWO'LIP KISS, SORTA $ELFISH, RIRI, BLAZ'D DONUT, SP'ICE COLD, "
        "HIGH'BISCUS, FENTY GLOW, IS IT FU$$Y:  BIS-DIGLYCERYL POLYACYLADIPATE-2, "
        "DIISOSTEARYL MALATE, HYDROGENATED POLYISOBUTENE, SQUALANE"
    )
    out = strip_shade_name_prefix(raw)
    # The colon-separator removed; only INCI remains.
    assert out.startswith("BIS-DIGLYCERYL POLYACYLADIPATE-2")
    assert "TWO'LIP KISS" not in out
    assert "SORTA $ELFISH" not in out
    assert "FENTY GLOW" not in out
    assert "DIISOSTEARYL MALATE" in out


def test_strip_shade_prefix_preserves_clean_inci() -> None:
    """A normal INCI list with no shade prefix is unchanged."""
    raw = (
        "AQUA, GLYCERIN, NIACINAMIDE, BUTYLENE GLYCOL, "
        "PHENOXYETHANOL, SODIUM HYALURONATE"
    )
    out = strip_shade_name_prefix(raw)
    assert out == raw


def test_strip_shade_prefix_does_not_strip_single_token_prefix() -> None:
    """Single-word prefix isn't a shade list — guard against
    false-positive on legit ingredient strings that happen to
    contain a colon (e.g. 'Active: Niacinamide')."""
    raw = "ACTIVE: AQUA, GLYCERIN, NIACINAMIDE"
    out = strip_shade_name_prefix(raw)
    # No comma in the prefix → not detected as shade list → unchanged
    assert out == raw


def test_strip_shade_prefix_does_not_strip_section_headers() -> None:
    """Headers like 'INGREDIENTS:' or 'WATER PHASE:' are single-token
    even if uppercase. Should NOT be stripped (they're informative)."""
    raw = "INGREDIENTS: AQUA, GLYCERIN"
    out = strip_shade_name_prefix(raw)
    assert out == raw


def test_strip_shade_prefix_handles_none_and_non_string() -> None:
    assert strip_shade_name_prefix(None) is None
    assert strip_shade_name_prefix(123) == 123
    assert strip_shade_name_prefix("") == ""


def test_has_shade_name_prefix_distinguishes() -> None:
    assert has_shade_name_prefix("X'Y, A$B: AQUA, GLYCERIN") is True
    assert has_shade_name_prefix("AQUA, GLYCERIN") is False
    assert has_shade_name_prefix(None) is False


# ---------------------------------------------------------------------------
# audit_seed_data
# ---------------------------------------------------------------------------


def test_audit_full_fenty_example_produces_clean_output_and_summary() -> None:
    """End-to-end on a synthetic seed_data shaped like the user's
    failing PDP. Both fixes should fire and the review_summary should
    record what was done."""
    seed = {
        "title": "Gloss Bomb Stix High-Shine Gloss Stick — Shimmering Icy Amethy$t",
        "description": "LIP COLOR + CARE LINK&rsquo;D UP\n\nSTRAIGHT UP: \nIt&rsquo;s about to get juicy.",
        "pdp_ingredients_raw": (
            "TWO'LIP KISS, SORTA $ELFISH, RIRI: BIS-DIGLYCERYL POLYACYLADIPATE-2, DIISOSTEARYL MALATE"
        ),
        "brand": "Fenty Beauty",
    }
    cleaned, summary = audit_seed_data(seed)

    # Description had HTML entities decoded
    assert "&rsquo;" not in cleaned["description"]
    assert "It’s about to get juicy" in cleaned["description"]

    # Ingredients had shade-prefix stripped
    assert cleaned["pdp_ingredients_raw"].startswith("BIS-DIGLYCERYL POLYACYLADIPATE-2")
    assert "TWO'LIP KISS" not in cleaned["pdp_ingredients_raw"]

    # Original input is unmodified (no in-place mutation surprise)
    assert "&rsquo;" in seed["description"]
    assert "TWO'LIP KISS" in seed["pdp_ingredients_raw"]

    # Review summary records both fixes
    assert summary["auditor"] == AUDITOR_VERSION
    assert summary["review_status"] == "auto_corrected"
    assert "html_entities_in_description" in summary["issues_detected"]
    assert "shade_prefix_in_pdp_ingredients_raw" in summary["issues_detected"]
    assert "decoded_html_entities_in_description" in summary["fixes_applied"]
    assert "stripped_shade_prefix_from_pdp_ingredients_raw" in summary["fixes_applied"]


def test_audit_returns_no_issues_for_clean_input() -> None:
    """Already-clean seed should pass through unchanged with status
    'no_issues_found' so we can distinguish 'audited and OK' from
    'never audited'."""
    seed = {
        "title": "Vitamin C Serum",
        "description": "A daily serum.",
        "pdp_ingredients_raw": "AQUA, ASCORBIC ACID, GLYCERIN",
    }
    cleaned, summary = audit_seed_data(seed)
    assert cleaned == seed
    assert summary["review_status"] == "no_issues_found"
    assert summary["issues_detected"] == []
    assert summary["fixes_applied"] == []


def test_audit_recurses_into_snapshot() -> None:
    """seed_data.snapshot is where shopify scrapes mirror many text
    fields. The auditor must clean those too — otherwise the
    chat-mode response (which sometimes reads from snapshot) shows
    uncleaned content."""
    seed = {
        "title": "X",
        "snapshot": {
            "description": "It&rsquo;s nice",
            "pdp_ingredients_raw": "SHADE1, SHADE2: AQUA, GLYCERIN",
        },
    }
    cleaned, summary = audit_seed_data(seed)
    assert cleaned["snapshot"]["description"] == "It’s nice"
    assert cleaned["snapshot"]["pdp_ingredients_raw"] == "AQUA, GLYCERIN"
    assert "html_entities_in_snapshot.description" in summary["issues_detected"]
    assert "shade_prefix_in_snapshot.pdp_ingredients_raw" in summary["issues_detected"]


def test_audit_handles_none_seed_data_safely() -> None:
    """Defensive — never crash on None or non-dict input."""
    cleaned, summary = audit_seed_data(None)
    assert cleaned == {}
    assert summary["review_status"] == "issues_unresolved"
    assert "seed_data_not_a_dict" in summary["issues_detected"]


def test_audit_summary_has_auditor_version_and_timestamp() -> None:
    """Pin the summary shape — operator dashboards rely on these
    fields existing."""
    cleaned, summary = audit_seed_data({"description": "It&rsquo;s nice"})
    assert summary["auditor"] == AUDITOR_VERSION
    assert "audited_at" in summary
    # ISO-8601 UTC string
    assert "T" in summary["audited_at"]
    assert summary["audited_at"].endswith("+00:00")
