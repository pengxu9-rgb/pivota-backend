from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, delete, desc, func, select

from adapters.product_adapters import ShopifyProductAdapter
from db.canonical_commerce import (
    canonical_inventory_snapshots,
    canonical_offers,
    canonical_product_sources,
    canonical_products,
    canonical_variants,
)
from db.database import database
from db.products import get_cached_products
from services.offer_buyability import IN_STOCK_AVAILABILITY
from models.standard_product import StandardProduct, StandardProductVariant

logger = logging.getLogger(__name__)


def _model_dump(model: Any) -> Dict[str, Any]:
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        payload = dump()
    else:
        payload = model.dict()
    return _make_json_safe(payload)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _make_json_safe(payload: Any) -> Any:
    return json.loads(json.dumps(payload, default=_json_default, ensure_ascii=False))


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=_json_default, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: Any) -> str:
    normalized = "|".join(str(part or "").strip().lower() for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def make_canonical_product_id(merchant_id: str, platform: str, platform_product_id: str) -> str:
    return _stable_id("cp", merchant_id, platform, platform_product_id)


def make_canonical_variant_id(
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    platform_variant_id: str,
) -> str:
    return _stable_id("cv", merchant_id, platform, platform_product_id, platform_variant_id)


def make_canonical_offer_id(canonical_variant_id: str, currency: Optional[str]) -> str:
    return _stable_id("co", canonical_variant_id, currency or "usd")


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _as_variant_payload(variant: StandardProductVariant, canonical_variant_id: str) -> Dict[str, Any]:
    payload = _model_dump(variant)
    payload["canonical_variant_id"] = canonical_variant_id
    return payload


def _coerce_variants(product: StandardProduct) -> List[StandardProductVariant]:
    if product.variants:
        return list(product.variants)
    return [
        StandardProductVariant(
            id=str(product.id),
            title="Default",
            sku=product.sku,
            barcode=product.barcode,
            price=float(product.price or 0),
            compare_at_price=float(product.compare_at_price) if product.compare_at_price is not None else None,
            inventory_quantity=int(product.inventory_quantity or 0),
            options={},
            image_url=product.image_url,
            visible_option_labels=[],
            platform_metadata=product.platform_metadata or {},
        )
    ]


def standard_product_from_record_payload(merchant_id: str, product_data: Any) -> Optional[StandardProduct]:
    payload = product_data
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            logger.warning("Skipping unreadable product payload for merchant=%s", merchant_id)
            return None
    if not isinstance(payload, dict):
        return None

    if all(key in payload for key in ("id", "merchant_id", "platform", "price")):
        try:
            return StandardProduct(**payload)
        except Exception:
            logger.warning("Skipping invalid StandardProduct payload for merchant=%s", merchant_id, exc_info=True)
            return None

    raw = payload.get("raw")
    if isinstance(raw, dict):
        try:
            return ShopifyProductAdapter.convert_to_standard(raw, merchant_id)
        except Exception:
            logger.warning("Skipping unreadable Shopify raw payload for merchant=%s", merchant_id, exc_info=True)
            return None
    return None


def standard_product_from_cache_row(row: Dict[str, Any]) -> Optional[StandardProduct]:
    merchant_id = str(row.get("merchant_id") or "").strip()
    return standard_product_from_record_payload(merchant_id, row.get("product_data"))


def _build_cache_like_row(
    product_row: Dict[str, Any],
    variants_by_product: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    payload = dict(product_row.get("standard_product_data") or {})
    payload["canonical_product_id"] = product_row["canonical_product_id"]

    product_variants = []
    for variant in variants_by_product.get(product_row["canonical_product_id"], []):
        variant_payload = dict(variant.get("standard_variant_data") or {})
        variant_payload["canonical_variant_id"] = variant["canonical_variant_id"]
        product_variants.append(variant_payload)
    if product_variants:
        payload["variants"] = product_variants

    return {
        "merchant_id": product_row.get("merchant_id"),
        "platform": product_row.get("platform"),
        "platform_product_id": product_row.get("platform_product_id"),
        "product_data": payload,
        "cached_at": product_row.get("source_recorded_at") or product_row.get("updated_at"),
        "expires_at": product_row.get("expires_at"),
        "canonical_product_id": product_row.get("canonical_product_id"),
    }


async def load_canonical_cache_rows(
    *,
    merchant_id: str,
    platform: Optional[str] = None,
    include_expired: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    conditions = [canonical_products.c.merchant_id == merchant_id]
    if platform:
        conditions.append(canonical_products.c.platform == platform)
    if not include_expired:
        conditions.append(
            (canonical_products.c.expires_at.is_(None)) | (canonical_products.c.expires_at > datetime.now(timezone.utc))
        )

    query = (
        select(canonical_products)
        .where(and_(*conditions))
        .order_by(desc(canonical_products.c.source_recorded_at), desc(canonical_products.c.updated_at))
        .offset(offset)
    )
    if limit is not None:
        query = query.limit(limit)

    try:
        rows = await database.fetch_all(query)
    except Exception as exc:
        if "canonical_products" in str(exc):
            logger.warning("canonical_products lookup unavailable; falling back to products_cache")
            return []
        raise
    if not rows:
        return []

    product_rows = [dict(row) for row in rows]
    if not all("canonical_product_id" in row for row in product_rows):
        logger.warning("Canonical lookup returned non-canonical rows; falling back to products_cache")
        return []
    product_ids = [row["canonical_product_id"] for row in product_rows]
    try:
        variant_rows = await database.fetch_all(
            select(canonical_variants)
            .where(canonical_variants.c.canonical_product_id.in_(product_ids))
            .order_by(canonical_variants.c.platform_variant_id.asc())
        )
    except Exception as exc:
        if "canonical_variants" in str(exc):
            logger.warning("canonical_variants lookup unavailable; returning product-only canonical rows")
            variant_rows = []
        else:
            raise
    variants_by_product: Dict[str, List[Dict[str, Any]]] = {}
    for row in variant_rows:
        payload = dict(row)
        variants_by_product.setdefault(payload["canonical_product_id"], []).append(payload)

    return [_build_cache_like_row(row, variants_by_product) for row in product_rows]


async def load_canonical_cache_row(
    *,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
) -> Optional[Dict[str, Any]]:
    rows = await load_canonical_cache_rows(
        merchant_id=merchant_id,
        platform=platform,
        include_expired=True,
    )
    for row in rows:
        if str(row.get("platform_product_id")) == str(platform_product_id):
            return row
    return None


async def upsert_canonical_product(
    product: StandardProduct,
    *,
    source_name: str,
    source_type: str,
    source_recorded_at: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
    raw_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    canonical_product_id = make_canonical_product_id(product.merchant_id, product.platform, product.product_id or product.id)
    product_payload = _model_dump(product)
    product_payload["canonical_product_id"] = canonical_product_id
    product_payload_hash = _hash_payload(product_payload)

    product_values = {
        "canonical_product_id": canonical_product_id,
        "merchant_id": product.merchant_id,
        "platform": product.platform,
        "platform_product_id": str(product.product_id or product.id),
        "title": product.title,
        "description": product.description,
        "brand": product.vendor,
        "category": product.product_type,
        "default_image_url": product.image_url,
        "status": str(product.status) if product.status is not None else None,
        "orderable": product.orderable,
        "currency": product.currency,
        "visible_attributes": product.visible_attributes or {},
        "ingredient_ids": product.ingredient_ids or [],
        "standard_product_data": product_payload,
        "source_payload_hash": product_payload_hash,
        "source_recorded_at": source_recorded_at,
        "expires_at": expires_at,
    }

    variants = _coerce_variants(product)

    async with database.transaction():
        existing = await database.fetch_one(
            select(canonical_products.c.canonical_product_id).where(
                canonical_products.c.canonical_product_id == canonical_product_id
            )
        )
        if existing:
            await database.execute(
                canonical_products.update()
                .where(canonical_products.c.canonical_product_id == canonical_product_id)
                .values(**product_values)
            )
        else:
            await database.execute(canonical_products.insert().values(**product_values))

        await database.execute(
            delete(canonical_offers).where(canonical_offers.c.canonical_product_id == canonical_product_id)
        )
        await database.execute(
            delete(canonical_variants).where(canonical_variants.c.canonical_product_id == canonical_product_id)
        )

        for variant in variants:
            canonical_variant_id = make_canonical_variant_id(
                product.merchant_id,
                product.platform,
                str(product.product_id or product.id),
                str(variant.variant_id or variant.id),
            )
            variant_payload = _as_variant_payload(variant, canonical_variant_id)
            variant_hash = _hash_payload(variant_payload)

            await database.execute(
                canonical_variants.insert().values(
                    canonical_variant_id=canonical_variant_id,
                    canonical_product_id=canonical_product_id,
                    merchant_id=product.merchant_id,
                    platform=product.platform,
                    platform_product_id=str(product.product_id or product.id),
                    platform_variant_id=str(variant.variant_id or variant.id),
                    title=variant.title,
                    sku=variant.sku,
                    barcode=variant.barcode,
                    currency=product.currency,
                    option_values=variant.options or {},
                    visible_option_labels=variant.visible_option_labels or [],
                    image_url=variant.image_url,
                    standard_variant_data=variant_payload,
                    source_payload_hash=variant_hash,
                    source_recorded_at=source_recorded_at,
                )
            )

            availability = "in_stock" if int(variant.inventory_quantity or 0) > 0 and bool(product.orderable) else "out_of_stock"
            await database.execute(
                canonical_offers.insert().values(
                    canonical_offer_id=make_canonical_offer_id(canonical_variant_id, product.currency),
                    canonical_product_id=canonical_product_id,
                    canonical_variant_id=canonical_variant_id,
                    merchant_id=product.merchant_id,
                    currency=product.currency or "USD",
                    amount=Decimal(str(variant.price or 0)),
                    compare_at_amount=Decimal(str(variant.compare_at_price)) if variant.compare_at_price is not None else None,
                    availability=availability,
                    orderable=product.orderable,
                    checkout_url=None,
                    source_payload_hash=variant_hash,
                    source_recorded_at=source_recorded_at,
                )
            )
            await database.execute(
                canonical_inventory_snapshots.insert().values(
                    canonical_product_id=canonical_product_id,
                    canonical_variant_id=canonical_variant_id,
                    merchant_id=product.merchant_id,
                    quantity=int(variant.inventory_quantity or 0),
                    availability=availability,
                    observed_at=source_recorded_at,
                    source=source_name,
                    stale=False,
                    source_payload_hash=variant_hash,
                )
            )
            await database.execute(
                canonical_product_sources.insert().values(
                    canonical_product_id=canonical_product_id,
                    canonical_variant_id=canonical_variant_id,
                    merchant_id=product.merchant_id,
                    platform=product.platform,
                    platform_product_id=str(product.product_id or product.id),
                    platform_variant_id=str(variant.variant_id or variant.id),
                    source_type=source_type,
                    source_name=source_name,
                    source_recorded_at=source_recorded_at,
                    payload_hash=variant_hash,
                    raw_payload=_make_json_safe(raw_payload or variant_payload),
                    is_primary=True,
                )
            )

        await database.execute(
            canonical_product_sources.insert().values(
                canonical_product_id=canonical_product_id,
                canonical_variant_id=None,
                merchant_id=product.merchant_id,
                platform=product.platform,
                platform_product_id=str(product.product_id or product.id),
                platform_variant_id=None,
                source_type=source_type,
                source_name=source_name,
                source_recorded_at=source_recorded_at,
                payload_hash=product_payload_hash,
                raw_payload=_make_json_safe(raw_payload or product_payload),
                is_primary=True,
            )
        )

    return {
        "canonical_product_id": canonical_product_id,
        "canonical_variant_count": len(variants),
        "platform": product.platform,
        "platform_product_id": str(product.product_id or product.id),
    }


async def backfill_canonical_products_from_cache(
    *,
    merchant_id: str,
    platform: str = "shopify",
    include_expired: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    rows = await get_cached_products(
        merchant_id=merchant_id,
        platform=platform,
        include_expired=include_expired,
    )
    processed = 0
    skipped = 0
    canonical_product_ids: List[str] = []
    for row in rows[: limit or len(rows)]:
        product = standard_product_from_cache_row(row)
        if not product:
            skipped += 1
            continue
        result = await upsert_canonical_product(
            product,
            source_name="products_cache.standard_product.v1",
            source_type="products_cache",
            source_recorded_at=_coerce_datetime(row.get("cached_at")) or _coerce_datetime(product.updated_at),
            expires_at=_coerce_datetime(row.get("expires_at")),
            raw_payload=row.get("product_data") if isinstance(row.get("product_data"), dict) else None,
        )
        canonical_product_ids.append(result["canonical_product_id"])
        processed += 1
    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "processed": processed,
        "skipped": skipped,
        "canonical_product_ids": canonical_product_ids,
    }


async def canonical_cache_parity(
    *,
    merchant_id: str,
    platform: str = "shopify",
    include_expired: bool = True,
) -> Dict[str, Any]:
    cache_rows = await get_cached_products(
        merchant_id=merchant_id,
        platform=platform,
        include_expired=include_expired,
    )
    canonical_rows = await load_canonical_cache_rows(
        merchant_id=merchant_id,
        platform=platform,
        include_expired=include_expired,
    )
    cache_ids = sorted({str(row.get("platform_product_id") or "") for row in cache_rows if row.get("platform_product_id")})
    canonical_ids = sorted({str(row.get("platform_product_id") or "") for row in canonical_rows if row.get("platform_product_id")})
    return {
        "merchant_id": merchant_id,
        "platform": platform,
        "cache_product_count": len(cache_ids),
        "canonical_product_count": len(canonical_ids),
        "missing_in_canonical": [item for item in cache_ids if item not in canonical_ids],
        "missing_in_cache": [item for item in canonical_ids if item not in cache_ids],
        "matched": cache_ids == canonical_ids,
    }


# =====================================================================
# UCP probe variant selection
# =====================================================================

_PROBE_VARIANT_GID_PREFIX = "gid://shopify/ProductVariant/"


async def select_probe_variant_gid(merchant_id: str) -> Optional[str]:
    """One Shopify variant GID for a merchant, for the Store Audit UCP probe.

    WHY THIS EXISTS. services/ucpStoreAuditProbe.js (gateway) reaches its
    `tested` tier — the one that records `priced_facts.checkout_status`, the
    only signal in the lane that separates a store an agent can actually buy
    from from one that merely advertises UCP — ONLY when the claim hands it a
    variant_gid. That gid comes from verification_runs.product_key, and no
    enqueue path has ever set it. So the tested tier has never executed in
    production and every route in the system sits at `detected`.

    DETERMINISTIC BY CONSTRUCTION. The probe's one sanctioned side effect is a
    single create_checkout against the merchant's own store, so repeated
    reprobes must keep landing on the SAME variant rather than spraying
    abandoned checkouts across the catalogue. Do not make this "pick a
    random/newest variant" — the stability is the point.

    ORDER BY (length, value), NOT by the raw string. platform_variant_id is a
    String column holding Shopify's monotonically increasing integer ids, and
    plain lexicographic order is NOT stable as a catalogue grows: a newly
    ingested 14-digit `41...` sorts before an existing 13-digit `42...` and
    silently steals the pick. Ordering on length first makes this numeric order
    for digit-only ids, so the choice is "the merchant's oldest variant" — which
    new ingests cannot displace. func.length is the portable spelling; a numeric
    CAST would be neither portable nor safe against the non-numeric ids the
    guard below still has to reject.

    Returns None whenever a purchasable Shopify variant cannot be named, which
    puts the probe back on exactly today's behaviour (detected tier).
    """
    merchant = str(merchant_id or "").strip()
    if not merchant:
        return None

    # Availability lives on canonical_offers, the variant identity on
    # canonical_variants; a variant with no offer row has no availability to
    # judge, so the join is inner on purpose.
    query = (
        select(canonical_variants.c.platform_variant_id)
        .select_from(
            canonical_variants.join(
                canonical_offers,
                and_(
                    canonical_offers.c.canonical_variant_id
                    == canonical_variants.c.canonical_variant_id,
                    canonical_offers.c.merchant_id
                    == canonical_variants.c.merchant_id,
                ),
            )
        )
        .where(
            and_(
                canonical_variants.c.merchant_id == merchant,
                canonical_variants.c.platform == "shopify",
                func.lower(func.trim(canonical_offers.c.availability)).in_(
                    sorted(IN_STOCK_AVAILABILITY)
                ),
                # `orderable` is folded into availability on the write path
                # (see upsert_canonical_product), but an offer that says
                # outright that it cannot be ordered must not be handed to a
                # probe that will try to check out with it. NULL is unknown,
                # not False, and stays eligible.
                canonical_offers.c.orderable.isnot(False),
            )
        )
        .order_by(
            func.length(canonical_variants.c.platform_variant_id).asc(),
            canonical_variants.c.platform_variant_id.asc(),
        )
        .limit(1)
    )
    try:
        row = await database.fetch_one(query)
    except Exception:
        # Never let a catalogue lookup fail an enqueue: no variant is a
        # supported outcome (the probe stays on its detected tier), an
        # unhandled exception here would drop the reprobe entirely.
        logger.warning(
            "select_probe_variant_gid: lookup failed for merchant=%s",
            merchant, exc_info=True,
        )
        return None
    if not row:
        return None

    # The claim endpoint only forwards a value that already starts with the GID
    # prefix, and merchant_ucp_checkout rejects a non-numeric id upstream. A
    # non-numeric platform_variant_id here would therefore travel as far as the
    # merchant's door before being refused — reject it at the source instead.
    variant_id = str(row["platform_variant_id"] or "").strip()
    if not variant_id.isdigit():
        return None
    return f"{_PROBE_VARIANT_GID_PREFIX}{variant_id}"
