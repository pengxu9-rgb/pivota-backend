"""Durable category_kind resolution (K-beauty common-core / category modules).

The agent-decision-grade data contract requires a durable category_kind in
{skincare, haircare, supplement} -- it drives claim-safety screening, required
disclaimers, and the serving gate's category checks, so the contract says it
must be durable and correct, NOT heuristic-defaulted.

Today only topical beauty paths are classified (catalog_products.category_path,
e.g. "beauty/skincare/..."); supplements have no category signal at all. This
resolver maps both:
  * skincare / haircare -- authoritative from the beauty category_path prefix.
  * supplement -- conservative text detection (no path exists), gated on
    ingestible signals so topical beauty (makeup sticks, setting powder) is
    never misread as a supplement.
Anything else returns None ("unknown") -- we never guess a category_kind.

Pure functions (no DB / no I/O): used by the sync write path, a backfill, and
read-time fallback; unit-tested with synthetic inputs.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

SKINCARE = "skincare"
HAIRCARE = "haircare"
SUPPLEMENT = "supplement"
CATEGORY_KINDS = frozenset({SKINCARE, HAIRCARE, SUPPLEMENT})

# Ingestible dosage forms (BB Lab uses "30 sticks"; Ownist jelly/powder, etc.).
_SUPPLEMENT_FORM_RE = re.compile(
    r"\b(?:capsules?|tablets?|softgels?|gummies|gummy|sachets?|sticks?|"
    r"powder|jelly|drink\s+mix|drinkable)\b",
    re.IGNORECASE,
)
# Explicit ingestible-supplement nouns -- strong enough on their own.
_SUPPLEMENT_NOUN_RE = re.compile(
    r"\b(?:dietary\s+supplements?|supplements?|probiotics?|multivitamins?|"
    r"vitamins?|nutricosmetics?|inner\s*beauty)\b",
    re.IGNORECASE,
)
# Ingestible actives -- only count as supplement when paired with a dosage form,
# since some (collagen, hyaluronic acid) also appear in topical skincare.
_INGESTIBLE_ACTIVE_RE = re.compile(
    r"\b(?:collagen|biotin|probiotics?|hyaluronic\s+acid|glutathione|"
    r"vitamin\s*[a-e]|zinc|omega|peptides?)\b",
    re.IGNORECASE,
)


def _blob(*values: Any) -> str:
    parts = [str(v or "") for v in values]
    return " ".join(parts).lower()


def _tags_text(tags: Any) -> str:
    if isinstance(tags, str):
        return tags.lower()
    if isinstance(tags, Iterable):
        return " ".join(str(t or "") for t in tags).lower()
    return ""


def _looks_like_supplement(text: str) -> bool:
    if _SUPPLEMENT_NOUN_RE.search(text):
        return True
    if _SUPPLEMENT_FORM_RE.search(text) and _INGESTIBLE_ACTIVE_RE.search(text):
        return True
    return False


def resolve_category_kind(
    category_path: Optional[str] = None,
    product_type: Optional[str] = None,
    title: Optional[str] = None,
    tags: Any = None,
) -> Optional[str]:
    """Resolve a durable category_kind, or None when it can't be determined
    confidently. Skincare/haircare come from the beauty category_path;
    supplements from conservative ingestible-signal detection."""
    path = (category_path or "").strip().lower()
    if path.startswith("beauty/skincare/"):
        return SKINCARE
    if path.startswith("beauty/haircare/"):
        return HAIRCARE
    # Any other topical beauty path (bath/body/makeup/tools) is not one of the
    # three contract kinds -- and must NOT fall through to supplement detection.
    if path.startswith("beauty/"):
        return None

    text = _blob(product_type, title) + " " + _tags_text(tags)
    if _looks_like_supplement(text):
        return SUPPLEMENT
    return None
