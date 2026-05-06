"""Pure-function deterministic matchers for seed → PDP linkage.

These take dicts as input (no DB calls) so they can be unit-tested without
infrastructure. The runner module wires them to actual queries against
catalog_products.

Each matcher returns either:
  - {"product_key": "<pk>", "confidence": 0.0–1.0, "matcher": "<name>",
     "evidence": <str>} on a confident match
  - None when the seed cannot be confidently matched by that strategy

`matches_for_seed` is the priority-ordered driver: tries the matchers
left-to-right, returns the first non-None result, else None.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

# pg_trgm similarity threshold mirroring the GIN trigram index. 0.85 is
# strict enough that "MAC Ruby Woo Lipstick" and "MAC Ruby Woo Mini" do
# NOT collapse to the same PDP (similarity ≈ 0.78), but "MAC Ruby Woo
# Matte Lipstick" and "MAC Ruby Woo Lipstick" DO match (similarity ≈ 0.92).
TITLE_SIMILARITY_THRESHOLD = 0.85

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_token(value: Optional[str]) -> str:
    if value is None:
        return ""
    return _NON_ALNUM_RE.sub(" ", str(value).lower()).strip()


def _trigrams(text: str) -> set:
    """pg_trgm-style trigrams: lowercase, padded with two leading and one
    trailing space, sliced into 3-char windows. Mirrors PostgreSQL's
    `show_trgm()` for parity with the GIN index."""
    if not text:
        return set()
    padded = "  " + text.lower() + " "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def trigram_similarity(a: Optional[str], b: Optional[str]) -> float:
    """Jaccard similarity over trigrams. Mirrors pg_trgm.similarity()."""
    a_grams = _trigrams(a or "")
    b_grams = _trigrams(b or "")
    if not a_grams or not b_grams:
        return 0.0
    intersection = len(a_grams & b_grams)
    union = len(a_grams | b_grams)
    return intersection / union if union else 0.0


def normalize_canonical_url(url: Optional[str]) -> str:
    """Lowercase host, strip query string + fragment, trim trailing slash.
    Returns empty string for blank / unparseable input. Requires a netloc
    (host); a bare path-only string isn't accepted as a URL."""
    if not url:
        return ""
    raw = str(url).strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or ""
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    scheme = parsed.scheme.lower() if parsed.scheme else "https"
    return f"{scheme}://{host}{path}"


def source_product_id_match(
    *,
    seed: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Strategy 1 — match seed.external_product_id against
    catalog_products.source_product_id.

    Returns the unique candidate when exactly one matches; ambiguous
    multi-match returns None (the seed is left for the title-trigram
    matcher to disambiguate via brand).

    Confidence is high (0.95) for unique source_product_id matches —
    these are typically the same SKU registered both as an internal
    catalog row AND as an external referral seed.
    """
    target = _normalize_token(seed.get("external_product_id"))
    if not target:
        return None
    hits: List[Dict[str, Any]] = []
    for cand in candidates:
        cand_id = _normalize_token(cand.get("source_product_id"))
        if cand_id and cand_id == target:
            hits.append(cand)
    if len(hits) != 1:
        return None  # ambiguous (or none) — defer
    pdp = hits[0]
    return {
        "product_key": pdp.get("product_key"),
        "confidence": 0.95,
        "matcher": "source_product_id_match",
        "evidence": f"source_product_id={target}",
    }


def canonical_url_match(
    *,
    seed: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Strategy 2 — normalize and exact-match canonical_url.

    Returns the unique candidate when one matches exactly. Multi-match
    is ambiguous (catalog row could be duplicate; defer).

    Confidence 0.90 — same product page on the merchant's site is a
    strong signal but not as authoritative as source_product_id.
    """
    seed_url = normalize_canonical_url(seed.get("canonical_url"))
    if not seed_url:
        # Fall back to destination_url if canonical not set on the seed.
        seed_url = normalize_canonical_url(seed.get("destination_url"))
    if not seed_url:
        return None
    hits: List[Dict[str, Any]] = []
    for cand in candidates:
        cand_url = normalize_canonical_url(cand.get("canonical_url"))
        if cand_url and cand_url == seed_url:
            hits.append(cand)
    if len(hits) != 1:
        return None
    pdp = hits[0]
    return {
        "product_key": pdp.get("product_key"),
        "confidence": 0.90,
        "matcher": "canonical_url_match",
        "evidence": f"canonical_url={seed_url}",
    }


def title_brand_match(
    *,
    seed: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
    similarity_threshold: float = TITLE_SIMILARITY_THRESHOLD,
) -> Optional[Dict[str, Any]]:
    """Strategy 3 — trigram-similarity match on title within the same brand.

    Brand is derived from the seed's domain or title heuristically; if
    no candidate has a matching brand, the matcher gives up rather than
    cross-matching brands.

    Returns the highest-similarity candidate above the threshold. Ties
    or all-below-threshold returns None.

    Confidence is the trigram similarity score itself (0.85–1.0 range).
    """
    seed_title = _normalize_token(seed.get("title"))
    if not seed_title:
        return None
    seed_brand_hints = _seed_brand_hints(seed)
    best: Optional[Dict[str, Any]] = None
    best_sim = 0.0
    for cand in candidates:
        cand_title = _normalize_token(cand.get("title"))
        cand_brand = _normalize_token(cand.get("brand"))
        if not cand_title:
            continue
        # Brand match passes when any seed hint substring-matches the
        # candidate brand (or vice versa). "charlotte" hint matches
        # "charlotte tilbury" candidate brand.
        if seed_brand_hints and cand_brand:
            brand_ok = any(
                hint in cand_brand or cand_brand in hint
                for hint in seed_brand_hints
                if hint
            )
            if not brand_ok:
                continue
        sim = trigram_similarity(seed_title, cand_title)
        if sim > best_sim:
            best_sim = sim
            best = cand
    if best is None or best_sim < similarity_threshold:
        return None
    return {
        "product_key": best.get("product_key"),
        "confidence": round(float(best_sim), 4),
        "matcher": "title_brand_match",
        "evidence": f"title_sim={best_sim:.3f} brand_hint={','.join(sorted(seed_brand_hints)) or 'none'}",
    }


def _seed_brand_hints(seed: Dict[str, Any]) -> set:
    """Best-effort brand extraction from seed metadata. Returns the set of
    candidate brand tokens (lowercased, alphanumeric) the matcher will
    accept."""
    hints: set = set()
    # Explicit `brand` field in seed_data, if present.
    seed_data = seed.get("seed_data") or {}
    if isinstance(seed_data, dict):
        brand = seed_data.get("brand") or seed_data.get("brand_name")
        if brand:
            hints.add(_normalize_token(brand))
    # Domain-derived brand: e.g. mac.com → mac, sephora.com → sephora.
    # Multi-merchant retailers (sephora, ulta, amazon) shouldn't be used
    # as a brand hint — they sell many brands. Keep an explicit blocklist.
    _MULTI_MERCHANT_DOMAINS = {
        "sephora", "ulta", "amazon", "walmart", "target", "macys",
        "nordstrom", "hsn", "qvc", "ebay", "etsy",
    }
    domain = (seed.get("domain") or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if domain:
        head = domain.split(".")[0]
        if head and head not in _MULTI_MERCHANT_DOMAINS:
            hints.add(_normalize_token(head))
    # First word of title as brand hint — many product titles lead with brand.
    title = _normalize_token(seed.get("title"))
    if title:
        parts = title.split()
        if parts:
            hints.add(parts[0])
    hints.discard("")
    return hints


_MATCHERS: List[Any] = [
    source_product_id_match,
    canonical_url_match,
    title_brand_match,
]


def matches_for_seed(
    *,
    seed: Dict[str, Any],
    candidates: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Try matchers in priority order; return the first confident match,
    or None when the seed cannot be deterministically linked."""
    cand_list = list(candidates)
    for matcher in _MATCHERS:
        result = matcher(seed=seed, candidates=cand_list)
        if result is not None:
            return result
    return None
