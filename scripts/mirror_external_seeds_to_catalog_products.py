#!/usr/bin/env python3
"""
Mirror active external_product_seeds into catalog_products.

This is intentionally a narrow bridge for the canonical PDP migration:
  - source table: external_product_seeds
  - destination: catalog_products
  - identity tuple: (merchant_id='external_seed',
                     platform='external_seed',
                     source_product_id=external_product_id)

It is idempotent. Dry-run is the default; pass --apply to insert missing
catalog_products rows. Existing catalog_products rows are not overwritten.
New mirror rows mint their deterministic sig_* at insert time, but public
serving still depends on the downstream quality / identity / offer gates.

Attached seeds are included. Those rows already represent review-gated product
identity edges, and PDP offer fusion needs the mirrored catalog_product/offer
chain behind the external_seed group member.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.beauty_external_ranking import (
    normalize_external_seed_structured_ingredient_ids,
)
from services.catalog_identity import make_content_key
from services.catalog_sync_service import make_pivota_canonical_fields
from services.pdp_category_classifier import (
    fold_category_from_variants,
    resolve_path_from_row,
)
from services.pdp_lifecycle import compute_lifecycle_stage
from services.pdp_taxonomy import derive_taxonomy_v1
from services.text_normalization.brand_case import proper_case_brand


MERCHANT_ID = "external_seed"
MERCHANT_NAME = "External Seed"
PLATFORM = "external_seed"
CATALOG_TRACK = "external_referral"
TRUTH_TIER = "observed"
READINESS_TIER = "referral_only"
SOURCE_SYSTEM = "external_product_seeds_mirror_v1"
CATEGORY_CONFIDENCE_REGEX_AT_MIRROR = 0.85
CATEGORY_LABEL_SOURCE_AT_MIRROR = "regex_backfill_at_mirror"

# Phase 7d: Path B writes the full canonical chain (skus + offers +
# merchants), matching Phase 7a Path C. Without this, JOIN catalog_offers
# returns 0 rows for mirrored products → canonical recall surfaces them
# without prices. The chain shapes here mirror the agent ingest helpers
# in services/catalog_enrichment_agent/ingestion.py.
SKU_SUFFIX = "::canonical"
OFFER_ID_PREFIX = "offer:external_seed:"
OFFER_MODE = "redirect"
PRICE_CONFIDENCE_AT_MIRROR = Decimal("0.6")


def _derive_mirror_sku_key(product_key: str) -> str:
    """One canonical SKU per mirrored product. Keeps the chain
    semantically identical to Path C (`<product_key>::canonical`)."""
    return f"{product_key}{SKU_SUFFIX}"


def _derive_mirror_offer_id(product_key: str) -> str:
    """Deterministic offer id keyed off product_key. Hashed so the
    column-length limit (catalog_offers.offer_id is VARCHAR(64) per
    schema) is safe even for long external_product_id values."""
    digest = hashlib.sha256(product_key.encode("utf-8")).hexdigest()[:32]
    return f"{OFFER_ID_PREFIX}{digest}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Insert missing catalog_products rows. Default is dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit rows inserted / previewed (0 = all missing rows).",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=20,
        help="Number of sample rows to include in the report.",
    )
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def resolve_mirror_category_metadata(
    *,
    category: Optional[str],
    product_type: Optional[str],
    title: Optional[str],
    variants: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Classify only newly inserted mirror rows.

    Existing catalog_products rows are intentionally not overwritten by the
    mirror; this helper is used only on the INSERT path so future mirrors do not
    recreate NULL category_path rows.

    Phase O-5: when product-level fields don't match a pattern, fall back to
    variant-level fields via fold_category_from_variants. Hit at variant level
    sets category_label_source='variant_aggregate' (confidence 0.85) instead
    of the regex_backfill_at_mirror tag.
    """
    folded = fold_category_from_variants(
        category=category, product_type=product_type, title=title, variants=variants,
    )
    if folded is None:
        return {
            "category_path": None,
            "category_confidence": None,
            "category_label_source": None,
            "category_label": None,
        }
    (label, path), source, confidence = folded
    # Preserve the historical mirror-specific source tag when the product-level
    # fields matched, so existing telemetry (e.g. category_label_source =
    # 'regex_backfill_at_mirror' dashboards) keeps working. Variant fallbacks
    # use the new 'variant_aggregate' tag so they can be distinguished.
    if source == "merchant_payload":
        label_source = CATEGORY_LABEL_SOURCE_AT_MIRROR
        label_confidence = CATEGORY_CONFIDENCE_REGEX_AT_MIRROR
    else:
        label_source = source
        label_confidence = confidence
    return {
        "category_path": path,
        "category_confidence": label_confidence,
        "category_label_source": label_source,
        "category_label": label,
    }


# Phase O-1 followup — common JSONB paths that scrapers / agents put
# tag-like data in, ordered most-trusted → least-trusted. The mirror
# returns the FIRST non-empty list it finds. Keeps "scraper schema
# drift" contained: any new scraper that uses a known path is
# automatically supported; a new path requires one extension here.
_SEED_DATA_TAG_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("derived", "recall", "tags"),  # Pivota-derived recall doc
    ("snapshot", "tags"),            # Shopify-style scraped snapshot
    ("product", "tags"),             # generic scraper "product" wrapper
    ("tags",),                        # top-level
)

# Phase O-5 — same pattern as _SEED_DATA_TAG_PATHS but for variant arrays.
# Returns the first non-empty list found. Used to feed
# fold_category_from_variants on the mirror INSERT path so variant-level
# category signals reach the canonical row even when product-level fields
# don't carry them.
_SEED_DATA_VARIANT_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("snapshot", "variants"),
    ("product", "variants"),
    ("variants",),
)


# Top-level seed_data keys carrying force-fill PDP content. These are
# surfaced flat onto product_payload so the PIVOTA-Agent PDP composer
# can find them at the shape it already reads (top-level on product),
# rather than buried under product_payload.seed_data where the read path
# doesn't look.
_SEED_DATA_RICH_CONTENT_KEYS = (
    "pdp_description_raw",
    "pdp_details_sections",
    "pdp_how_to_use_raw",
    "pdp_faq_items",
    "pdp_ingredients_raw",
    "pdp_review_summary",
    "ingredient_intel",
    "key_ingredients",
    "inci_list",
    "ingredient_tokens",
    "commerce_facts_v1",
    "pdp_force_fill_v1",
    "pdp_field_capture_status",
    "pdp_field_quality_summary",
    "bundle_components",
    "bundle_component_refs",
    "review_summary",
    "shade_detail_label",
    "variant_detail_label",
    "product_kind",
)


def _extract_rich_content_for_payload(seed_data: Any) -> Dict[str, Any]:
    """Project the force-filled PDP content from seed_data into a flat
    dict mirrored at the top of product_payload.

    These keys are already populated by upstream force-fill on 3,500+
    of the 4,578 external_seed rows; the previous mirror discarded them
    by writing only description/brand/product_type/category. The PDP
    composer reads top-level shape — nesting under seed_data hid them.
    """
    if not isinstance(seed_data, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in _SEED_DATA_RICH_CONTENT_KEYS:
        if key in seed_data and seed_data[key] is not None:
            out[key] = seed_data[key]
    return out


def _extract_variants_from_seed_data(seed_data: Any) -> List[Any]:
    if not isinstance(seed_data, dict):
        return []
    for path in _SEED_DATA_VARIANT_PATHS:
        node: Any = seed_data
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
            if node is None:
                break
        if isinstance(node, list) and node:
            return node
    return []


def _extract_tags_from_seed_data(seed_data: Any) -> List[str]:
    """Walk seed_data JSONB for tag-like content. Returns [] if none found
    (the same semantics ingest_standard_products uses on Path A: empty list
    means "we looked and found nothing", not "field doesn't exist").
    Always returns deduped, stripped, non-empty strings."""
    if not isinstance(seed_data, dict):
        return []
    for path in _SEED_DATA_TAG_PATHS:
        node: Any = seed_data
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
            if node is None:
                break
        if node is None:
            continue
        # Accept either a list or a comma-separated string (some Shopify
        # snapshots flatten tags to a single string).
        if isinstance(node, list):
            values = node
        elif isinstance(node, str):
            values = node.split(",")
        else:
            continue
        out: List[str] = []
        for item in values:
            if isinstance(item, dict):
                candidate = str(item.get("name") or item.get("label") or "").strip()
            else:
                candidate = str(item or "").strip()
            if candidate and candidate not in out:
                out.append(candidate)
        if out:
            return out
    return []


async def _ensure_external_seed_merchant() -> None:
    """Phase 7d: idempotent UPSERT of the singleton 'external_seed'
    merchant row. Path B has all mirrored products under one synthetic
    merchant — this runs once per --apply invocation so catalog_offers
    foreign keys resolve cleanly. Cheap; safe to call repeatedly."""
    await database.execute(
        """
        INSERT INTO catalog_merchants
          (merchant_id, merchant_name, primary_platform, status,
           source_system, source_ref, metadata_json)
        VALUES
          (:merchant_id, :merchant_name, :primary_platform, 'active',
           :source_system, :source_ref, CAST(:metadata_json AS jsonb))
        ON CONFLICT (merchant_id) DO UPDATE SET
          merchant_name = COALESCE(EXCLUDED.merchant_name, catalog_merchants.merchant_name),
          primary_platform = COALESCE(EXCLUDED.primary_platform, catalog_merchants.primary_platform),
          status = 'active',
          source_system = EXCLUDED.source_system,
          metadata_json = EXCLUDED.metadata_json,
          updated_at = NOW()
        """,
        {
            "merchant_id": MERCHANT_ID,
            "merchant_name": MERCHANT_NAME,
            "primary_platform": PLATFORM,
            "source_system": SOURCE_SYSTEM,
            "source_ref": None,
            "metadata_json": json.dumps(
                {
                    "synthetic": True,
                    "purpose": "Aggregator merchant for external-seed mirrored products",
                    "owner_phase": "7d",
                },
                ensure_ascii=False,
            ),
        },
    )


async def _upsert_canonical_sku_for_mirror_row(
    product_key: str,
    row_dict: Dict[str, Any],
) -> None:
    """Phase 7d: write the canonical SKU row for a mirrored product.
    Matches Path C convention — one synthetic 'canonical' SKU per
    product. The actual variant-level data lives only in seed_data on
    external_product_seeds; we don't expand variants here because the
    redirect-flow (offer_mode='redirect') hands the user off to the
    merchant's storefront for selection."""
    sku_key = _derive_mirror_sku_key(product_key)
    # Propagate the seed's ingredient evidence into the canonical SKU. Seeds carry structured ingredient ids
    # (seed_data.reviewed_ingredient_ids / canonical_ingredient_ids / platform_metadata) and/or a title we can
    # infer from; previously this was hardcoded to [] which dropped all ingredient data at migration and made
    # ingredient-constrained beauty search (e.g. "vitamin c serum") return nothing. (#1659)
    seed_data_for_ingredients = row_dict.get("seed_data")
    if not isinstance(seed_data_for_ingredients, dict):
        seed_data_for_ingredients = {}
    mirror_ingredient_ids = normalize_external_seed_structured_ingredient_ids(
        row_dict, seed_data_for_ingredients
    )
    sku_payload = {
        "synthetic_canonical_variant": True,
        "source": "external_product_seeds_mirror_v1",
        "external_product_id": row_dict.get("external_product_id"),
        "destination_url": row_dict.get("destination_url"),
    }
    await database.execute(
        """
        INSERT INTO catalog_skus
          (sku_key, product_key, merchant_id, platform,
           source_product_id, source_variant_id, sku, barcode,
           title, currency, image_url,
           visible_attributes, visible_option_labels, ingredient_ids,
           sku_payload, readiness_tier)
        VALUES
          (:sku_key, :product_key, :merchant_id, :platform,
           :source_product_id, :source_variant_id, :sku, :barcode,
           :title, :currency, :image_url,
           CAST(:visible_attributes AS jsonb),
           CAST(:visible_option_labels AS jsonb),
           CAST(:ingredient_ids AS jsonb),
           CAST(:sku_payload AS jsonb), :readiness_tier)
        ON CONFLICT (sku_key) DO UPDATE SET
          title = EXCLUDED.title,
          image_url = EXCLUDED.image_url,
          currency = EXCLUDED.currency,
          ingredient_ids = EXCLUDED.ingredient_ids,
          sku_payload = EXCLUDED.sku_payload,
          readiness_tier = EXCLUDED.readiness_tier,
          updated_at = NOW()
        """,
        {
            "sku_key": sku_key,
            "product_key": product_key,
            "merchant_id": MERCHANT_ID,
            "platform": PLATFORM,
            "source_product_id": row_dict.get("external_product_id"),
            # Phase 7d fix: the catalog_skus unique index
            # `idx_catalog_skus_source_identity` is on
            # (merchant_id, platform, source_variant_id) — only 3
            # columns. A literal 'canonical' here makes every Path B
            # sku collide with the first inserted one. Match Path C
            # agent's convention (product_key as source_variant_id;
            # sku_key = source_variant_id + '::canonical') so each
            # product has a distinct identity tuple.
            "source_variant_id": product_key,
            "sku": row_dict.get("external_product_id"),
            "barcode": None,
            "title": row_dict.get("title"),
            "currency": row_dict.get("price_currency") or "USD",
            "image_url": row_dict.get("image_url"),
            "visible_attributes": json.dumps({}, ensure_ascii=False),
            "visible_option_labels": json.dumps([], ensure_ascii=False),
            "ingredient_ids": json.dumps(mirror_ingredient_ids, ensure_ascii=False),
            "sku_payload": json.dumps(sku_payload, ensure_ascii=False, default=_json_default),
            "readiness_tier": READINESS_TIER,
        },
    )


async def _upsert_canonical_offer_for_mirror_row(
    product_key: str,
    row_dict: Dict[str, Any],
) -> None:
    """Phase 7d: write the canonical offer row carrying price + currency
    + availability. This is the field that fixes the "all prices zero"
    bug observed in chat-mode canonical_chain results — once Path B
    rows have offers, both `_fetch_canonical_search_rows` (this backend)
    and PIVOTA-Agent's canonicalCatalogSearch helper can JOIN them and
    emit a price.

    `price_amount` from external_product_seeds is mapped 1:1 to all
    three pricing columns (no spread between list/effective/best on
    this path — the seed has only the displayed retailer price)."""
    sku_key = _derive_mirror_sku_key(product_key)
    offer_id = _derive_mirror_offer_id(product_key)
    raw_price = row_dict.get("price_amount")
    try:
        list_price_value = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        list_price_value = None
    offer_payload = {
        "source": "external_product_seeds_mirror_v1",
        "destination_url": row_dict.get("destination_url"),
        "canonical_url": row_dict.get("canonical_url"),
        "domain": row_dict.get("domain"),
        "external_seed_id": row_dict.get("id"),
        "market": row_dict.get("market"),
    }
    await database.execute(
        """
        INSERT INTO catalog_offers
          (offer_id, sku_key, product_key, merchant_id,
           catalog_track, truth_tier, readiness_tier, offer_mode,
           channel, availability, inventory_quantity, currency,
           list_price, merchant_effective_price, estimated_best_price,
           price_confidence, source_system, source_ref, offer_payload)
        VALUES
          (:offer_id, :sku_key, :product_key, :merchant_id,
           :catalog_track, :truth_tier, :readiness_tier, :offer_mode,
           :channel, :availability, :inventory_quantity, :currency,
           :list_price, :merchant_effective_price, :estimated_best_price,
           :price_confidence, :source_system, :source_ref,
           CAST(:offer_payload AS jsonb))
        ON CONFLICT (offer_id) DO UPDATE SET
          availability = EXCLUDED.availability,
          inventory_quantity = EXCLUDED.inventory_quantity,
          currency = EXCLUDED.currency,
          list_price = EXCLUDED.list_price,
          merchant_effective_price = EXCLUDED.merchant_effective_price,
          estimated_best_price = EXCLUDED.estimated_best_price,
          price_confidence = EXCLUDED.price_confidence,
          offer_payload = EXCLUDED.offer_payload,
          updated_at = NOW()
        """,
        {
            "offer_id": offer_id,
            "sku_key": sku_key,
            "product_key": product_key,
            "merchant_id": MERCHANT_ID,
            "catalog_track": CATALOG_TRACK,
            "truth_tier": TRUTH_TIER,
            "readiness_tier": READINESS_TIER,
            "offer_mode": OFFER_MODE,
            "channel": "external_referral",
            "availability": row_dict.get("availability"),
            "inventory_quantity": None,
            "currency": row_dict.get("price_currency") or "USD",
            "list_price": list_price_value,
            "merchant_effective_price": list_price_value,
            "estimated_best_price": list_price_value,
            "price_confidence": (
                str(PRICE_CONFIDENCE_AT_MIRROR) if list_price_value is not None else None
            ),
            "source_system": SOURCE_SYSTEM,
            "source_ref": row_dict.get("id"),
            "offer_payload": json.dumps(offer_payload, ensure_ascii=False, default=_json_default),
        },
    )


def _compute_mirror_lifecycle_stage(
    row_dict: Dict[str, Any],
    category_meta: Dict[str, Any],
    seed_tags: List[str],
    taxonomy: Dict[str, Any],
) -> str:
    """Build the stage_input dict matching Path B's row shape and
    delegate to compute_lifecycle_stage. Mirror rows never set
    pdp_scope here, so this path tops out at validated."""
    stage_input = {
        "title": row_dict.get("title"),
        "description": row_dict.get("mirrored_description"),
        "image_url": row_dict.get("image_url"),
        "category_path": category_meta.get("category_path"),
        "tags": seed_tags,
        "demographic": taxonomy.get("demographic"),
        "use_case_tags": taxonomy.get("use_case_tags"),
        "lifestyle_tags": taxonomy.get("lifestyle_tags"),
        "pdp_scope": None,
        "source_system": SOURCE_SYSTEM,
    }
    return compute_lifecycle_stage(stage_input)


async def _table_exists(name: str) -> bool:
    row = await database.fetch_one(
        "SELECT to_regclass(:table_name) AS regclass",
        {"table_name": f"public.{name}"},
    )
    return bool(row and row["regclass"])


async def _required_schema() -> Dict[str, Any]:
    required_tables = ["external_product_seeds", "catalog_products"]
    table_status = {table: await _table_exists(table) for table in required_tables}

    index_rows = await database.fetch_all(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = 'catalog_products'
          AND indexdef ILIKE '%merchant_id%'
          AND indexdef ILIKE '%platform%'
          AND indexdef ILIKE '%source_product_id%'
          AND indexdef ILIKE '%UNIQUE%'
        ORDER BY indexname
        """
    )
    identity_unique_indexes = [dict(row) for row in index_rows]
    return {
        "tables": table_status,
        "identity_unique_indexes": identity_unique_indexes,
        "ok": all(table_status.values()) and bool(identity_unique_indexes),
    }


COMMON_CTES = """
WITH active_all AS (
  SELECT *
  FROM external_product_seeds
  WHERE lower(coalesce(status, '')) = 'active'
),
active_standalone AS (
  SELECT *
  FROM active_all
  WHERE coalesce(attached_product_key, '') = ''
),
active_attached AS (
  SELECT *
  FROM active_all
  WHERE coalesce(attached_product_key, '') <> ''
),
active_mirrorable AS (
  SELECT *
  FROM active_all
),
ranked AS (
  SELECT
    eps.*,
    row_number() OVER (
      PARTITION BY eps.external_product_id
      ORDER BY
        CASE WHEN eps.market = 'US' THEN 0 ELSE 1 END,
        eps.updated_at DESC NULLS LAST,
        eps.created_at DESC NULLS LAST,
        eps.id ASC
    ) AS rn,
    count(*) OVER (PARTITION BY eps.external_product_id) AS duplicate_count,
    nullif(
      btrim(
        coalesce(
          eps.seed_data#>>'{snapshot,description}',
          eps.seed_data#>>'{snapshot,description_text}',
          eps.seed_data#>>'{snapshot,pdp_description}',
          eps.seed_data#>>'{snapshot,pdp_description_raw}',
          eps.seed_data#>>'{snapshot,overview}',
          eps.seed_data#>>'{snapshot,summary}',
          eps.seed_data->>'description',
          eps.seed_data->>'description_text',
          eps.seed_data->>'pdp_description',
          eps.seed_data->>'pdp_description_raw',
          eps.seed_data->>'overview',
          eps.seed_data->>'summary',
          ''
        )
      ),
      ''
    ) AS mirrored_description,
    nullif(
      btrim(
        coalesce(
          eps.seed_data#>>'{snapshot,brand}',
          eps.seed_data->>'brand',
          eps.seed_data#>>'{snapshot,vendor}',
          eps.seed_data->>'vendor',
          ''
        )
      ),
      ''
    ) AS mirrored_brand,
    nullif(
      btrim(
        coalesce(
          eps.seed_data#>>'{snapshot,product_type}',
          eps.seed_data->>'product_type',
          eps.seed_data#>>'{snapshot,kind}',
          eps.seed_data->>'kind',
          ''
        )
      ),
      ''
    ) AS mirrored_product_type,
    nullif(
      btrim(
        coalesce(
          eps.seed_data#>>'{snapshot,category}',
          eps.seed_data->>'category',
          eps.seed_data#>>'{recall_doc,recall_category}',
          eps.seed_data#>>'{derived,recall_category}',
          ''
        )
      ),
      ''
    ) AS mirrored_category
  FROM active_mirrorable eps
  WHERE nullif(btrim(coalesce(eps.external_product_id, '')), '') IS NOT NULL
),
candidates AS (
  SELECT *
  FROM ranked
  WHERE rn = 1
    AND length(external_product_id) <= 128
    AND length('prod::external_seed::external_seed::' || external_product_id) <= 255
    AND nullif(btrim(coalesce(title, '')), '') IS NOT NULL
),
missing AS (
  SELECT c.*
  FROM candidates c
  LEFT JOIN catalog_products cp
    ON cp.merchant_id = 'external_seed'
   AND cp.platform = 'external_seed'
   AND cp.source_product_id = c.external_product_id
  WHERE cp.product_key IS NULL
)
"""


async def _fetch_scalar(sql: str, values: Optional[Dict[str, Any]] = None) -> int:
    value = await database.fetch_val(sql, values or {})
    return int(value or 0)


async def _build_report(*, sample_limit: int, limit: int, apply: bool) -> Dict[str, Any]:
    schema = await _required_schema()
    if not schema["ok"]:
        return {
            "ok": False,
            "apply": apply,
            "schema": schema,
            "error": "required tables or catalog_products identity unique index missing",
        }

    totals_sql = (
        COMMON_CTES
        + """
        SELECT
          (SELECT count(*) FROM external_product_seeds) AS external_total,
          (SELECT count(*) FROM external_product_seeds WHERE lower(coalesce(status, '')) = 'active') AS external_active,
          (SELECT count(*) FROM active_all) AS active_all,
          (SELECT count(*) FROM active_standalone) AS active_standalone,
          (SELECT count(*) FROM active_attached) AS active_attached,
          (SELECT count(*) FROM active_standalone WHERE nullif(btrim(coalesce(external_product_id, '')), '') IS NULL) AS active_standalone_missing_external_product_id,
          (SELECT count(*) FROM active_attached WHERE nullif(btrim(coalesce(external_product_id, '')), '') IS NULL) AS active_attached_missing_external_product_id,
          (SELECT count(*) FROM ranked WHERE duplicate_count > 1) AS duplicate_active_mirrorable_rows,
          (SELECT count(*) FROM (SELECT external_product_id FROM ranked GROUP BY external_product_id HAVING count(*) > 1) d) AS duplicate_active_mirrorable_groups,
          (SELECT count(*) FROM ranked WHERE rn = 1 AND length(external_product_id) > 128) AS skipped_source_product_id_too_long,
          (SELECT count(*) FROM ranked WHERE rn = 1 AND length('prod::external_seed::external_seed::' || external_product_id) > 255) AS skipped_product_key_too_long,
          (SELECT count(*) FROM ranked WHERE rn = 1 AND nullif(btrim(coalesce(title, '')), '') IS NULL) AS skipped_missing_title,
          (SELECT count(*) FROM candidates) AS deduped_valid_candidates,
          (SELECT count(*) FROM candidates WHERE nullif(btrim(coalesce(image_url, '')), '') IS NOT NULL) AS candidates_with_image,
          (SELECT count(*) FROM candidates WHERE length(coalesce(mirrored_description, '')) >= 50) AS candidates_with_description_50,
          (SELECT count(*) FROM candidates WHERE nullif(btrim(coalesce(image_url, '')), '') IS NOT NULL AND length(coalesce(mirrored_description, '')) >= 50) AS candidates_visible_quality_ready,
          (SELECT count(*) FROM missing) AS missing_catalog_products,
          (SELECT count(*) FROM catalog_products) AS catalog_products_total,
          (SELECT count(*) FROM catalog_products WHERE pivota_signature_id IS NOT NULL) AS catalog_products_with_sig,
          (SELECT count(*) FROM catalog_products WHERE merchant_id = 'external_seed' AND platform = 'external_seed') AS catalog_products_external_seed,
          (SELECT count(*) FROM catalog_products WHERE merchant_id = 'external_seed' AND platform = 'external_seed' AND pivota_signature_id IS NOT NULL) AS catalog_products_external_seed_with_sig,
          (SELECT count(*) FROM catalog_products WHERE merchant_id = 'external_seed' AND platform = 'external_seed' AND coalesce(source_system, '') <> 'external_product_seeds_mirror_v1') AS legacy_external_seed_catalog_rows,
          (SELECT count(*) FROM catalog_products WHERE pivota_signature_id IS NOT NULL AND image_url IS NOT NULL AND length(coalesce(image_url, '')) > 0 AND length(coalesce(description, '')) >= 50) AS catalog_products_visible_quality_with_sig
        """
    )
    totals_row = await database.fetch_one(totals_sql)
    totals = dict(totals_row or {})

    sample_values = {"sample_limit": max(0, sample_limit)}
    missing_sample_rows = await database.fetch_all(
        COMMON_CTES
        + """
        SELECT
          id,
          external_product_id,
          market,
          tool,
          domain,
          attached_product_key,
          title,
          destination_url,
          image_url,
          length(coalesce(mirrored_description, '')) AS description_length,
          duplicate_count
        FROM missing
        ORDER BY updated_at DESC NULLS LAST, id ASC
        LIMIT :sample_limit
        """,
        sample_values,
    )

    duplicate_sample_rows = await database.fetch_all(
        COMMON_CTES
        + """
        SELECT
          external_product_id,
          count(*) AS rows,
          array_agg(id ORDER BY updated_at DESC NULLS LAST, id ASC) AS seed_ids
        FROM ranked
        GROUP BY external_product_id
        HAVING count(*) > 1
        ORDER BY rows DESC, external_product_id ASC
        LIMIT :sample_limit
        """,
        sample_values,
    )

    by_market_rows = await database.fetch_all(
        COMMON_CTES
        + """
        SELECT market, count(*) AS rows
        FROM candidates
        GROUP BY market
        ORDER BY rows DESC, market ASC
        LIMIT :sample_limit
        """,
        sample_values,
    )

    by_domain_rows = await database.fetch_all(
        COMMON_CTES
        + """
        SELECT coalesce(nullif(lower(domain), ''), 'unknown') AS domain, count(*) AS rows
        FROM candidates
        GROUP BY 1
        ORDER BY rows DESC, domain ASC
        LIMIT :sample_limit
        """,
        sample_values,
    )

    report: Dict[str, Any] = {
        "ok": True,
        "apply": apply,
        "limit": limit,
        "schema": schema,
        "totals": totals,
        "missing_sample": [dict(row) for row in missing_sample_rows],
        "duplicate_sample": [dict(row) for row in duplicate_sample_rows],
        "candidate_by_market": [dict(row) for row in by_market_rows],
        "candidate_by_domain": [dict(row) for row in by_domain_rows],
    }
    return report


async def _apply(limit: int) -> int:
    # Phase 7d: Path B writes the full canonical chain. The merchant
    # row is a single synthetic 'external_seed' aggregator — upsert it
    # once before the per-row loop so catalog_offers FKs resolve.
    await _ensure_external_seed_merchant()

    limit_clause = ""
    values: Dict[str, Any] = {}
    if limit > 0:
        limit_clause = "LIMIT :limit"
        values["limit"] = limit

    rows = await database.fetch_all(
        COMMON_CTES
        + f"""
        SELECT
          'prod::external_seed::external_seed::' || external_product_id AS product_key,
          id,
          external_product_id,
          attached_product_key,
          market,
          tool,
          domain,
          title,
          destination_url,
          canonical_url,
          price_amount,
          price_currency,
          availability,
          image_url,
          seed_data,
          updated_at,
          duplicate_count,
          rn,
          mirrored_description,
          mirrored_brand,
          mirrored_product_type,
          mirrored_category
        FROM missing
        ORDER BY updated_at DESC NULLS LAST, id ASC
        {limit_clause}
        """,
        values,
    )
    inserted = 0
    for row in rows or []:
        row_dict = dict(row)
        mirrored_at = datetime.now(timezone.utc).isoformat()
        pivota_fields = make_pivota_canonical_fields(
            MERCHANT_ID,
            PLATFORM,
            str(row_dict.get("external_product_id") or ""),
        )
        category_meta = resolve_mirror_category_metadata(
            category=row_dict.get("mirrored_category"),
            product_type=row_dict.get("mirrored_product_type"),
            title=row_dict.get("title"),
            variants=_extract_variants_from_seed_data(row_dict.get("seed_data")),
        )
        # Phase O-1 followup: extract tags from seed_data so external
        # crawl data flows into the canonical tags column. Always writes
        # a list (possibly empty) to keep semantics consistent with
        # ingest_standard_products on Path A: empty = "we looked, no tags
        # found", NULL = "row predates the column".
        seed_tags = _extract_tags_from_seed_data(row_dict.get("seed_data"))
        # Phase O-2: derived taxonomy v1 for external-seed mirror rows.
        # Same pure-function call as Path A. price comes from the seed
        # row's price_amount; title + description + tags drive the
        # keyword extractors. mig 076.
        try:
            seed_price_value = float(row_dict.get("price_amount")) if row_dict.get("price_amount") is not None else None
        except (TypeError, ValueError):
            seed_price_value = None
        taxonomy = derive_taxonomy_v1(
            price=seed_price_value,
            title=row_dict.get("title"),
            description=row_dict.get("mirrored_description"),
            tags=seed_tags,
        )
        # Phase O-4: compute lifecycle stage. Mirror rows default to
        # pdp_scope=NULL (no scope assignment in this script — the
        # catalog_products row has unverified by Phase 6 default), so
        # the row promotes to candidate / validated only when content
        # + taxonomy + category_path are present, never to published
        # via this path (would need governance-side multi-merchant
        # promotion). mig 077.
        mirror_lifecycle_stage = _compute_mirror_lifecycle_stage(
            row_dict, category_meta, seed_tags, taxonomy
        )
        mirrored_brand_display = proper_case_brand(row_dict.get("mirrored_brand"))
        product_payload = {
            "external_seed": {
                "id": row_dict.get("id"),
                "external_product_id": row_dict.get("external_product_id"),
                "market": row_dict.get("market"),
                "tool": row_dict.get("tool"),
                "domain": row_dict.get("domain"),
                "destination_url": row_dict.get("destination_url"),
                "canonical_url": row_dict.get("canonical_url"),
                "price_amount": row_dict.get("price_amount"),
                "price_currency": row_dict.get("price_currency"),
                "availability": row_dict.get("availability"),
                "updated_at": row_dict.get("updated_at"),
            },
            "seed_data": row_dict.get("seed_data"),
            "mirror_meta": {
                "source_system": SOURCE_SYSTEM,
                "mirrored_at": mirrored_at,
                "duplicate_count": row_dict.get("duplicate_count"),
                "selection_rank": row_dict.get("rn"),
            },
            # Structured brand object so the PIVOTA-Agent PDP composer's
            # resolveProductBrandLabel picks up `product.brand.name`
            # without falling back to the bare `brand` column (which the
            # ingest path may still store lowercase for older rows).
            **(
                {"brand": {"name": mirrored_brand_display}}
                if mirrored_brand_display
                else {}
            ),
            # Flatten the force-filled PDP fields up to the top level so
            # the read path finds them without traversing seed_data.
            **_extract_rich_content_for_payload(row_dict.get("seed_data")),
        }
        freshness_json = {
            "mirrored_from": "external_product_seeds",
            "source_seed_id": row_dict.get("id"),
            "source_updated_at": row_dict.get("updated_at"),
            "mirrored_at": mirrored_at,
        }
        inserted_row = await database.fetch_one(
            """
            INSERT INTO catalog_products (
              product_key,
              merchant_id,
              platform,
              source_product_id,
              catalog_track,
              truth_tier,
              readiness_tier,
              source_system,
              source_ref,
              pivota_signature_id,
              pivota_canonical_url,
              pivota_signature_minted_at,
              title,
              description,
              brand,
              product_type,
              category,
              category_path,
              category_confidence,
              category_label_source,
              canonical_url,
              image_url,
              product_payload,
              freshness_json,
              tags,
              price_tier,
              use_case_tags,
              lifestyle_tags,
              demographic,
              pdp_lifecycle_stage,
              content_key,
              created_at,
              updated_at
            )
            VALUES (
              :product_key,
              :merchant_id,
              :platform,
              :source_product_id,
              :catalog_track,
              :truth_tier,
              :readiness_tier,
              :source_system,
              :source_ref,
              :pivota_signature_id,
              :pivota_canonical_url,
              :pivota_signature_minted_at,
              :title,
              :description,
              :brand,
              :product_type,
              :category,
              :category_path,
              :category_confidence,
              :category_label_source,
              :canonical_url,
              :image_url,
              CAST(:product_payload AS jsonb),
              CAST(:freshness_json AS jsonb),
              CAST(:tags AS jsonb),
              :price_tier,
              CAST(:use_case_tags AS jsonb),
              CAST(:lifestyle_tags AS jsonb),
              :demographic,
              :pdp_lifecycle_stage,
              :content_key,
              now(),
              now()
            )
            ON CONFLICT (merchant_id, platform, source_product_id) DO NOTHING
            RETURNING product_key
            """,
            {
                "product_key": row_dict.get("product_key"),
                "merchant_id": MERCHANT_ID,
                "platform": PLATFORM,
                "source_product_id": row_dict.get("external_product_id"),
                "catalog_track": CATALOG_TRACK,
                "truth_tier": TRUTH_TIER,
                "readiness_tier": READINESS_TIER,
                "source_system": SOURCE_SYSTEM,
                "source_ref": row_dict.get("id"),
                "pivota_signature_id": pivota_fields["pivota_signature_id"],
                "pivota_canonical_url": pivota_fields["pivota_canonical_url"],
                "pivota_signature_minted_at": pivota_fields["pivota_signature_minted_at"],
                "title": row_dict.get("title"),
                "description": row_dict.get("mirrored_description"),
                # Display-cased — see services.text_normalization.brand_case.
                # The dedup/identity key still uses the lowercase form via
                # services.catalog_identity.normalize_brand, so this only
                # affects what users see.
                "brand": mirrored_brand_display or row_dict.get("mirrored_brand"),
                "product_type": row_dict.get("mirrored_product_type"),
                "category": row_dict.get("mirrored_category"),
                "category_path": category_meta.get("category_path"),
                "category_confidence": category_meta.get("category_confidence"),
                "category_label_source": category_meta.get("category_label_source"),
                "canonical_url": row_dict.get("destination_url"),
                "image_url": row_dict.get("image_url"),
                "product_payload": json.dumps(product_payload, ensure_ascii=False, default=_json_default),
                "freshness_json": json.dumps(freshness_json, ensure_ascii=False, default=_json_default),
                "tags": json.dumps(seed_tags, ensure_ascii=False),
                "price_tier": taxonomy["price_tier"],
                "use_case_tags": json.dumps(taxonomy["use_case_tags"], ensure_ascii=False),
                "lifestyle_tags": json.dumps(taxonomy["lifestyle_tags"], ensure_ascii=False),
                "demographic": taxonomy["demographic"],
                "pdp_lifecycle_stage": mirror_lifecycle_stage,
                # Stage 1 (mig 083): content-derived identity. Mirror
                # paths typically don't carry a GTIN on the seed, so
                # content_key for external_seed rows is brand+title-
                # only; that's acceptable because Stage 2's auto-grouper
                # tolerates GTIN absence by falling back to brand+title
                # trigram for cross-merchant matching.
                "content_key": make_content_key(
                    row_dict.get("mirrored_brand"),
                    row_dict.get("title"),
                    None,
                ),
            },
        )
        if inserted_row:
            inserted += 1

        # Phase 7d: write canonical sku + offer for this product
        # regardless of whether the catalog_products INSERT was new or
        # a no-op. The chain inserts use ON CONFLICT DO UPDATE so
        # existing rows get healed on every --apply pass — that's how
        # legacy Path B rows (mirrored before 7d) pick up their
        # missing skus/offers without needing a separate backfill
        # script. The product_key is the same identifier on both
        # paths, so chain writes stay attached to the correct PDP.
        product_key_for_chain = row_dict.get("product_key")
        if product_key_for_chain:
            try:
                await _upsert_canonical_sku_for_mirror_row(product_key_for_chain, row_dict)
                await _upsert_canonical_offer_for_mirror_row(product_key_for_chain, row_dict)
            except Exception as exc:
                # Don't break the whole apply if one row's chain fails
                # — log and continue. The next --apply pass will retry
                # via the same idempotent ON CONFLICT path.
                print(
                    f"WARNING: chain write failed for product_key={product_key_for_chain}: {exc!r}",
                    file=sys.stderr,
                )
    return inserted


def _render_markdown(report: Dict[str, Any]) -> str:
    totals = report.get("totals") or {}
    lines = [
        "# External Seeds → Catalog Products Mirror",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- apply: `{report.get('apply')}`",
        f"- limit: `{report.get('limit')}`",
        "",
        "## Totals",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
    ]
    for key in [
        "external_total",
        "external_active",
        "active_all",
        "active_standalone",
        "active_attached",
        "active_standalone_missing_external_product_id",
        "active_attached_missing_external_product_id",
        "duplicate_active_mirrorable_rows",
        "duplicate_active_mirrorable_groups",
        "skipped_source_product_id_too_long",
        "skipped_product_key_too_long",
        "skipped_missing_title",
        "deduped_valid_candidates",
        "candidates_with_image",
        "candidates_with_description_50",
        "candidates_visible_quality_ready",
        "missing_catalog_products",
        "catalog_products_total",
        "catalog_products_with_sig",
        "catalog_products_external_seed",
        "catalog_products_external_seed_with_sig",
        "legacy_external_seed_catalog_rows",
        "catalog_products_visible_quality_with_sig",
        "inserted_catalog_products",
        "post_apply_missing_catalog_products",
        "post_apply_catalog_products_total",
        "post_apply_catalog_products_with_sig",
        "post_apply_catalog_products_external_seed",
        "post_apply_catalog_products_external_seed_with_sig",
        "post_apply_legacy_external_seed_catalog_rows",
        "post_apply_catalog_products_visible_quality_with_sig",
    ]:
        if key in totals:
            lines.append(f"| `{key}` | {totals[key]} |")
    lines.append("")
    lines.append("## Candidate By Market")
    lines.append("")
    lines.append("| Market | Rows |")
    lines.append("| --- | ---: |")
    for row in report.get("candidate_by_market") or []:
        lines.append(f"| {row.get('market') or 'unknown'} | {row.get('rows')} |")
    lines.append("")
    lines.append("## Top Candidate Domains")
    lines.append("")
    lines.append("| Domain | Rows |")
    lines.append("| --- | ---: |")
    for row in report.get("candidate_by_domain") or []:
        lines.append(f"| {row.get('domain') or 'unknown'} | {row.get('rows')} |")
    lines.append("")
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    await database.connect()
    try:
        before = await _build_report(
            sample_limit=args.sample_limit,
            limit=args.limit,
            apply=args.apply,
        )
        if not before.get("ok"):
            return before

        report = before
        if args.apply:
            legacy_rows = int((before.get("totals") or {}).get("legacy_external_seed_catalog_rows") or 0)
            if legacy_rows > 0:
                before.setdefault("warnings", []).append(
                    "legacy external_seed catalog rows exist; continuing because the "
                    "(merchant_id, platform, source_product_id) unique index prevents "
                    "duplicate catalog identities"
                )
            inserted = await _apply(args.limit)
            after = await _build_report(
                sample_limit=args.sample_limit,
                limit=args.limit,
                apply=args.apply,
            )
            before_totals = before.get("totals") or {}
            after_totals = after.get("totals") or {}
            report = after
            report["warnings"] = list(before.get("warnings") or []) + list(after.get("warnings") or [])
            report["before_totals"] = before_totals
            report["totals"] = {
                **before_totals,
                "inserted_catalog_products": inserted,
                "post_apply_missing_catalog_products": after_totals.get("missing_catalog_products"),
                "post_apply_catalog_products_total": after_totals.get("catalog_products_total"),
                "post_apply_catalog_products_with_sig": after_totals.get("catalog_products_with_sig"),
                "post_apply_catalog_products_external_seed": after_totals.get("catalog_products_external_seed"),
                "post_apply_catalog_products_external_seed_with_sig": after_totals.get("catalog_products_external_seed_with_sig"),
                "post_apply_legacy_external_seed_catalog_rows": after_totals.get("legacy_external_seed_catalog_rows"),
                "post_apply_catalog_products_visible_quality_with_sig": after_totals.get("catalog_products_visible_quality_with_sig"),
            }
        return report
    finally:
        await database.disconnect()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_run(args))
    json_blob = json.dumps(report, indent=2, ensure_ascii=False, default=_json_default)
    _write_if_requested(args.output_json, json_blob + "\n")
    _write_if_requested(args.output_md, _render_markdown(report))
    print(json_blob)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
