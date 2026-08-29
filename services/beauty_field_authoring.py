"""Merchant-authored beauty-field write path with per-subcategory schemas.

v2.1: different beauty subcategories need different fields. Skincare /
haircare / bath / body have ingredients + how-to-use + skin concerns.
Tools have material + use_with + care instructions. Makeup is mixed.
Fragrance has notes + family. Prompting the merchant for INCI on a
foundation brush (as v2.0 did) is wrong; this module fixes that by
routing each subcategory to its own field set.

Storage layout:

  raw_inci           → beauty_sku_ingredients.raw_inci (per-SKU)
  how_to_use_text    → beauty_usage_guides.how_to_use_text (per-product)
  skin_concerns      → beauty_product_profiles.concerns_json (per-product)
  tool_material      → beauty_product_profiles.profile_payload.tool_material
  use_with           → beauty_product_profiles.profile_payload.use_with
  care_instructions  → beauty_product_profiles.profile_payload.care_instructions

The first three columns are first-class on existing beauty_* tables.
The tools-specific fields live inside profile_payload JSONB — adding
real columns is a future migration; the JSONB blob keeps the v2.1
shape lean. Reads on the gateway / agent_pdp_view will need to
project from profile_payload too (v2.2 follow-up).

Race safety / source-precedence rules carry over from v2.0:
SELECT … FOR UPDATE on catalog_products row; merchant_payload guard on
raw_inci (per-SKU); no source guard on the other fields because there's
no competing writer yet.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from db.database import database

logger = logging.getLogger(__name__)


# ---- Source enum constants -------------------------------------------------

SOURCE_MERCHANT_PAYLOAD = "merchant_payload"
SOURCE_MERCHANT_AUTHORED = "merchant_authored"
SOURCE_AURORA_INGEST = "aurora_ingest"


# ---- Per-subcategory schemas ----------------------------------------------
# Single source of truth for "what fields apply where." The completeness
# endpoint reads from here to filter the queue + return per-product field
# metadata; the write endpoint reads from here to validate that a posted
# field is actually applicable to the product's subcategory.
#
# Each schema entry has:
#   subcategory_kind  — short identifier used by the UI to pick a form
#   label             — human-readable label (e.g. "Skincare")
#   fields            — ordered list of field specs the UI renders
#
# Field types:
#   text       — single-line input
#   textarea   — multi-line input
#   enum       — single-select from allowed_values
#   enum_multi — multi-select from allowed_values

ALLOWED_SKIN_CONCERNS = (
    "oily", "dry", "combination", "normal", "sensitive",
    "acne-prone", "aging", "hyperpigmentation", "redness", "dullness",
)

ALLOWED_FINISH = ("matte", "satin", "dewy", "shimmer", "metallic", "glossy")
ALLOWED_SCENT_FAMILY = (
    "floral", "citrus", "woody", "oriental", "fresh", "fruity", "musky", "spicy",
)


_SKINCARE_FIELDS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "raw_inci",
        "type": "textarea",
        "label": "Ingredient list (INCI)",
        "placeholder": "Aqua / Water, Glycerin, Niacinamide, ...",
        "hint": "Paste the full INCI list. Agents use this for ingredient questions.",
    },
    {
        "name": "how_to_use_text",
        "type": "textarea",
        "label": "How to use",
        "placeholder": "Apply morning and evening to clean skin.",
        "hint": "Application instructions or routine notes.",
    },
    {
        "name": "skin_concerns",
        "type": "enum_multi",
        "label": "Skin concerns this targets",
        "allowed_values": list(ALLOWED_SKIN_CONCERNS),
        "hint": "Pick all that apply. Drives shopping-search relevance.",
    },
)

_TOOLS_FIELDS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "tool_material",
        "type": "text",
        "label": "Bristle / handle material",
        "placeholder": "Synthetic fibers, horse hair, nylon, ...",
        "hint": "What the brush, sponge, or applicator is made of.",
    },
    {
        "name": "use_with",
        "type": "text",
        "label": "Use with",
        "placeholder": "Eyeshadow, foundation, powder, ...",
        "hint": "Which product type this tool is designed for.",
    },
    {
        "name": "care_instructions",
        "type": "textarea",
        "label": "Care + cleaning",
        "placeholder": "Wash weekly with mild soap & water; dry flat.",
        "hint": "How merchants should clean and maintain it.",
    },
)


# Two top-level groups the UI surfaces as separate tabs. "Beauty care" =
# skincare-shape products (skincare/haircare/bath/body/makeup) which ALL
# get prompted for INCI + how-to-use. "Beauty tools" = brushes / sponges /
# applicators which get material + use-with + care instructions. Keeping
# these distinct in the API + UI is the v2.1.1 simplification (users
# found a single mixed Beauty tab with mid-flow form-switching confusing).
SUBCATEGORY_GROUP_BEAUTY_CARE = "beauty_care"
SUBCATEGORY_GROUP_BEAUTY_TOOLS = "beauty_tools"

SUBCATEGORY_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # Skincare / haircare / bath / body all share the same field set —
    # ingredients, how-to-use, and (loosely) skin concerns. Haircare uses
    # the same skin_concerns enum for v1 since the overlap (oily, dry,
    # sensitive) covers the most-asked cases.
    "beauty/skincare/": {
        "subcategory_kind": "skincare",
        "subcategory_group": SUBCATEGORY_GROUP_BEAUTY_CARE,
        "label": "Skincare",
        "fields": _SKINCARE_FIELDS,
    },
    "beauty/haircare/": {
        "subcategory_kind": "haircare",
        "subcategory_group": SUBCATEGORY_GROUP_BEAUTY_CARE,
        "label": "Haircare",
        "fields": _SKINCARE_FIELDS,
    },
    "beauty/bath/": {
        "subcategory_kind": "bath",
        "subcategory_group": SUBCATEGORY_GROUP_BEAUTY_CARE,
        "label": "Bath",
        "fields": _SKINCARE_FIELDS,
    },
    "beauty/body/": {
        "subcategory_kind": "body",
        "subcategory_group": SUBCATEGORY_GROUP_BEAUTY_CARE,
        "label": "Body",
        "fields": _SKINCARE_FIELDS,
    },
    # Makeup loosely fits skincare's INCI + how-to-use but skin_concerns
    # is genuinely wrong for it (a lipstick doesn't "target oily skin").
    # For v2.1 we use INCI + how_to_use only; finish/shade family come
    # in v2.2 as they're per-SKU and need shade-row schema work.
    "beauty/makeup/": {
        "subcategory_kind": "makeup",
        "subcategory_group": SUBCATEGORY_GROUP_BEAUTY_CARE,
        "label": "Makeup",
        "fields": (
            _SKINCARE_FIELDS[0],  # raw_inci
            _SKINCARE_FIELDS[1],  # how_to_use_text
        ),
    },
    # Tools — distinct group from beauty care. Brushes / sponges /
    # applicators have material/use_with/care, NOT ingredients.
    "beauty/tools/": {
        "subcategory_kind": "tools",
        "subcategory_group": SUBCATEGORY_GROUP_BEAUTY_TOOLS,
        "label": "Beauty tools",
        "fields": _TOOLS_FIELDS,
    },
    # Fragrance, accessories, others are NOT in the queue for v2.1 —
    # they need their own schemas (notes/family for fragrance; closer
    # to fashion for accessories). Adding them is a v2.2 follow-up.
}


# Reverse-lookup map: subcategory_group → list of category_path prefixes
# in that group. Used by the route's GROUP filter so the SQL `LIKE`
# clauses stay narrow.
SUBCATEGORY_GROUPS: Dict[str, Tuple[str, ...]] = {
    SUBCATEGORY_GROUP_BEAUTY_CARE: tuple(
        prefix for prefix, schema in SUBCATEGORY_SCHEMAS.items()
        if schema["subcategory_group"] == SUBCATEGORY_GROUP_BEAUTY_CARE
    ),
    SUBCATEGORY_GROUP_BEAUTY_TOOLS: tuple(
        prefix for prefix, schema in SUBCATEGORY_SCHEMAS.items()
        if schema["subcategory_group"] == SUBCATEGORY_GROUP_BEAUTY_TOOLS
    ),
}


def subcategory_for_path(category_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Match a catalog_products.category_path against the schema table.
    Returns the matching schema dict (with subcategory_kind, label,
    fields) or None when the path doesn't fit any v2.1 schema (in which
    case the product is excluded from the queue)."""
    if not category_path or not isinstance(category_path, str):
        return None
    lower = category_path.lower()
    for prefix, schema in SUBCATEGORY_SCHEMAS.items():
        if lower.startswith(prefix):
            return schema
    return None


def field_names_for_subcategory(subcategory_kind: str) -> Tuple[str, ...]:
    """Field names supported by a given subcategory_kind. Used by the
    PUT endpoint to validate that posted fields are applicable."""
    for schema in SUBCATEGORY_SCHEMAS.values():
        if schema["subcategory_kind"] == subcategory_kind:
            return tuple(f["name"] for f in schema["fields"])
    return tuple()


# All field names ever supported, used as the write dispatcher's contract.
_ALL_KNOWN_FIELDS: Tuple[str, ...] = (
    "raw_inci",
    "how_to_use_text",
    "skin_concerns",
    "tool_material",
    "use_with",
    "care_instructions",
)

# Fields that go into beauty_product_profiles.profile_payload JSONB
# rather than a first-class column. Adding a column for each future
# subcategory field would explode the schema; the JSONB blob keeps the
# v2.1 surface lean.
_PROFILE_PAYLOAD_FIELDS: Tuple[str, ...] = (
    "tool_material",
    "use_with",
    "care_instructions",
)


# Per-field write outcomes — same strings as fashion_field_authoring so
# the UI's outcome-handling code can be shared.
WRITE_OUTCOME_WRITTEN = "written"
WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED = "skipped_payload_owned"
WRITE_OUTCOME_PRODUCT_NOT_FOUND = "product_not_found"
WRITE_OUTCOME_UNCHANGED = "unchanged"


def _product_key(merchant_id: str, platform: str, source_product_id: str) -> str:
    return f"prod::{merchant_id}::{platform}::{source_product_id}"


def product_usage_guide_id(product_key: str) -> str:
    """THE id of a product's one product-level `beauty_usage_guides` row.

    `how_to_use` describes the product, not a variant, so a product_key has
    exactly one guide row and it carries `sku_key = NULL` (this schema's
    product-level marker — `routes/merchant_products.py` joins on
    `bug.sku_key IS NULL`). Catalog ingest derives the same id through this
    function, so the two writers converge on one row instead of racing two
    NULL-sku rows past that join.
    """
    digest = hashlib.sha256(f"usage::{product_key}::product".encode()).hexdigest()[:20]
    return f"guide_{digest}"


def _normalize_text_field(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _normalize_skin_concerns(value: Any) -> Optional[List[str]]:
    if not isinstance(value, list):
        return None
    cleaned: Set[str] = set()
    for v in value:
        if isinstance(v, str) and v.strip() in ALLOWED_SKIN_CONCERNS:
            cleaned.add(v.strip())
    if not cleaned:
        return None
    return sorted(cleaned)


async def _ensure_product_exists(product_key: str) -> Optional[Dict[str, Any]]:
    """Pin the catalog_products row. Returns dict with content_key +
    category_path, or None when the row doesn't exist."""
    row = await database.fetch_one(
        "SELECT content_key, category_path FROM catalog_products "
        "WHERE product_key = :pk FOR UPDATE",
        {"pk": product_key},
    )
    return dict(row) if row else None


async def _list_sku_keys(product_key: str) -> List[str]:
    rows = await database.fetch_all(
        "SELECT sku_key FROM catalog_skus WHERE product_key = :pk",
        {"pk": product_key},
    )
    return [r["sku_key"] for r in rows or []]


async def _write_raw_inci(
    *, product_key: str, merchant_id: str, value: str,
) -> str:
    sku_keys = await _list_sku_keys(product_key)
    if not sku_keys:
        return WRITE_OUTCOME_UNCHANGED

    wrote = 0
    payload_owned = 0
    for sku_key in sku_keys:
        existing = await database.fetch_one(
            "SELECT source_system FROM beauty_sku_ingredients "
            "WHERE sku_key = :sk FOR UPDATE",
            {"sk": sku_key},
        )
        if existing is not None and existing["source_system"] == SOURCE_MERCHANT_PAYLOAD:
            payload_owned += 1
            continue
        await database.execute(
            """
            INSERT INTO beauty_sku_ingredients (
              sku_key, product_key, merchant_id, raw_inci, source_system, updated_at
            ) VALUES (:sk, :pk, :mid, :inci, :src, NOW())
            ON CONFLICT (sku_key) DO UPDATE SET
              raw_inci = EXCLUDED.raw_inci,
              source_system = EXCLUDED.source_system,
              updated_at = NOW()
            """,
            {"sk": sku_key, "pk": product_key, "mid": merchant_id,
             "inci": value, "src": SOURCE_MERCHANT_AUTHORED},
        )
        wrote += 1

    if wrote == 0 and payload_owned > 0:
        return WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED
    return WRITE_OUTCOME_WRITTEN if wrote > 0 else WRITE_OUTCOME_UNCHANGED


async def _write_how_to_use(
    *, product_key: str, merchant_id: str, value: str,
) -> str:
    guide_id = product_usage_guide_id(product_key)
    await database.execute(
        """
        INSERT INTO beauty_usage_guides (
          guide_id, product_key, sku_key, merchant_id, how_to_use_text, updated_at
        ) VALUES (:gid, :pk, NULL, :mid, :txt, NOW())
        ON CONFLICT (guide_id) DO UPDATE SET
          how_to_use_text = EXCLUDED.how_to_use_text,
          updated_at = NOW()
        """,
        {"gid": guide_id, "pk": product_key, "mid": merchant_id, "txt": value},
    )
    return WRITE_OUTCOME_WRITTEN


async def _write_skin_concerns(
    *, product_key: str, merchant_id: str, value: List[str],
) -> str:
    await database.execute(
        """
        INSERT INTO beauty_product_profiles (
          product_key, merchant_id, concerns_json, updated_at
        ) VALUES (:pk, :mid, CAST(:concerns AS jsonb), NOW())
        ON CONFLICT (product_key) DO UPDATE SET
          concerns_json = EXCLUDED.concerns_json,
          updated_at = NOW()
        """,
        {"pk": product_key, "mid": merchant_id, "concerns": json.dumps(value)},
    )
    return WRITE_OUTCOME_WRITTEN


async def _write_profile_payload_field(
    *, product_key: str, merchant_id: str, field_name: str, value: str,
) -> str:
    """UPSERT a single key inside beauty_product_profiles.profile_payload
    JSONB. Used for subcategory-specific fields (tools, fragrance) that
    don't have their own first-class columns yet.

    Uses jsonb_set so concurrent writes to different keys don't
    clobber each other. The COALESCE keeps existing keys intact when
    profile_payload is null on a fresh row."""
    await database.execute(
        """
        INSERT INTO beauty_product_profiles (
          product_key, merchant_id, profile_payload, updated_at
        ) VALUES (
          :pk, :mid,
          jsonb_build_object(CAST(:field AS text), to_jsonb(CAST(:value AS text))),
          NOW()
        )
        ON CONFLICT (product_key) DO UPDATE SET
          profile_payload = jsonb_set(
            COALESCE(beauty_product_profiles.profile_payload, '{}'::jsonb),
            ARRAY[:field],
            to_jsonb(CAST(:value AS text)),
            true
          ),
          updated_at = NOW()
        """,
        {"pk": product_key, "mid": merchant_id, "field": field_name, "value": value},
    )
    return WRITE_OUTCOME_WRITTEN


async def write_merchant_authored_beauty_fields(
    *,
    merchant_id: str,
    platform: str,
    source_product_id: str,
    **fields: Any,
) -> Dict[str, str]:
    """Write merchant-authored beauty values, dispatching each field to
    the right storage based on its name.

    Accepts an arbitrary set of field names (the keyword args). Only
    field names listed in `_ALL_KNOWN_FIELDS` are processed; unknown
    fields are ignored silently to make backwards-compatible client
    changes safe.

    Returns a per-field outcomes dict — same shape as v2.0 so the UI's
    outcome-handling code can be shared:
      'written' | 'skipped_payload_owned' | 'product_not_found' | 'unchanged'

    Subcategory validation: the function does NOT validate that the
    posted field is applicable to the product's subcategory. The route
    layer can enforce that if it wants stricter semantics; for the
    write service we accept anything in the known-fields list, since
    the upstream UI is built off the same schema and won't post
    irrelevant fields.
    """
    pk = _product_key(merchant_id, platform, source_product_id)
    # Filter to known fields and drop nulls.
    inputs = {k: v for k, v in fields.items() if k in _ALL_KNOWN_FIELDS and v is not None}
    result: Dict[str, str] = {}
    if not inputs:
        return result

    async with database.transaction():
        product_row = await _ensure_product_exists(pk)
        if product_row is None:
            for field in inputs:
                result[field] = WRITE_OUTCOME_PRODUCT_NOT_FOUND
            logger.info(
                "beauty_authoring.product_not_found product_key=%s", pk,
            )
            return result

        for field, value in inputs.items():
            if field == "raw_inci":
                normalized = _normalize_text_field(value)
                if normalized is None:
                    result[field] = WRITE_OUTCOME_UNCHANGED
                    continue
                result[field] = await _write_raw_inci(
                    product_key=pk, merchant_id=merchant_id, value=normalized,
                )
            elif field == "how_to_use_text":
                normalized = _normalize_text_field(value)
                if normalized is None:
                    result[field] = WRITE_OUTCOME_UNCHANGED
                    continue
                result[field] = await _write_how_to_use(
                    product_key=pk, merchant_id=merchant_id, value=normalized,
                )
            elif field == "skin_concerns":
                normalized_list = _normalize_skin_concerns(value)
                if normalized_list is None:
                    result[field] = WRITE_OUTCOME_UNCHANGED
                    continue
                result[field] = await _write_skin_concerns(
                    product_key=pk, merchant_id=merchant_id, value=normalized_list,
                )
            elif field in _PROFILE_PAYLOAD_FIELDS:
                normalized = _normalize_text_field(value)
                if normalized is None:
                    result[field] = WRITE_OUTCOME_UNCHANGED
                    continue
                result[field] = await _write_profile_payload_field(
                    product_key=pk, merchant_id=merchant_id,
                    field_name=field, value=normalized,
                )

    logger.info(
        "beauty_authoring.applied product_key=%s outcomes=%s", pk, result,
    )
    return result
