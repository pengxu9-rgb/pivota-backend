"""Structured skincare attributes (K-beauty SKINCARE module).

The data contract's skincare attribute set is mostly served by existing schema
(skin_concern, key_actives, routine_step, size, certs). This fills the niche-new
gaps an agent needs to match fit and stay claim-safe:

  * format     -- the product form (serum, cream, sheet mask, sunscreen, ...),
  * spf_value  -- the SPF rating for sunscreens (an OTC drug; the agent must
                  surface it), and
  * fragrance_free -- a load-bearing flag for sensitive-skin fit,

plus merging labelled concentration into the key-actives list so an active reads
as "Niacinamide 5%", not just "Niacinamide".

format and spf_value are derivable from product text (no authoring needed);
fragrance_free is detected only from an explicit claim (never inferred from an
ingredient list, which would be unsafe). Pure functions -- reused by the read
path, sync extraction, and authoring; unit-tested.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Canonical skincare formats, ordered so more specific phrases win.
_FORMAT_PATTERNS = [
    ("sheet mask", re.compile(r"\bsheet\s*masks?\b", re.IGNORECASE)),
    ("sunscreen", re.compile(r"\b(?:sunscreens?|sun\s*cream|spf)\b", re.IGNORECASE)),
    ("cleanser", re.compile(r"\b(?:cleanser|cleansing\s*(?:foam|gel|oil|balm)|face\s*wash)\b", re.IGNORECASE)),
    ("toner", re.compile(r"\btoners?\b", re.IGNORECASE)),
    ("essence", re.compile(r"\bessences?\b", re.IGNORECASE)),
    ("ampoule", re.compile(r"\bampoules?\b", re.IGNORECASE)),
    ("serum", re.compile(r"\bserums?\b", re.IGNORECASE)),
    ("eye cream", re.compile(r"\beye\s*creams?\b", re.IGNORECASE)),
    ("mask", re.compile(r"\b(?:masks?|sleeping\s*pack)\b", re.IGNORECASE)),
    ("oil", re.compile(r"\b(?:face\s*oil|facial\s*oil)\b", re.IGNORECASE)),
    ("mist", re.compile(r"\bmists?\b", re.IGNORECASE)),
    ("gel", re.compile(r"\bgel\b", re.IGNORECASE)),
    ("lotion", re.compile(r"\blotions?\b", re.IGNORECASE)),
    ("cream", re.compile(r"\b(?:creams?|moisturi[sz]ers?)\b", re.IGNORECASE)),
    ("balm", re.compile(r"\bbalms?\b", re.IGNORECASE)),
    ("exfoliant", re.compile(r"\b(?:exfoliants?|peel(?:ing)?|scrubs?)\b", re.IGNORECASE)),
]

_SPF_RE = re.compile(r"\bspf\s*(\d{1,3})\b", re.IGNORECASE)

# Explicit fragrance-free claims only. "unscented" is intentionally excluded --
# it can still contain masking fragrance, so it is not a safe fragrance-free flag.
_FRAGRANCE_FREE_RE = re.compile(
    r"\b(?:fragrance[\s-]?free|no\s+(?:added\s+)?fragrance|parfum[\s-]?free|no\s+parfum)\b",
    re.IGNORECASE,
)


def extract_format(
    title: Optional[str] = None,
    product_type: Optional[str] = None,
    category_path: Optional[str] = None,
) -> Optional[str]:
    """Resolve a canonical skincare format from product text, or None."""
    blob = " ".join(str(v or "") for v in (title, product_type, category_path))
    for fmt, pattern in _FORMAT_PATTERNS:
        if pattern.search(blob):
            return fmt
    return None


def extract_spf_value(*texts: Any) -> Optional[int]:
    """First plausible SPF rating found in the given texts, or None."""
    for text in texts:
        match = _SPF_RE.search(str(text or ""))
        if match:
            value = int(match.group(1))
            if 1 <= value <= 110:
                return value
    return None


def detect_fragrance_free(*texts: Any) -> bool:
    """True only when an explicit fragrance-free claim is present."""
    blob = " ".join(str(t or "") for t in texts)
    return bool(_FRAGRANCE_FREE_RE.search(blob))


def _active_label(active: Dict[str, Any]) -> str:
    return str(active.get("label") or active.get("ingredient") or active.get("name") or "").strip()


def _concentration_index(concentration_notes: Any) -> Dict[str, str]:
    """Build {lowercased ingredient label -> concentration} from the notes,
    tolerating either a list of dicts or a flat {label: value} mapping."""
    index: Dict[str, str] = {}
    if isinstance(concentration_notes, dict):
        for key, value in concentration_notes.items():
            label = str(key or "").strip().lower()
            conc = str(value or "").strip()
            if label and conc:
                index[label] = conc
        return index
    if isinstance(concentration_notes, list):
        for item in concentration_notes:
            if not isinstance(item, dict):
                continue
            label = _active_label(item).lower()
            conc = str(item.get("concentration") or item.get("percent") or "").strip()
            if label and conc:
                index[label] = conc
    return index


def merge_concentration_into_actives(
    active_ingredients: List[Dict[str, Any]],
    concentration_notes: Any,
) -> List[Dict[str, Any]]:
    """Return active-ingredient dicts enriched with a `concentration` value where
    one is known. Non-destructive: leaves an active untouched when no match or
    when it already carries a concentration."""
    index = _concentration_index(concentration_notes)
    if not index:
        return active_ingredients
    merged: List[Dict[str, Any]] = []
    for active in active_ingredients:
        if not isinstance(active, dict):
            merged.append(active)
            continue
        if active.get("concentration"):
            merged.append(active)
            continue
        conc = index.get(_active_label(active).lower())
        merged.append({**active, "concentration": conc} if conc else active)
    return merged
