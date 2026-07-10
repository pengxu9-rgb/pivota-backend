"""Shared merchant-fit ranking for SKU buyer-path lanes.

The raw opportunity score remains the diagnostic signal. This module adds the
merchant-facing ordering signal: among evidenced third-party-controlled lanes,
which lane is the most credible direct-site conversion play for the merchant.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from services.buyer_path_stable_controllers import stable_buyer_path_controller_hosts
from services.promo_terms import is_promo_term


THIRD_PARTY_OWNERSHIP = {
    "competitor-owned",
    "forum-owned",
    "marketplace-owned",
    "publisher-owned",
    "retailer-owned",
}
THIRD_PARTY_ROUTES = {"brand", "forum", "marketplace", "publisher", "retailer"}
SIDEWAYS_QUERY_CLASSES = {"attribute", "objection", "sidewalk"}
SIDEWAYS_AXES = {"attribute", "objection", "sidewalk"}
HEAD_QUERY_CLASSES = {"category", "head"}
HEAD_AXES = {"category", "head"}
# LLM value-prop discovery prompts (extract_winnable_prompts + scenario
# elicitation). They carry the coarse axis="category" like the head terms, but
# are SPECIFIC by construction — anchored to the product's own differentiators.
# They classify as sideways lanes (the winnable pool), never head pressure;
# keying on axis alone put "bone conduction headphones for swimming without
# phone" into do_not_chase_yet as a "broad category prompt".
LLM_PROMPT_SOURCES = {"llm_winnable", "llm_scenario"}


def _row_prompt_source(row: Mapping[str, Any]) -> str:
    return str(row.get("prompt_source") or "").strip().lower()

# Synthetic probe-query placeholder: when the real query generator falls short of
# the requested prompt count it pads with `"<title> shopper question <n>"`
# (agent_center_bd_report_service `_build_per_sku_base_query_specs`). Those are
# scaffolding, not real shopper questions — they must never reach merchant-facing
# copy (an action headline like "Own the answer to '… shopper question 15'") nor
# count as a real open lane.
_SYNTHETIC_QUERY_RE = re.compile(r"shopper question\s+\d+\s*$", re.IGNORECASE)


def is_synthetic_probe_query(query: Any) -> bool:
    """True for an auto-generated placeholder probe query (not a real shopper
    question). Used to keep scaffolding out of lanes + merchant-facing copy."""
    return bool(_SYNTHETIC_QUERY_RE.search(str(query or "").strip()))


_WORD_RE = re.compile(r"[a-z0-9]+")
_PHRASE_CLEAN_RE = re.compile(r"[^a-z0-9]+")
# Conversion-intent vocabulary for lane ranking. Was beauty-only (built for the
# BB Lab pilot), which starved every electronics lane of conversion_fit — an
# audio row could only score via "best"/"buy"/"online" (~0.04). Phrases are
# vertical-DISTINCTIVE (a beauty phrase never appears in an audio query and
# vice versa), so a flat superset is safe: only the phrases present in the
# query fire. Weights mirror the beauty tiers.
_CONVERSION_PHRASES: Tuple[Tuple[str, float], ...] = (
    # beauty / supplement (original set)
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
    ("supplement", 0.07),
    ("stick", 0.07),
    ("jelly", 0.07),
    ("collagen", 0.06),
    # electronics / audio (Mojawa pilot: spec-anchored buying intent)
    ("bone conduction", 0.2),
    ("noise cancelling", 0.18),
    ("noise cancellation", 0.18),
    ("waterproof", 0.16),
    ("battery life", 0.16),
    ("open ear", 0.14),
    ("wireless", 0.1),
    ("bluetooth", 0.1),
    ("for swimming", 0.14),
    ("for running", 0.12),
    ("for gym", 0.12),
    # vertical-neutral commerce intent
    ("buy online", 0.1),
    ("for sale", 0.1),
    ("subscription", 0.08),
    ("replenishment", 0.08),
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
_BROAD_HEAD_PHRASES = (
    "best ",
    "top ",
    "popular ",
    "what is ",
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
            # A promo/marketing/operational phrase ("skincare discount", "free
            # shipping", "exclude_rebuy") is never a product attribute; letting
            # it into the evidence phrases lets it become a lane and then the
            # merchant's headline recommendation (the DAMDAM "skincare discount"
            # first-move). Gate it here too, mirroring _clean_prompt_term.
            if is_promo_term(phrase.lower()):
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


def build_sideways_wedge(
    rows: Iterable[Mapping[str, Any]],
    *,
    product_evidence: Optional[Mapping[str, Any]] = None,
    limit: int = 4,
) -> Dict[str, Any]:
    """Summarize the first merchant-fit lane to win before broad prompt fights.

    This is intentionally evidence-only: it ranks measured, third-party-controlled
    rows and never invents a sideways prompt that was not in the prompt matrix.
    """
    candidates = [
        row for row in rows
        if isinstance(row, Mapping)
        and str(row.get("query") or "").strip()
        and is_third_party_controlled_lane(row)
        and has_lane_demand(row)
    ]
    if not candidates:
        return {
            "head_prompt_pressure": [],
            "sideways_wedge_lanes": [],
            "recommended_beachhead_lane": None,
            "why_this_lane_not_the_head_prompt": None,
            "canonical_page_play": None,
            "do_not_chase_yet": [],
        }

    prioritized = prioritize_lanes(
        candidates,
        product_evidence=product_evidence,
    )
    head_pressure = [
        _sideways_wedge_lane_chip(row, lane_type="head_prompt")
        for row in prioritized
        if _is_head_prompt_pressure(row)
    ][:max(0, limit)]
    sideways = [
        _sideways_wedge_lane_chip(row, lane_type="sideways_wedge")
        for row in prioritized
        if _is_sideways_wedge_lane(row)
    ][:max(0, limit)]
    beachhead = sideways[0] if sideways else None

    do_not: List[Dict[str, Any]] = []
    if beachhead:
        beachhead_query = _clean(beachhead.get("query"))
        for item in head_pressure:
            if _clean(item.get("query")) != beachhead_query:
                deferred = dict(item)
                deferred["reason"] = (
                    "Broad category prompts are usually a higher-cost first fight "
                    "when third-party controllers already shape the route."
                )
                do_not.append(deferred)
        for row in prioritized:
            if not _is_sideways_wedge_lane(row):
                continue
            if _clean(row.get("query")) == beachhead_query:
                continue
            penalties = _raw_str_list(row.get("fit_penalties"))
            if any(penalty.startswith("lifestyle_drift:") for penalty in penalties):
                deferred = _sideways_wedge_lane_chip(row, lane_type="defer")
                deferred["reason"] = (
                    "Ranked lower until product evidence explicitly supports this "
                    "lifestyle positioning."
                )
                do_not.append(deferred)
    why = _sideways_wedge_why(beachhead=beachhead, do_not=do_not)
    return {
        "head_prompt_pressure": head_pressure,
        "sideways_wedge_lanes": sideways,
        "recommended_beachhead_lane": beachhead,
        "why_this_lane_not_the_head_prompt": why,
        "canonical_page_play": _sideways_wedge_canonical_play(beachhead),
        "do_not_chase_yet": do_not[:max(0, limit)],
    }


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


def _is_sideways_wedge_lane(row: Mapping[str, Any]) -> bool:
    if _row_prompt_source(row) in LLM_PROMPT_SOURCES:
        return True
    query_class = _state(row.get("query_class"))
    axis = _state(row.get("axis"))
    return query_class in SIDEWAYS_QUERY_CLASSES or axis in SIDEWAYS_AXES


def _is_head_prompt_pressure(row: Mapping[str, Any]) -> bool:
    # A specific LLM discovery prompt is never head pressure, even when its
    # phrasing starts with "best ..." — specificity comes from the generator
    # contract (anchored to the product's own differentiators), not the prefix.
    if _row_prompt_source(row) in LLM_PROMPT_SOURCES:
        return False
    query_class = _state(row.get("query_class"))
    axis = _state(row.get("axis"))
    if query_class in HEAD_QUERY_CLASSES or axis in HEAD_AXES:
        return True
    query = _clean(row.get("query"))
    return any(query.startswith(phrase.strip()) or phrase in query for phrase in _BROAD_HEAD_PHRASES)


def _sideways_wedge_lane_chip(row: Mapping[str, Any], *, lane_type: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "query": row.get("query"),
        "lane_type": lane_type,
        "axis": row.get("axis"),
        "query_class": row.get("query_class"),
        "prompt_source": row.get("prompt_source"),
        "ownership_state": row.get("ownership_state"),
        "source_route": row.get("source_route"),
        "controllers": _lane_controllers(row),
        "opportunity_score": row.get("opportunity_score"),
        "demand_signal": row.get("demand_signal"),
    }
    for key in (
        "lane_priority_score",
        "merchant_fit_score",
        "conversion_fit_score",
        "merchant_fit_reasons",
        "fit_penalties",
        "selection_reason",
    ):
        if key in row:
            out[key] = row.get(key)
    return out


def _sideways_wedge_why(
    *,
    beachhead: Optional[Mapping[str, Any]],
    do_not: List[Mapping[str, Any]],
) -> Optional[str]:
    if not beachhead:
        return None
    query = str(beachhead.get("query") or "").strip()
    if not query:
        return None
    deferred_query = next(
        (
            str(item.get("query") or "").strip()
            for item in do_not
            if str(item.get("query") or "").strip()
        ),
        "",
    )
    if deferred_query:
        return (
            f"Start with \"{query}\" before \"{deferred_query}\" because it is "
            "product-specific, commercially useful, and easier to make the official "
            "page the best cited + buyable route."
        )
    return (
        f"Start with \"{query}\" as the beachhead because it is product-specific, "
        "commercially useful, and already shows third-party-controlled demand."
    )


def _sideways_wedge_canonical_play(
    beachhead: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not beachhead:
        return None
    query = str(beachhead.get("query") or "").strip()
    if not query:
        return None
    return {
        "lane": query,
        "play": (
            f"Make the official product page the cited + buyable canonical page for {query}."
        ),
        "operator_moves": [
            "first-order offer",
            "starter + replenishment bundle",
            "subscription incentive",
            "why-buy-direct proof",
        ],
        "pivota_path": (
            "Serve and monitor the cited + buyable canonical page with agent-checkout readiness."
        ),
        "economics_policy": (
            "Mechanics only: no exact discount depths, bundle prices, savings "
            "percentages, or margin claims without audited promo or margin evidence."
        ),
    }


def _lane_controllers(row: Mapping[str, Any]) -> List[str]:
    return stable_buyer_path_controller_hosts(row)[:3]


def _host_from_any(value: Any) -> str:
    if isinstance(value, Mapping):
        raw = value.get("host") or value.get("domain") or value.get("url") or value.get("uri")
    else:
        raw = value
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^https?://", "", text)
    text = text.split("/", 1)[0]
    return text if "." in text else ""


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


def _raw_str_list(value: Any) -> List[str]:
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
    return _unique(str(item or "").strip() for item in values if str(item or "").strip())


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


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


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
