"""Check or refresh the existing, reviewed Kiokii US referral offers.

Run on the reviewed backend image from the dedicated production crawl-subnet
job. Refresh requires KIOKII_US_RETAILER_WRITE_ENABLED=1 and the complete persisted cohort.
An unusable storefront read leaves the previous observation to expire normally.
"""

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

from services import crawl_politeness, shopify_edge_pacer
from services.catalog_enrichment_agent.surratt_retailer_apply import apply_reviewed_retailer, retailer_witness
from services.catalog_identity import validated_source_gtin
from services.curated_brand_feed import _note_response, shopify_product_to_record
from services.exact_retailer_shades import read_offers
from services.outbound_links_service import is_destination_domain_allowed
from services.shopify_presentment import no_cookie_client, presentment_currency
from services.surratt_retailer_pilot import KIOKII_INITIAL_OFFER_IDS, KIOKII_INITIAL_RETAILER_COHORT, KIOKII_RETAILER_COHORT


COHORT = KIOKII_RETAILER_COHORT
(LISTING_KEY, LISTING), = COHORT.listings.items()
SHOP_URL = f"https://{LISTING.host}/en-us/meta.json"
PRODUCT_URL = f"{LISTING.family_url}.js?country=US"
SHADE_IDENTITIES = {
    "51629126189376": ("01 Zen Matcha", "K0504000648"),
    "51629126123840": ("02 Banana", "K0504000649"),
    "51629126222144": ("03 Manuka Honey", "K0504000650"),
    "51896079974720": ("04 Juicy Peach", "K0504000994"),
    "51629126156608": ("05 Cranberry Pop", "K0504000651"),
    "51896079876416": ("06 Rose Sorbet", "K0504000993"),
}
PROJECT = "pivota-prod"
DATABASE = "pivota_08220842_Zsqlgz"
HOST = "10.25.0.2"
JOB_PREFIX = "meitu-kiokii-us-retailer-"
AGENT = "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)"


def _image_url(value):
    if not isinstance(value, str):
        return ""
    return "https:" + value if value.startswith("//") else value


def storefront_record(shop, product, observed_at, *, cohort=COHORT):
    listing = cohort.listings[LISTING_KEY]
    if not isinstance(shop, dict) or not isinstance(product, dict):
        raise ValueError("reviewed Kiokii storefront did not return product and shop objects")
    if (str(shop.get("id")), shop.get("domain"), shop.get("myshopify_domain")) != (
        "68650303808", LISTING.host, "kiokiiand.myshopify.com",
    ) or not isinstance(shop.get("ships_to_countries"), list) or "US" not in shop["ships_to_countries"]:
        raise ValueError("reviewed Shopify seller or US shipping changed")
    if (str(product.get("id")), product.get("handle"), product.get("vendor")) != (
        LISTING.native_product_id, "amuse-banana-lip-oil-4-5ml", "AMUSE",
    ) or not isinstance(product.get("variants"), list):
        raise ValueError("reviewed Shopify listing changed")

    variants = {}
    for row in product["variants"]:
        if not isinstance(row, dict):
            raise ValueError("reviewed Kiokii storefront returned an invalid variant")
        native = str(row.get("id"))
        if native not in listing.variants:
            continue
        price, stock = row.get("price"), row.get("available")
        if (native in variants or validated_source_gtin(row.get("barcode")) != listing.variants[native][1]
                or (row.get("title"), row.get("sku")) != SHADE_IDENTITIES[native]
                or row.get("option1") != SHADE_IDENTITIES[native][0]
                or type(price) is not int or not 0 < price < 1_000_000 or type(stock) is not bool):
            raise ValueError("reviewed shade, SKU, GTIN, price or stock is unproven")
        image = row.get("featured_image")
        variants[native] = {
            **row, "price": price / 100,
            "featured_image": {"src": _image_url(image.get("src"))} if isinstance(image, dict) else None,
        }
    if variants.keys() != listing.variants.keys():
        raise ValueError("reviewed Kiokii colors are incomplete")

    record = shopify_product_to_record(
        {**product, "product_type": product.get("type"), "body_html": product.get("description"),
         "variants": [variants[native] for native in listing.variants],
         "images": [{"src": _image_url(value)} for value in product.get("images") or []]},
        domain=LISTING.host, category_path="beauty/makeup/lips/lip_oil", brand_override="AMUSE",
        emit_native_variants=True, currency="USD", source_role="retailer", retailer_name="Kiokii and...",
    )
    if not record or len(record.get("offers") or []) != 1 or not record["pdp"]["category_path"]:
        raise ValueError("reviewed Kiokii record is incomplete")
    # The mapper defaults to the generic Shopify path. Keep the verified US landing.
    record["offers"][0].update(canonical_url=LISTING.family_url, destination_url=LISTING.family_url,
                               validated_at=observed_at.isoformat())
    return record


async def fetch_storefront(*, cohort=COHORT):
    shopify_edge_pacer.mark_shopify_host(LISTING.host)
    async with no_cookie_client(
        follow_redirects=False, trust_env=False, timeout=15,
        headers={"User-Agent": AGENT, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
                 "Cache-Control": "no-cache"},
    ) as client:
        await crawl_politeness.before_request(SHOP_URL, user_agent=AGENT, max_wait=10)
        shop = await client.get(SHOP_URL)
        _note_response(SHOP_URL, shop)
        if shop.status_code != 200 or str(shop.url) != SHOP_URL:
            raise ValueError("reviewed Kiokii Shopify identity endpoint moved or failed")
        await crawl_politeness.before_request(PRODUCT_URL, user_agent=AGENT, max_wait=10)
        product = await client.get(PRODUCT_URL)
        _note_response(PRODUCT_URL, product)
        currency, problem = presentment_currency(product.headers.get_list("set-cookie"))
        if product.status_code != 200 or str(product.url) != PRODUCT_URL or currency != "USD" or problem:
            raise ValueError("reviewed Kiokii listing did not confirm US storefront currency")
        return storefront_record(shop.json(), product.json(), datetime.now(timezone.utc), cohort=cohort)


def check_environment(*, write):
    job = os.getenv("CLOUD_RUN_JOB", "")
    dsn = urlsplit(os.environ["DATABASE_URL"])
    stamp = Path("/app/.image_commit_sha").read_text().strip()
    if (
        (os.getenv("PIVOTA_ENV"), os.getenv("GOOGLE_CLOUD_PROJECT")) != ("production", PROJECT)
        or not job.startswith(JOB_PREFIX) or not os.getenv("CLOUD_RUN_EXECUTION", "").startswith(f"{job}-")
        or (dsn.hostname, dsn.path) != (HOST, f"/{DATABASE}")
        or not re.fullmatch(r"[0-9a-f]{40}", stamp) or os.getenv("KIOKII_RELEASE_COMMIT") != stamp
    ):
        raise ValueError("expected the reviewed Kiokii production job, image and database")
    if write and os.getenv("KIOKII_US_RETAILER_WRITE_ENABLED") != "1":
        raise ValueError("reviewed Kiokii refresh writes are disabled")
    return stamp


async def _persisted_cohort(db):
    rows = await retailer_witness(db, {"key": LISTING_KEY, "seller": LISTING.merchant_id, "url": LISTING.family_url})
    offer_ids = {row["offer_id"] for row in rows}
    native_ids = {row["source_variant_id"] for row in rows}
    original = KIOKII_INITIAL_RETAILER_COHORT.listings[LISTING_KEY].variants
    if len(rows) == len(original) and offer_ids == KIOKII_INITIAL_OFFER_IDS and native_ids == original.keys():
        return KIOKII_INITIAL_RETAILER_COHORT
    if len(rows) == len(LISTING.variants) and KIOKII_INITIAL_OFFER_IDS <= offer_ids and native_ids == LISTING.variants.keys():
        return COHORT
    raise ValueError("refresh requires the complete original two or expanded six Kiokii offers")


async def refresh_existing(db, record, *, write, cohort=COHORT):
    async with db.transaction():
        await db.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE" + ("" if write else " READ ONLY"))
        if not await db.fetch_val("""
            SELECT host(inet_server_addr())=:host AND current_database()=:name AND NOT pg_is_in_recovery()
        """, {"host": HOST, "name": DATABASE}):
            raise ValueError("connection did not reach the reviewed production primary")
        if write:
            await db.execute("SELECT pg_advisory_xact_lock(1160308171, 2)")
        if await _persisted_cohort(db) is not cohort:
            raise ValueError("Kiokii refresh scope changed since storefront capture")
        allowlist = [row["domain"] for row in await db.fetch_all("""
            SELECT domain FROM outbound_link_allowed_domains WHERE market='US' AND status='active'
        """)]
        if not is_destination_domain_allowed(destination_url=LISTING.family_url, allowed_domains=allowlist):
            raise ValueError("reviewed Kiokii US signed redirect domain is not allowed")
        planned = await apply_reviewed_retailer(db, record, write=False, cohort=cohort)
        variant_count = len(cohort.listings[LISTING_KEY].variants)
        if (planned["product_key"], planned["seller_ref"], planned["existing"], len(planned["offers"])) != (
            LISTING_KEY, LISTING.merchant_id, variant_count, variant_count,
        ):
            raise ValueError("refresh requires every previously ingested offer in its reviewed scope")
        if not write:
            return planned
        applied = await apply_reviewed_retailer(db, record, write=True, cohort=cohort)
        if applied != planned:
            raise ValueError("Kiokii refresh differs from its read-only plan")
        for offer_id, proof in applied["offers"].items():
            actual = {
                (row["merchant_id"], row["offer_id"], row["destination_url"], Decimal(str(row["price_amount"])) * 100)
                for row in await read_offers(db, proof["canonical_native_variant_id"], timeout=5, cohort=cohort)
            }
            expected = {(LISTING.merchant_id, offer_id, proof["retailer_landing_url"], proof["price_minor"])}
            if actual != (expected if proof["availability"] == "in_stock" else set()):
                raise ValueError("Kiokii exact retailer reader differs from the refreshed offer")
        return applied


async def run(mode):
    write = mode == "refresh"
    stamp = check_environment(write=write)
    from db.database import database

    await database.connect()
    try:
        async with database.transaction():
            await database.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cohort = await _persisted_cohort(database)
        record = await fetch_storefront(cohort=cohort)
        return {"scope": "kiokii_us_exact_referrals", "mode": mode, "release_sha": stamp,
                "listing": await refresh_existing(database, record, write=write, cohort=cohort)}
    finally:
        await database.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "refresh"))
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args().mode)), sort_keys=True), flush=True)
    except (ValueError, httpx.HTTPError, crawl_politeness.CrawlPaced, crawl_politeness.RobotsDisallowed) as exc:
        print(json.dumps({"scope": "kiokii_us_exact_referrals", "status": "held", "reason": str(exc)}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
