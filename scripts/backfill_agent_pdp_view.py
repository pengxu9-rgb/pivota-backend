"""Stage 3a-ii backfill — populate agent_pdp_view from existing catalog tables.

Stage 3a-i (migration 085) added the denormalized agent_pdp_view table; this
script is the one-shot backfill that seeds it from
catalog_products × catalog_skus × catalog_offers × product_group_members ×
external_product_seeds. Stage 3a-iii adds the writer hook that keeps it
fresh on every seed_data commit; Stage 3a-iv ships the read endpoint.

Grouping model
--------------
One agent_pdp_view row per content_key. Rows in catalog_products that
share a content_key represent the same physical product carried by
different merchants/paths — they get folded into one denormalized view
with:

  * offers[] aggregated across every catalog_offers row whose product_key
    belongs to the content_key group
  * variants[] aggregated across every catalog_skus row in the group
  * a single "canonical content source" row chosen for title / description
    / brand / image / category / lifecycle / sync_status

Canonical-content tiebreak (deterministic so re-runs converge):

  1. Prefer the catalog_products row marked is_primary=true in
     product_group_members (the multi-seller-canonical choice).
  2. Then prefer rows whose pivota_signature_id is set (Phase C-4
     guarantees signed rows are the indexed surface).
  3. Then lowest product_key ASC (stable, hash-derived ordering).

Description fallback
--------------------
If catalog_products.description is empty on the chosen canonical row,
fall back to external_product_seeds.seed_data->>'description' for any
seed attached to one of the group's product_keys. external_product_seeds
is intentional bootstrap content (memory:
project_pivota_external_seed_bootstrap) — its seed_data is the canonical
PDP body for external-redirect products. We never invent description
prose; if neither source has one we leave NULL and let Stage 3a-iv emit
the JSON-LD fallback (pivota-agent-ui#167 ships the corresponding
SEO-side fallback for the rendered tag).

Mock/synthetic boundary (memory: feedback_mock_data_never_to_merchant):
we DO NOT synthesize descriptions or offer prose. Every field copied
into agent_pdp_view originates in a primary catalog table or
external_product_seeds.seed_data (employee-authored bootstrap data).

Usage
-----
Dry-run (default):
  python3 scripts/backfill_agent_pdp_view.py --limit 200

Apply:
  python3 scripts/backfill_agent_pdp_view.py --apply --limit 200 --offset 0

Full backfill in one shot (only on small / staging DBs):
  python3 scripts/backfill_agent_pdp_view.py --apply --limit 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_identity import normalize_gtin  # noqa: E402

logger = logging.getLogger("backfill_agent_pdp_view")

# Top-N offers stored per row. Schema docstring (mig 085) says <=5;
# matches the AggregateOffer behavior on the frontend.
OFFER_TOP_N = 5

# Cap variants at 50 — matches the schema comment. Tom Ford foundation
# has 40 shades, our prod p99.
VARIANT_CAP = 50

REFRESH_SOURCE = "backfill_3a_ii"

PDP_URL_PREFIX = "https://agent.pivota.cc/products/"


# ---------------------------------------------------------------------
# Per-content_key fetch + assembly
# ---------------------------------------------------------------------

async def _fetch_content_keys(*, limit: int, offset: int) -> List[str]:
    """Stable content_key window. We page by content_key ASC so each
    chunk processes a disjoint slice — no double-writes, safe to resume
    on partial failures.
    """
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    offset_clause = "OFFSET :offset" if offset > 0 else ""
    sql = f"""
        SELECT DISTINCT content_key
        FROM catalog_products
        WHERE content_key IS NOT NULL
        ORDER BY content_key ASC
        {limit_clause}
        {offset_clause}
    """
    params: Dict[str, Any] = {}
    if limit > 0:
        params["limit"] = int(limit)
    if offset > 0:
        params["offset"] = int(offset)
    rows = await database.fetch_all(sql, params)
    return [r["content_key"] for r in rows or []]


async def _fetch_products_for_key(content_key: str) -> List[Dict[str, Any]]:
    """All catalog_products rows sharing this content_key, joined to
    product_group_members on the (merchant_id, platform, source_product_id)
    composite to surface is_primary + product_group_id.
    """
    rows = await database.fetch_all(
        """
        SELECT
          cp.product_key,
          cp.merchant_id,
          cp.platform,
          cp.source_product_id,
          cp.title,
          cp.description,
          cp.brand,
          cp.product_type,
          cp.category,
          cp.image_url,
          cp.product_payload,
          cp.tags,
          cp.price_tier,
          cp.use_case_tags,
          cp.lifestyle_tags,
          cp.demographic,
          cp.pdp_lifecycle_stage,
          cp.pivota_signature_id,
          cp.canonical_url,
          cp.sync_status,
          cp.created_at,
          pgm.product_group_id,
          pgm.is_primary AS group_is_primary
        FROM catalog_products cp
        LEFT JOIN product_group_members pgm
          ON pgm.merchant_id = cp.merchant_id
         AND pgm.platform = cp.platform
         AND pgm.platform_product_id = cp.source_product_id
        WHERE cp.content_key = :ck
        """,
        {"ck": content_key},
    )
    return [dict(r) for r in rows or []]


async def _fetch_skus_for_keys(product_keys: List[str]) -> List[Dict[str, Any]]:
    if not product_keys:
        return []
    rows = await database.fetch_all(
        """
        SELECT
          sku_key, product_key, merchant_id, source_variant_id, source_product_id,
          sku, barcode, title, currency, image_url, visible_attributes,
          visible_option_labels
        FROM catalog_skus
        WHERE product_key = ANY(:keys)
        """,
        {"keys": product_keys},
    )
    return [dict(r) for r in rows or []]


async def _fetch_offers_for_keys(product_keys: List[str]) -> List[Dict[str, Any]]:
    if not product_keys:
        return []
    rows = await database.fetch_all(
        """
        SELECT
          o.offer_id, o.sku_key, o.product_key, o.merchant_id,
          o.availability, o.currency, o.list_price,
          o.merchant_effective_price, o.estimated_best_price,
          m.merchant_name
        FROM catalog_offers o
        LEFT JOIN catalog_merchants m ON m.merchant_id = o.merchant_id
        WHERE o.product_key = ANY(:keys)
        """,
        {"keys": product_keys},
    )
    return [dict(r) for r in rows or []]


async def _fetch_external_seed_for_keys(product_keys: List[str]) -> Optional[Dict[str, Any]]:
    """First active external_product_seed attached to any of these
    product_keys. We only need it as a content fallback so any one row
    is fine.
    """
    if not product_keys:
        return None
    row = await database.fetch_one(
        """
        SELECT id, attached_product_key, title, image_url, seed_data,
               canonical_url, destination_url
        FROM external_product_seeds
        WHERE attached_product_key = ANY(:keys)
          AND status = 'active'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"keys": product_keys},
    )
    return dict(row) if row else None


def _pick_canonical(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic canonical-row pick. See module docstring for the
    tiebreak ladder.
    """
    def key(r: Dict[str, Any]) -> Tuple[int, int, str]:
        # group_is_primary True sorts before False (0 < 1)
        primary_rank = 0 if r.get("group_is_primary") else 1
        sig_rank = 0 if r.get("pivota_signature_id") else 1
        return (primary_rank, sig_rank, r.get("product_key") or "")

    return sorted(rows, key=key)[0]


def _coalesce_first(*values: Any) -> Any:
    """First non-empty value (after coercing to str-trim for strings)."""
    for v in values:
        if v is None:
            continue
        if isinstance(v, str):
            stripped = v.strip()
            if stripped:
                return stripped
        else:
            return v
    return None


def _read_image_urls(payload: Any) -> List[str]:
    """Pull a deduped image-URL list out of product_payload JSONB. Real
    upstream shapes vary (Shopify nests under .images[].src, Wix has
    .media, our external seeds drop a flat array). Try the common ones.
    """
    if not isinstance(payload, dict):
        return []
    found: List[str] = []

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            url = value.strip()
            if url not in found:
                found.append(url)

    for v in payload.get("image_urls") or []:
        _add(v)
    for v in payload.get("images") or []:
        if isinstance(v, str):
            _add(v)
        elif isinstance(v, dict):
            _add(v.get("src") or v.get("url"))
    if not found:
        _add(payload.get("image_url"))
    return found


def _normalize_offer(offer: Dict[str, Any], primary_merchant_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Project a catalog_offers row + merchants join down to the
    offer-shape we store in agent_pdp_view.offers JSONB. Returns None if
    the offer has no usable price.
    """
    price = _coalesce_first(
        offer.get("merchant_effective_price"),
        offer.get("estimated_best_price"),
        offer.get("list_price"),
    )
    if price is None:
        return None
    try:
        price_decimal = Decimal(price)
    except Exception:
        return None
    return {
        "merchant_id": offer.get("merchant_id"),
        "merchant_name": offer.get("merchant_name"),
        "price": float(price_decimal),
        "currency": offer.get("currency"),
        "availability": offer.get("availability"),
        # No url column on catalog_offers — use the merchant's PDP if
        # we have it on the corresponding catalog_products row. Caller
        # supplies it via the merchant_url_by_id lookup below.
        "url": None,
        "is_primary": offer.get("merchant_id") == primary_merchant_id,
    }


def _aggregate_offers(
    offers: List[Dict[str, Any]],
    primary_merchant_id: Optional[str],
    merchant_url_by_id: Dict[str, Optional[str]],
) -> Tuple[
    Optional[str],  # currency
    Optional[Decimal],  # price_min
    Optional[Decimal],  # price_max
    int,  # offer_count
    List[Dict[str, Any]],  # top-N offers
]:
    """Compute price aggregates + top-N offers from raw catalog_offers
    rows. Stable ordering: primary merchant first, then price ASC, then
    merchant_id ASC for determinism.
    """
    normalized: List[Dict[str, Any]] = []
    for o in offers:
        n = _normalize_offer(o, primary_merchant_id)
        if not n:
            continue
        merchant_id = n.get("merchant_id") or ""
        n["url"] = merchant_url_by_id.get(merchant_id)
        normalized.append(n)

    if not normalized:
        return None, None, None, 0, []

    # Prefer a single currency across the group — pick the modal one.
    # Mixed-currency groups are rare but real (e.g. .com vs .ca);
    # surface the dominant currency at the row level, keep each offer's
    # own currency intact.
    currency_counts: Dict[str, int] = {}
    for n in normalized:
        c = n.get("currency") or ""
        if c:
            currency_counts[c] = currency_counts.get(c, 0) + 1
    currency = (
        max(currency_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if currency_counts
        else None
    )

    prices_in_currency = [
        Decimal(str(n["price"])) for n in normalized
        if currency is None or n.get("currency") == currency
    ]
    price_min = min(prices_in_currency) if prices_in_currency else None
    price_max = max(prices_in_currency) if prices_in_currency else None

    def offer_sort_key(o: Dict[str, Any]) -> Tuple[int, float, str]:
        return (
            0 if o.get("is_primary") else 1,
            float(o.get("price") or 0.0),
            o.get("merchant_id") or "",
        )

    top = sorted(normalized, key=offer_sort_key)[:OFFER_TOP_N]
    return currency, price_min, price_max, len(normalized), top


def _aggregate_variants(skus: List[Dict[str, Any]], canonical_source_product_id: Optional[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Build variants[]. Drop the "singleton placeholder" SKU where
    source_variant_id == source_product_id (Path A sync inserts one
    such row per product to make the SKU table joinable even when
    upstream has no variants — that's not a real variant).
    """
    variants: List[Dict[str, Any]] = []
    for s in skus:
        svid = s.get("source_variant_id")
        spid = s.get("source_product_id")
        if svid and spid and str(svid) == str(spid):
            continue
        variants.append({
            "variant_id": svid,
            "sku": s.get("sku"),
            "title": s.get("title"),
            "options": s.get("visible_option_labels") or s.get("visible_attributes") or {},
            "image_url": s.get("image_url"),
            "currency": s.get("currency"),
            "merchant_id": s.get("merchant_id"),
        })

    # Deterministic ordering: by sku then variant_id to make re-runs
    # produce byte-identical JSONB blobs (helps diffing in admin tools).
    variants.sort(key=lambda v: (v.get("sku") or "", str(v.get("variant_id") or "")))

    count = len(variants)
    return variants[:VARIANT_CAP], count


def _pick_gtin13(skus: List[Dict[str, Any]]) -> Optional[str]:
    for s in skus:
        bar = s.get("barcode")
        if not bar:
            continue
        canon = normalize_gtin(str(bar))
        if canon:
            return canon
    return None


def _build_taxonomy_tags(canonical: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fields = {
        "price_tier": canonical.get("price_tier"),
        "use_case_tags": canonical.get("use_case_tags"),
        "lifestyle_tags": canonical.get("lifestyle_tags"),
        "demographic": canonical.get("demographic"),
        "product_type": canonical.get("product_type"),
        "category": canonical.get("category"),
        "tags": canonical.get("tags"),
    }
    tags = {k: v for k, v in fields.items() if v not in (None, "", [], {})}
    return tags or None


def _build_breadcrumb(canonical: Dict[str, Any], pdp_url: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    title = _coalesce_first(canonical.get("title"))
    if not title:
        return None
    crumbs: List[Dict[str, Any]] = [
        {"position": 1, "name": "Home", "item": "https://agent.pivota.cc/"},
    ]
    category = _coalesce_first(canonical.get("category"), canonical.get("product_type"))
    if category:
        crumbs.append({"position": len(crumbs) + 1, "name": category, "item": None})
    brand = _coalesce_first(canonical.get("brand"))
    if brand:
        crumbs.append({"position": len(crumbs) + 1, "name": brand, "item": None})
    crumbs.append({"position": len(crumbs) + 1, "name": title, "item": pdp_url})
    return crumbs


def _assemble_row(
    *,
    content_key: str,
    products: List[Dict[str, Any]],
    skus: List[Dict[str, Any]],
    offers: List[Dict[str, Any]],
    external_seed: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Produce the agent_pdp_view row payload from raw source rows.
    Returns None when the data is too thin to be useful (no title).
    """
    canonical = _pick_canonical(products)
    title = _coalesce_first(canonical.get("title"))
    if not title:
        return None

    seed_data = (external_seed or {}).get("seed_data") or {}
    if isinstance(seed_data, str):
        # Some legacy rows store seed_data as a JSON string; tolerate.
        try:
            seed_data = json.loads(seed_data)
        except Exception:
            seed_data = {}

    description = _coalesce_first(
        canonical.get("description"),
        seed_data.get("description"),
        seed_data.get("short_description"),
    )

    image_url = _coalesce_first(
        canonical.get("image_url"),
        (external_seed or {}).get("image_url"),
        seed_data.get("image_url"),
    )

    image_urls = _read_image_urls(canonical.get("product_payload"))
    if image_url and image_url not in image_urls:
        image_urls.insert(0, image_url)
    seed_images = seed_data.get("image_urls") if isinstance(seed_data, dict) else None
    if isinstance(seed_images, list):
        for u in seed_images:
            if isinstance(u, str) and u.strip() and u.strip() not in image_urls:
                image_urls.append(u.strip())

    # product_group_id: any group row in the cluster wins. They should
    # agree; if not, prefer the canonical row's. Then any non-null.
    product_group_id = _coalesce_first(
        canonical.get("product_group_id"),
        *[p.get("product_group_id") for p in products],
    )

    # Primary merchant: the is_primary=true row in product_group_members
    # if any; else the canonical row's merchant.
    primary_merchant_id = None
    for p in products:
        if p.get("group_is_primary"):
            primary_merchant_id = p.get("merchant_id")
            break
    if not primary_merchant_id:
        primary_merchant_id = canonical.get("merchant_id")

    # Build a merchant_id → canonical_url lookup so offers carry a URL.
    merchant_url_by_id: Dict[str, Optional[str]] = {}
    for p in products:
        mid = p.get("merchant_id")
        if mid and mid not in merchant_url_by_id:
            merchant_url_by_id[mid] = p.get("canonical_url")

    currency, price_min, price_max, offer_count, top_offers = _aggregate_offers(
        offers, primary_merchant_id, merchant_url_by_id
    )

    variants_capped, variants_count = _aggregate_variants(
        skus, canonical.get("source_product_id")
    )
    gtin13 = _pick_gtin13(skus)

    sig = canonical.get("pivota_signature_id")
    pdp_url = f"{PDP_URL_PREFIX}{sig}" if sig else None
    breadcrumb = _build_breadcrumb(canonical, pdp_url)
    taxonomy_tags = _build_taxonomy_tags(canonical)
    category_path = _coalesce_first(canonical.get("category"), canonical.get("product_type"))

    return {
        "content_key": content_key,
        "pivota_signature_id": sig,
        "product_group_id": product_group_id,
        "brand": canonical.get("brand"),
        "title": title,
        "description": (description[:5000] if isinstance(description, str) else None),
        "image_url": image_url,
        "image_urls": image_urls or None,
        "currency": currency,
        "price_min": price_min,
        "price_max": price_max,
        "offer_count": offer_count,
        "offers": top_offers or None,
        "variants": variants_capped or None,
        "variants_count": variants_count,
        "gtin13": gtin13,
        "category_path": category_path,
        "taxonomy_tags": taxonomy_tags,
        "breadcrumb": breadcrumb,
        "pdp_lifecycle_stage": canonical.get("pdp_lifecycle_stage"),
        "sync_status": canonical.get("sync_status"),
        "primary_merchant_id": primary_merchant_id,
        "refresh_source": REFRESH_SOURCE,
    }


# ---------------------------------------------------------------------
# UPSERT
# ---------------------------------------------------------------------

# refreshed_at / refreshed_by_proposal_id are owned by the writer hook
# (Stage 3a-iii). The backfill sets refreshed_at to NOW() so a
# subsequent writer commit can compare timestamps deterministically.
UPSERT_SQL = """
    INSERT INTO agent_pdp_view (
      content_key, pivota_signature_id, product_group_id,
      brand, title, description, image_url, image_urls,
      currency, price_min, price_max, offer_count, offers,
      variants, variants_count, gtin13,
      category_path, taxonomy_tags, breadcrumb,
      pdp_lifecycle_stage, sync_status, primary_merchant_id,
      refreshed_at, refresh_source
    ) VALUES (
      :content_key, :pivota_signature_id, :product_group_id,
      :brand, :title, :description, :image_url, CAST(:image_urls AS jsonb),
      :currency, :price_min, :price_max, :offer_count, CAST(:offers AS jsonb),
      CAST(:variants AS jsonb), :variants_count, :gtin13,
      :category_path, CAST(:taxonomy_tags AS jsonb), CAST(:breadcrumb AS jsonb),
      :pdp_lifecycle_stage, :sync_status, :primary_merchant_id,
      NOW(), :refresh_source
    )
    ON CONFLICT (content_key) DO UPDATE SET
      pivota_signature_id = EXCLUDED.pivota_signature_id,
      product_group_id = EXCLUDED.product_group_id,
      brand = EXCLUDED.brand,
      title = EXCLUDED.title,
      description = EXCLUDED.description,
      image_url = EXCLUDED.image_url,
      image_urls = EXCLUDED.image_urls,
      currency = EXCLUDED.currency,
      price_min = EXCLUDED.price_min,
      price_max = EXCLUDED.price_max,
      offer_count = EXCLUDED.offer_count,
      offers = EXCLUDED.offers,
      variants = EXCLUDED.variants,
      variants_count = EXCLUDED.variants_count,
      gtin13 = EXCLUDED.gtin13,
      category_path = EXCLUDED.category_path,
      taxonomy_tags = EXCLUDED.taxonomy_tags,
      breadcrumb = EXCLUDED.breadcrumb,
      pdp_lifecycle_stage = EXCLUDED.pdp_lifecycle_stage,
      sync_status = EXCLUDED.sync_status,
      primary_merchant_id = EXCLUDED.primary_merchant_id,
      refreshed_at = NOW(),
      refresh_source = EXCLUDED.refresh_source
"""


def _to_jsonb(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, default=str)


async def _upsert_row(row: Dict[str, Any]) -> None:
    params = dict(row)
    params["image_urls"] = _to_jsonb(row.get("image_urls"))
    params["offers"] = _to_jsonb(row.get("offers"))
    params["variants"] = _to_jsonb(row.get("variants"))
    params["taxonomy_tags"] = _to_jsonb(row.get("taxonomy_tags"))
    params["breadcrumb"] = _to_jsonb(row.get("breadcrumb"))
    await database.execute(UPSERT_SQL, params)


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------

async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    content_keys = await _fetch_content_keys(limit=args.limit, offset=args.offset)
    logger.info(
        "loaded %d content_keys (limit=%d offset=%d)",
        len(content_keys), args.limit, args.offset,
    )

    outcomes: Dict[str, int] = {
        "content_keys_considered": len(content_keys),
        "rows_assembled": 0,
        "rows_skipped_no_title": 0,
        "rows_upserted": 0,
        "rows_skipped_no_op_in_dry_run": 0,
    }
    samples: List[Dict[str, Any]] = []

    for ck in content_keys:
        products = await _fetch_products_for_key(ck)
        if not products:
            continue
        product_keys = [p["product_key"] for p in products]
        skus = await _fetch_skus_for_keys(product_keys)
        offers = await _fetch_offers_for_keys(product_keys)
        external_seed = await _fetch_external_seed_for_keys(product_keys)

        row = _assemble_row(
            content_key=ck,
            products=products,
            skus=skus,
            offers=offers,
            external_seed=external_seed,
        )
        if row is None:
            outcomes["rows_skipped_no_title"] += 1
            continue
        outcomes["rows_assembled"] += 1
        if len(samples) < 5:
            samples.append({
                "content_key": ck,
                "title": row["title"],
                "brand": row["brand"],
                "offer_count": row["offer_count"],
                "variants_count": row["variants_count"],
                "primary_merchant_id": row["primary_merchant_id"],
            })

        if not args.apply:
            outcomes["rows_skipped_no_op_in_dry_run"] += 1
            continue
        await _upsert_row(row)
        outcomes["rows_upserted"] += 1

    return {"outcome_counts": outcomes, "samples": samples}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually UPSERT agent_pdp_view rows. Default: dry-run.",
    )
    p.add_argument(
        "--limit", type=int, default=200,
        help="Max content_keys to process this run (0 = all). Default 200.",
    )
    p.add_argument(
        "--offset", type=int, default=0,
        help="Skip the first N content_keys. Use to paginate across chunks.",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
