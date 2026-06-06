"""Shared merchant-fit ranking for SKU buyer-path lanes.

The raw opportunity score remains the diagnostic signal. This module adds the
merchant-facing ordering signal: among evidenced third-party-controlled lanes,
which lane is the most credible direct-site conversion play for the merchant.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


THIRD_PARTY_OWNERSHIP = {
    "competitor-owned",
    "forum-owned",
    "marketplace-owned",
    "publisher-owned",
    "retailer-owned",
}
THIRD_PARTY_ROUTES = {"brand", "forum", "marketplace", "publisher", "retailer"}

_WORD_RE = re.compile(r"[a-z0-9]+")
_PHRASE_CLEAN_RE = re.compile(r"[^a-z0-9]+")
_CONVERSION_PHRASES: Tuple[Tuple[str, float], ...] = (
    ("vitamin c", 0.34),
    ("collagen jelly", 0.2),
    ("healthy skin", 0.18),
    ("anti age", 0.18),
    ("anti aging", 0.18),
    ("korean", 0.16),
    ("k beauty", 0.16),
    ("kbeauty", 0.16),
    ("before bed", 0.14),
    ("halal", 0.14),
    ("buy online", 0.1),
    ("for sale", 0.1),
    ("subscription", 0.08),
    ("replenishment", 0.08),
    ("supplement", 0.07),
    ("stick", 0.07),
    ("jelly", 0.07),
    ("collagen", 0.06),
    ("best", 0.04),
    ("buy", 0.04),
    ("online", 0.04),
)
_LIFESTYLE_DRIFT_PHRASES = (
    "healthy snacks",
    "healthy snack",
    "snacks",
    "snack",
)
_LOW_UTILITY_PHRASES = (
    "popular",
    "trending",
    "what is",
    "top collagen",
)
_BRANDED_INTENT_WORDS = {"where", "shop", "buy", "sale", "online"}
_HIGH_SIGNAL_SOURCES = {
    "attribute_graph:ingredient",
    "attribute_graph:use_case",
    "attribute_graph:proof",
    "attribute_graph:geography",
    "attribute_graph:certification",
    "attribute_graph:certification_constraint",
    "attribute_graph:format",
    "attribute_graph:category",
}
_PRODUCT_TEXT_KEYS = (
    "title",
    "raw_title",
    "product_type",
    "category",
    "subcategory",
    "description",
    "body",
    "tags",
    "tag",
)


def build_lane_product_evidence(
    *,
    product: Optional[Mapping[str, Any]] = None,
    sku_ctx: Optional[Mapping[str, Any]] = None,
    attribute_graph: Optional[Mapping[str, Any]] = None,
    identity: Optional[Mapping[str, Any]] = None,
    sku_title: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect phrases that are evidenced by product data or the attribute graph."""
    phrase_sources: Dict[str, List[str]] = {}
    explicit_text_phrases: List[str] = []

    def add(value: Any, source: str, *, explicit: bool = False) -> None:
        for phrase in _phrases_from_any(value):
            if len(phrase) < 3:
                continue
            bucket = phrase_sources.setdefault(phrase, [])
            if source not in bucket:
                bucket.append(source)
            if explicit and phrase not in explicit_text_phrases:
                explicit_text_phrases.append(phrase)

    product_map = _as_mapping(product)
    sku_map = _as_mapping(sku_ctx)
    identity_map = _as_mapping(identity)

    for key in _PRODUCT_TEXT_KEYS:
        add(product_map.get(key), f"product:{key}", explicit=True)
        add(sku_map.get(key), f"sku_ctx:{key}", explicit=True)

    sku_product = sku_map.get("sku")
    if isinstance(sku_product, Mapping):
        for key in _PRODUCT_TEXT_KEYS:
            add(sku_product.get(key), f"sku_ctx.sku:{key}", explicit=True)

    add(sku_title, "sku_title", explicit=True)
    add(identity_map.get("name"), "identity:name", explicit=True)
    anchors = identity_map.get("anchors")
    if isinstance(anchors, Mapping):
        for key in ("category", "product_type", "brand"):
            add(anchors.get(key), f"identity.anchors:{key}", explicit=True)

    graph = _as_mapping(attribute_graph)
    classes = graph.get("classes")
    if isinstance(classes, Mapping):
        for class_name, values in classes.items():
            add(values, f"attribute_graph:{class_name}")

    return {
        "phrases": sorted(phrase_sources),
        "phrase_sources": phrase_sources,
        "explicit_text_phrases": explicit_text_phrases,
    }


def is_third_party_controlled_lane(row: Mapping[str, Any]) -> bool:
    ownership = _state(row.get("ownership_state"))
    route = _state(row.get("source_route"))
    if ownership == "merchant-owned":
        return False
    return ownership in THIRD_PARTY_OWNERSHIP or route in THIRD_PARTY_ROUTES


def has_lane_demand(row: Mapping[str, Any]) -> bool:
    return _float(row.get("opportunity_score")) > 0 or _float(row.get("demand_signal")) > 0


def enrich_lane_priority(
    row: Mapping[str, Any],
    *,
    product_evidence: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a row copy with deterministic merchant-fit priority metadata."""
    out = dict(row)
    meta = lane_priority(row, product_evidence=product_evidence)
    out.update(meta)
    return out


def prioritize_lanes(
    rows: Iterable[Mapping[str, Any]],
    *,
    product_evidence: Optional[Mapping[str, Any]] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    enriched = [
        enrich_lane_priority(row, product_evidence=product_evidence)
        for row in rows
        if isinstance(row, Mapping)
    ]
    enriched.sort(key=lane_priority_sort_key)
    return enriched[:limit] if limit is not None else enriched


def lane_priority_sort_key(row: Mapping[str, Any]) -> Tuple[float, float, float, float, str]:
    """Sort key for already-enriched rows; highest priority sorts first."""
    return (
        -_float(row.get("lane_priority_score")),
        -_float(row.get("merchant_fit_score")),
        -_float(row.get("conversion_fit_score")),
        -_float(row.get("opportunity_score")),
        str(row.get("query") or "").strip().lower(),
    )


def lane_priority(
    row: Mapping[str, Any],
    *,
    product_evidence: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    query = _clean(row.get("query"))
    evidence = _as_mapping(product_evidence)
    basis = _clean_list(row.get("attribute_basis")) or _clean_list(row.get("why_fit"))
    phrase_sources = _phrase_sources(evidence)
    evidence_phrases = set(phrase_sources)

    reasons: List[str] = []
    basis_hits: List[str] = []
    evidence_hits: List[str] = []
    for phrase in basis:
        if _phrase_in_query(phrase, query):
            basis_hits.append(phrase)
    for phrase in evidence_phrases:
        if _phrase_in_query(phrase, query):
            evidence_hits.append(phrase)

    high_signal_hits = [
        phrase for phrase in evidence_hits
        if any(source in _HIGH_SIGNAL_SOURCES for source in phrase_sources.get(phrase, []))
    ]
    if high_signal_hits:
        reasons.extend(f"evidenced:{phrase}" for phrase in high_signal_hits[:4])
    if basis_hits:
        reasons.extend(f"basis:{phrase}" for phrase in basis_hits[:4])

    query_tokens = _tokens(query)
    title_fit = _title_or_identity_fit(query, evidence)
    if title_fit:
        reasons.append("title_or_identity_match")

    merchant_fit = 0.0
    merchant_fit += min(0.55, len(high_signal_hits) * 0.16)
    merchant_fit += min(0.28, len(basis_hits) * 0.09)
    merchant_fit += min(0.2, len(evidence_hits) * 0.04)
    if title_fit:
        merchant_fit += 0.12
    if "collagen" in query_tokens and ("jelly" in query_tokens or "stick" in query_tokens):
        merchant_fit += 0.1
        reasons.append("sku_format_combo")
    merchant_fit = min(1.0, merchant_fit)

    conversion_fit = 0.0
    conversion_reasons: List[str] = []
    for phrase, weight in _CONVERSION_PHRASES:
        if _phrase_in_query(phrase, query):
            conversion_fit += weight
            conversion_reasons.append(phrase)
    conversion_fit = min(1.0, conversion_fit)
    reasons.extend(f"conversion:{phrase}" for phrase in conversion_reasons[:4])

    penalties: List[str] = []
    penalty = 0.0
    lifestyle_drift = [
        phrase for phrase in _LIFESTYLE_DRIFT_PHRASES
        if _phrase_in_query(phrase, query)
    ]
    if lifestyle_drift:
        if _lifestyle_explicitly_supported(lifestyle_drift, evidence):
            conversion_fit = min(1.0, conversion_fit + 0.18)
            reasons.append(f"explicit_lifestyle_positioning:{lifestyle_drift[0]}")
        else:
            penalty += 0.42
            penalties.append(f"lifestyle_drift:{lifestyle_drift[0]}")
    for phrase in _LOW_UTILITY_PHRASES:
        if _phrase_in_query(phrase, query):
            penalty += 0.08
            penalties.append(f"low_utility:{phrase}")
            break
    if title_fit and not (_BRANDED_INTENT_WORDS & query_tokens) and _float(row.get("opportunity_score")) <= 0:
        penalty += 0.1
        penalties.append("low_opportunity_branded_lane")

    opportunity_norm = min(1.0, _float(row.get("opportunity_score")) / 20.0)
    demand_norm = min(1.0, _float(row.get("demand_signal")))
    priority = (
        merchant_fit * 0.45
        + conversion_fit * 0.39
        + opportunity_norm * 0.10
        + demand_norm * 0.06
        - penalty
    )
    priority = max(0.0, min(1.0, priority))

    return {
        "lane_priority_score": round(priority, 4),
        "merchant_fit_score": round(merchant_fit, 4),
        "conversion_fit_score": round(conversion_fit, 4),
        "merchant_fit_reasons": _unique(reasons)[:8],
        "fit_penalties": _unique(penalties),
        "selection_reason": _selection_reason(
            query=query,
            reasons=reasons,
            penalties=penalties,
        ),
    }


def _selection_reason(*, query: str, reasons: List[str], penalties: List[str]) -> str:
    clean_reasons = _unique(reasons)
    clean_penalties = _unique(penalties)
    if clean_reasons and clean_penalties:
        return (
            f"{query or 'This lane'} has stronger merchant-fit evidence, with "
            f"{', '.join(clean_penalties)} applied."
        )
    if clean_reasons:
        return f"{query or 'This lane'} has stronger merchant-fit evidence."
    if clean_penalties:
        return f"{query or 'This lane'} is ranked lower because {', '.join(clean_penalties)}."
    return f"{query or 'This lane'} is ranked by evidenced demand and opportunity."


def _lifestyle_explicitly_supported(phrases: List[str], evidence: Mapping[str, Any]) -> bool:
    explicit = _clean_list(evidence.get("explicit_text_phrases"))
    if not explicit:
        return False
    explicit_text = " ".join(explicit)
    return any(_phrase_in_query(phrase, explicit_text) for phrase in phrases)


def _title_or_identity_fit(query: str, evidence: Mapping[str, Any]) -> bool:
    sources = _phrase_sources(evidence)
    title_phrases = [
        phrase for phrase, phrase_sources in sources.items()
        if any(
            source.startswith(("product:title", "product:raw_title", "sku_title", "identity:name"))
            for source in phrase_sources
        )
    ]
    return any(_phrase_in_query(phrase, query) for phrase in title_phrases if len(phrase) >= 5)


def _phrase_sources(evidence: Mapping[str, Any]) -> Dict[str, List[str]]:
    raw = evidence.get("phrase_sources")
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, List[str]] = {}
    for phrase, sources in raw.items():
        clean_phrase = _clean(phrase)
        if not clean_phrase:
            continue
        if isinstance(sources, (list, tuple, set)):
            out[clean_phrase] = [
                str(source or "").strip().lower()
                for source in sources
                if str(source or "").strip()
            ]
        else:
            source = str(sources or "").strip().lower()
            out[clean_phrase] = [source] if source else []
    return out


def _phrases_from_any(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        phrases: List[str] = []
        for item in value.values():
            phrases.extend(_phrases_from_any(item))
        return phrases
    if isinstance(value, (list, tuple, set)):
        phrases = []
        for item in value:
            phrases.extend(_phrases_from_any(item))
        return phrases
    text = _clean(value)
    if not text:
        return []
    phrases = [text]
    words = _token_list(text)
    if len(words) >= 2 and len(text) <= 80:
        for size in (2, 3):
            for idx in range(0, max(0, len(words) - size + 1)):
                phrases.append(" ".join(words[idx:idx + size]))
    return _unique(phrases)


def _clean_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Mapping):
        values = list(value.values())
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]
    return _unique(_clean(item) for item in values if _clean(item))


def _clean(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("&", " and ")
    text = _PHRASE_CLEAN_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _state(value: Any) -> str:
    return str(value or "").strip().lower()


def _tokens(value: Any) -> set[str]:
    return set(_WORD_RE.findall(str(value or "").lower()))


def _token_list(value: Any) -> List[str]:
    return _WORD_RE.findall(str(value or "").lower())


def _phrase_in_query(phrase: str, query: str) -> bool:
    clean_phrase = _clean(phrase)
    clean_query = _clean(query)
    if not clean_phrase or not clean_query:
        return False
    return f" {clean_phrase} " in f" {clean_query} "


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out
