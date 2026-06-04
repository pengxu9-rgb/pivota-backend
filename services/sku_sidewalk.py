"""SKU attribute graphing and sidewalk prompt generation.

The free SKU wedge has a tight prompt budget, so sidewalk prompts must come
from product-substantiated attributes instead of loose keyword expansion. This
module keeps the data shapes small and explicit so later scoring/rendering can
reuse the same attribute_basis and evidence without re-parsing product copy.
"""

from __future__ import annotations

from html import unescape
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


ATTRIBUTE_CLASSES: Tuple[str, ...] = (
    "category",
    "format",
    "ingredient",
    "certification_constraint",
    "audience",
    "use_case",
    "geography",
    "offer_variant",
    "proof",
    "exclusion",
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")

_CATEGORY_TERMS: Tuple[Tuple[str, str], ...] = (
    ("collagen", "collagen"),
    ("sunscreen", "sunscreen"),
    ("sun screen", "sunscreen"),
    ("deodorant", "deodorant"),
    ("serum", "serum"),
    ("hair vitamin", "hair vitamin"),
    ("balm", "balm"),
    ("supplement", "supplement"),
)
_FORMAT_TERMS: Tuple[Tuple[str, str], ...] = (
    ("refill pod", "refill pod"),
    ("refill pods", "refill pod"),
    ("stick", "stick"),
    ("sticks", "stick"),
    ("gummy", "gummy"),
    ("gummies", "gummy"),
    ("balm", "balm"),
    ("powder", "powder"),
    ("jelly", "jelly"),
    ("capsule", "capsule"),
    ("capsules", "capsule"),
    ("serum", "serum"),
)
_INGREDIENT_TERMS: Tuple[Tuple[str, str], ...] = (
    ("fish collagen", "fish collagen"),
    ("marine collagen", "marine collagen"),
    ("low molecular collagen", "low molecular collagen"),
    ("vitamin c", "vitamin c"),
    ("glycine", "glycine"),
    ("zinc oxide", "zinc oxide"),
    ("titanium dioxide", "titanium dioxide"),
    ("niacinamide", "niacinamide"),
    ("hyaluronic acid", "hyaluronic acid"),
)
_CERTIFICATION_TERMS: Tuple[Tuple[str, str], ...] = (
    ("halal certified", "halal"),
    ("halal-certified", "halal"),
    ("halal", "halal"),
    ("reef safe", "reef-safe"),
    ("reef-safe", "reef-safe"),
    ("vegan", "vegan"),
    ("fragrance free", "fragrance-free"),
    ("fragrance-free", "fragrance-free"),
    ("baking soda free", "baking-soda-free"),
    ("baking-soda-free", "baking-soda-free"),
    ("cruelty free", "cruelty-free"),
    ("cruelty-free", "cruelty-free"),
    ("mineral", "mineral"),
)
_AUDIENCE_TERMS: Tuple[Tuple[str, str], ...] = (
    ("kids", "kids"),
    ("children", "kids"),
    ("child", "kids"),
    ("baby", "kids"),
    ("postpartum", "postpartum"),
    ("men", "men"),
    ("sensitive skin", "sensitive skin"),
    ("eczema prone", "sensitive skin"),
    ("eczema-prone", "sensitive skin"),
    ("pregnancy", "pregnancy"),
    ("pregnant", "pregnancy"),
)
_USE_CASE_TERMS: Tuple[Tuple[str, str], ...] = (
    ("before bed", "before bed"),
    ("bedtime", "before bed"),
    ("bed time", "before bed"),
    ("good night", "before bed"),
    ("travel", "travel"),
    ("on the go", "travel"),
    ("on-the-go", "travel"),
    ("portable", "travel"),
    ("gym", "gym"),
    ("summer camp", "summer"),
    ("summer", "summer"),
    ("sensitive skin", "sensitive skin"),
)
_GEOGRAPHY_TERMS: Tuple[Tuple[str, str], ...] = (
    ("k beauty", "k-beauty"),
    ("k-beauty", "k-beauty"),
    ("korean", "korean"),
    ("singapore", "singapore"),
    ("uae", "uae"),
    ("united states", "us"),
    (" usa ", "us"),
)
_EXCLUSION_TERMS: Tuple[Tuple[str, str], ...] = (
    ("no water needed", "no water"),
    ("no water", "no water"),
    ("without water", "no water"),
    ("water free", "no water"),
    ("water-free", "no water"),
    ("no melatonin", "no melatonin"),
    ("melatonin free", "no melatonin"),
    ("melatonin-free", "no melatonin"),
    ("no baking soda", "no baking soda"),
    ("baking soda free", "no baking soda"),
    ("baking-soda-free", "no baking soda"),
    ("sugar free", "sugar-free"),
    ("sugar-free", "sugar-free"),
    ("no artificial dye", "no artificial dye"),
    ("no white cast", "no white cast"),
)
_PROOF_TERMS: Tuple[Tuple[str, str], ...] = (
    ("dermatologist tested", "dermatologist-tested"),
    ("dermatologist-tested", "dermatologist-tested"),
    ("clinically tested", "clinically tested"),
    ("award", "award"),
    ("reviews", "reviews"),
    ("low molecular", "low molecular"),
    ("1000 da", "1000 da"),
    ("1,000 da", "1000 da"),
)

_MEDICAL_BLOCKLIST: Tuple[str, ...] = (
    "sleep aid",
    "helps you sleep",
    "help you sleep",
    "sleep support",
    "cure",
    "treat",
    "heals",
    "healing",
    "medical",
    "eczema",
    "therapy",
    "doctor recommended",
    "pregnancy safe",
    "safe for kids",
)
_GENERIC_CATEGORIES = {"", "product", "products", "item", "items"}


def _clean_attr(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unescape(_HTML_TAG_RE.sub(" ", text))
    return _SPACE_RE.sub(" ", text).strip(" \t\r\n,.;:/")


def _search_text(value: Any) -> str:
    text = _clean_attr(value)
    text = text.replace("-", " ")
    text = re.sub(r"[^a-z0-9$]+", " ", text)
    return f" {_SPACE_RE.sub(' ', text).strip()} "


def _has_phrase(text: str, phrase: str) -> bool:
    needle = _search_text(phrase).strip()
    return bool(needle) and f" {needle} " in _search_text(text)


def _dedupe(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        cleaned = _clean_attr(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _tags_from_raw(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return _dedupe(raw.split(","))
    if isinstance(raw, list):
        return _dedupe(str(item) for item in raw)
    return []


def _string_values(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            out.extend(_string_values(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_string_values(item))
        return out
    return []


def _iter_product_sources(product: Mapping[str, Any]) -> Iterable[Tuple[str, str]]:
    attrs = product.get("attributes_raw")
    attrs = attrs if isinstance(attrs, dict) else {}

    for value in (
        product.get("product_type"),
        product.get("category"),
        attrs.get("product_type"),
        attrs.get("category"),
    ):
        if value:
            yield "product_type", str(value)

    for tag in _tags_from_raw(attrs.get("tags") or product.get("tags")):
        yield "tag", tag

    for value in (product.get("title"), product.get("raw_title")):
        if value:
            yield "title", str(value)

    for value in (
        attrs.get("description"),
        attrs.get("body_html"),
        product.get("description"),
    ):
        if value:
            yield "body", str(value)

    for collection_key in ("variants", "options"):
        raw_collection = attrs.get(collection_key)
        if isinstance(raw_collection, list):
            for item in raw_collection:
                joined = " ".join(_string_values(item))
                if joined.strip():
                    yield "variant", joined


def _add_attr(
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
    class_name: str,
    attr: str,
    source: str,
) -> None:
    cleaned = _clean_attr(attr)
    if not cleaned:
        return
    bucket = classes.setdefault(class_name, [])
    if cleaned not in bucket:
        bucket.append(cleaned)
    evidence.setdefault(cleaned, source)


def _add_lexicon_matches(
    *,
    text: str,
    source: str,
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
    class_name: str,
    lexicon: Iterable[Tuple[str, str]],
) -> None:
    for phrase, attr in lexicon:
        if _has_phrase(text, phrase):
            _add_attr(classes, evidence, class_name, attr, source)


def _add_offer_and_proof_attrs(
    product: Mapping[str, Any],
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
) -> None:
    attrs = product.get("attributes_raw")
    attrs = attrs if isinstance(attrs, dict) else {}

    rating = attrs.get("aggregateRating")
    if isinstance(rating, dict) and (rating.get("ratingValue") or rating.get("reviewCount")):
        _add_attr(classes, evidence, "proof", "reviews", "structured_data")

    offers = attrs.get("offers")
    offers_list = offers if isinstance(offers, list) else [offers]
    for offer in offers_list:
        if not isinstance(offer, dict):
            continue
        availability = _clean_attr(offer.get("availability"))
        if "in stock" in availability or "instock" in availability:
            _add_attr(classes, evidence, "offer_variant", "in stock", "structured_data")
        price = _clean_attr(offer.get("price"))
        if price:
            _add_attr(classes, evidence, "offer_variant", f"${price}", "structured_data")


def _add_quantity_attrs(
    sources: Iterable[Tuple[str, str]],
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
) -> None:
    quantity_re = re.compile(
        r"\b\d+(?:\.\d+)?\s*(?:sticks?|packs?|packages?|ct|count|ml|mg|g|oz)\b",
        re.IGNORECASE,
    )
    for source, text in sources:
        for match in quantity_re.findall(text or ""):
            _add_attr(classes, evidence, "offer_variant", match, source)


def build_sku_attribute_graph(product: dict) -> dict:
    """Normalize substantiated product facts into sidewalk-ready classes.

    Evidence is intentionally attached to the attribute, not the class, because
    prompt generation and later rendering both need to prove each lane came
    from product data rather than inferred category lore.
    """
    safe_product: Mapping[str, Any] = product if isinstance(product, dict) else {}
    classes: Dict[str, List[str]] = {name: [] for name in ATTRIBUTE_CLASSES}
    evidence: Dict[str, str] = {}
    sources = list(_iter_product_sources(safe_product))

    for source, text in sources:
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="category",
            lexicon=_CATEGORY_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="format",
            lexicon=_FORMAT_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="ingredient",
            lexicon=_INGREDIENT_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="certification_constraint",
            lexicon=_CERTIFICATION_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="audience",
            lexicon=_AUDIENCE_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="use_case",
            lexicon=_USE_CASE_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="geography",
            lexicon=_GEOGRAPHY_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="exclusion",
            lexicon=_EXCLUSION_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="proof",
            lexicon=_PROOF_TERMS,
        )

    _add_offer_and_proof_attrs(safe_product, classes, evidence)
    _add_quantity_attrs(sources, classes, evidence)

    if "stick" in classes["format"] and "no water" in classes["exclusion"]:
        _add_attr(
            classes,
            evidence,
            "use_case",
            "travel",
            evidence.get("no water") or evidence.get("stick") or "body",
        )

    for class_name in ATTRIBUTE_CLASSES:
        classes[class_name] = _dedupe(classes.get(class_name, []))

    return {"classes": classes, "evidence": evidence}


def _first_buyer_category(classes: Mapping[str, List[str]], product_type: str) -> Optional[str]:
    for category in classes.get("category") or []:
        cleaned = _clean_attr(category)
        if cleaned not in _GENERIC_CATEGORIES and cleaned != "supplement":
            return cleaned
    product_type_text = _clean_attr(product_type)
    for _phrase, category in _CATEGORY_TERMS:
        if _has_phrase(product_type_text, category) and category not in _GENERIC_CATEGORIES:
            return category
    return None


def _plural_format(value: str) -> str:
    value = _clean_attr(value)
    if value == "stick":
        return "sticks"
    if value == "gummy":
        return "gummies"
    if value == "capsule":
        return "capsules"
    if value == "refill pod":
        return "refill pods"
    return value


def _geo_for_query(value: str) -> str:
    cleaned = _clean_attr(value)
    if cleaned == "k-beauty":
        return "korean"
    return cleaned


def _clean_query(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" \t\r\n,.;:/")
    return value.lower()


def _query_has_category(query: str, category: str) -> bool:
    return bool(category) and _has_phrase(query, category)


def _violates_guardrail(query: str) -> bool:
    q = _clean_query(query)
    if "sleep" in q:
        return True
    for blocked in _MEDICAL_BLOCKLIST:
        if blocked in q:
            return True
    if "safe" in q and "reef-safe" not in q:
        return True
    return False


def _basis_evidence(graph: Mapping[str, Any], basis: Iterable[str]) -> List[str]:
    evidence = graph.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    out: List[str] = []
    seen = set()
    for attr in basis:
        source = str(evidence.get(attr) or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        out.append(source)
    return out


def _candidate(
    graph: Mapping[str, Any],
    query: str,
    basis: Iterable[str],
    weight: float,
) -> Optional[Dict[str, Any]]:
    cleaned_basis = _dedupe(basis)
    if not cleaned_basis:
        return None
    cleaned_query = _clean_query(query)
    if not cleaned_query or _violates_guardrail(cleaned_query):
        return None
    return {
        "query": cleaned_query,
        "axis": "sidewalk",
        "attribute_basis": cleaned_basis,
        "evidence": _basis_evidence(graph, cleaned_basis),
        "intent_weight": float(weight),
    }


def _benefit_for_category(category: str) -> Optional[str]:
    if category == "collagen":
        return "skin"
    if category == "sunscreen":
        return "summer"
    if category == "deodorant":
        return "gym"
    return None


def generate_sidewalk_query_specs(
    graph: dict,
    *,
    title: str,
    product_type: str,
    n: int,
    sku_ctx: dict | None = None,
) -> list[dict]:
    """Create deterministic sidewalk prompts from evidenced attributes only."""
    del title, sku_ctx  # Reserved for later rendering/scoring without API churn.
    target = max(0, int(n or 0))
    if target <= 0:
        return []

    safe_graph: Mapping[str, Any] = graph if isinstance(graph, dict) else {}
    raw_classes = safe_graph.get("classes")
    classes: Mapping[str, List[str]] = raw_classes if isinstance(raw_classes, dict) else {}
    category = _first_buyer_category(classes, product_type or "")
    if not category:
        return []

    formats = list(classes.get("format") or [])
    constraints = list(classes.get("certification_constraint") or [])
    audiences = list(classes.get("audience") or [])
    use_cases = [item for item in (classes.get("use_case") or []) if item != "sleep"]
    geographies = list(classes.get("geography") or [])
    ingredients = list(classes.get("ingredient") or [])
    exclusions = list(classes.get("exclusion") or [])

    candidates: List[Dict[str, Any]] = []

    def add(query: str, basis: Iterable[str], weight: float) -> None:
        if not _query_has_category(query, category):
            return
        item = _candidate(safe_graph, query, basis, weight)
        if item:
            candidates.append(item)

    primary_format = formats[0] if formats else ""
    plural_format = _plural_format(primary_format) if primary_format else ""
    primary_constraint = constraints[0] if constraints else ""

    for constraint in constraints[:3]:
        if plural_format:
            add(
                f"{constraint} {category} {plural_format}",
                [constraint, category, primary_format],
                0.95,
            )
            for use_case in use_cases[:2]:
                if use_case == "travel":
                    continue
                add(
                    f"{constraint} {category} {plural_format} {use_case}",
                    [constraint, category, primary_format, use_case],
                    0.98,
                )
        else:
            add(f"{constraint} {category}", [constraint, category], 0.86)

    for geography in geographies[:2]:
        geo = _geo_for_query(geography)
        if primary_constraint and plural_format:
            add(
                f"{primary_constraint} {geo} {category} {plural_format}",
                [primary_constraint, geography, category, primary_format],
                0.94,
            )
        elif plural_format:
            add(
                f"{geo} {category} {plural_format}",
                [geography, category, primary_format],
                0.87,
            )

    for exclusion in exclusions[:3]:
        if primary_format:
            lane_use_cases = use_cases[:2]
            if exclusion == "no water" and "travel" in use_cases:
                lane_use_cases = ["travel"]
            for use_case in lane_use_cases:
                add(
                    f"{category} {primary_format} {exclusion} {use_case}",
                    [category, primary_format, exclusion, use_case],
                    0.97,
                )
        for audience in audiences[:2]:
            add(
                f"{exclusion} {category} for {audience}",
                [exclusion, category, audience],
                0.89,
            )
        if plural_format:
            add(
                f"{exclusion} {category} {plural_format}",
                [exclusion, category, primary_format],
                0.84,
            )

    for audience in audiences[:3]:
        for use_case in use_cases[:3]:
            if audience == use_case:
                # "sensitive skin" is both an audience and a use_case; pairing it
                # with itself stutters ("...for sensitive skin sensitive skin").
                continue
            add(
                f"{category} for {audience} {use_case}",
                [category, audience, use_case],
                0.9,
            )

    for use_case in use_cases[:3]:
        if plural_format:
            add(
                f"{use_case} {category} {plural_format}",
                [use_case, category, primary_format],
                0.85,
            )

    benefit = _benefit_for_category(category)
    for ingredient in ingredients[:4]:
        if plural_format:
            category_text = "" if category in ingredient else f" {category}"
            add(
                f"{ingredient}{category_text} {plural_format}",
                [ingredient, category, primary_format],
                0.91,
            )
        if benefit:
            query = (
                f"{ingredient} {benefit}"
                if category in ingredient
                else f"{ingredient} {category} {benefit}"
            )
            add(query, [ingredient, category], 0.83)

    deduped: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        query = item["query"]
        existing = deduped.get(query)
        if not existing or item["intent_weight"] > existing["intent_weight"]:
            deduped[query] = item

    ordered = sorted(
        deduped.values(),
        key=lambda item: (
            -float(item.get("intent_weight") or 0),
            -len(item.get("attribute_basis") or []),
            item.get("query") or "",
        ),
    )
    return ordered[:target]
