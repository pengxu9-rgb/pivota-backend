"""Display-title normalization for the agent-facing surface.

The agent_pdp_view title (mig 085, Stage 3a) is what a frontier agent shows a
shopper, so it must be a clean product name -- not a raw storefront string like
``NUTRIONE CO., LTD [Bundle] Good Night Collagen 30 sticks's page``. An output
audit found such raw-title leaks reaching agent + merchant copy.

This is deliberately a DISPLAY normalizer, distinct from
``normalize_product_title_for_search`` in bd_cold_start_service (which strips
trailing pack/size descriptors to build a search query). For display we KEEP
size/pack info -- "30 sticks" is exactly what a buyer wants to see -- and only
remove non-product noise: leading bracket tags, a leading legal-entity seller
prefix, and a trailing "'s page" artifact. Every transform has a conservative
backoff so we never return something thinner than the original product name.

Kept dependency-light (regex only) so the assembler can import it cheaply.
"""

from __future__ import annotations

import re

# Leading marketing/bundle bracket tags: "[Bundle]", "[SALE] [New]", etc.
_LEADING_BRACKET_TAGS_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")

# A leading corporate/legal-entity seller name that prefixes the product, e.g.
# "NUTRIONE CO., LTD ", "Acme Inc. - ", "Foo Co.,Ltd : ". Non-greedy up to the
# FIRST legal suffix so we strip the seller prefix, not the product name. The
# trailing separator requirement (whitespace, optionally after , : -) avoids
# matching a product that merely contains a suffix-like word.
_LEADING_ENTITY_PREFIX_RE = re.compile(
    r"^\s*.{1,60}?\b"
    r"(?:CO\.?\s*,?\s*LTD|CO\.?\s*,?\s*LIMITED|LIMITED|LTD|INC|"
    r"L\.?L\.?C|CORP(?:ORATION)?|GMBH|PTE\.?\s*,?\s*LTD)"
    r"\b\.?\s*[,:\-]?\s+",
    re.IGNORECASE,
)

# Trailing "'s page" / "’s page" artifact from constructed identity copy.
_TRAILING_PAGE_ARTIFACT_RE = re.compile(r"\s*['’]s\s+page\s*$", re.IGNORECASE)


def _clean_surface(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip(" \t\r\n,.;:-|/")


def _token_count(title: str) -> int:
    return len(re.findall(r"\S+", title or ""))


def normalize_display_title(raw_title: str) -> str:
    """Clean a storefront title for display on the agent surface.

    Removes leading bracket tags, a leading legal-entity seller prefix, and a
    trailing "'s page" artifact, while KEEPING size/pack info. Falls back to a
    bracket-only strip (or the original) whenever a transform would leave the
    title too thin to be a usable product name.
    """
    original = _clean_surface(raw_title)
    if not original:
        return raw_title or ""

    cleaned = original
    # Seller prefix and bracket tags can appear in either order
    # ("NUTRIONE CO., LTD [Bundle] ...") -- loop until stable.
    for _ in range(3):
        before = cleaned
        cleaned = _clean_surface(_LEADING_BRACKET_TAGS_RE.sub("", cleaned))
        cleaned = _clean_surface(_LEADING_ENTITY_PREFIX_RE.sub("", cleaned))
        if cleaned == before:
            break
    cleaned = _clean_surface(_TRAILING_PAGE_ARTIFACT_RE.sub("", cleaned))

    if _is_usable(cleaned, original):
        return cleaned

    # Backoff: a bracket-only strip is the safest meaningful cleanup.
    bracket_only = _clean_surface(_LEADING_BRACKET_TAGS_RE.sub("", original))
    if _is_usable(bracket_only, original):
        return bracket_only
    return original


def _is_usable(candidate: str, original: str) -> bool:
    if len(candidate) < 3 or not any(ch.isalpha() for ch in candidate):
        return False
    # Don't collapse a multi-word product down to a single token.
    if _token_count(candidate) < 2 <= _token_count(original):
        return False
    return True
