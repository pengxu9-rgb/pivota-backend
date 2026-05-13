"""Pure-Python assembler for agent_pdp_view rows (Stage 3a).

The agent_pdp_view denormalized table (mig 085, Stage 3a-i) is fed from
two surfaces:

  * The one-shot backfill (Stage 3a-ii — scripts/backfill_agent_pdp_view.py)
  * The writer hook (Stage 3a-iii — services/seed_data_writer.py), which
    refreshes affected rows on every seed_data commit.

Both call the same pure-Python assembler defined here. Keeping it
side-effect-free (no DB writes inside _assemble_row) means the writer
hook can dry-run / preview the next row state without touching prod,
and the backfill stays trivially testable.

The DB SELECTs live with the callers (script vs writer hook) because
their batching shapes differ — the backfill pulls a content_key window;
the writer hook chases a single content_key per commit.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from services.catalog_identity import normalize_gtin

# Top-N offers stored per row. Schema docstring (mig 085) says <=5;
# matches the AggregateOffer behavior on the frontend.
OFFER_TOP_N = 5

# Cap variants at 50 — matches the schema comment. Tom Ford foundation
# has 40 shades, our prod p99.
VARIANT_CAP = 50

PDP_URL_PREFIX = "https://agent.pivota.cc/products/"

# Refresh-source tag the backfill writes when seeding rows from
# scratch. The Stage 3a-iii writer hook sets its own source ("writer_commit"
# or similar) so the audit trail distinguishes one-shot fills from
# incremental refreshes.
BACKFILL_REFRESH_SOURCE = "backfill_3a_ii"


def coalesce_first(*values: Any) -> Any:
    """First non-empty value (string trim aware)."""
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


def pick_canonical(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic canonical-row pick.

    Tiebreak ladder:
      1. product_group_members.is_primary = true  (multi-seller canonical)
      2. catalog_products.pivota_signature_id is set  (indexed surface)
      3. lowest product_key ASC  (stable hash-derived ordering)
    """
    def key(r: Dict[str, Any]) -> Tuple[int, int, str]:
        primary_rank = 0 if r.get("group_is_primary") else 1
        sig_rank = 0 if r.get("pivota_signature_id") else 1
        return (primary_rank, sig_rank, r.get("product_key") or "")

    return sorted(rows, key=key)[0]


def read_image_urls(payload: Any) -> List[str]:
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


def normalize_offer(offer: Dict[str, Any], primary_merchant_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Project a catalog_offers row + merchants join down to the
    offer-shape we store in agent_pdp_view.offers JSONB. Returns None if
    the offer has no usable price.
    """
    price = coalesce_first(
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
        "url": None,  # caller injects via merchant_url_by_id
        "is_primary": offer.get("merchant_id") == primary_merchant_id,
    }


def aggregate_offers(
    offers: List[Dict[str, Any]],
    primary_merchant_id: Optional[str],
    merchant_url_by_id: Dict[str, Optional[str]],
) -> Tuple[
    Optional[str],
    Optional[Decimal],
    Optional[Decimal],
    int,
    List[Dict[str, Any]],
]:
    """Compute price aggregates + top-N offers. Stable ordering:
    primary merchant first, then price ASC, then merchant_id ASC.
    """
    normalized: List[Dict[str, Any]] = []
    for o in offers:
        n = normalize_offer(o, primary_merchant_id)
        if not n:
            continue
        merchant_id = n.get("merchant_id") or ""
        n["url"] = merchant_url_by_id.get(merchant_id)
        normalized.append(n)

    if not normalized:
        return None, None, None, 0, []

    # Modal currency: dominant one across the group. Mixed-currency
    # groups (rare but real — .com vs .ca) surface the dominant code at
    # the row level; each offer keeps its own currency intact.
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

    def sort_key(o: Dict[str, Any]) -> Tuple[int, float, str]:
        return (
            0 if o.get("is_primary") else 1,
            float(o.get("price") or 0.0),
            o.get("merchant_id") or "",
        )

    top = sorted(normalized, key=sort_key)[:OFFER_TOP_N]
    return currency, price_min, price_max, len(normalized), top


def aggregate_variants(skus: List[Dict[str, Any]], canonical_source_product_id: Optional[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Build variants[]. Drop the "singleton placeholder" SKU where
    source_variant_id == source_product_id (Path A sync inserts one such
    row per product to keep SKU joins valid even when upstream has no
    variants — not a real variant).
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

    # Deterministic ordering: SKU then variant_id. Keeps re-runs
    # byte-identical for diff-friendly admin tooling.
    variants.sort(key=lambda v: (v.get("sku") or "", str(v.get("variant_id") or "")))

    count = len(variants)
    return variants[:VARIANT_CAP], count


def pick_gtin13(skus: List[Dict[str, Any]]) -> Optional[str]:
    """Pick the canonical 14-char GTIN for the content_key group.

    Two failure modes to avoid:
      1. agent_pdp_view.gtin13 is VARCHAR(14); normalize_gtin passes
         15+ digit malformed inputs through unchanged. Skip those —
         they aren't valid GTIN-14.
      2. SKUs in the same content_key group can carry different
         barcodes (data noise, or genuine cross-merchant disagreement).
         Pick the modal value so we deterministically converge on the
         GTIN that appears most often; tiebreak by lex order so re-runs
         are byte-identical.
    """
    counts: Dict[str, int] = {}
    for s in skus:
        bar = s.get("barcode")
        if not bar:
            continue
        canon = normalize_gtin(str(bar))
        if not canon or len(canon) != 14:
            continue
        counts[canon] = counts.get(canon, 0) + 1
    if not counts:
        return None
    # Sort by count DESC, then GTIN ASC for stable tiebreak.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def build_taxonomy_tags(canonical: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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


def build_breadcrumb(canonical: Dict[str, Any], pdp_url: Optional[str]) -> Optional[List[Dict[str, Any]]]:
    title = coalesce_first(canonical.get("title"))
    if not title:
        return None
    crumbs: List[Dict[str, Any]] = [
        {"position": 1, "name": "Home", "item": "https://agent.pivota.cc/"},
    ]
    category = coalesce_first(canonical.get("category"), canonical.get("product_type"))
    if category:
        crumbs.append({"position": len(crumbs) + 1, "name": category, "item": None})
    brand = coalesce_first(canonical.get("brand"))
    if brand:
        crumbs.append({"position": len(crumbs) + 1, "name": brand, "item": None})
    crumbs.append({"position": len(crumbs) + 1, "name": title, "item": pdp_url})
    return crumbs


def assemble_row(
    *,
    content_key: str,
    products: List[Dict[str, Any]],
    skus: List[Dict[str, Any]],
    offers: List[Dict[str, Any]],
    external_seed: Optional[Dict[str, Any]],
    refresh_source: str = BACKFILL_REFRESH_SOURCE,
) -> Optional[Dict[str, Any]]:
    """Produce the agent_pdp_view row payload from raw source rows.
    Returns None when the data is too thin to be useful (no title).
    Side-effect-free: caller persists.
    """
    canonical = pick_canonical(products)
    title = coalesce_first(canonical.get("title"))
    if not title:
        return None

    seed_data = (external_seed or {}).get("seed_data") or {}
    if isinstance(seed_data, str):
        try:
            seed_data = json.loads(seed_data)
        except Exception:
            seed_data = {}

    description = coalesce_first(
        canonical.get("description"),
        seed_data.get("description"),
        seed_data.get("short_description"),
    )

    image_url = coalesce_first(
        canonical.get("image_url"),
        (external_seed or {}).get("image_url"),
        seed_data.get("image_url"),
    )

    image_urls = read_image_urls(canonical.get("product_payload"))
    if image_url and image_url not in image_urls:
        image_urls.insert(0, image_url)
    seed_images = seed_data.get("image_urls") if isinstance(seed_data, dict) else None
    if isinstance(seed_images, list):
        for u in seed_images:
            if isinstance(u, str) and u.strip() and u.strip() not in image_urls:
                image_urls.append(u.strip())

    product_group_id = coalesce_first(
        canonical.get("product_group_id"),
        *[p.get("product_group_id") for p in products],
    )

    primary_merchant_id = None
    for p in products:
        if p.get("group_is_primary"):
            primary_merchant_id = p.get("merchant_id")
            break
    if not primary_merchant_id:
        primary_merchant_id = canonical.get("merchant_id")

    merchant_url_by_id: Dict[str, Optional[str]] = {}
    for p in products:
        mid = p.get("merchant_id")
        if mid and mid not in merchant_url_by_id:
            merchant_url_by_id[mid] = p.get("canonical_url")

    currency, price_min, price_max, offer_count, top_offers = aggregate_offers(
        offers, primary_merchant_id, merchant_url_by_id
    )

    variants_capped, variants_count = aggregate_variants(
        skus, canonical.get("source_product_id")
    )
    gtin13 = pick_gtin13(skus)

    sig = canonical.get("pivota_signature_id")
    pdp_url = f"{PDP_URL_PREFIX}{sig}" if sig else None
    breadcrumb = build_breadcrumb(canonical, pdp_url)
    taxonomy_tags = build_taxonomy_tags(canonical)
    category_path = coalesce_first(canonical.get("category"), canonical.get("product_type"))

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
        "refresh_source": refresh_source,
    }


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


def to_jsonb(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, default=str)


def row_to_upsert_params(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project assemble_row() output to the bind-parameter dict shape
    UPSERT_SQL expects (JSONB columns → JSON-encoded strings)."""
    params = dict(row)
    params["image_urls"] = to_jsonb(row.get("image_urls"))
    params["offers"] = to_jsonb(row.get("offers"))
    params["variants"] = to_jsonb(row.get("variants"))
    params["taxonomy_tags"] = to_jsonb(row.get("taxonomy_tags"))
    params["breadcrumb"] = to_jsonb(row.get("breadcrumb"))
    return params
