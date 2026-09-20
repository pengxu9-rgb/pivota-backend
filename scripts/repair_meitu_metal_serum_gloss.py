#!/usr/bin/env python3
"""Repair the reviewed attached JUNGSAEMMOOL SG gloss row in place.

Dry run by default. --apply performs one guarded transaction, including PDP,
eligibility, and trust refresh. This intentionally does not mint a seed mirror.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PRODUCT_KEY = "ext:jungsaemmool-lip-pression-metal-serum-gloss::66a3c8a4"
EXTERNAL_ID = "jungsaemmool:615e47aee567b863"
MERCHANT_ID = "merch_obs_88382424262f3e0f"
OFFER_MERCHANT_ID = "agent_seed::jungsaemmool"
URL = "https://jsmbeauty.sg/products/lip-pression-metal-serum-gloss"
OLD_PRICE = Decimal("28.20")
NEW_PRICE = Decimal("30.00")
REASON = "meitu_20260920_verified_sg_variant_repair"
CATEGORY_SOURCE = "meitu_sg_gloss_reviewed_0920"


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise RuntimeError(reason)


def as_dict(row):
    return dict(row) if row is not None else None


def load_json(value):
    return json.loads(value) if isinstance(value, str) else value


async def run(apply: bool) -> None:
    require(os.environ.get("PIVOTA_SERVING_PRICING_REGIONS") == "US,SG", "serving_regions_must_be_US_SG")
    from db.database import database
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
    from services.index_pipeline_state_service import recompute_serving_eligibility, serving_pricing_regions
    from services.catalog_row_trust_upserter import upsert_catalog_row_trust

    require(serving_pricing_regions() == ["US", "SG"], "serving_regions_import_mismatch")
    await database.connect()
    try:
        product = as_dict(await database.fetch_one("""
            SELECT product_key, content_key, merchant_id, source_system, source_product_id,
                   title, brand, canonical_url, category, product_type, category_path,
                   suppression_reason, suppressed_at, sync_status
            FROM catalog_products WHERE product_key = :key
        """, {"key": PRODUCT_KEY}))
        require(product is not None, "product_missing")
        require(product["merchant_id"] == MERCHANT_ID and
                product["source_system"] == "catalog_enrichment_agent_v1" and
                product["source_product_id"] == "jungsaemmool-lip-pression-metal-serum-gloss" and
                product["title"] == "LIP-PRESSION Metal Serum Gloss" and
                product["brand"] == "JUNGSAEMMOOL" and
                product["canonical_url"] == URL and
                product["category"] == "makeup" and
                product["product_type"] == "makeup" and
                product["category_path"] == "beauty/makeup" and
                product["suppression_reason"] is None and
                product["suppressed_at"] is None, "product_precondition_changed")

        seed = as_dict(await database.fetch_one("""
            SELECT id, external_product_id, market, domain, status, attached_product_key,
                   seller_ref, price_amount, price_currency, seed_data
            FROM external_product_seeds
            WHERE external_product_id = :external_id AND market = 'US'
        """, {"external_id": EXTERNAL_ID}))
        require(seed is not None and seed["status"] == "active" and
                seed["domain"] == "jsmbeauty.sg" and
                seed["attached_product_key"] == PRODUCT_KEY and
                seed["seller_ref"] == MERCHANT_ID and
                seed["price_currency"] == "SGD" and
                Decimal(str(seed["price_amount"])) == NEW_PRICE, "seed_precondition_changed")
        seed_data = load_json(seed["seed_data"])
        variants = seed_data.get("variants") or []
        variant_ids = [str(v.get("variant_id") or v.get("id") or "") for v in variants]
        require(len(variants) == 12 and len(set(variant_ids)) == 12 and all(variant_ids),
                "seed_variant_identity_changed")
        require(all(v.get("currency") == "SGD" and
                    Decimal(str(v.get("price"))) == NEW_PRICE for v in variants),
                "seed_variant_price_changed")
        require(seed_data.get("category") == "Lip Gloss" and
                (seed_data.get("snapshot") or {}).get("category") == "Lip Gloss",
                "seed_category_changed")

        chain = [dict(r) for r in await database.fetch_all("""
            SELECT s.sku_key, s.source_variant_id, s.merchant_id AS sku_merchant,
                   s.suppressed_at AS sku_suppressed,
                   o.offer_id, o.merchant_id AS offer_merchant, o.source_system AS offer_source,
                   o.currency, o.list_price, o.merchant_effective_price,
                   o.estimated_best_price, o.suppressed_at AS offer_suppressed
            FROM catalog_skus s
            LEFT JOIN catalog_offers o ON o.sku_key = s.sku_key AND o.product_key = s.product_key
            WHERE s.product_key = :key
            ORDER BY s.sku_key
        """, {"key": PRODUCT_KEY})]
        require(len(chain) == 13, "expected_13_sku_offer_pairs")
        live = [r for r in chain if str(r["source_variant_id"]) in variant_ids]
        placeholder = [r for r in chain if str(r["source_variant_id"]) == PRODUCT_KEY]
        require(len(live) == 12 and len(placeholder) == 1 and
                {str(r["source_variant_id"]) for r in live} == set(variant_ids),
                "sku_variant_set_mismatch")
        require(placeholder[0]["sku_key"] == PRODUCT_KEY + "::canonical" and
                placeholder[0]["offer_id"] == "offer:catalog_enrichment_agent_v1:734ef791e2bd961e",
                "canonical_placeholder_mismatch")
        bad = [r for r in chain if not (r["sku_merchant"] == MERCHANT_ID and
                    r["offer_merchant"] == OFFER_MERCHANT_ID and
                    r["offer_source"] == "catalog_enrichment_agent_v1" and
                    r["sku_suppressed"] is None and r["offer_suppressed"] is None and
                    r["currency"] == "SGD" and
                    all(Decimal(str(r[p])) == OLD_PRICE for p in
                        ("list_price", "merchant_effective_price", "estimated_best_price"))
                    )]
        require(not bad, "offer_precondition_changed:" + json.dumps([{
            "sku_key": r["sku_key"], "sku_merchant": r["sku_merchant"],
            "offer_merchant": r["offer_merchant"], "offer_source": r["offer_source"],
            "currency": r["currency"], "prices": [str(r[p]) for p in
                ("list_price", "merchant_effective_price", "estimated_best_price")],
            "sku_suppressed": str(r["sku_suppressed"]),
            "offer_suppressed": str(r["offer_suppressed"]),
        } for r in bad]))

        before = as_dict(await database.fetch_one("""
            SELECT serving_eligible FROM index_pipeline_state WHERE content_key = :ck
        """, {"ck": product["content_key"]}))
        print("REPAIR_PREFLIGHT=" + json.dumps({
            "apply": apply, "product_key": PRODUCT_KEY, "content_key": product["content_key"],
            "seed_id": seed["id"], "variant_offers": len(live),
            "synthetic_offers": len(placeholder),
            "before_serving_eligible": before and before["serving_eligible"],
            "old_price": str(OLD_PRICE), "new_price": str(NEW_PRICE),
        }, default=str))
        if not apply:
            return

        async with database.transaction():
            locked_seed = as_dict(await database.fetch_one("""
                SELECT status, attached_product_key, price_amount, price_currency, seed_data
                FROM external_product_seeds WHERE id = :id FOR UPDATE
            """, {"id": seed["id"]}))
            require(locked_seed is not None and locked_seed["status"] == "active" and
                    locked_seed["attached_product_key"] == PRODUCT_KEY and
                    locked_seed["price_currency"] == "SGD" and
                    Decimal(str(locked_seed["price_amount"])) == NEW_PRICE and
                    load_json(locked_seed["seed_data"]) == seed_data,
                    "seed_changed_since_preflight")
            updated = await database.fetch_all("""
                UPDATE catalog_products SET
                    category = 'Lip Gloss', product_type = 'Lip Gloss',
                    category_path = 'beauty/makeup/lip/gloss',
                    category_confidence = 0.95,
                    category_label_source = :category_source,
                    updated_at = NOW()
                WHERE product_key = :key AND content_key = :ck
                  AND merchant_id = :merchant AND source_system = 'catalog_enrichment_agent_v1'
                  AND source_product_id = 'jungsaemmool-lip-pression-metal-serum-gloss'
                  AND title = 'LIP-PRESSION Metal Serum Gloss' AND brand = 'JUNGSAEMMOOL'
                  AND canonical_url = :url AND category = 'makeup' AND product_type = 'makeup'
                  AND category_path = 'beauty/makeup'
                  AND suppression_reason IS NULL AND suppressed_at IS NULL
                  AND EXISTS (SELECT 1 FROM external_product_seeds e WHERE e.id = :seed_id
                              AND e.status = 'active' AND e.attached_product_key = catalog_products.product_key
                              AND e.price_currency = 'SGD' AND e.price_amount = :new_price)
                RETURNING product_key
            """, {"key": PRODUCT_KEY, "ck": product["content_key"], "merchant": MERCHANT_ID,
                    "url": URL, "seed_id": seed["id"], "new_price": NEW_PRICE,
                    "category_source": CATEGORY_SOURCE})
            require(len(updated) == 1, "product_cas_failed")

            for row in live:
                changed = await database.fetch_all("""
                    UPDATE catalog_offers SET list_price = :new_price,
                        merchant_effective_price = :new_price,
                        estimated_best_price = :new_price,
                        offer_payload = COALESCE(offer_payload, CAST('{}' AS jsonb))
                            || jsonb_build_object('meitu_sg_price_reviewed_0920', 'SGD30'),
                        updated_at = NOW()
                    WHERE offer_id = :offer_id AND sku_key = :sku_key AND product_key = :key
                      AND merchant_id = :merchant AND source_system = 'catalog_enrichment_agent_v1'
                      AND currency = 'SGD' AND list_price = :old_price
                      AND merchant_effective_price = :old_price
                      AND estimated_best_price = :old_price
                      AND suppressed_at IS NULL
                    RETURNING offer_id
                """, {"offer_id": row["offer_id"], "sku_key": row["sku_key"],
                        "key": PRODUCT_KEY, "merchant": OFFER_MERCHANT_ID,
                        "old_price": OLD_PRICE, "new_price": NEW_PRICE})
                require(len(changed) == 1, "variant_offer_cas_failed:" + str(row["source_variant_id"]))

            old = placeholder[0]
            retired_offer = await database.fetch_all("""
                UPDATE catalog_offers SET suppression_reason = :reason,
                    suppressed_at = NOW(), updated_at = NOW()
                WHERE offer_id = :offer_id AND sku_key = :sku_key AND product_key = :key
                  AND merchant_id = :merchant AND source_system = 'catalog_enrichment_agent_v1'
                  AND currency = 'SGD' AND list_price = :old_price
                  AND merchant_effective_price = :old_price
                  AND estimated_best_price = :old_price AND suppressed_at IS NULL
                RETURNING offer_id
            """, {"reason": REASON, "offer_id": old["offer_id"],
                    "sku_key": old["sku_key"], "key": PRODUCT_KEY,
                    "merchant": OFFER_MERCHANT_ID, "old_price": OLD_PRICE})
            require(len(retired_offer) == 1, "canonical_offer_cas_failed")
            retired_sku = await database.fetch_all("""
                UPDATE catalog_skus SET suppression_reason = :reason,
                    suppressed_at = NOW(), updated_at = NOW()
                WHERE sku_key = :sku_key AND product_key = :key
                  AND merchant_id = :merchant AND source_variant_id = :key
                  AND suppressed_at IS NULL
                RETURNING sku_key
            """, {"reason": REASON, "sku_key": old["sku_key"],
                    "key": PRODUCT_KEY, "merchant": MERCHANT_ID})
            require(len(retired_sku) == 1, "canonical_sku_cas_failed")

            active = as_dict(await database.fetch_one("""
                SELECT COUNT(*) AS n, MIN(list_price) AS min_price,
                       MAX(list_price) AS max_price,
                       COUNT(DISTINCT sku_key) AS sku_count
                FROM catalog_offers WHERE product_key = :key AND suppressed_at IS NULL
            """, {"key": PRODUCT_KEY}))
            require(active is not None and int(active["n"]) == 12 and
                    int(active["sku_count"]) == 12 and
                    Decimal(str(active["min_price"])) == NEW_PRICE and
                    Decimal(str(active["max_price"])) == NEW_PRICE,
                    "active_offer_set_postcondition_failed")

            refreshed = await refresh_agent_pdp_view_for_content_key(
                product["content_key"], refresh_source=REASON, db=database,
                external_seed_id=seed["id"])
            require(refreshed, "pdp_refresh_failed")
            eligible = await recompute_serving_eligibility(
                product["content_key"], reason=REASON, db=database, strict=True)
            require(eligible, "eligibility_refresh_failed")
            trusted = await upsert_catalog_row_trust(db=database, product_key=PRODUCT_KEY)
            require(trusted, "trust_refresh_failed")
            view = as_dict(await database.fetch_one("""
                SELECT variants_count, price_min, price_max, currency, variants
                FROM agent_pdp_view WHERE content_key = :ck
            """, {"ck": product["content_key"]}))
            require(view is not None and view["variants_count"] == 12 and
                    Decimal(str(view["price_min"])) == NEW_PRICE and
                    Decimal(str(view["price_max"])) == NEW_PRICE and
                    view["currency"] == "SGD", "pdp_postcondition_failed")
            visible_variants = load_json(view["variants"])
            require(len(visible_variants) == 12 and
                    PRODUCT_KEY not in {str(v.get("variant_id")) for v in visible_variants},
                    "synthetic_variant_still_visible")
        print("REPAIR_APPLIED=" + json.dumps({"product_key": PRODUCT_KEY,
            "variant_offers_updated": 12, "synthetic_offer_retired": 1,
            "synthetic_sku_retired": 1, "pdp_variants": 12,
            "pdp_price": str(NEW_PRICE), "serving_eligible": True,
            "trust_refreshed": True}))
    finally:
        await database.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.apply))
