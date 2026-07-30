from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from models.standard_product import (
    ProductStatus,
    StandardProduct,
    StandardProductVariant,
    _INGREDIENT_CANONICAL_ALIASES,
    _normalize_ingredient_ids,
    _normalize_visible_attribute_labels,
    _normalize_visible_attribute_text,
    _normalized_visible_term_matches,
)
from services.external_seed_search import ensure_json_obj, stable_external_product_id


BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION = "beauty_external_ranking_v1"
EXTERNAL_SEED_MERCHANT_ID = "external_seed"

_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "from",
    "in",
    "my",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}

_CATEGORY_ANCHORS: Dict[str, List[str]] = {
    "serum": ["serum", "serums"],
    "moisturizer": ["moisturizer", "moisturizers", "moisturiser", "moisturisers"],
    "cleanser": ["cleanser", "cleansers"],
    "toner": ["toner", "toners"],
    "sunscreen": ["sunscreen", "sun screen", "spf"],
    "foundation": ["foundation", "foundations"],
    "blush": ["blush", "blushes"],
    "lipstick": ["lipstick", "lipsticks"],
    "gloss": ["gloss", "glosses", "lip gloss", "lip glosses"],
}

_STRICT_CATEGORY_INTENT_LABELS = {
    "cleanser",
    "moisturizer",
    "sunscreen",
    "foundation",
    "blush",
    "lipstick",
    "gloss",
    "toner",
}

_CONCERN_TERMS: Dict[str, List[str]] = {
    "acne": ["acne", "blemish", "breakout", "breakouts"],
    "pores": ["pore", "pores"],
    "hydrating": ["hydrating", "hydrate", "hydration"],
    "sensitive_skin": ["sensitive skin", "sensitive-skin"],
    "brightening": ["brightening", "brighten"],
}

_FORMULA_TERMS: Dict[str, List[str]] = {
    "fragrance_free": [
        "fragrance free",
        "fragrance-free",
        "free fragrance",
        "without fragrance",
        "no fragrance",
        "sin fragancia",
    ],
}

_FORM_FACTOR_TERMS = ("gel", "mist", "pads", "cream", "balm", "eye", "travel", "jumbo")
_EXCLUSION_BUNDLE_TERMS = ("routine", "duo", "set", "kit", "bundle")
_PACKAGING_VARIANT_TERMS: Dict[str, List[str]] = {
    "travel_size": [
        "travel size",
        "travel-size",
        "travel",
        "mini",
        "mini size",
        "trial size",
        "sample size",
        "deluxe sample",
    ],
    "jumbo": [
        "jumbo",
        "value size",
        "full size",
        "full-size",
        "larger size",
        "large size",
    ],
}


@dataclass
class RankedExternalBeautyCandidate:
    row: Dict[str, Any]
    seed_data: Dict[str, Any]
    filter_product: StandardProduct
    external_product_id: str
    title: str
    description: str
    brand: str
    domain: str
    canonical_url: str
    destination_url: str
    product_type: str
    category: Optional[str]
    availability: Optional[str]
    price_amount: Optional[float]
    price_currency: Optional[str]
    source_order: int
    brand_term_hit: int
    updated_at: Any
    candidate_score: float = 0.0
    source_boost: float = 0.0
    quality_penalties_total: float = 0.0
    ranking_features: Dict[str, Any] = field(default_factory=dict)
    ranking_score_breakdown: Dict[str, Any] = field(default_factory=dict)
    ranking_drop_reason: List[str] = field(default_factory=list)

    def as_feature_dump(self) -> Dict[str, Any]:
        visible_option_labels: List[str] = []
        for variant in self.filter_product.variants or []:
            for label in variant.visible_option_labels or []:
                normalized = str(label or "").strip()
                if normalized and normalized not in visible_option_labels:
                    visible_option_labels.append(normalized)
        return {
            "candidate_source": "external_seed",
            "external_product_id": self.external_product_id,
            "title": self.title,
            "domain": self.domain,
            "canonical_url": self.canonical_url,
            "destination_url": self.destination_url,
            "source_order": self.source_order,
            "updated_at": str(self.updated_at) if self.updated_at is not None else None,
            "brand_term_hit": self.brand_term_hit,
            "availability": self.availability,
            "price_amount": self.price_amount,
            "price_currency": self.price_currency,
            "normalized_product_type": self.product_type,
            "normalized_visible_attributes": self.filter_product.visible_attributes,
            "normalized_visible_option_labels": visible_option_labels,
            "normalized_ingredient_ids": list(self.filter_product.ingredient_ids or []),
            "ranking_features": self.ranking_features,
            "ranking_score_breakdown": self.ranking_score_breakdown,
            "ranking_drop_reason": list(self.ranking_drop_reason),
            "candidate_score": self.candidate_score,
            "source_boost": self.source_boost,
            "quality_penalties_total": self.quality_penalties_total,
        }


def _strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def _normalize_query_text(text: Optional[str]) -> str:
    return " ".join(_strip_accents(str(text or "").strip().lower()).split())


def _infer_category_labels_from_text(text: Optional[str]) -> List[str]:
    normalized = _normalize_query_text(text)
    if not normalized:
        return []
    labels: List[str] = []
    for label, terms in _CATEGORY_ANCHORS.items():
        if any(_normalized_visible_term_matches(normalized, term) for term in terms):
            labels.append(label)
    return labels


def _infer_ingredient_ids_from_text(text: Optional[str]) -> List[str]:
    normalized = _normalize_query_text(text)
    if not normalized:
        return []
    matched: List[str] = []
    for phrase, canonical in sorted(
        _INGREDIENT_CANONICAL_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if _normalized_visible_term_matches(normalized, phrase) and canonical not in matched:
            matched.append(canonical)
    return matched


def _tokenize_query_terms(text: str) -> List[str]:
    normalized = _normalize_query_text(text)
    if not normalized:
        return []
    tokens = [token for token in re.split(r"[^a-z0-9]+", normalized) if token]
    deduped: List[str] = []
    for token in tokens:
        if len(token) <= 1 or token in _QUERY_STOPWORDS:
            continue
        if token not in deduped:
            deduped.append(token)
    return deduped


def _coerce_string_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        if raw.strip().startswith("["):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = None
            if parsed is not None:
                return _coerce_string_list(parsed)
        parts = [part.strip() for part in re.split(r"[;,|]", raw) if part.strip()]
        return parts
    if isinstance(raw, (list, tuple, set)):
        values: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                for key in ("label", "value", "name", "id", "ingredient_id"):
                    candidate = str(item.get(key) or "").strip()
                    if candidate:
                        values.append(candidate)
                        break
            else:
                candidate = str(item or "").strip()
                if candidate:
                    values.append(candidate)
        return values
    if isinstance(raw, dict):
        values: List[str] = []
        for key in (
            "visible_attributes",
            "visibleAttributes",
            "ingredient_ids",
            "ingredientIds",
            "reviewed_ingredient_ids",
            "reviewedIngredientIds",
            "canonical_ingredient_ids",
            "canonicalIngredientIds",
            "tags",
        ):
            if key in raw:
                values.extend(_coerce_string_list(raw.get(key)))
        return values
    return []


def _seed_snapshot(seed_data: Dict[str, Any]) -> Dict[str, Any]:
    return ensure_json_obj(seed_data.get("snapshot"))


def _extract_seed_tags(seed_data: Dict[str, Any]) -> List[str]:
    snapshot = _seed_snapshot(seed_data)
    deduped: List[str] = []
    for raw in (
        seed_data.get("tags"),
        snapshot.get("tags"),
        seed_data.get("product", {}).get("tags") if isinstance(seed_data.get("product"), dict) else None,
    ):
        for tag in _coerce_string_list(raw):
            if tag not in deduped:
                deduped.append(tag)
    return deduped


def _extract_seed_visible_attributes(row: Dict[str, Any], seed_data: Dict[str, Any]) -> Dict[str, List[str]]:
    snapshot = _seed_snapshot(seed_data)
    merged: Dict[str, List[str]] = {}

    def merge_bucket(bucket: str, raw: Any) -> None:
        if raw is None:
            return
        items = _coerce_string_list(raw)
        if not items:
            return
        existing = merged.setdefault(bucket, [])
        for item in items:
            normalized = _normalize_visible_attribute_text(item)
            if not normalized:
                continue
            canonical = normalized.replace(" ", "_") if bucket != "product_category" else normalized.replace(" ", "_")
            if canonical not in existing:
                existing.append(canonical)

    def merge_visible_attribute_map(raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        normalized = _normalize_visible_attribute_labels(raw)
        for bucket, labels in normalized.items():
            for label in labels:
                merge_bucket(bucket, label)

    for container in (
        row,
        seed_data,
        snapshot,
        ensure_json_obj(seed_data.get("platform_metadata")),
        ensure_json_obj(snapshot.get("platform_metadata")),
        ensure_json_obj(seed_data.get("beauty_meta")),
        ensure_json_obj(seed_data.get("beautyMeta")),
    ):
        if not isinstance(container, dict):
            continue
        merge_visible_attribute_map(container.get("visible_attributes"))
        merge_visible_attribute_map(container.get("visibleAttributes"))
        for bucket in ("product_category", "skin_concern", "formula_constraint"):
            merge_bucket(bucket, container.get(bucket))

    if not merged.get("product_category"):
        inferred_categories = _infer_category_labels_from_text(
            " ".join(
                [
                    str(row.get("title") or ""),
                    str(seed_data.get("title") or ""),
                    str(row.get("canonical_url") or ""),
                    str(row.get("destination_url") or ""),
                ]
            )
        )
        for label in inferred_categories:
            merge_bucket("product_category", label)

    return {bucket: labels for bucket, labels in merged.items() if labels}


def normalize_external_seed_structured_ingredient_ids(
    row: Dict[str, Any],
    seed_data: Dict[str, Any],
) -> List[str]:
    snapshot = _seed_snapshot(seed_data)
    deduped: List[str] = []

    for raw in (
        row.get("reviewed_ingredient_ids"),
        row.get("canonical_ingredient_ids"),
        row.get("ingredient_ids"),
        row.get("platform_metadata"),
        seed_data.get("reviewed_ingredient_ids"),
        seed_data.get("canonical_ingredient_ids"),
        seed_data.get("ingredient_ids"),
        seed_data.get("platform_metadata"),
        snapshot.get("reviewed_ingredient_ids"),
        snapshot.get("canonical_ingredient_ids"),
        snapshot.get("ingredient_ids"),
        snapshot.get("platform_metadata"),
    ):
        normalized_values = _normalize_ingredient_ids(_coerce_string_list(raw))
        for value in normalized_values:
            if value not in deduped:
                deduped.append(value)

    inferred = _infer_ingredient_ids_from_text(
        " ".join(
            [
                str(row.get("title") or ""),
                str(seed_data.get("title") or ""),
                str(row.get("canonical_url") or ""),
                str(row.get("destination_url") or ""),
            ]
        )
    )
    for value in inferred:
        if value not in deduped:
            deduped.append(value)

    return deduped


def normalize_external_seed_product_type(
    row: Dict[str, Any],
    seed_data: Dict[str, Any],
) -> str:
    snapshot = _seed_snapshot(seed_data)
    for candidate in (
        row.get("category"),
        seed_data.get("category"),
        snapshot.get("category"),
        seed_data.get("product_type"),
        snapshot.get("product_type"),
        seed_data.get("productType"),
        snapshot.get("productType"),
    ):
        text = str(candidate or "").strip()
        if text and text.lower() != "external":
            return text
    inferred_categories = _infer_category_labels_from_text(
        " ".join(
            [
                str(row.get("title") or ""),
                str(seed_data.get("title") or ""),
                str(row.get("canonical_url") or ""),
                str(row.get("destination_url") or ""),
            ]
        )
    )
    if inferred_categories:
        return inferred_categories[0].replace("_", " ").title()
    return ""


def _build_external_seed_visible_attributes(product_type: Optional[str]) -> Dict[str, List[str]]:
    normalized = _normalize_visible_attribute_text(product_type).replace(" ", "_")
    if normalized and normalized in _CATEGORY_ANCHORS:
        return {"product_category": [normalized]}
    return {}


def _normalize_seed_variants(seed_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    snapshot = _seed_snapshot(seed_data)
    raw_variants = seed_data.get("variants")
    if not isinstance(raw_variants, list):
        raw_variants = snapshot.get("variants")
    return [item for item in raw_variants if isinstance(item, dict)] if isinstance(raw_variants, list) else []


def _normalize_seed_variant_options(variant: Dict[str, Any]) -> Dict[str, str]:
    options = variant.get("options")
    if isinstance(options, dict):
        normalized: Dict[str, str] = {}
        for key, value in options.items():
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if key_text and value_text:
                normalized[key_text] = value_text
        return normalized
    if isinstance(options, list):
        normalized = {}
        for item in options:
            if not isinstance(item, dict):
                continue
            key_text = str(item.get("name") or item.get("key") or "").strip()
            value_text = str(item.get("value") or item.get("label") or "").strip()
            if key_text and value_text:
                normalized[key_text] = value_text
        return normalized
    return {}


def _derive_seed_variant_visible_option_labels(variant: Dict[str, Any]) -> List[str]:
    explicit = _coerce_string_list(variant.get("visible_option_labels"))
    normalized = [label for label in explicit if label]
    for key, value in _normalize_seed_variant_options(variant).items():
        key_norm = _normalize_query_text(str(key or ""))
        value_norm = _normalize_query_text(str(value or ""))
        if not key_norm or not value_norm:
            continue
        if key_norm == "spf":
            label = f"spf_{re.sub(r'[^a-z0-9]+', '_', value_norm).strip('_')}"
            if label and label not in normalized:
                normalized.append(label)
    return normalized


def build_external_seed_filter_product(
    *,
    row: Dict[str, Any],
    seed_data: Dict[str, Any],
    external_product: Optional[Dict[str, Any]] = None,
) -> StandardProduct:
    external_product = dict(external_product or {})
    title = str(
        external_product.get("title")
        or row.get("title")
        or seed_data.get("title")
        or "External product"
    ).strip() or "External product"
    description = str(
        external_product.get("description")
        or seed_data.get("description")
        or seed_data.get("pdp_description_raw")
        or ""
    ).strip()
    product_id = str(
        external_product.get("product_id")
        or external_product.get("id")
        or row.get("external_product_id")
        or seed_data.get("external_product_id")
        or stable_external_product_id(row.get("canonical_url") or row.get("destination_url") or "")
    ).strip()
    try:
        price = float(
            external_product.get("price")
            or row.get("price_amount")
            or seed_data.get("price_amount")
            or 0
        )
    except Exception:
        price = 0.0
    # This default is STRUCTURAL, not an observation: `StandardProduct.currency`
    # is a required `str`, and this object is the FILTER/ranking product, never
    # the served projection. It must not escape — `build_ranked_external_beauty
    # _candidate` above deliberately does not read it back, because doing so
    # re-imported the fallback and defeated #1634 one hop upstream.
    currency = str(
        external_product.get("currency")
        or row.get("price_currency")
        or seed_data.get("price_currency")
        or "USD"
    ).strip().upper() or "USD"
    availability = str(
        external_product.get("availability")
        or row.get("availability")
        or seed_data.get("availability")
        or "unknown"
    ).strip().lower()
    ingredient_ids = normalize_external_seed_structured_ingredient_ids(row, seed_data)
    product_type = normalize_external_seed_product_type(row, seed_data)
    variants: List[StandardProductVariant] = []
    explicit_visible_attributes = _extract_seed_visible_attributes(row, seed_data)
    fallback_visible_attributes = _build_external_seed_visible_attributes(product_type)
    for bucket, labels in fallback_visible_attributes.items():
        existing = explicit_visible_attributes.setdefault(bucket, [])
        for label in labels:
            if label not in existing:
                existing.append(label)
    for idx, variant in enumerate(_normalize_seed_variants(seed_data)):
        variant_id = str(variant.get("variant_id") or variant.get("id") or variant.get("sku") or f"{product_id}_{idx + 1}").strip()
        try:
            variant_price = float(
                variant.get("price_amount")
                or variant.get("price")
                or price
            )
        except Exception:
            variant_price = price
        variant_availability = str(variant.get("availability") or availability).strip().lower()
        variants.append(
            StandardProductVariant(
                id=variant_id,
                variant_id=variant_id,
                title=str(variant.get("title") or variant.get("name") or f"Variant {idx + 1}"),
                price=variant_price,
                inventory_quantity=0 if variant_availability in {"out_of_stock", "outofstock", "sold_out"} else 999,
                options=_normalize_seed_variant_options(variant),
                image_url=variant.get("image_url"),
                visible_option_labels=_derive_seed_variant_visible_option_labels(variant),
            )
        )

    return StandardProduct(
        id=product_id,
        product_id=product_id,
        platform="external",
        merchant_id=EXTERNAL_SEED_MERCHANT_ID,
        title=title,
        description=description,
        vendor=str(
            seed_data.get("vendor")
            or seed_data.get("brand")
            or seed_data.get("manufacturer")
            or row.get("domain")
            or ""
        ).strip()
        or None,
        product_type=product_type or None,
        tags=_extract_seed_tags(seed_data),
        visible_attributes=explicit_visible_attributes,
        ingredient_ids=ingredient_ids,
        price=price,
        currency=currency,
        inventory_quantity=0 if availability in {"out_of_stock", "outofstock", "sold_out"} else 999,
        image_url=external_product.get("image_url") or row.get("image_url") or seed_data.get("image_url"),
        variants=variants,
        status=ProductStatus.ACTIVE,
        in_stock=availability not in {"out_of_stock", "outofstock", "sold_out"},
        platform_metadata={
            "external_seed_id": row.get("id"),
            "canonical_url": row.get("canonical_url") or seed_data.get("canonical_url"),
            "destination_url": row.get("destination_url") or seed_data.get("destination_url"),
            **({"reviewed_ingredient_ids": ingredient_ids} if ingredient_ids else {}),
        },
    )


def _extract_query_category_labels(normalized_query: str) -> List[str]:
    return _infer_category_labels_from_text(normalized_query)


def _extract_query_formula_labels(normalized_query: str) -> List[str]:
    labels: List[str] = []
    for label, terms in _FORMULA_TERMS.items():
        if any(_normalized_visible_term_matches(normalized_query, term) for term in terms):
            labels.append(label)
    return labels


def _extract_query_concern_labels(normalized_query: str) -> List[str]:
    labels: List[str] = []
    for label, terms in _CONCERN_TERMS.items():
        if any(_normalized_visible_term_matches(normalized_query, term) for term in terms):
            labels.append(label)
    return labels


def _extract_query_ingredient_ids(normalized_query: str) -> List[str]:
    return _infer_ingredient_ids_from_text(normalized_query)


def _candidate_blob(candidate: RankedExternalBeautyCandidate) -> str:
    parts: List[str] = [
        candidate.title,
        candidate.description,
        candidate.product_type,
        candidate.brand,
        candidate.domain,
        candidate.canonical_url,
        candidate.destination_url,
        " ".join(candidate.filter_product.ingredient_ids or []),
    ]
    for values in (candidate.filter_product.visible_attributes or {}).values():
        parts.extend(values or [])
    for variant in candidate.filter_product.variants or []:
        parts.append(str(variant.title or ""))
        for label in variant.visible_option_labels or []:
            parts.append(label)
    return _normalize_query_text(" ".join(part for part in parts if str(part or "").strip()))


def _candidate_category_labels(candidate: RankedExternalBeautyCandidate) -> List[str]:
    categories = list((candidate.filter_product.visible_attributes or {}).get("product_category") or [])
    normalized_type = _normalize_visible_attribute_text(candidate.product_type).replace(" ", "_")
    if normalized_type and normalized_type not in categories:
        categories.append(normalized_type)
    return categories


def _candidate_category_anchor_blob(candidate: RankedExternalBeautyCandidate) -> str:
    return _normalize_query_text(
        " ".join(
            [
                candidate.title,
                candidate.product_type,
                " ".join(_candidate_category_labels(candidate)),
            ]
        )
    )


def _candidate_has_sun_protection(candidate: RankedExternalBeautyCandidate) -> bool:
    visible_option_labels: List[str] = []
    for variant in candidate.filter_product.variants or []:
        for label in variant.visible_option_labels or []:
            normalized = _normalize_query_text(label)
            if normalized:
                visible_option_labels.append(normalized)
    haystack = _normalize_query_text(
        " ".join(
            [
                candidate.title,
                candidate.product_type,
                " ".join(_candidate_category_labels(candidate)),
                " ".join(visible_option_labels),
            ]
        )
    )
    return bool(
        "sunscreen" in haystack
        or "sun screen" in haystack
        or "spf" in haystack
        or any(label.startswith("spf_") for label in visible_option_labels)
    )


def _form_factor_labels(text: str) -> List[str]:
    labels: List[str] = []
    normalized = _normalize_query_text(text)
    for term in _FORM_FACTOR_TERMS:
        if _normalized_visible_term_matches(normalized, term):
            labels.append(term)
    return labels


def _packaging_variant_labels(text: str) -> List[str]:
    labels: List[str] = []
    normalized = _normalize_query_text(text)
    for label, terms in _PACKAGING_VARIANT_TERMS.items():
        if any(_normalized_visible_term_matches(normalized, term) for term in terms):
            labels.append(label)
    return labels


def build_ranked_external_beauty_candidate(
    row: Dict[str, Any],
    *,
    source_order: int,
) -> RankedExternalBeautyCandidate:
    row_dict = dict(row or {})
    seed_data = ensure_json_obj(row_dict.get("seed_data"))
    product = build_external_seed_filter_product(row=row_dict, seed_data=seed_data)
    canonical_url = str(row_dict.get("canonical_url") or seed_data.get("canonical_url") or row_dict.get("destination_url") or "").strip()
    destination_url = str(row_dict.get("destination_url") or seed_data.get("destination_url") or canonical_url).strip()
    external_product_id = str(
        row_dict.get("external_product_id")
        or seed_data.get("external_product_id")
        or stable_external_product_id(canonical_url or destination_url)
    ).strip()
    try:
        price_amount = float(row_dict.get("price_amount") or seed_data.get("price_amount") or product.price or 0)
    except Exception:
        price_amount = None
    try:
        brand_term_hit = max(0, int(row_dict.get("brand_term_hit") or 0))
    except Exception:
        brand_term_hit = 0
    return RankedExternalBeautyCandidate(
        row=row_dict,
        seed_data=seed_data,
        filter_product=product,
        external_product_id=external_product_id,
        title=str(product.title or "").strip(),
        description=str(product.description or "").strip(),
        brand=str(product.vendor or seed_data.get("brand") or "").strip(),
        domain=str(row_dict.get("domain") or "").strip(),
        canonical_url=canonical_url,
        destination_url=destination_url,
        product_type=str(product.product_type or "").strip(),
        category=str(seed_data.get("category") or "").strip() or None,
        availability=str(row_dict.get("availability") or seed_data.get("availability") or "").strip() or None,
        price_amount=price_amount,
        # NOT `or product.currency`. That field is a StandardProduct `str` whose
        # default is "USD" (see build_external_seed_filter_product below), so
        # reading it here re-imported the very fallback #1634 removes — and it
        # did so UPSTREAM of the gateway's `_observed_currency`, making that
        # resolver unable to ever return None on this lane. The tail was moved,
        # not deleted. Only the two real observations are consulted.
        price_currency=str(row_dict.get("price_currency") or seed_data.get("price_currency") or "").strip().upper() or None,
        source_order=source_order,
        brand_term_hit=brand_term_hit,
        updated_at=row_dict.get("updated_at"),
    )


def score_external_beauty_candidate(
    candidate: RankedExternalBeautyCandidate,
    *,
    query: str,
) -> RankedExternalBeautyCandidate:
    normalized_query = _normalize_query_text(query)
    query_terms = _tokenize_query_terms(normalized_query)
    query_compact = re.sub(r"[^a-z0-9]+", "", normalized_query)
    title_text = _normalize_query_text(candidate.title)
    title_compact = re.sub(r"[^a-z0-9]+", "", title_text)
    blob = _candidate_blob(candidate)
    blob_compact = re.sub(r"[^a-z0-9]+", "", blob)
    category_anchor_blob = _candidate_category_anchor_blob(candidate)
    candidate_categories = _candidate_category_labels(candidate)
    candidate_concerns = list((candidate.filter_product.visible_attributes or {}).get("skin_concern") or [])
    candidate_formula = list((candidate.filter_product.visible_attributes or {}).get("formula_constraint") or [])
    candidate_ingredient_ids = list(candidate.filter_product.ingredient_ids or [])

    title_matches = sum(1 for term in query_terms if term and term in title_text)
    blob_matches = sum(1 for term in query_terms if term and term in blob)
    exact_phrase = 0.0
    if normalized_query:
        if normalized_query == title_text:
            exact_phrase = 1.0
        elif normalized_query in title_text:
            exact_phrase = 0.92
        elif normalized_query in blob:
            exact_phrase = 0.72
        elif query_compact and len(query_compact) >= 4 and query_compact in title_compact:
            exact_phrase = 0.82
        elif query_compact and len(query_compact) >= 4 and query_compact in blob_compact:
            exact_phrase = 0.7

    title_ratio = (title_matches / max(len(query_terms), 1)) if query_terms else 0.0
    blob_ratio = (blob_matches / max(len(query_terms), 1)) if query_terms else 0.0
    text_relevance = round(max(exact_phrase, 0.2 + (title_ratio * 0.35) + (blob_ratio * 0.2)), 4)

    query_categories = _extract_query_category_labels(normalized_query)
    query_concerns = _extract_query_concern_labels(normalized_query)
    query_formula = _extract_query_formula_labels(normalized_query)
    query_ingredient_ids = _extract_query_ingredient_ids(normalized_query)
    query_form_factors = _form_factor_labels(normalized_query)
    candidate_form_factors = _form_factor_labels(blob)
    query_packaging_labels = _packaging_variant_labels(normalized_query)
    candidate_packaging_labels = _packaging_variant_labels(blob)
    query_has_sun_protection_intent = "sunscreen" in query_categories or "spf" in normalized_query
    candidate_has_sun_protection = _candidate_has_sun_protection(candidate)
    query_has_strict_category_intent = any(
        label in _STRICT_CATEGORY_INTENT_LABELS for label in query_categories
    )

    category_match_count = sum(
        1 for label in query_categories if label in candidate_categories or label in category_anchor_blob
    )
    ingredient_match_count = sum(
        1
        for ingredient_id in query_ingredient_ids
        if ingredient_id in candidate_ingredient_ids or ingredient_id in blob
    )
    concern_match_count = sum(
        1
        for label in query_concerns
        if label in candidate_concerns or label in blob
    )
    formula_match_count = sum(
        1
        for label in query_formula
        if label in candidate_formula or label.replace("_", " ") in blob
    )
    form_factor_match_count = sum(1 for label in query_form_factors if label in candidate_form_factors or label in blob)
    packaging_match_count = sum(
        1 for label in query_packaging_labels if label in candidate_packaging_labels
    )

    category_anchor_score = min(0.28, category_match_count * 0.18)
    active_ingredient_score = min(0.28, ingredient_match_count * 0.2)
    concern_score = min(0.24, concern_match_count * 0.12)
    formula_score = min(0.12, formula_match_count * 0.08)
    form_factor_score = min(0.1, form_factor_match_count * 0.05)
    packaging_score = min(0.08, packaging_match_count * 0.04)
    ingredient_concern_synergy = (
        0.12
        if query_ingredient_ids and query_concerns and concern_match_count > 0
        else 0.0
    )

    penalties: Dict[str, float] = {}
    if query_categories and category_match_count <= 0:
        penalties["missing_category_anchor"] = (
            0.24
            if query_has_strict_category_intent
            else (0.1 if ingredient_concern_synergy > 0 else 0.24)
        )
    if query_ingredient_ids and ingredient_match_count <= 0:
        penalties["missing_active_ingredient"] = 0.12
    if query_concerns and concern_match_count <= 0:
        penalties["missing_concern_anchor"] = 0.18 if query_ingredient_ids else 0.1
    if query_formula and formula_match_count <= 0:
        penalties["missing_formula_constraint"] = 0.08
    if "sunscreen" in query_categories and "sunscreen" not in candidate_categories:
        penalties["missing_sunscreen_category"] = 0.12 if candidate_has_sun_protection else 0.08
    if "eye cream" in title_text and "eye" not in query_terms:
        penalties["eye_cream_mismatch"] = 0.22
    if any(term in blob for term in _EXCLUSION_BUNDLE_TERMS) and not any(term in query_terms for term in _EXCLUSION_BUNDLE_TERMS):
        penalties["bundle_or_routine_penalty"] = 0.14
    if query_form_factors and form_factor_match_count <= 0 and not (
        query_ingredient_ids and concern_match_count > 0
    ):
        penalties["form_factor_mismatch"] = 0.08
    if query_packaging_labels and packaging_match_count <= 0:
        penalties["packaging_mismatch"] = 0.08
    if not query_packaging_labels and "travel_size" in candidate_packaging_labels:
        penalties["travel_size_without_intent"] = 0.12
    if (
        "moisturizer" in query_categories
        and not query_has_sun_protection_intent
        and candidate_has_sun_protection
    ):
        penalties["sun_protection_without_intent"] = 0.1

    quality_penalties_total = round(sum(penalties.values()), 4)
    candidate_score = round(
        max(
            0.12,
            min(
                1.35,
                text_relevance
                + category_anchor_score
                + active_ingredient_score
                + concern_score
                + ingredient_concern_synergy
                + formula_score
                + form_factor_score
                + packaging_score
                - quality_penalties_total,
            ),
        ),
        4,
    )

    candidate.source_boost = 0.0
    candidate.quality_penalties_total = quality_penalties_total
    candidate.candidate_score = candidate_score
    candidate.ranking_features = {
        "query_terms": query_terms,
        "query_category_labels": query_categories,
        "query_concern_labels": query_concerns,
        "query_formula_labels": query_formula,
        "query_ingredient_ids": query_ingredient_ids,
        "query_form_factor_labels": query_form_factors,
        "query_packaging_labels": query_packaging_labels,
        "query_has_sun_protection_intent": query_has_sun_protection_intent,
        "query_has_strict_category_intent": query_has_strict_category_intent,
        "candidate_category_labels": candidate_categories,
        "candidate_concern_labels": candidate_concerns,
        "candidate_formula_labels": candidate_formula,
        "candidate_ingredient_ids": candidate_ingredient_ids,
        "candidate_form_factor_labels": candidate_form_factors,
        "candidate_packaging_labels": candidate_packaging_labels,
        "candidate_has_sun_protection": candidate_has_sun_protection,
        "title_matches": title_matches,
        "blob_matches": blob_matches,
        "category_match_count": category_match_count,
        "ingredient_match_count": ingredient_match_count,
        "concern_match_count": concern_match_count,
        "formula_match_count": formula_match_count,
        "form_factor_match_count": form_factor_match_count,
        "packaging_match_count": packaging_match_count,
    }
    candidate.ranking_score_breakdown = {
        "ranking_audit_version": BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION,
        "text_relevance": text_relevance,
        "category_anchor_score": round(category_anchor_score, 4),
        "active_ingredient_score": round(active_ingredient_score, 4),
        "concern_score": round(concern_score, 4),
        "formula_score": round(formula_score, 4),
        "form_factor_score": round(form_factor_score, 4),
        "packaging_score": round(packaging_score, 4),
        "ingredient_concern_synergy": round(ingredient_concern_synergy, 4),
        "quality_penalties": penalties,
        "quality_penalties_total": quality_penalties_total,
        "source_order_tie_break": candidate.source_order,
        "brand_term_hit_hint": candidate.brand_term_hit,
        "candidate_score": candidate_score,
        "source_boost": candidate.source_boost,
        "price_tie_break": candidate.price_amount,
    }
    return candidate


def rank_external_seed_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    query: str,
    limit: Optional[int] = None,
) -> List[RankedExternalBeautyCandidate]:
    ranked: List[RankedExternalBeautyCandidate] = []
    for idx, row in enumerate(rows or []):
        candidate = build_ranked_external_beauty_candidate(dict(row or {}), source_order=idx)
        ranked.append(score_external_beauty_candidate(candidate, query=query))

    ranked.sort(
        key=lambda item: (
            -(item.candidate_score + item.source_boost),
            item.source_order,
            item.price_amount if item.price_amount is not None else 999999.0,
        )
    )

    deduped: List[RankedExternalBeautyCandidate] = []
    seen_external_ids: set[str] = set()
    max_items = max(1, int(limit or len(ranked) or 1))
    for candidate in ranked:
        if not candidate.external_product_id or candidate.external_product_id in seen_external_ids:
            continue
        seen_external_ids.add(candidate.external_product_id)
        deduped.append(candidate)
        if len(deduped) >= max_items:
            break
    return deduped


def build_ranking_audit_record(
    *,
    query: str,
    raw_rows: Iterable[Dict[str, Any]],
    ranked_candidates: Iterable[RankedExternalBeautyCandidate],
) -> Dict[str, Any]:
    raw_rows_list = [dict(row or {}) for row in raw_rows or []]
    ranked_list = list(ranked_candidates or [])
    return {
        "ranking_audit_version": BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION,
        "query": query,
        "raw_seed_rows": [
            {
                "external_product_id": str(
                    row.get("external_product_id")
                    or ensure_json_obj(row.get("seed_data")).get("external_product_id")
                    or stable_external_product_id(row.get("canonical_url") or row.get("destination_url") or "")
                ).strip(),
                "title": str(row.get("title") or "").strip(),
                "domain": str(row.get("domain") or "").strip(),
                "canonical_url": str(row.get("canonical_url") or "").strip(),
                "destination_url": str(row.get("destination_url") or "").strip(),
                "source_order": idx,
                "updated_at": str(row.get("updated_at")) if row.get("updated_at") is not None else None,
                "brand_term_hit": row.get("brand_term_hit"),
            }
            for idx, row in enumerate(raw_rows_list)
        ],
        "ranked_candidates": [candidate.as_feature_dump() for candidate in ranked_list],
    }
