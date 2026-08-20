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
import json
import os
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
from services.brand_claim_service import normalize_host
from services.catalog_identity import make_content_key
from services.catalog_sync_service import (
    make_catalog_product_key,
    make_pivota_canonical_fields,
)
# Convergence P1.6: the seed→catalog_offers projection (offer id derivation +
# field mapping + upsert) is owned by services.external_offer_dual_write so the
# mirror and the on-demand / reconciliation paths cannot drift. This script's
# _derive_mirror_* / _upsert_canonical_offer_for_mirror_row now delegate there.
from services.external_offer_dual_write import (
    derive_mirror_offer_id as _shared_offer_id,
    derive_mirror_sku_key as _shared_sku_key,
    upsert_catalog_offer_from_seed_row as _shared_upsert_offer,
)
# ADR-009 D2 (docs/adr/ADR-009-seller-of-record-identity.md) + IDENTITY_REFERENCE
# §3 Trap T3: Path B must mint a real per-brand observed seller-of-record at
# ingestion instead of stuffing everything under the banned 'external_seed'
# bucket. See services/seller_identity.py.
from services.seller_identity import (
    BANNED_BUCKET_MERCHANT_ID,
    ensure_observed_seller,
)
from services.external_seed_servability import (
    backlink_seed_to_product,
    build_servable_quality_payload,
    make_external_seed_servable,
)
from services.pdp_category_classifier import (
    fold_category_from_variants,
    resolve_path_from_row,
)
from services.pdp_lifecycle import compute_lifecycle_stage
from services.pdp_taxonomy import derive_taxonomy_v1
from services.text_normalization.brand_case import proper_case_brand
# Fix Plan B: the durable top-level vertical (mig 173) + intake structure
# helpers. The mirror lane historically OMITTED resolved_vertical entirely, so
# ~83% of the catalog had a NULL vertical. Wire it in go-forward here, matching
# the ingest_standard_products signal set exactly (catalog_sync_service.py:1127).
from services.vertical_profiles import (
    DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD,
    is_vertical_unresolved,
    normalize_category,
    resolve_vertical,
    summarize_unresolved_vertical,
)


MERCHANT_ID = "external_seed"
MERCHANT_NAME = "External Seed"
PLATFORM = "external_seed"
CATALOG_TRACK = "external_referral"
TRUTH_TIER = "observed"
READINESS_TIER = "referral_only"
SOURCE_SYSTEM = "external_product_seeds_mirror_v1"
CATEGORY_CONFIDENCE_REGEX_AT_MIRROR = 0.85
CATEGORY_LABEL_SOURCE_AT_MIRROR = "regex_backfill_at_mirror"


def _unresolved_vertical_fail_threshold() -> float:
    """Fix Plan B T3: the intake brake trips when the share of
    structureless (unresolved-vertical) rows exceeds this. Configurable via
    MIRROR_UNRESOLVED_VERTICAL_FAIL_THRESHOLD (fraction, e.g. 0.20); falls back
    to the shared default. An out-of-range / unparseable value falls back too."""
    raw = os.getenv("MIRROR_UNRESOLVED_VERTICAL_FAIL_THRESHOLD")
    if raw is None:
        return DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD
    if 0.0 <= val <= 1.0:
        return val
    return DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD

# Phase 7d: Path B writes the full canonical chain (skus + offers +
# merchants), matching Phase 7a Path C. Without this, JOIN catalog_offers
# returns 0 rows for mirrored products → canonical recall surfaces them
# without prices. The chain shapes here mirror the agent ingest helpers
# in services/catalog_enrichment_agent/ingestion.py.
SKU_SUFFIX = "::canonical"
OFFER_ID_PREFIX = "offer:external_seed:"
OFFER_MODE = "redirect"
PRICE_CONFIDENCE_AT_MIRROR = Decimal("0.6")

# Also produce the serving-layer artifacts (quality snapshot + agent_pdp_view)
# for each mirrored seed so external-seed products land servable, not just
# indexed. The cheap attached_product_key back-link runs unconditionally (it
# fixes a real `no_seed` gap); the heavier quality+APV pass is flag-gated so it
# dark-launches and the scheduled mirror's load/behavior is opt-in.
MAKE_SERVABLE_ENV = "EXTERNAL_SEED_MIRROR_MAKE_SERVABLE"


def _make_servable_enabled() -> bool:
    return str(os.getenv(MAKE_SERVABLE_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def _derive_mirror_sku_key(product_key: str) -> str:
    """One canonical SKU per mirrored product (`<product_key>::canonical`).
    Delegates to the shared projection (services.external_offer_dual_write)."""
    return _shared_sku_key(product_key)


def _derive_mirror_offer_id(product_key: str) -> str:
    """Deterministic offer id keyed off product_key. Delegates to the shared
    projection (services.external_offer_dual_write) so the mirror and the
    on-demand dual-write derive byte-identical offer ids."""
    return _shared_offer_id(product_key)


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


def _extract_source_backed_signals(seed_data: Any) -> Dict[str, Any]:
    """The two source-backed signals the quality scorer's ATTRIBUTES component
    reads, projected out of seed_data at the shape
    ``build_servable_quality_payload`` expects.

    WHY THIS EXISTS. Measured on prod 2026-07-28: this script was the only one of
    the three ``build_servable_quality_payload`` call sites that passed neither
    ``raw_inci`` nor ``pdp_details_sections`` nor ``category``
    (``backfill_external_seed_quality_rescore`` and
    ``onboard_external_brand_from_crawl`` pass them, with comments saying why).
    So every row this ingest path mirrored was scored with an EMPTY attributes
    component and, whenever product_type was null, an empty brand_category one —
    capping it at 4-of-7 (57.1) or 3-of-7 (42.9). Those two scores were 3,456 of
    the 5,114 ``low_quality`` rows in prod, i.e. the backlog was substantially
    manufactured here, one 15-minute run at a time.

    The content was never missing — ``pdp_content_depth`` (which reads the DB)
    passes these same rows while the attributes component (which reads the
    payload) scored 0. This closes that gap at the writer, so the fix is
    "stop discarding what we already fetched", not new extraction.

    INCI source, and why it is NOT ``beauty_sku_ingredients``: the rescore script
    reads INCI from ``bsi.raw_inci``, but that table is populated by the
    ingredient harvester, which runs AFTER mirroring — at this point in the
    pipeline it has no row for this product. seed_data is what exists now, and it
    already carries the crawled ingredient text under the keys force-fill writes
    (both are in ``_SEED_DATA_RICH_CONTENT_KEYS``). A later harvester pass still
    improves the row through the rescore path; this only ensures the row does not
    start life at a manufactured 57.1.

    ``pdp_ingredients_raw`` is preferred over ``inci_list`` because
    ``build_servable_quality_payload`` splits the string on commas to rebuild the
    list, so handing it the raw string is the lossless direction. Both the
    top-level and ``snapshot`` nestings are checked, matching the COALESCE in
    ``backfill_external_seed_quality_rescore.FETCH``.
    """
    if not isinstance(seed_data, dict):
        return {}
    snapshot = seed_data.get("snapshot")
    roots = [seed_data, snapshot if isinstance(snapshot, dict) else {}]

    raw_inci = ""
    for root in roots:
        candidate = root.get("pdp_ingredients_raw")
        if isinstance(candidate, str) and candidate.strip():
            raw_inci = candidate.strip()
            break
        tokens = root.get("inci_list")
        if isinstance(tokens, (list, tuple)):
            joined = ", ".join(
                str(tok).strip() for tok in tokens if str(tok or "").strip()
            )
            if joined:
                raw_inci = joined
                break

    sections: Optional[List[Any]] = None
    for root in roots:
        value = root.get("pdp_details_sections")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                value = None
        if isinstance(value, (list, tuple)) and value:
            sections = list(value)
            break

    out: Dict[str, Any] = {}
    if raw_inci:
        out["raw_inci"] = raw_inci
    if sections:
        out["pdp_details_sections"] = sections
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


def _seed_gtin(seed_data: Any) -> Optional[str]:
    """R3 (ADR-011): best-effort barcode/GTIN read from the crawled seed_data
    snapshot, for the resolve-or-attach primitive — never hardcode None when
    the source carried a barcode. Checks the top level and the first variant
    (Shopify snapshots keep barcodes on variants)."""
    if not isinstance(seed_data, dict):
        return None
    for key in ("gtin", "gtin13", "gtin14", "barcode", "upc", "ean"):
        value = str(seed_data.get(key) or "").strip()
        if value:
            return value
    variants = seed_data.get("variants")
    if isinstance(variants, list) and variants and isinstance(variants[0], dict):
        for key in ("gtin", "barcode", "upc", "ean"):
            value = str(variants[0].get(key) or "").strip()
            if value:
                return value
    return None


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
    merchant_id: str = MERCHANT_ID,
) -> None:
    """Phase 7d: write the canonical SKU row for a mirrored product.

    `merchant_id` defaults to the legacy singleton so the existing repair /
    backfill callers (scripts/repair_external_seed_offer_mainline.py,
    scripts/backfill_canonical_chain_for_path_b_mirror.py) that heal EXISTING
    external_seed rows are unchanged. The forward Path B mirror now passes the
    per-brand observed seller (ADR-009 D2).
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
            "merchant_id": merchant_id,
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
    merchant_id: str = MERCHANT_ID,
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
    await _shared_upsert_offer(product_key, row_dict, merchant_id=merchant_id)


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


# ---------------------------------------------------------------------------
# Shared predicate fragments.
#
# Two SQL chains derive the SAME candidate set below: COMMON_CTES (the full
# report — carries every seed column plus the seed_data-derived text) and
# MISSING_MIRROR_CTES (the cheap "is anything missing?" chain). They MUST agree
# on which seed row wins its external_product_id group and on which winners are
# mirrorable, or the cheap chain reports "no work" while the report says there
# is. These fragments are concatenated into both so a change to the ranking or
# the candidate filters lands in both by construction — do not inline them.
#
# Plain concatenation, never .format()/f-strings: COMMON_CTES contains literal
# JSON paths like '{snapshot,description}' whose braces would be eaten.
# ---------------------------------------------------------------------------

def _active_status(alias: str = "") -> str:
    """The one definition of "this seed is active".

    Deliberately NOT btrimmed and NOT NULL-tolerant beyond `coalesce(..., '')`:
    a NULL or whitespace-padded status is INACTIVE. Both chains and the report's
    own `external_active` metric concatenate this, so the case-folding and the
    coalesce default cannot drift between them — the equivalence tests pin
    NULL, uppercase and padded statuses against it.
    """
    column = f"{alias}.status" if alias else "status"
    return f"lower(coalesce({column}, '')) = 'active'"


# Which seed row represents its external_product_id group: prefer the US market
# row, then the freshest, with `id` as the final deterministic tiebreak. Used as
# the row_number() window ORDER BY in COMMON_CTES and as the DISTINCT ON ORDER BY
# tail in MISSING_MIRROR_CTES; both alias external_product_seeds as `eps`.
# NOTE `eps.market = 'US'` is case-SENSITIVE: a lowercase 'us' row is not
# preferred. Pinned by a fixture row rather than left to inference.
_WINNER_ORDER_BY = """
        CASE WHEN eps.market = 'US' THEN 0 ELSE 1 END,
        eps.updated_at DESC NULLS LAST,
        eps.created_at DESC NULLS LAST,
        eps.id ASC
"""

# A group winner is mirrorable only if its identifiers fit the catalog columns
# (catalog_products.source_product_id is VARCHAR(128), product_key VARCHAR(255))
# and it carries a title (catalog_products.title is NOT NULL). Selected from the
# winner CTE in both chains, so column names are unqualified in both.
_CANDIDATE_FILTERS = """
    length(external_product_id) <= 128
    AND length('prod::external_seed::external_seed::' || external_product_id) <= 255
    AND nullif(btrim(coalesce(title, '')), '') IS NOT NULL
"""


COMMON_CTES = (
    """
WITH active_all AS (
  SELECT *
  FROM external_product_seeds
  WHERE """
    + _active_status()
    + """
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
-- Pre-computed ONCE so the anti-join below is a hash join, not a per-row scan.
--
-- `active_all` is a MATERIALIZED CTE (Postgres materialises any CTE referenced
-- more than once), and a materialised CTE carries NO STATISTICS. The planner
-- therefore estimated it at 63 rows when it holds 11,352, chose a Nested Loop
-- for the correlated `NOT EXISTS` in `missing`, and re-scanned the whole CTE
-- once per candidate. MEASURED on prod: 11,352 loops, 64,508,866 inner
-- iterations, 72.7s of a 76s query. Hoisting the join out makes the estimate
-- irrelevant — it runs once over 11,352 rows instead of 11,352 times.
-- NOT MATERIALIZED, and that keyword is the whole point of the fix holding.
--
-- The report's totals query references this CTE TWICE (`missing` and
-- `candidates_attached_present`), and Postgres materialises any CTE referenced
-- more than once — which would hand `attached_epids` the SAME statistics-free
-- misestimate that made `active_all` explode, one level down. MEASURED at prod
-- row counts (11,352 seeds / 5,683 attached): materialised the totals query
-- plans a Nested Loop Anti Join re-scanning this CTE 5,683 times (~16M
-- tuplestore reads) at 0.49s; NOT MATERIALIZED plans a Merge Anti Join with
-- loops=1 at 0.08s. Inlining also lets the planner use catalog_products'
-- indexes, which a tuplestore has none of.
attached_epids AS NOT MATERIALIZED (
  SELECT DISTINCT a.external_product_id
  FROM active_all a
  JOIN catalog_products cp_attached
    ON cp_attached.product_key = a.attached_product_key
),
ranked AS (
  SELECT
    eps.*,
    row_number() OVER (
      PARTITION BY eps.external_product_id
      ORDER BY"""
    + _WINNER_ORDER_BY
    + """    ) AS rn,
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
    AND"""
    + _CANDIDATE_FILTERS
    + """),
missing AS (
  -- A seed is "missing" only if it has NO mirrored catalog_products row yet,
  -- under EITHER identity: the legacy singleton merchant_id='external_seed'
  -- OR a per-brand observed seller (merch_obs_*, ADR-009 D2). The join is now
  -- keyed on (platform, source_product_id) with NO merchant literal, so a seed
  -- already mirrored under the bucket is treated as present and is NOT
  -- re-mirrored under a fresh observed identity — re-keying existing rows is the
  -- A9-4 parity backfill, explicitly out of scope here (ADR-009 D4).
  --
  -- A seed whose attached_product_key points at an EXISTING catalog_products row
  -- is also present: attached_product_key is the authoritative back-link, and the
  -- catalog_enrichment_agent path mints pdp.source_product_id (title slug) and
  -- seed.external_product_id (brand:hash) in DIFFERENT formats, so the
  -- (platform, source_product_id) join alone can never see that mirror. Without
  -- this conjunct every Path-C ingest spawned a merch_obs_* shadow product on the
  -- next materialization tick (39 COSRX shadows, 2026-07-16).
  --
  -- The attachment check is GROUP-level (NOT EXISTS over active_all by
  -- external_product_id), not just the `ranked` rn=1 winner: duplicate active
  -- seeds sharing an external_product_id can differ in attachment (the mirror's
  -- own post-hoc backlink step can leave mixed groups after partial failures),
  -- and ranking may prefer the unattached duplicate — which would mint the
  -- shadow anyway. If every attached target row is gone, the group becomes
  -- mirrorable again (self-heal preserved).
  SELECT c.*
  FROM candidates c
  LEFT JOIN catalog_products cp
    ON cp.platform = 'external_seed'
   AND cp.source_product_id = c.external_product_id
  LEFT JOIN attached_epids ae
    ON ae.external_product_id = c.external_product_id
  WHERE cp.product_key IS NULL
    AND ae.external_product_id IS NULL
)
"""
)


# ---------------------------------------------------------------------------
# Cheap missing-mirror chain.
#
# Same `missing` set as COMMON_CTES, derived WITHOUT touching seed_data.
#
# COMMON_CTES exists to build the full report, so its `ranked` CTE does
# `SELECT eps.*` plus a dozen `seed_data #>>` extractions per row. That forces
# Postgres to detoast the whole JSONB column and materialize it through a sort:
# external_product_seeds is 12,627 rows whose main heap is only ~25 MB, but its
# TOAST segment is ~207 MB, and none of it is needed to answer "does any active
# seed still lack a catalog mirror?". Measured on production 2026-08-17: the
# report chain never once completed in under 69s (mean ~125s, 2,595 calls over
# 36 days = ~83 hours of database time), while this chain answers the same
# question in ~0.15s off `idx_catalog_products_source_product_id_lookup` and
# `catalog_products_pkey`.
#
# Derivation, mirroring COMMON_CTES step for step:
#   * DISTINCT ON (external_product_id) with _WINNER_ORDER_BY == row_number()
#     OVER (PARTITION BY external_product_id ORDER BY <same>) = 1. Only the
#     winner's `title` is projected, because that is the one per-row column
#     _CANDIDATE_FILTERS reads; the two length checks are group-invariant.
#   * The LEFT JOIN ... WHERE cp.product_key IS NULL anti-join becomes NOT
#     EXISTS. Equivalent because product_key is catalog_products' primary key,
#     so it is NULL exactly when no row matched.
#   * The attached-backlink NOT EXISTS is inlined against external_product_seeds
#     with the `active_all` status predicate carried onto it.
# Checked against the report chain on live production data (2026-08-17): same
# winning seed row for all 11,352 groups, and identical `missing` sets both live
# and with the identity join forced to miss (9 rows, which does exercise the
# attached-backlink anti-join). Do NOT over-trust that run: production currently
# has 0 duplicate external_product_id groups, 0 over-length ids and 0 blank
# titles, so it could not exercise the winner ranking or the candidate filters
# at all. tests/test_missing_mirror_count_equivalence_postgres.py is the real
# proof of those — it constructs the duplicate groups production lacks.
# ---------------------------------------------------------------------------
MISSING_MIRROR_CTES = (
    """
WITH ranked AS (
  SELECT DISTINCT ON (eps.external_product_id)
    eps.external_product_id AS external_product_id,
    eps.title AS title
  FROM external_product_seeds eps
  WHERE """
    + _active_status("eps")
    + """
    AND nullif(btrim(coalesce(eps.external_product_id, '')), '') IS NOT NULL
  ORDER BY
    eps.external_product_id,"""
    + _WINNER_ORDER_BY
    + """),
candidates AS (
  SELECT external_product_id
  FROM ranked
  WHERE"""
    + _CANDIDATE_FILTERS
    + """),
missing AS (
  SELECT c.external_product_id
  FROM candidates c
  WHERE NOT EXISTS (
      SELECT 1
      FROM catalog_products cp
      WHERE cp.platform = 'external_seed'
        AND cp.source_product_id = c.external_product_id
    )
    AND NOT EXISTS (
      SELECT 1
      FROM external_product_seeds a
      JOIN catalog_products cp_attached
        ON cp_attached.product_key = a.attached_product_key
      WHERE """
    + _active_status("a")
    + """
        AND a.external_product_id = c.external_product_id
    )
)
"""
)


# The single spelling of the schema-preflight failure. Both `_build_report` and
# the materialization job (which runs `_required_schema()` on its own rather
# than building a report) surface it, and a caller that hardcoded its own copy
# would drift silently — the job's tests assert against THIS constant.
SCHEMA_REQUIRED_ERROR = (
    "required tables or catalog_products identity unique index missing"
)


async def _fetch_scalar(sql: str, values: Optional[Dict[str, Any]] = None) -> int:
    value = await database.fetch_val(sql, values or {})
    return int(value or 0)


async def count_missing_catalog_mirrors() -> int:
    """How many active seeds still have no catalog mirror.

    Identical to the report's `totals.missing_catalog_products`, but derived
    from MISSING_MIRROR_CTES instead of building the whole report — see that
    constant for why. Callers that only need to know whether there is work to
    do should use this and build the report only when it returns > 0.

    The count is exact rather than bounded by a LIMIT: the DISTINCT ON sort
    over the (narrow) seed rows dominates and cannot be short-circuited, so a
    bound would save nothing measurable — production timings are ~0.15s for the
    exact count vs ~0.11s for LIMIT 1 — while costing operators an honest number
    in the job's log line.
    """
    return await _fetch_scalar(MISSING_MIRROR_CTES + "SELECT count(*) FROM missing")


async def count_external_seed_mirrors_with_signature() -> int:
    """LEGACY-COHORT count: sig_*-carrying rows under the singleton
    merchant_id='external_seed' bucket.

    Read the name literally — this is NOT "mirrored rows with a signature".
    ADR-009 D2 moved mirroring onto per-brand observed sellers (merch_obs_*),
    and MERCHANT_ID is BANNED_BUCKET_MERCHANT_ID, which `_apply` raises on rather
    than write, so this number is structurally frozen: production reads 0 here
    while `platform='external_seed' AND pivota_signature_id IS NOT NULL` across
    all merchants reads 12,298 (measured 2026-08-17).

    Kept exactly as the report's `catalog_products_external_seed_with_sig` total
    spelled it, so the materialization job's summary key keeps its existing
    meaning. Changing it to the platform-wide count would be a behaviour change,
    not a bug fix — decide that separately from the preflight work.
    """
    return await _fetch_scalar(
        """
        SELECT count(*)
        FROM catalog_products
        WHERE merchant_id = :merchant_id
          AND platform = :platform
          AND pivota_signature_id IS NOT NULL
        """,
        {"merchant_id": MERCHANT_ID, "platform": PLATFORM},
    )


async def _build_report(*, sample_limit: int, limit: int, apply: bool) -> Dict[str, Any]:
    schema = await _required_schema()
    if not schema["ok"]:
        return {
            "ok": False,
            "apply": apply,
            "schema": schema,
            "error": SCHEMA_REQUIRED_ERROR,
        }

    totals_sql = (
        COMMON_CTES
        + """
        SELECT
          (SELECT count(*) FROM external_product_seeds) AS external_total,
          (SELECT count(*) FROM external_product_seeds WHERE """
        + _active_status()
        + """) AS external_active,
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
          (SELECT count(*) FROM candidates c
             JOIN attached_epids ae ON ae.external_product_id = c.external_product_id
          ) AS candidates_attached_present,
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


def _seller_domain_for_row(row_dict: Dict[str, Any]) -> str:
    """Best registrable-domain source for the row's seller identity: the seed's
    own `domain`, else the host of its destination_url, else canonical_url. Path
    B seeds always carry a brand site (that is what was crawled), so one of these
    is populated for real supply. Returns '' when none is — the caller then skips
    the row (never buckets it)."""
    for candidate in (
        row_dict.get("domain"),
        row_dict.get("destination_url"),
        row_dict.get("canonical_url"),
    ):
        host = normalize_host(candidate)
        if host:
            return host
    return ""


async def _apply(limit: int) -> Dict[str, Any]:
    """Returns ``{"inserted": int, "vertical_guard": dict}``.

    (Was annotated ``-> int`` while returning this dict — every caller already
    subscripts the result, so the annotation was simply wrong.)
    """
    # ADR-009 D2: Path B no longer stuffs every crawled brand under the singleton
    # 'external_seed' merchant. Each row now resolves-or-mints its own per-brand
    # observed seller (services/seller_identity.ensure_observed_seller), which
    # upserts the catalog_merchants row on demand — so the old
    # _ensure_external_seed_merchant() pre-pass is gone from this write path (the
    # function is retained only for the legacy repair/backfill scripts).
    skipped_no_seller = 0
    skipped_brand_guard = 0  # ADR-008 prevent-at-intake (convergence P1.4)
    # Fix Plan B T3: per-run vertical-structure accounting for the intake brake.
    rows_considered = 0
    rows_unresolved_vertical = 0

    limit_clause = ""
    values: Dict[str, Any] = {}
    if limit > 0:
        limit_clause = "LIMIT :limit"
        values["limit"] = limit

    rows = await database.fetch_all(
        COMMON_CTES
        + f"""
        SELECT
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
        external_product_id = str(row_dict.get("external_product_id") or "")
        # ADR-009 D2: resolve-or-mint this row's per-brand seller-of-record.
        # brand comes from the crawled snapshot (mirrored_brand); domain from the
        # crawled brand site. If we cannot form a real brand+registrable-domain
        # identity we SKIP the row loudly — we never fall back to the banned
        # 'external_seed' bucket (no silent placeholder identity).
        seller_domain = _seller_domain_for_row(row_dict)
        try:
            seller_merchant_id = await ensure_observed_seller(
                brand=row_dict.get("mirrored_brand") or "",
                domain=seller_domain,
                source_system=SOURCE_SYSTEM,
                primary_platform=PLATFORM,
            )
        except ValueError as exc:
            skipped_no_seller += 1
            print(
                f"SKIP: no seller identity for external_product_id="
                f"{external_product_id!r} (brand={row_dict.get('mirrored_brand')!r}, "
                f"domain={seller_domain!r}): {exc}",
                file=sys.stderr,
            )
            continue
        # ADR-009 D2 write-boundary tripwire: the banned bucket must NEVER be the
        # resolved seller-of-record for a new write. Fail loudly (founder
        # no-fallback directive) rather than let a bucketed row slip through.
        if seller_merchant_id == BANNED_BUCKET_MERCHANT_ID:
            raise RuntimeError(
                "ADR-009 D2 violation: refusing to mirror a new catalog row under "
                f"the banned 'external_seed' bucket (external_product_id="
                f"{external_product_id!r})"
            )
        product_key = make_catalog_product_key(
            seller_merchant_id, PLATFORM, external_product_id
        )

        # Stage 1 (mig 083): content-derived identity for the mirrored row.
        # The ADR-011 primitive below may re-align it to an existing entity.
        content_key_value = make_content_key(
            row_dict.get("mirrored_brand"), row_dict.get("title"), None
        )

        from services.intake_identity import (
            ACTION_SKIP as _IDENTITY_SKIP,
            DOOR_EXTERNAL_SEED_MIRROR as _DOOR_MIRROR,
            canonical_gtin as _canonical_gtin,
            intake_identity_enabled as _intake_identity_enabled,
            resolve_or_attach_content_identity as _resolve_or_attach,
        )

        # ADR-011: any crawled barcode rides the gtin match-attribute column,
        # never folded into content_key. Populated regardless of the flag so
        # the match corpus builds ahead of rollout.
        gtin_value = _canonical_gtin(_seed_gtin(row_dict.get("seed_data")))

        if _intake_identity_enabled(_DOOR_MIRROR):
            # ADR-011 resolve-or-attach (flag-gated; composes the ADR-008
            # brand guard, so the legacy standalone guard call below is
            # replaced when ON). Tier-0 exact only; SKIP suppresses the mint
            # (observed-data door) with review enqueued.
            ident = await _resolve_or_attach(
                brand=row_dict.get("mirrored_brand"),
                title=row_dict.get("title"),
                gtin=gtin_value,
                canonical_url=row_dict.get("canonical_url")
                or row_dict.get("destination_url"),
                source_product_id=external_product_id,
                door=_DOOR_MIRROR,
                merchant_ctx={
                    "merchant_id": seller_merchant_id,
                    "platform": PLATFORM,
                    "source_domain": row_dict.get("domain"),
                    "product_key": product_key,
                },
            )
            if ident.get("action") == _IDENTITY_SKIP:
                skipped_brand_guard += 1
                print(
                    "SKIP: ADR-011 identity gate — brand already canonical under "
                    "another merchant "
                    f"(external_product_id={external_product_id!r}); review enqueued"
                )
                continue
            content_key_value = ident.get("content_key") or content_key_value
        else:
            # ADR-008 prevent-at-intake (convergence P1.4): the mirror door now
            # runs the SAME brand-fragmentation guard as the audit door. An
            # observed seed whose brand+host is already canonical under a
            # DIFFERENT merchant must not mint a fragmented orphan — the conflict
            # goes to review instead. Fail-open on guard errors (never blocks a
            # legitimate mirror on the guard's account).
            try:
                from services.audit_index_intake import (
                    apply_intake_brand_fragmentation_guard,
                )

                guard = await apply_intake_brand_fragmentation_guard(
                    seller_merchant_id,
                    {
                        "product_key": product_key,
                        "brand": row_dict.get("mirrored_brand"),
                        "source_domain": row_dict.get("domain"),
                        "canonical_url": row_dict.get("canonical_url"),
                        "content_key": row_dict.get("content_key"),
                    },
                    door="external_seed_mirror",
                    block_on_conflict=True,
                )
                if guard.get("action") == "skip":
                    skipped_brand_guard += 1
                    print(
                        "SKIP: ADR-008 brand guard — brand already canonical under "
                        f"{guard.get('conflict_merchant_id')} "
                        f"(external_product_id={external_product_id!r}); review enqueued"
                    )
                    continue
            except Exception as exc:  # noqa: BLE001 — guard must never break the mirror
                print(f"WARN: brand guard errored for {external_product_id!r}: {exc}")

        mirrored_at = datetime.now(timezone.utc).isoformat()
        pivota_fields = make_pivota_canonical_fields(
            seller_merchant_id,
            PLATFORM,
            external_product_id,
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
        # Fix Plan B T4: case/trim-normalize the free-text category BEFORE it is
        # written (no semantic renames). NULL stays NULL.
        normalized_category = normalize_category(row_dict.get("mirrored_category"))
        # Fix Plan B T1: durable top-level vertical (mig 173). Resolve here on the
        # external-seed mirror lane exactly as ingest_standard_products does at
        # catalog_sync_service.py:1127 — category signals + a title blob that also
        # folds description + tags so a SKU whose vertical only shows in its tags
        # resolves consistently with the report path (which reads this column
        # first). See services.vertical_profiles.
        vertical_signals = {
            "product_type": row_dict.get("mirrored_product_type"),
            "category": normalized_category,
            "category_path": category_meta.get("category_path"),
        }
        resolved_vertical = resolve_vertical(
            vertical_signals,
            title=" ".join(
                str(part)
                for part in (
                    row_dict.get("title"),
                    row_dict.get("mirrored_description"),
                    *(seed_tags or []),
                )
                if part
            ),
        )
        # Fix Plan B T3: count rows that carry no machine-readable structure at
        # all (resolved 'other' AND no category/product_type/category_path). The
        # per-run brake below trips if their share is too high.
        rows_considered += 1
        if is_vertical_unresolved(resolved_vertical, vertical_signals):
            rows_unresolved_vertical += 1
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
              resolved_vertical,
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
              gtin,
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
              :resolved_vertical,
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
              :gtin,
              now(),
              now()
            )
            ON CONFLICT (merchant_id, platform, source_product_id) DO NOTHING
            RETURNING product_key
            """,
            {
                "product_key": product_key,
                "merchant_id": seller_merchant_id,
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
                # T4: case/trim-normalized (no semantic rename).
                "category": normalized_category,
                # T1: durable top-level vertical (mig 173).
                "resolved_vertical": resolved_vertical,
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
                # Stage 1 (mig 083): content-derived identity, computed above
                # (and possibly re-aligned to an existing entity by the
                # ADR-011 resolve-or-attach primitive when its flag is ON).
                "content_key": content_key_value,
                # ADR-011: GTIN match-attribute (canonicalized), never in the key.
                "gtin": gtin_value,
            },
        )
        if inserted_row:
            inserted += 1

        # ADR-009 ratified decision 1 (no-fallback): stamp the deterministic
        # SINGLETON product_group_id for the mirrored product (and the
        # crawl-onboard path, which delegates to this `_apply`) so it carries a
        # pg — the offer path keys on pg with zero branching. Runs regardless of
        # whether the INSERT was new or a no-op (like the sku/offer chain below),
        # so legacy mirrored rows get healed too. ON CONFLICT DO NOTHING — never
        # overwrites a real/curated group (no auto-merge). Uses the SAME
        # content_key the row was written with, so singleton and autogroup
        # converge on one pg. content_key NULL → pg-NULL + observable log.
        from services.product_group_autogrouper import (
            ensure_singleton_group_membership as _ensure_singleton_pg,
        )

        await _ensure_singleton_pg(
            merchant_id=seller_merchant_id,
            platform=PLATFORM,
            source_product_id=row_dict.get("external_product_id"),
            content_key=content_key_value,
        )

        # Phase 7d: write canonical sku + offer for this product
        # regardless of whether the catalog_products INSERT was new or
        # a no-op. The chain inserts use ON CONFLICT DO UPDATE so
        # existing rows get healed on every --apply pass — that's how
        # legacy Path B rows (mirrored before 7d) pick up their
        # missing skus/offers without needing a separate backfill
        # script. The product_key is the same identifier on both
        # paths, so chain writes stay attached to the correct PDP.
        product_key_for_chain = product_key
        if product_key_for_chain:
            try:
                await _upsert_canonical_sku_for_mirror_row(
                    product_key_for_chain, row_dict, merchant_id=seller_merchant_id
                )
                await _upsert_canonical_offer_for_mirror_row(
                    product_key_for_chain, row_dict, merchant_id=seller_merchant_id
                )
            except Exception as exc:
                # Don't break the whole apply if one row's chain fails
                # — log and continue. The next --apply pass will retry
                # via the same idempotent ON CONFLICT path.
                print(
                    f"WARNING: chain write failed for product_key={product_key_for_chain}: {exc!r}",
                    file=sys.stderr,
                )

        # Serving-layer artifacts so the mirrored row lands servable, not just
        # indexed. The attached_product_key back-link is unconditional (fixes the
        # `no_seed` gap — the mirror creates the product but never linked the
        # seed); the heavier quality-snapshot + agent_pdp_view pass dark-launches
        # behind MAKE_SERVABLE_ENV. Best-effort: never roll back the identity row.
        if product_key_for_chain:
            seed_id = str(row_dict.get("id") or "")
            try:
                if seed_id and not row_dict.get("attached_product_key"):
                    await backlink_seed_to_product(seed_id, product_key_for_chain)
                if seed_id and _make_servable_enabled():
                    await make_external_seed_servable(
                        product_key=product_key_for_chain,
                        seed_id=seed_id,
                        source_product_id=str(row_dict.get("external_product_id") or ""),
                        quality_payload=build_servable_quality_payload(
                            title=row_dict.get("title"),
                            description=row_dict.get("mirrored_description"),
                            price=row_dict.get("price_amount"),
                            image_url=row_dict.get("image_url"),
                            brand=row_dict.get("mirrored_brand"),
                            product_type=row_dict.get("mirrored_product_type"),
                            # `mirrored_category` is already SELECTed and already
                            # used at the catalog_products write below; only this
                            # call was not passing it. product_type is null on
                            # most crawled seeds, and the builder falls back
                            # product_type -> category to populate
                            # global_category_id, so without it the
                            # brand_category component scores 0 on exactly the
                            # rows that have a perfectly good category_kind.
                            category=row_dict.get("mirrored_category"),
                            # ATTRIBUTES component — see
                            # _extract_source_backed_signals for why these were
                            # absent and why seed_data (not beauty_sku_ingredients)
                            # is the right source at this point in the pipeline.
                            **_extract_source_backed_signals(
                                row_dict.get("seed_data")
                            ),
                        ),
                        reason="external_seed_mirror",
                    )
            except Exception as exc:
                print(
                    f"WARNING: servability step failed for product_key={product_key_for_chain}: {exc!r}",
                    file=sys.stderr,
                )
    if skipped_no_seller:
        # Loud, not silent (founder no-fallback directive): these rows carried no
        # resolvable brand+registrable-domain identity, so ADR-009 D2 forbids
        # mirroring them under any bucket. They are left un-mirrored on purpose.
        print(
            f"NOTE: skipped {skipped_no_seller} seed row(s) with no resolvable "
            "seller-of-record identity (ADR-009 D2 — no fallback bucket)",
            file=sys.stderr,
        )
    if skipped_brand_guard:
        # Loud, not silent: these seeds collide with a brand already canonical
        # under another merchant (ADR-008); each enqueued a review task instead
        # of minting a fragmented orphan.
        print(
            f"NOTE: skipped {skipped_brand_guard} seed row(s) via the ADR-008 "
            "brand-fragmentation guard (review tasks enqueued)",
            file=sys.stderr,
        )
    # Fix Plan B T3 — intake structure brake. Emit the per-run summary line and
    # flag (never silently) when too large a share of this batch carried no
    # machine-readable vertical at all. _run turns should_fail into a non-zero
    # process exit so "stop ingesting structureless garbage" is enforceable.
    guard = summarize_unresolved_vertical(
        rows_unresolved_vertical,
        rows_considered,
        threshold=_unresolved_vertical_fail_threshold(),
    )
    print(f"NOTE: {guard['summary']}", file=sys.stderr)
    if guard["should_fail"]:
        print(
            "ERROR: unresolved-vertical share "
            f"{guard['share'] * 100:.1f}% exceeds the {guard['threshold'] * 100:.1f}% "
            "intake brake — refusing to treat this mirror run as clean "
            "(set MIRROR_UNRESOLVED_VERTICAL_FAIL_THRESHOLD to adjust).",
            file=sys.stderr,
        )
    return {"inserted": inserted, "vertical_guard": guard}


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
            apply_result = await _apply(args.limit)
            inserted = apply_result["inserted"]
            vertical_guard = apply_result["vertical_guard"]
            after = await _build_report(
                sample_limit=args.sample_limit,
                limit=args.limit,
                apply=args.apply,
            )
            before_totals = before.get("totals") or {}
            after_totals = after.get("totals") or {}
            report = after
            report["warnings"] = list(before.get("warnings") or []) + list(after.get("warnings") or [])
            # Fix Plan B T3: surface the vertical-structure guard in the report,
            # and fail the run (report ok=False -> non-zero exit) when the brake
            # tripped, so a garbage-heavy ingest cannot be treated as clean.
            report["vertical_guard"] = vertical_guard
            if vertical_guard.get("should_fail"):
                report["ok"] = False
                report.setdefault("warnings", []).append(vertical_guard["summary"])
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
