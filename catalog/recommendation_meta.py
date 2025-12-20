"""
Helpers to derive weak recommendation metadata for products.

The goal is to expose lightweight, machine-readable grouping and facet signals
for recommendation use-cases, without changing core ingestion flows.

Schema stored under products_cache.product_data.recommendation_meta:
{
    "version": 1,
    "group_id": "651785399260" | null,
    "tags_raw": ["Group-651785399260", "Cat:Brush", ...],
    "tags": ["group-651785399260", "cat:brush", ...],
    "facets": {
        "cat": "brush" | null,
        "area": ["face", "eyes", ...],
        "use": ["foundation", "powder", ...],
        "material": ["synthetic", "natural", ...],
        "hair": ["goat", "squirrel", ...],
        "shape": [...],
        "feature": [...],
        "series": [...],
        "color": [...],
        "ships": ["48h"]  # e.g. parsed from ships-48h
    }
}
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from models.standard_product import StandardProduct

TagInput = Union[str, Sequence[str], None]


def _normalize_tag_value(value: str) -> str:
    """Normalize a single tag to a canonical form (lowercase, stripped)."""
    return str(value or "").strip().lower()


def _unique(values: Iterable[str]) -> List[str]:
    """Return a list with duplicates removed while preserving order."""
    seen = set()
    result: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def parse_tags(raw_tags: TagInput) -> Tuple[List[str], List[str]]:
    """
    Parse raw tags from Shopify / StandardProduct into:

    - tags_raw: original, human-facing tags (trimmed, empty filtered)
    - tags_norm: normalized tags (lowercase, trimmed, deduplicated)

    raw_tags may be:
    - None / empty
    - comma-separated string (Shopify products.tags)
    - list/tuple of strings (StandardProduct.tags)
    """
    if raw_tags is None:
        return [], []

    items: List[str] = []
    if isinstance(raw_tags, str):
        # Shopify typically stores tags as a comma-separated string. Some upstream
        # systems use ';' instead. Treat both as delimiters.
        text = raw_tags.replace(";", ",")
        items = [part.strip() for part in text.split(",")]
    else:
        # Assume sequence of strings.
        for item in raw_tags:
            items.append(str(item).strip())

    tags_raw: List[str] = [tag for tag in items if tag]
    tags_norm_all: List[str] = [_normalize_tag_value(tag) for tag in tags_raw]
    tags_norm: List[str] = _unique([tag for tag in tags_norm_all if tag])

    return tags_raw, tags_norm


def derive_facets(tags_norm: List[str]) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Derive group_id and structured facets from normalized tags.

    Rules:
    - group_id: first tag matching /^group-(\\d+)$/
    - Recognized prefixes (case-insensitive on normalized tags):
      - cat:
      - area:
      - use:
      - mat: or material:
      - hair:
      - shape:
      - feature:
      - series:
      - color:
      - ships- (and ships:)

    Facets:
    - cat: single string or None
    - others: arrays, de-duplicated, empty arrays omitted in output
    """
    group_id: Optional[str] = None

    cat_value: Optional[str] = None
    area_values: List[str] = []
    use_values: List[str] = []
    material_values: List[str] = []
    hair_values: List[str] = []
    shape_values: List[str] = []
    feature_values: List[str] = []
    series_values: List[str] = []
    color_values: List[str] = []
    ships_values: List[str] = []

    # Support both group-<id> and group:<id>, where <id> is numeric for now.
    group_pattern = re.compile(r"^group[-:]([0-9]+)$")

    for tag in tags_norm:
        if not tag:
            continue

        # group_id
        if group_id is None:
            group_match = group_pattern.match(tag)
            if group_match:
                group_id = group_match.group(1)
                # Continue processing tag so that it can also appear in tags list
                # but not used for facets beyond group_id.

        # Structured facets by prefix
        if tag.startswith("cat:"):
            value = tag[len("cat:") :].strip()
            # First-wins semantics to avoid flakiness when tag order changes.
            if value and cat_value is None:
                cat_value = value
            continue

        if tag.startswith("area:"):
            value = tag[len("area:") :].strip()
            if value:
                area_values.append(value)
            continue

        if tag.startswith("use:"):
            value = tag[len("use:") :].strip()
            if value:
                use_values.append(value)
            continue

        if tag.startswith("mat:"):
            value = tag[len("mat:") :].strip()
            if value:
                material_values.append(value)
            continue

        if tag.startswith("material:"):
            value = tag[len("material:") :].strip()
            if value:
                material_values.append(value)
            continue

        if tag.startswith("hair:"):
            value = tag[len("hair:") :].strip()
            if value:
                hair_values.append(value)
            continue

        if tag.startswith("shape:"):
            value = tag[len("shape:") :].strip()
            if value:
                shape_values.append(value)
            continue

        if tag.startswith("feature:"):
            value = tag[len("feature:") :].strip()
            if value:
                feature_values.append(value)
            continue

        if tag.startswith("series:"):
            value = tag[len("series:") :].strip()
            if value:
                series_values.append(value)
            continue

        if tag.startswith("color:"):
            value = tag[len("color:") :].strip()
            if value:
                color_values.append(value)
            continue

        if tag.startswith("ships-"):
            value = tag[len("ships-") :].strip()
            if value:
                ships_values.append(value)
            continue

        if tag.startswith("ships:"):
            value = tag[len("ships:") :].strip()
            if value:
                ships_values.append(value)
            continue

    facets: Dict[str, Any] = {}

    if cat_value:
        facets["cat"] = cat_value

    if area_values:
        facets["area"] = _unique(area_values)

    if use_values:
        facets["use"] = _unique(use_values)

    if material_values:
        facets["material"] = _unique(material_values)

    if hair_values:
        facets["hair"] = _unique(hair_values)

    if shape_values:
        facets["shape"] = _unique(shape_values)

    if feature_values:
        facets["feature"] = _unique(feature_values)

    if series_values:
        facets["series"] = _unique(series_values)

    if color_values:
        facets["color"] = _unique(color_values)

    if ships_values:
        facets["ships"] = _unique(ships_values)

    return group_id, facets


def derive_recommendation_meta(
    standard_product: Optional[StandardProduct],
    raw_shopify_product: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Derive recommendation_meta structure from StandardProduct and raw Shopify product.

    - Prefer raw Shopify tags (product.tags string) when available.
    - Fallback to StandardProduct.tags when raw tags are missing.
    - Always return a stable structure with version field.
    """
    raw_tags_source: TagInput = None

    raw_shopify_product = raw_shopify_product or {}

    # Shopify Admin API: tags is usually a comma-separated string.
    if isinstance(raw_shopify_product, dict):
        tags_value = raw_shopify_product.get("tags")
        if tags_value:
            raw_tags_source = tags_value

    # Fallback to StandardProduct.tags if present.
    if raw_tags_source is None and standard_product is not None:
        if getattr(standard_product, "tags", None):
            raw_tags_source = list(standard_product.tags)

    tags_raw, tags_norm = parse_tags(raw_tags_source)
    group_id, facets = derive_facets(tags_norm)

    return {
        "version": 1,
        "group_id": group_id,
        "tags_raw": tags_raw,
        "tags": tags_norm,
        "facets": facets,
    }
