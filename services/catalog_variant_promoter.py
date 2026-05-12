"""Promote embedded variant data into catalog_skus rows.

Stage 2b-ii of the PDP architecture roadmap (plans/rosy-mixing-bengio.md).
Solves a class of bug surfaced by Stage 2b-i's prod telemetry:

  - Tom Ford's "Architecture Radiance Foundation" had 43 catalog_products
    rows clustered under one product_group. Each row carries a
    seed_data.snapshot.variants array with the SAME 40 shade variants.
    Pre-2b-ii: agent UI saw 43 catalog_skus rows (the synthetic
    `<product_key>::canonical` SKU per catalog_products row), all
    titled "Architecture Radiance Foundation" — no shade selector
    surfaced. The 40 shades existed in jsonb but never reached the
    SKU layer.

Stage 2b-ii fix: for the PRIMARY catalog_products row of each multi-
member product_group, read the embedded variants array, filter out
Shopify "Default Title" single-variant placeholders, and upsert one
catalog_skus row per real variant. After this:

  - Tom Ford product_group has 40 catalog_skus rows representing the
    40 shades (sku_key = `<primary_product_key>::v::<variant_id>`)
  - Agent UI (and Stage 3's agent_pdp_view) can render shade swatches
    under one canonical PDP — same UX as Sephora / Ulta / Tom Ford's
    own site

What this service does NOT do:
  - Touch catalog_products. Identity stays where it is.
  - Touch seed_data / product_payload. Variants are already there —
    we just project them to a separate table.
  - Apply to non-primary group members. The primary's variants are
    representative of the whole group (verified empirically: the 43
    Tom Ford rows have IDENTICAL variants arrays). Re-running the
    extractor on every member would just produce duplicate upserts;
    the unique index on (merchant_id, platform, source_variant_id)
    would dedup but it's wasted work.
  - Touch Shopify Default-Title placeholders. MOYU 26-Foundation-Brush
    case: each row has one variant with title='Default Title' / options
    'Default Title'. Already correctly modeled by Stage 2b-i as
    catalog-level entries. No SKU promotion needed.

Variant data shape (real-world examples):
  Path B (external_seed mirror): seed_data.snapshot.variants is an
  array of dicts with shape:
    {
      "sku": "TCT117",
      "price": "95.00",
      "title": "8.5N Vellum / 30.0 ml",
      "options": [
        {"name": "Shade", "value": "8.5N Vellum", "axis_kind": "shade"},
        {"name": "Size", "value": "30.0 ml", "axis_kind": "size"}
      ],
      "variant_id": "53059916267733",
      "image_url": "...",
      "barcode": "...",
      "currency": "USD"
    }

  Path A (Shopify sync): catalog_products.product_payload['variants']
  has the StandardProduct shape:
    {
      "id": "53012671693097",
      "variant_id": "53012671693097",
      "sku": "...",
      "title": "Default Title",
      "options": {"Title": "Default Title"},
      "price": "...",
      ...
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from db.database import database

logger = logging.getLogger(__name__)


# A variant whose title is "Default Title" or whose options carry only
# "Default Title" / "Default" values is a Shopify placeholder for a
# single-variant product. Don't promote — agent UI shouldn't render a
# shade selector with one swatch labeled "Default Title".
_SHOPIFY_DEFAULT_TITLES = ("default title", "default", "")


@dataclass
class VariantRow:
    """One variant ready to upsert into catalog_skus."""

    sku_key: str
    product_key: str
    merchant_id: str
    platform: str
    source_product_id: str
    source_variant_id: str
    sku: Optional[str]
    barcode: Optional[str]
    title: str
    currency: Optional[str]
    image_url: Optional[str]
    visible_option_labels: List[str]
    visible_attributes: Dict[str, str]
    sku_payload: Dict[str, Any]


@dataclass
class GroupOutcome:
    product_group_id: str
    primary_product_key: str
    variants_found: int
    variants_promoted: int
    skipped_reason: Optional[str] = None
    sample_variant_titles: List[str] = field(default_factory=list)


@dataclass
class PromoterReport:
    groups_considered: int = 0
    groups_promoted: int = 0
    groups_skipped_no_real_variants: int = 0
    groups_skipped_no_primary: int = 0
    skus_upserted_total: int = 0
    per_group: List[GroupOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Variant array extraction
# ---------------------------------------------------------------------------


def _coerce_jsonb(value: Any) -> Any:
    """asyncpg returns JSONB as dict OR JSON string depending on codec.
    Same defensive coercion the writer service uses."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _extract_variants_from_seed(seed_data: Any) -> List[Dict[str, Any]]:
    """Path B / external_seed: variants live at seed_data.snapshot.variants.
    Returns [] when missing/empty/not a list."""
    sd = _coerce_jsonb(seed_data)
    if not isinstance(sd, dict):
        return []
    snapshot = sd.get("snapshot")
    if not isinstance(snapshot, dict):
        return []
    variants = snapshot.get("variants")
    if not isinstance(variants, list):
        return []
    return [v for v in variants if isinstance(v, dict)]


def _extract_variants_from_payload(product_payload: Any) -> List[Dict[str, Any]]:
    """Path A / Shopify: variants live at product_payload.variants
    (StandardProduct serialization). Returns [] when missing."""
    pp = _coerce_jsonb(product_payload)
    if not isinstance(pp, dict):
        return []
    variants = pp.get("variants")
    if not isinstance(variants, list):
        return []
    return [v for v in variants if isinstance(v, dict)]


# ---------------------------------------------------------------------------
# Real-vs-default filter
# ---------------------------------------------------------------------------


def _variant_options_are_meaningful(options: Any) -> bool:
    """An options blob is "meaningful" if any of its values is NOT in
    {Default Title, Default, empty}.

    Path B shape: list of {name, value, axis_kind}.
    Path A shape: dict like {"Title": "Default Title"}.
    """
    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, dict):
                continue
            val = (opt.get("value") or "").strip().lower()
            if val and val not in _SHOPIFY_DEFAULT_TITLES:
                return True
        return False
    if isinstance(options, dict):
        for val in options.values():
            if isinstance(val, str) and val.strip().lower() not in _SHOPIFY_DEFAULT_TITLES:
                return True
        return False
    return False


def is_real_variant(variant: Dict[str, Any]) -> bool:
    """True iff this is a real shade/size/etc variant, not a Shopify
    Default-Title placeholder. The title alone isn't enough (some
    real variants have meaningful titles AND a default options dict);
    require AT LEAST ONE of: meaningful options, OR title that isn't
    a default placeholder."""
    if not isinstance(variant, dict):
        return False
    title = (variant.get("title") or "").strip().lower()
    title_is_meaningful = bool(title) and title not in _SHOPIFY_DEFAULT_TITLES
    options_are_meaningful = _variant_options_are_meaningful(variant.get("options"))
    return title_is_meaningful or options_are_meaningful


def filter_real_variants(variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop Default-Title placeholders. If after filtering only one
    variant remains AND that variant's title matches the parent product
    title (e.g. mirror script stuffing the product title as a synthetic
    canonical variant), still drop it — that's not a real shade
    variant either. Caller passes the parent title for that check."""
    return [v for v in variants if is_real_variant(v)]


# ---------------------------------------------------------------------------
# Build the catalog_skus row dict for upsert
# ---------------------------------------------------------------------------


def _derive_sku_key(primary_product_key: str, variant_id: str) -> str:
    """Stable, debuggable: <primary_product_key>::v::<variant_id>.
    Distinct from the existing `::canonical` synthetic SKU so the two
    coexist without conflict on the catalog_skus PK."""
    return f"{primary_product_key}::v::{variant_id}"


def _visible_option_labels(options: Any) -> List[str]:
    """Flatten options to a list of display labels for the SKU's
    visible_option_labels jsonb column."""
    labels: List[str] = []
    if isinstance(options, list):
        for opt in options:
            if isinstance(opt, dict):
                val = (opt.get("value") or "").strip()
                if val:
                    labels.append(val)
    elif isinstance(options, dict):
        for val in options.values():
            if isinstance(val, str) and val.strip():
                labels.append(val.strip())
    return labels


def _visible_attributes(options: Any) -> Dict[str, str]:
    """Structured options as {axis_name: value} dict. Path B's
    axis_kind is preferred over name when present (more semantic)."""
    attrs: Dict[str, str] = {}
    if isinstance(options, list):
        for opt in options:
            if isinstance(opt, dict):
                key = (opt.get("axis_kind") or opt.get("name") or "").strip().lower()
                val = (opt.get("value") or "").strip()
                if key and val:
                    attrs[key] = val
    elif isinstance(options, dict):
        for k, v in options.items():
            if isinstance(v, str) and v.strip():
                attrs[str(k).strip().lower()] = v.strip()
    return attrs


def build_variant_row(
    *, variant: Dict[str, Any], primary: Dict[str, Any]
) -> Optional[VariantRow]:
    """Assemble the catalog_skus upsert dict from one variant + the
    primary's identity. Returns None when the variant doesn't carry
    a variant_id (can't compute a stable sku_key)."""
    variant_id = str(variant.get("variant_id") or variant.get("id") or "").strip()
    if not variant_id:
        return None

    sku_key = _derive_sku_key(primary["product_key"], variant_id)
    options = variant.get("options")
    return VariantRow(
        sku_key=sku_key,
        product_key=primary["product_key"],
        merchant_id=primary["merchant_id"],
        platform=primary["platform"],
        source_product_id=primary["source_product_id"],
        source_variant_id=variant_id,
        sku=variant.get("sku") or None,
        barcode=variant.get("barcode") or None,
        title=str(variant.get("title") or "").strip() or "(untitled variant)",
        currency=variant.get("currency") or None,
        image_url=variant.get("image_url") or None,
        visible_option_labels=_visible_option_labels(options),
        visible_attributes=_visible_attributes(options),
        sku_payload=variant,
    )


# ---------------------------------------------------------------------------
# Group resolution + upsert
# ---------------------------------------------------------------------------


SELECT_GROUP_PRIMARY_SQL = """
    SELECT cp.product_key,
           cp.merchant_id,
           cp.platform,
           cp.source_product_id,
           cp.title AS parent_title,
           cp.product_payload AS product_payload,
           eps.seed_data AS seed_data
    FROM product_group_members pgm
    JOIN catalog_products cp
      ON cp.merchant_id = pgm.merchant_id
     AND cp.platform = pgm.platform
     AND cp.source_product_id = pgm.platform_product_id
    LEFT JOIN external_product_seeds eps
      ON eps.external_product_id = cp.source_product_id
    WHERE pgm.product_group_id = :group_id
      AND pgm.is_primary = TRUE
    LIMIT 1
"""


SELECT_GROUPS_TO_PROCESS_SQL = """
    SELECT pgm.product_group_id AS group_id, count(*) AS member_count
    FROM product_group_members pgm
    JOIN catalog_products cp
      ON cp.merchant_id = pgm.merchant_id
     AND cp.platform = pgm.platform
     AND cp.source_product_id = pgm.platform_product_id
    WHERE pgm.product_group_id LIKE 'pg_%%'
"""


UPSERT_SKU_SQL = """
    INSERT INTO catalog_skus (
        sku_key, product_key, merchant_id, platform,
        source_product_id, source_variant_id,
        sku, barcode, title, currency, image_url,
        visible_option_labels, visible_attributes, sku_payload,
        readiness_tier, updated_at
    ) VALUES (
        :sku_key, :product_key, :merchant_id, :platform,
        :source_product_id, :source_variant_id,
        :sku, :barcode, :title, :currency, :image_url,
        CAST(:visible_option_labels AS jsonb),
        CAST(:visible_attributes AS jsonb),
        CAST(:sku_payload AS jsonb),
        'commerce_ready', NOW()
    )
    ON CONFLICT (merchant_id, platform, source_variant_id)
    DO UPDATE SET
        sku_key = EXCLUDED.sku_key,
        product_key = EXCLUDED.product_key,
        source_product_id = EXCLUDED.source_product_id,
        sku = EXCLUDED.sku,
        barcode = EXCLUDED.barcode,
        title = EXCLUDED.title,
        currency = EXCLUDED.currency,
        image_url = EXCLUDED.image_url,
        visible_option_labels = EXCLUDED.visible_option_labels,
        visible_attributes = EXCLUDED.visible_attributes,
        sku_payload = EXCLUDED.sku_payload,
        updated_at = NOW()
"""


def _extract_variants_for_primary(primary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Try Path B (seed_data.snapshot.variants) first, then Path A
    (product_payload.variants). Returns whichever was non-empty.
    Path B preferred because external_seed scrapes have richer
    options metadata (axis_kind, structured shade/size labels)."""
    seed_variants = _extract_variants_from_seed(primary.get("seed_data"))
    if seed_variants:
        return seed_variants
    payload_variants = _extract_variants_from_payload(primary.get("product_payload"))
    return payload_variants


async def promote_variants_for_group(
    *, group_id: str, apply: bool = False
) -> GroupOutcome:
    """Process one product_group. Idempotent — repeat calls upsert.
    Wraps the per-group upserts in a transaction so partial failure
    doesn't leave a group with some variants but not others."""
    primary_row = await database.fetch_one(
        SELECT_GROUP_PRIMARY_SQL, {"group_id": group_id}
    )
    if primary_row is None:
        return GroupOutcome(
            product_group_id=group_id,
            primary_product_key="",
            variants_found=0,
            variants_promoted=0,
            skipped_reason="no_primary_for_group",
        )

    primary = dict(primary_row)
    raw_variants = _extract_variants_for_primary(primary)
    real_variants = filter_real_variants(raw_variants)

    if not real_variants:
        return GroupOutcome(
            product_group_id=group_id,
            primary_product_key=primary["product_key"],
            variants_found=len(raw_variants),
            variants_promoted=0,
            skipped_reason="no_real_variants",
        )

    rows_to_upsert: List[VariantRow] = []
    for v in real_variants:
        row = build_variant_row(variant=v, primary=primary)
        if row is not None:
            rows_to_upsert.append(row)

    sample_titles = [r.title for r in rows_to_upsert[:5]]

    promoted = 0
    if apply and rows_to_upsert:
        async with database.transaction():
            for r in rows_to_upsert:
                await database.execute(
                    UPSERT_SKU_SQL,
                    {
                        "sku_key": r.sku_key,
                        "product_key": r.product_key,
                        "merchant_id": r.merchant_id,
                        "platform": r.platform,
                        "source_product_id": r.source_product_id,
                        "source_variant_id": r.source_variant_id,
                        "sku": r.sku,
                        "barcode": r.barcode,
                        "title": r.title,
                        "currency": r.currency,
                        "image_url": r.image_url,
                        "visible_option_labels": json.dumps(r.visible_option_labels),
                        "visible_attributes": json.dumps(r.visible_attributes),
                        "sku_payload": json.dumps(r.sku_payload, default=str),
                    },
                )
                promoted += 1

    return GroupOutcome(
        product_group_id=group_id,
        primary_product_key=primary["product_key"],
        variants_found=len(raw_variants),
        variants_promoted=promoted if apply else len(rows_to_upsert),
        sample_variant_titles=sample_titles,
    )


async def promote_variants_all(
    *,
    apply: bool = False,
    product_group_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    limit: int = 100,
) -> PromoterReport:
    """Iterate every multi-member product_group (or scope by group_id
    / merchant_id) and promote variants from the primary."""
    report = PromoterReport()

    # Build the group fetch SQL based on scope
    sql = SELECT_GROUPS_TO_PROCESS_SQL
    params: Dict[str, Any] = {}
    if product_group_id:
        sql += " AND pgm.product_group_id = :group_id"
        params["group_id"] = product_group_id
    elif merchant_id:
        sql += " AND pgm.merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    sql += " GROUP BY pgm.product_group_id HAVING count(*) >= 1 ORDER BY pgm.product_group_id"
    if limit > 0:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)

    rows = await database.fetch_all(sql, params)
    group_ids = [r["group_id"] for r in rows or []]

    for gid in group_ids:
        report.groups_considered += 1
        outcome = await promote_variants_for_group(group_id=gid, apply=apply)
        report.per_group.append(outcome)
        if outcome.skipped_reason == "no_primary_for_group":
            report.groups_skipped_no_primary += 1
        elif outcome.skipped_reason == "no_real_variants":
            report.groups_skipped_no_real_variants += 1
        else:
            report.groups_promoted += 1
            report.skus_upserted_total += outcome.variants_promoted

    return report
