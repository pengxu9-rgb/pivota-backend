"""Project missing mirror-variant offers from stored, product-scoped evidence.

No fetches, SKU creation, price-freshness stamps, or checkout-readiness promotion.
Existing offers (including withdrawn offers) are never overwritten or revived.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import re
from urllib.parse import urlsplit

from db.database import database
from services.catalog_enrichment_agent.ingestion import variant_own_price
from services.catalog_offer_writer_guard import guard_catalog_offer_rows
from services.catalog_offer_link_repair import plan_hash
from services.variant_identity import MERCHANT_ISSUED, variant_id_provenance
from services.offer_seller_identity import normalize_host
from services.seller_identity import resolve_seed_seller_identity

SOURCE = "variant_offer_projection_v1"
MIRROR = "external_product_seeds_mirror_v1"

#: The kill switch for the two automatic callers of `project_missing_variant_offers` (the variant
#: promoter and the external-offer dual write). DEFAULT ON, which is what production ran before
#: the switch existed; only "0", "false" or "off" (any case) turn it off. Read on every call, so
#: an operator can stop the projection without a deploy. Direct callers (repair scripts, tests)
#: are not gated: they asked for the projection by name.
PROJECTION_ENABLED_ENV = "CATALOG_VARIANT_OFFER_PROJECTION_ENABLED"
_PROJECTION_OFF = frozenset({"0", "false", "off"})


def projection_enabled() -> bool:
    return os.getenv(PROJECTION_ENABLED_ENV, "").strip().lower() not in _PROJECTION_OFF
PRODUCT_SQL = """
SELECT product_key, merchant_id, source_product_id, source_domain, source_ref, brand
FROM catalog_products
WHERE product_key=:pk AND source_system=:src AND platform='external_seed'
AND suppressed_at IS NULL AND suppression_reason IS NULL
"""
SEED_SQL = """
SELECT seed_data, seller_ref, domain, canonical_url, destination_url, market FROM external_product_seeds
WHERE attached_product_key=:pk AND status='active' AND id::text=:seed_id
"""
SKUS_SQL = """
SELECT sku_key, source_variant_id, currency FROM catalog_skus
WHERE product_key=:pk AND merchant_id=:merchant AND platform='external_seed'
AND sku_key NOT LIKE '%::canonical'
AND suppressed_at IS NULL AND suppression_reason IS NULL
"""
TEMPLATES_SQL = """
SELECT merchant_id, currency, market, source_domain, offer_type, is_first_party,
       why_buy_direct, offer_payload, source_ref
FROM catalog_offers
WHERE product_key=:pk AND (sku_key=:canonical OR (offer_id=:mirror_offer_id AND source_system=:mirror_source)) AND merchant_id=:merchant
AND suppressed_at IS NULL AND suppression_reason IS NULL
ORDER BY offer_id
"""
EXISTING_SQL = """
SELECT sku_key, merchant_id, market, currency, suppressed_at, suppression_reason FROM catalog_offers WHERE product_key=:pk
"""
INSERT_SQL = """
INSERT INTO catalog_offers
(offer_id,sku_key,product_key,merchant_id,catalog_track,truth_tier,readiness_tier,
 offer_mode,channel,availability,currency,market,list_price,merchant_effective_price,
 estimated_best_price,source_system,source_domain,source_ref,offer_type,is_first_party,
 why_buy_direct,offer_payload)
SELECT :offer_id,CAST(:sku_key AS VARCHAR),CAST(:product_key AS VARCHAR),CAST(:merchant_id AS VARCHAR),'external_referral','observed',
 'referral_only','redirect','external_referral',:availability,CAST(:currency AS VARCHAR),CAST(:market AS VARCHAR),
 :list_price,:merchant_effective_price,:estimated_best_price,:source_system,
 :source_domain,:source_ref,:offer_type,:is_first_party,:why_buy_direct,
 CAST(:offer_payload AS jsonb)
WHERE NOT EXISTS (SELECT 1 FROM catalog_offers WHERE product_key=CAST(:product_key AS VARCHAR)
 AND sku_key=CAST(:sku_key AS VARCHAR) AND merchant_id=CAST(:merchant_id AS VARCHAR)
 AND (market=CAST(:market AS VARCHAR) OR suppressed_at IS NOT NULL OR suppression_reason IS NOT NULL))
ON CONFLICT (offer_id) DO NOTHING RETURNING offer_id
"""


def as_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def variants_from_seed(seed):
    seed = as_json(seed)
    if not isinstance(seed, dict):
        return []
    variants = seed.get("variants")
    if not isinstance(variants, list) or not variants:
        snapshot = as_json(seed.get("snapshot"))
        variants = snapshot.get("variants") if isinstance(snapshot, dict) else None
    return [v for v in variants or [] if isinstance(v, dict)] if isinstance(variants, list) else []


def listing_identity(value):
    try:
        url = urlsplit(value or "")
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.port not in (None, 443)
            or not url.path
        ):
            return None
        return normalize_host(url.hostname), url.path.rstrip("/") or "/"
    except (ValueError, TypeError):
        return None


def seed_scope(product, seed):
    """Validate the current listing, not only its historic attachment to a product."""
    domain = normalize_host(product.get("source_domain"))
    declared = normalize_host(seed.get("domain"))
    urls = [seed.get(k) for k in ("destination_url", "canonical_url") if seed.get(k)]
    listings = [listing_identity(value) for value in urls]
    if (
        not domain
        or declared not in (None, domain)
        or not listings
        or any(item is None or item[0] != domain for item in listings)
        or not seed.get("market")
    ):
        return None
    seller = str(seed.get("seller_ref") or "").strip()
    if not seller:
        data = as_json(seed.get("seed_data"))
        data = data if isinstance(data, dict) else {}
        snapshot = as_json(data.get("snapshot"))
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        brand = snapshot.get("brand") or data.get("brand") or product.get("brand")
        try:
            seller = resolve_seed_seller_identity(brand=brand, domain=domain)["merchant_id"]
        except ValueError:
            return None
    if seller != product["merchant_id"]:
        return None
    return {"seed_listing_identities": set(listings), "seed_market": seed["market"]}


def own_availability(variant):
    mapping = {
        "in_stock": "in_stock",
        "instock": "in_stock",
        "available": "in_stock",
        "out_of_stock": "out_of_stock",
        "outofstock": "out_of_stock",
        "sold_out": "out_of_stock",
        "unavailable": "out_of_stock",
        "low_stock": "low_stock",
        "limited": "low_stock",
    }
    states = {
        mapping.get(str(variant.get(key) or "").strip().lower().replace("-", "_").replace(" ", "_"), "unknown")
        for key in ("availability", "stock")
    }
    # An explicit unavailability signal wins over any contradictory positive signal.
    if variant.get("available") is False or "out_of_stock" in states:
        return "out_of_stock"
    if "low_stock" in states:
        return "low_stock"
    if variant.get("available") is True or "in_stock" in states:
        return "in_stock"
    return "unknown"


def plan_offers(product, variants, skus, templates, existing):
    """Own-price projection. Ambiguous IDs or seller/market templates are refused."""
    counts = collections.Counter()
    by_id = collections.defaultdict(list)
    for variant in variants:
        vid = str(variant.get("variant_id") or variant.get("id") or "").strip()
        by_id[vid].append(variant)
    prior = {(o["sku_key"], o["merchant_id"], o["market"]) for o in existing}
    withdrawn = {
        (o["sku_key"], o["merchant_id"]) for o in existing if o.get("suppressed_at") or o.get("suppression_reason")
    }
    rows = []
    for sku in skus:
        vid = str(sku.get("source_variant_id") or "").strip()
        if (
            len(vid) > 128
            or variant_id_provenance(
                vid, product_id=product.get("source_product_id"), product_key=product["product_key"]
            )
            != MERCHANT_ISSUED
        ):
            counts["unverified_variant_identity"] += 1
            continue
        candidates = by_id.get(vid, [])
        if len(candidates) != 1:
            counts["missing_or_ambiguous_variant"] += 1
            continue
        variant = candidates[0]
        # Numeric and Shopify GID aliases must identify the same physical variant.
        # A label match or one preferred field must never override a conflicting ID.
        aliases = set()
        for key in ("variant_id", "id", "shopify_variant_id"):
            claim = re.fullmatch(r"(?:gid://shopify/ProductVariant/)?([0-9]+)", str(variant.get(key) or "").strip())
            if claim:
                aliases.add(claim.group(1))
        if aliases != {vid.rsplit("/", 1)[-1]}:
            counts["conflicting_variant_identity"] += 1
            continue
        price = variant_own_price(variant)
        currency = str(variant.get("price_currency") or variant.get("currency") or "").strip().upper()
        if (
            price is None
            or not math.isfinite(price)
            or price > 9999999999.99
            or round(price, 2) <= 0
            or not currency
            or currency != sku.get("currency")
        ):
            counts["missing_own_price_or_currency_mismatch"] += 1
            continue
        matching = [
            t for t in templates if t.get("currency") == currency and t.get("merchant_id") == product["merchant_id"]
        ]
        # Multiple templates for one market are ambiguous, even if they look similar.
        scoped = collections.defaultdict(list)
        for template in matching:
            scoped[template.get("market")].append(template)
        if not scoped:
            counts["no_matching_canonical_template"] += 1
        for market, choices in scoped.items():
            identity = (sku["sku_key"], product["merchant_id"], market)
            if identity in prior or (sku["sku_key"], product["merchant_id"]) in withdrawn:
                counts["existing_offer_preserved"] += 1
                continue
            if len(choices) != 1 or not market:
                counts["ambiguous_or_missing_market_template"] += 1
                continue
            template = choices[0]
            payload = as_json(template.get("offer_payload"))
            if not isinstance(payload, dict):
                counts["destination_or_seller_mismatch"] += 1
                continue
            destination = payload.get("destination_url") or payload.get("canonical_url")
            try:
                url = urlsplit(destination or "")
                valid = (
                    url.scheme == "https"
                    and url.hostname == product.get("source_domain")
                    and not url.username
                    and not url.password
                    and url.port in (None, 443)
                    and bool(url.path)
                )
            except (ValueError, TypeError):
                valid = False
            if (
                not valid
                or template.get("source_domain") != product.get("source_domain")
                or ("seed_market" in product and market != product["seed_market"])
                or (
                    "seed_listing_identities" in product
                    and listing_identity(destination) not in product["seed_listing_identities"]
                )
            ):
                counts["destination_or_seller_mismatch"] += 1
                continue
            row = dict(
                offer_id="offer:variant-projection:"
                + hashlib.sha256(
                    json.dumps(
                        [product["product_key"], sku["sku_key"], product["merchant_id"], market, currency, destination],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()[:40],
                sku_key=sku["sku_key"],
                product_key=product["product_key"],
                merchant_id=product["merchant_id"],
                availability=own_availability(variant),
                currency=currency,
                market=market,
                list_price=price,
                merchant_effective_price=price,
                estimated_best_price=price,
                source_system=SOURCE,
                source_domain=product["source_domain"],
                source_ref=destination,
                offer_type=template.get("offer_type"),
                is_first_party=bool(template.get("is_first_party")),
                why_buy_direct=template.get("why_buy_direct"),
                offer_payload=json.dumps(
                    {
                        "source_system": SOURCE,
                        "external_seed_id": product["source_ref"],
                        "variant_id": vid,
                        "variant_id_provenance": MERCHANT_ISSUED,
                        "price_from": "variant",
                        "destination_url": destination,
                        "price_freshness": "stored_snapshot_only",
                    }
                ),
            )
            rows.append(row)
    return rows, dict(counts)


async def project_missing_variant_offers(product_key, *, apply=False, db=None, include_manifest=False, expected_plan_hash=None):
    db = db or database
    def refuse(reason):
        if apply and expected_plan_hash is not None:
            raise RuntimeError("variant_offer_product_plan_changed")
        return {"planned": 0, "inserted": 0, "skips": {reason: 1}}
    async with db.transaction():
        product = await db.fetch_one(PRODUCT_SQL + (" FOR UPDATE" if apply else ""), {"pk": product_key, "src": MIRROR})
        if not product:
            return refuse("product_not_live_mirror")
        product = dict(product)
        if product["merchant_id"] == "external_seed" or not product.get("source_ref"):
            return refuse("seller_or_seed_missing")
        seeds = await db.fetch_all(
            SEED_SQL + (" FOR SHARE" if apply else ""), {"pk": product_key, "seed_id": product["source_ref"]}
        )
        if len(seeds) != 1:
            return refuse("active_attached_seed_missing")
        scope = seed_scope(product, dict(seeds[0]))
        if scope is None:
            return refuse("seed_listing_or_seller_mismatch")
        product.update(scope)
        skus = [
            dict(s)
            for s in await db.fetch_all(
                SKUS_SQL + (" FOR UPDATE" if apply else ""), {"pk": product_key, "merchant": product["merchant_id"]}
            )
        ]
        templates = [
            dict(t)
            for t in await db.fetch_all(
                TEMPLATES_SQL + (" FOR UPDATE" if apply else ""),
                {"pk": product_key, "canonical": product_key + "::canonical", "merchant": product["merchant_id"],
                 "mirror_offer_id": "offer:external_seed:" + hashlib.sha256(product_key.encode()).hexdigest()[:32],
                 "mirror_source": MIRROR},
            )
        ]
        existing = [dict(o) for o in await db.fetch_all(EXISTING_SQL, {"pk": product_key})]
        rows, skips = plan_offers(product, variants_from_seed(seeds[0]["seed_data"]), skus, templates, existing)
        accepted, rejected, _ = await guard_catalog_offer_rows(rows, db=db, live_only=True, require_live_links=True)
        counts = collections.Counter(skips)
        counts.update(rejected)
        digest = plan_hash(accepted)
        if apply and expected_plan_hash is not None and digest != expected_plan_hash:
            raise RuntimeError("variant_offer_product_plan_changed")
        inserted = 0
        if apply:
            for row in accepted:
                inserted += bool(await db.fetch_val(INSERT_SQL, row))
            if inserted:
                await db.execute(
                    """INSERT INTO writer_audit_log(writer_name,batch_id,applied_rows,skipped_rows,reasons,actor)
                 VALUES(:name,:batch,:inserted,:skipped,CAST(:reasons AS jsonb),:actor)""",
                    {
                        "name": SOURCE,
                        "batch": SOURCE + ":" + hashlib.sha256(product_key.encode()).hexdigest()[:32],
                        "inserted": inserted,
                        "skipped": sum(counts.values()),
                        "reasons": json.dumps(dict(counts)),
                        "actor": SOURCE,
                    },
                )
        return {"planned": len(accepted), "inserted": inserted, "skips": dict(counts),
                **({"manifest": accepted, "plan_sha256": digest} if include_manifest else {})}
