"""Structured haircare attributes (K-beauty HAIRCARE module).

Mirrors ``skincare_attributes`` for the haircare category. The data contract's
haircare attribute set is mostly served by existing schema (hair concern via
concerns, key ingredients, size, usage). This fills the niche-new gaps an agent
needs to match fit and to *trust* a vegan/cruelty-free positioning:

  * format         -- shampoo/conditioner/treatment/hair serum/mask/oil/...,
  * sulfate_free / silicone_free -- load-bearing formulation flags for the
    "clean haircare" fit an agent matches on, and
  * vegan_status / cruelty_free_status -- VERIFIED vs merely CLAIMED.

That last distinction is the contract's load-bearing point for Anuko: existing
``lifestyle_tags`` already keyword-match "vegan"/"cruelty-free" off the product
text (mere *presence*), which is not trustworthy. A claim is **verified** only
when it is backed by a recognized certifying authority -- the Vegan Society /
Certified Vegan (vegan.org) / V-Label for vegan, Leaping Bunny / PETA / Choose
Cruelty Free for cruelty-free -- otherwise it is **claimed** (the marketing word
with nothing behind it). An agent can surface "verified vegan" with confidence
and treat "claimed" as unproven.

format and the formulation flags are derivable from product text; cert status
prefers an authored ``certifications`` list but recognizes named authorities in
text. Conservative by design: a bare "vegan" tag never reads as verified. Pure
functions (regex only) -- reused by the read path, sync extraction, and
authoring; unit-tested.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Canonical haircare formats, ordered so more specific phrases win.
_FORMAT_PATTERNS = [
    ("dry shampoo", re.compile(r"\bdry\s*shampoo\b", re.IGNORECASE)),
    ("shampoo", re.compile(r"\bshampoo(?:\s*bar)?\b", re.IGNORECASE)),
    ("conditioner", re.compile(r"\b(?:conditioner|co-?wash)\b", re.IGNORECASE)),
    ("leave-in", re.compile(r"\bleave[\s-]?in\b", re.IGNORECASE)),
    ("hair mask", re.compile(r"\b(?:hair\s*masks?|deep\s*conditioning\s*(?:mask|treatment))\b", re.IGNORECASE)),
    ("scalp treatment", re.compile(r"\bscalp\b[\w\s-]{0,20}?\b(?:treatment|tonic|serum|scrub|exfoliat\w+|care)\b", re.IGNORECASE)),
    ("treatment", re.compile(r"\b(?:treatments?|ampoules?)\b", re.IGNORECASE)),
    ("hair serum", re.compile(r"\b(?:hair\s*serums?|serums?)\b", re.IGNORECASE)),
    ("hair oil", re.compile(r"\b(?:hair\s*oils?|scalp\s*oil)\b", re.IGNORECASE)),
    ("hair tonic", re.compile(r"\btonics?\b", re.IGNORECASE)),
    ("hair mist", re.compile(r"\bmists?\b", re.IGNORECASE)),
    ("styling", re.compile(r"\b(?:styling\s*(?:cream|gel|wax|balm)|pomade|hair\s*wax|hair\s*gel)\b", re.IGNORECASE)),
    ("hair essence", re.compile(r"\bessences?\b", re.IGNORECASE)),
]

# Explicit "no sulfates" claims only -- never inferred from an ingredient list.
_SULFATE_FREE_RE = re.compile(
    r"\b(?:sulfate[\s-]?free|sulphate[\s-]?free|no\s+(?:added\s+)?sulfates?|"
    r"sls[\s-]?free|sls[\s/]?sles[\s-]?free)\b",
    re.IGNORECASE,
)

# Explicit "no silicones" claims only.
_SILICONE_FREE_RE = re.compile(
    r"\b(?:silicone[\s-]?free|no\s+(?:added\s+)?silicones?|dimethicone[\s-]?free)\b",
    re.IGNORECASE,
)

# Certification status values.
CERT_VERIFIED = "verified"
CERT_CLAIMED = "claimed"

# Certification kinds.
CERT_KIND_VEGAN = "vegan"
CERT_KIND_CRUELTY_FREE = "cruelty_free"

# Recognized certifying AUTHORITIES per kind. Matching one of these -- whether in
# an authored certifications entry or in product text -- is what makes a claim
# *verified*. PETA runs both a vegan and a cruelty-free ("Beauty Without
# Bunnies") program, so it counts for both kinds.
_AUTHORITY_RE: Dict[str, "re.Pattern[str]"] = {
    CERT_KIND_VEGAN: re.compile(
        r"\b(?:the\s+)?vegan\s+society\b|\bvegan\s+trademark\b|"
        r"\bcertified\s+vegan\b|\bvegan\s+action\b|\bvegan\.org\b|"
        r"\bv[\s-]?label\b|\beve\s+vegan\b|\bpeta\b",
        re.IGNORECASE,
    ),
    CERT_KIND_CRUELTY_FREE: re.compile(
        r"\bleaping\s+bunny\b|\bcruelty\s+free\s+international\b|\bccic\b|"
        r"\bchoose\s+cruelty[\s-]?free\b|\bpeta\b",
        re.IGNORECASE,
    ),
}

# Mere *presence* of the concept in text -- only ever yields "claimed".
_CONCEPT_RE: Dict[str, "re.Pattern[str]"] = {
    CERT_KIND_VEGAN: re.compile(r"\bvegan\b", re.IGNORECASE),
    CERT_KIND_CRUELTY_FREE: re.compile(
        r"\bcruelty[\s-]?free\b|\bnot\s+tested\s+on\s+animals\b", re.IGNORECASE
    ),
}


def extract_format(
    title: Optional[str] = None,
    product_type: Optional[str] = None,
    category_path: Optional[str] = None,
) -> Optional[str]:
    """Resolve a canonical haircare format from product text, or None."""
    blob = " ".join(str(v or "") for v in (title, product_type, category_path))
    for fmt, pattern in _FORMAT_PATTERNS:
        if pattern.search(blob):
            return fmt
    return None


def detect_sulfate_free(*texts: Any) -> bool:
    """True only when an explicit sulfate-free claim is present."""
    blob = " ".join(str(t or "") for t in texts)
    return bool(_SULFATE_FREE_RE.search(blob))


def detect_silicone_free(*texts: Any) -> bool:
    """True only when an explicit silicone-free claim is present."""
    blob = " ".join(str(t or "") for t in texts)
    return bool(_SILICONE_FREE_RE.search(blob))


def _cert_entries(certifications: Any) -> List[Dict[str, str]]:
    """Normalize an authored certifications value into a list of
    ``{"type": ..., "authority": ...}`` entries, tolerating either a list of
    dicts/strings or a flat ``{type: authority|bool}`` mapping."""
    entries: List[Dict[str, str]] = []
    if isinstance(certifications, dict):
        for key, value in certifications.items():
            kind = str(key or "").strip().lower().replace("-", "_").replace(" ", "_")
            authority = "" if isinstance(value, bool) else str(value or "").strip()
            entries.append({"type": kind, "authority": authority})
        return entries
    if isinstance(certifications, list):
        for item in certifications:
            if isinstance(item, dict):
                kind = str(item.get("type") or item.get("kind") or "").strip().lower()
                kind = kind.replace("-", "_").replace(" ", "_")
                authority = str(
                    item.get("authority") or item.get("certifier") or item.get("body") or ""
                ).strip()
                entries.append({"type": kind, "authority": authority})
            elif isinstance(item, str):
                # A bare string entry like "vegan" or "Leaping Bunny": treat the
                # whole token as the authority text and let kind-matching decide.
                entries.append({"type": "", "authority": item.strip()})
    return entries


def _authority_recognized(kind: str, authority: str) -> bool:
    pattern = _AUTHORITY_RE.get(kind)
    return bool(authority) and bool(pattern) and bool(pattern.search(authority))


def classify_cert(kind: str, certifications: Any, *texts: Any) -> Optional[str]:
    """Classify a cert kind as ``verified`` (recognized authority), ``claimed``
    (the concept is asserted but unbacked), or None (absent).

    Authored ``certifications`` win: an entry whose authority matches a
    recognized certifier is verified; an entry of the right kind without a
    recognized authority is claimed. Failing an authored entry, text is scanned
    -- a named authority verifies, a bare concept word only claims.
    """
    concept_re = _CONCEPT_RE.get(kind)
    claimed = False

    for entry in _cert_entries(certifications):
        entry_kind = entry.get("type") or ""
        authority = entry.get("authority") or ""
        # An entry matches this kind if it is explicitly tagged with it, or it is
        # an untyped/bare entry whose authority/text implies the kind.
        kind_match = entry_kind == kind or (
            entry_kind in ("", kind)
            and ((concept_re and concept_re.search(authority)) or _authority_recognized(kind, authority))
        )
        if not kind_match:
            continue
        if _authority_recognized(kind, authority):
            return CERT_VERIFIED
        claimed = True

    blob = " ".join(str(t or "") for t in texts)
    if blob.strip():
        if _authority_recognized(kind, blob):
            return CERT_VERIFIED
        if concept_re and concept_re.search(blob):
            claimed = True

    return CERT_CLAIMED if claimed else None


def classify_vegan(certifications: Any, *texts: Any) -> Optional[str]:
    """verified / claimed / None for the vegan claim."""
    return classify_cert(CERT_KIND_VEGAN, certifications, *texts)


def classify_cruelty_free(certifications: Any, *texts: Any) -> Optional[str]:
    """verified / claimed / None for the cruelty-free claim."""
    return classify_cert(CERT_KIND_CRUELTY_FREE, certifications, *texts)
