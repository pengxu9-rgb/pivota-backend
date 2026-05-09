"""Deterministic seed content auditor + corrector.

In-backend equivalent of codex's `seed-content-audit` + `seed-correction`
skills (per docs/CODEX_SKILLS.md). The user reported that the codex
cycle keeps regressing — that data race is fixed by PR #409 (preserve
review fields on re-mirror), but we still need the actual auditor +
corrector to populate those fields. Doing it in Python here so the
fixes are version-controlled, reproducible, and don't depend on
codex availability.

Per the codex skill rule: **no LLM-authored product copy**. All
corrections here are deterministic — regex / rule-based fixes only.

Coverage today:
  - HTML entity decoding (e.g. `&rsquo;` → `'`, `&nbsp;` → space) on
    text fields. Trigger: scraped seed_data fields had raw entities
    that surfaced in chat-mode PDPs. Fixed via `html.unescape`.
  - Shade-name prefix stripping in INCI ingredients lists. Trigger:
    Fenty Beauty `pdp_ingredients_raw` looked like
    `"TWO'LIP KISS, SORTA $ELFISH, ...: BIS-DIGLYCERYL POLYACYLADIPATE-2, ..."`
    where the part before `:` is shade names accidentally captured
    by the extractor as part of the ingredients string. Detect the
    prefix pattern (multiple uppercase comma-separated tokens with
    $/'/% chars) and drop everything before the colon.

Out of scope (would need separate work or LLM judgment):
  - Locale normalization (Korean text in US listings)
  - Image URL liveness validation
  - Boilerplate (cookie banners, nav) stripping from descriptions

The auditor returns a `review_summary` dict written into
`external_product_seeds.seed_data.review_summary`. PR #409 ensures
this field survives subsequent backfill / re-extraction runs.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


AUDITOR_VERSION = "seed_content_audit_v1"


# ---------------------------------------------------------------------------
# Pattern for detecting shade-name contamination in INCI ingredient lists.
# ---------------------------------------------------------------------------

# A shade-name prefix looks like:
#   "<NAME1>, <NAME2>, ..., <NAMEK>: <real_inci>"
# where each NAMEi is an UPPERCASE token possibly containing $/'/&/!/numbers
# (Fenty's stylized shade names: "SORTA $ELFISH", "TWO'LIP KISS", etc.).
# Real INCI starts after the colon (BIS-DIGLYCERYL..., AQUA, etc.).
#
# We require >= 2 comma-separated tokens before the colon to avoid false
# positives on legitimate INCI strings that happen to have a colon
# (e.g. an ingredient name that contains punctuation). >= 2 tokens is
# also the lower bound for "this looks like a list, not a header".
_SHADE_PREFIX_RE = re.compile(
    r"^\s*"
    # First token: uppercase + optional special chars + space allowed
    r"([A-Z][\w\$\'\&\!\-]*(?:\s+[A-Z\$\'\&\!\d][\w\$\'\&\!\-]*)*)"
    # Second through Nth tokens, each preceded by comma
    r"(?:,\s*[A-Z\$\'\&\!\d][\w\$\'\&\!\-]*(?:\s+[A-Z\$\'\&\!\d][\w\$\'\&\!\-]*)*)+"
    # Colon separator
    r"\s*:\s*"
)


# Text fields in seed_data that should have HTML entities decoded.
_TEXT_FIELDS_TO_DECODE: Tuple[str, ...] = (
    "title",
    "description",
    "pdp_description_raw",
    "pdp_ingredients_raw",
    "pdp_active_ingredients_raw",
    "pdp_how_to_use_raw",
    "raw_ingredient_text_clean",
)


# Ingredient-list fields that may have shade-name prefix contamination.
_INGREDIENT_FIELDS_TO_DESHADE: Tuple[str, ...] = (
    "pdp_ingredients_raw",
    "raw_ingredient_text_clean",
)


def html_entity_decode(text: Optional[str]) -> Optional[str]:
    """Decode HTML entities (`&rsquo;` → `'`, `&nbsp;` → space, etc.).
    Returns the input unchanged when not a string."""
    if text is None or not isinstance(text, str):
        return text
    return html.unescape(text)


def has_html_entities(text: Optional[str]) -> bool:
    """Heuristic: a string contains HTML entities if `html.unescape`
    returns a different value. Cheap; runs the decode anyway."""
    if not isinstance(text, str):
        return False
    return html.unescape(text) != text


def strip_shade_name_prefix(ingredients: Optional[str]) -> Optional[str]:
    """If an INCI list begins with a shade-name prefix (Fenty pattern),
    strip everything up to and including the first colon.

    Example:
      input:  "TWO'LIP KISS, SORTA $ELFISH, RIRI: BIS-DIGLYCERYL POLY..."
      output: "BIS-DIGLYCERYL POLY..."

    Returns the input unchanged when not a string or when no shade
    prefix is detected."""
    if not isinstance(ingredients, str):
        return ingredients
    m = _SHADE_PREFIX_RE.match(ingredients)
    if not m:
        return ingredients
    return ingredients[m.end():]


def has_shade_name_prefix(ingredients: Optional[str]) -> bool:
    """True iff strip_shade_name_prefix would change the input."""
    if not isinstance(ingredients, str):
        return False
    return bool(_SHADE_PREFIX_RE.match(ingredients))


def audit_seed_data(seed_data: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Run all deterministic checks on a seed_data dict. Returns a pair
    (cleaned_seed_data, review_summary). Doesn't mutate the input.

    `review_summary` is a small dict that can be merged into
    seed_data["review_summary"] to mark the row as audited. Includes
    the auditor version, timestamp, list of issues detected, and
    list of fixes applied."""
    issues: List[str] = []
    fixes_applied: List[str] = []

    if not isinstance(seed_data, dict):
        # Defensive: nothing to audit. Return an empty cleaned dict +
        # a summary that records the unusable input.
        return {}, _make_review_summary(["seed_data_not_a_dict"], [])

    cleaned: Dict[str, Any] = dict(seed_data)

    # 1. HTML entity decoding on text fields.
    for key in _TEXT_FIELDS_TO_DECODE:
        val = cleaned.get(key)
        if not isinstance(val, str):
            continue
        if has_html_entities(val):
            issues.append(f"html_entities_in_{key}")
            cleaned[key] = html_entity_decode(val)
            fixes_applied.append(f"decoded_html_entities_in_{key}")

    # 2. Shade-name prefix stripping on INCI fields.
    for key in _INGREDIENT_FIELDS_TO_DESHADE:
        val = cleaned.get(key)
        if not isinstance(val, str):
            continue
        if has_shade_name_prefix(val):
            issues.append(f"shade_prefix_in_{key}")
            cleaned[key] = strip_shade_name_prefix(val)
            fixes_applied.append(f"stripped_shade_prefix_from_{key}")

    # Also recurse into seed_data["snapshot"] (shopify-scraped pages
    # mirror many fields under here).
    snapshot = cleaned.get("snapshot")
    if isinstance(snapshot, dict):
        snap_cleaned = dict(snapshot)
        snap_changed = False
        for key in _TEXT_FIELDS_TO_DECODE:
            val = snap_cleaned.get(key)
            if isinstance(val, str) and has_html_entities(val):
                issues.append(f"html_entities_in_snapshot.{key}")
                snap_cleaned[key] = html_entity_decode(val)
                fixes_applied.append(f"decoded_html_entities_in_snapshot.{key}")
                snap_changed = True
        for key in _INGREDIENT_FIELDS_TO_DESHADE:
            val = snap_cleaned.get(key)
            if isinstance(val, str) and has_shade_name_prefix(val):
                issues.append(f"shade_prefix_in_snapshot.{key}")
                snap_cleaned[key] = strip_shade_name_prefix(val)
                fixes_applied.append(f"stripped_shade_prefix_from_snapshot.{key}")
                snap_changed = True
        if snap_changed:
            cleaned["snapshot"] = snap_cleaned

    review_summary = _make_review_summary(issues, fixes_applied)
    return cleaned, review_summary


def _make_review_summary(issues: List[str], fixes_applied: List[str]) -> Dict[str, Any]:
    """Build the review_summary payload written back into seed_data.
    Keeping the shape stable so downstream tooling (codex skills,
    operator dashboards) can rely on it."""
    if fixes_applied:
        status = "auto_corrected"
    elif issues:
        status = "issues_unresolved"
    else:
        status = "no_issues_found"
    return {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "auditor": AUDITOR_VERSION,
        "review_status": status,
        "issues_detected": issues,
        "fixes_applied": fixes_applied,
    }
