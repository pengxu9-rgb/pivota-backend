"""Plan, ingest or refresh the four reviewed cohort A US referral offers.

The cohort's listings, storefront identity checks and job settings are read from
config/reviewed_retailer_cohorts.json. The production database the job's DATABASE_URL names, and
the server it reaches, must hash to the sha256 pins in scripts/ops/reviewed_job_pins.py; any
mismatch holds the run before a write.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

from services import crawl_politeness, shopify_edge_pacer
from services.catalog_enrichment_agent.reviewed_retailer_apply import apply_reviewed_retailer
from services.catalog_identity import validated_source_gtin
from services.curated_brand_feed import _note_response, shopify_product_to_record
from services.exact_retailer_shades import read_offers
from services.outbound_links_service import is_destination_domain_allowed
from services.shopify_presentment import presentment_currency
from services.exact_retailer_cohorts import COHORT_A_CONFIG, RETAILERS
from scripts.ops.reviewed_job_pins import (
    PROD_DB_HOST_SHA256, PROD_DB_NAME_SHA256, require_dsn_pins, require_server_pins,
)


REFRESH = COHORT_A_CONFIG["refresh"]
PROD_PROJECT = "pivota-prod"
JOB_PREFIX = REFRESH["job_prefix"]
WRITE_FLAG = REFRESH["write_flag"]
RELEASE_COMMIT_ENV = REFRESH["release_commit_env"]
SCOPE = REFRESH["scope"]
AGENT = "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)"
STORE_NAMES = REFRESH["store_names"]


def storefront_record(listing, payload, observed_at):
    if (
        not isinstance(payload, dict)
        or str(payload.get("id")) != listing.native_product_id
        or payload.get("handle") != REFRESH["handle"]
        or str(payload.get("vendor", "")).strip().casefold() not in set(REFRESH["vendor_names"])
        or REFRESH["title_token"] not in str(payload.get("title", "")).casefold()
        or not isinstance(payload.get("variants"), list)
    ):
        raise ValueError("storefront listing identity changed")
    variants = []
    seen = set()
    for raw in payload["variants"]:
        if not isinstance(raw, dict):
            raise ValueError("storefront returned an invalid variant")
        native = str(raw.get("id"))
        if native not in listing.variants:
            continue
        if native in seen or validated_source_gtin(raw.get("barcode")) != listing.variants[native][1]:
            raise ValueError("retailer shade or UPC changed")
        price, available = raw.get("price"), raw.get("available")
        if type(price) is not int or not 0 < price < 1_000_000 or type(available) is not bool:
            raise ValueError("retailer price or stock is unproven")
        image = raw.get("featured_image")
        image = image.get("src") if isinstance(image, dict) else None
        image = "https:" + image if isinstance(image, str) and image.startswith("//") else image
        if not isinstance(image, str) or urlsplit(image).scheme != "https":
            raise ValueError("native shade image is unproven")
        variants.append({**raw, "price": price / 100, "featured_image": {"src": image}})
        seen.add(native)
    if seen != listing.variants.keys():
        raise ValueError("reviewed shade pair is missing")
    description = str(payload.get("description") or "")
    if str(payload.get("type") or "").casefold() != REFRESH["product_type"] and not description.casefold().startswith(
        REFRESH["description_prefix"]
    ):
        raise ValueError("retailer product category changed")
    record = shopify_product_to_record(
        {**payload, "product_type": payload.get("type"), "body_html": description,
         "variants": variants, "images": [{"src": variants[0]["featured_image"]["src"]}]},
        domain=listing.host, category_path=REFRESH["category_path"], brand_override=REFRESH["brand_override"],
        emit_native_variants=True, currency="USD", source_role="retailer", retailer_name=STORE_NAMES[listing.host],
    )
    if not record or len(record.get("offers") or []) != 1:
        raise ValueError("reviewed retailer record is incomplete")
    record["offers"][0]["validated_at"] = observed_at.isoformat()
    return record


async def fetch_storefront(listing):
    url = f"{listing.family_url}.js?country=US"
    shopify_edge_pacer.mark_shopify_host(listing.host)
    await crawl_politeness.before_request(url, user_agent=AGENT, max_wait=10)
    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=15,
        headers={"User-Agent": AGENT, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
                 "Cache-Control": "no-cache"},
    ) as client:
        response = await client.get(url)
    _note_response(url, response)
    currency, problem = presentment_currency(response.headers.get_list("set-cookie"))
    if response.status_code != 200 or currency != "USD" or problem:
        raise ValueError(f"{listing.host} did not confirm US storefront currency")
    return storefront_record(listing, response.json(), datetime.now(timezone.utc))


def check_environment(mode):
    job = os.getenv("CLOUD_RUN_JOB", "")
    configured = urlsplit(os.environ["DATABASE_URL"])
    stamp = Path("/app/.image_commit_sha").read_text().strip()
    if (
        (os.getenv("PIVOTA_ENV"), os.getenv("GOOGLE_CLOUD_PROJECT")) != ("production", PROD_PROJECT)
        or not job.startswith(JOB_PREFIX)
        or not os.getenv("CLOUD_RUN_EXECUTION", "").startswith(f"{job}-")
        or not re.fullmatch(r"[0-9a-f]{40}", stamp)
        or os.getenv(RELEASE_COMMIT_ENV) != stamp
    ):
        raise ValueError("expected the reviewed production job, image and database")
    require_dsn_pins(configured, host_sha256=PROD_DB_HOST_SHA256, name_sha256=PROD_DB_NAME_SHA256)
    if mode != "plan" and os.getenv(WRITE_FLAG) != "1":
        raise ValueError("reviewed cohort A writes are disabled")
    return stamp


async def apply_cohort(db, records, *, mode):
    write = mode != "plan"
    async with db.transaction():
        await db.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE" + ("" if write else " READ ONLY"))
        await require_server_pins(db, host_sha256=PROD_DB_HOST_SHA256, name_sha256=PROD_DB_NAME_SHA256,
                                  reason="connection did not reach the reviewed production primary")
        if write:
            await db.execute("SELECT pg_advisory_xact_lock(1160308171, 1)")
        readable_index = await db.fetch_val("""
            SELECT indisvalid AND indisready FROM pg_index
            WHERE indexrelid=to_regclass('idx_catalog_skus_barcode')
        """)
        if readable_index is not True:
            raise ValueError("canonical barcode index is not valid")
        allowlist = [row["domain"] for row in await db.fetch_all("""
            SELECT domain FROM outbound_link_allowed_domains WHERE market='US' AND status='active'
        """)]
        if not all(is_destination_domain_allowed(destination_url=listing.family_url, allowed_domains=allowlist)
                   for listing in RETAILERS.values()):
            raise ValueError("reviewed US signed redirect domains are not both active")
        planned = [await apply_reviewed_retailer(db, record, write=False) for record in records]
        if {entry["product_key"] for entry in planned} != RETAILERS.keys():
            raise ValueError("production plan exceeds the reviewed retailer scope")
        if mode == "refresh" and any(entry["existing"] != 2 for entry in planned):
            raise ValueError("refresh requires the four previously ingested exact offers")
        if not write:
            return planned
        applied = [await apply_reviewed_retailer(db, record, write=True) for record in records]
        if planned != applied:
            raise ValueError("production write differs from its preflight")
        canonical_ids = {canonical for listing in RETAILERS.values() for canonical, _ in listing.variants.values()}
        for canonical in sorted(canonical_ids):
            expected = {
                (row["seller_ref"], offer_id, proof["retailer_landing_url"], proof["price_minor"])
                for row in applied for offer_id, proof in row["offers"].items()
                if proof["canonical_native_variant_id"] == canonical and proof["availability"] == "in_stock"
            }
            actual = {
                (row["merchant_id"], row["offer_id"], row["destination_url"], round(row["price_amount"] * 100))
                for row in await read_offers(db, canonical, timeout=5)
            }
            if actual != expected:
                raise ValueError("production exact retailer reader does not match the verified cohort")
        return applied


async def run(mode):
    stamp = check_environment(mode)
    from db.database import database

    records = [await fetch_storefront(listing) for listing in RETAILERS.values()]
    await database.connect()
    try:
        return {"scope": SCOPE, "mode": mode, "release_sha": stamp,
                "listings": await apply_cohort(database, records, mode=mode)}
    finally:
        await database.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply", "refresh"))
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args().mode)), sort_keys=True), flush=True)
    except ValueError as exc:
        print(json.dumps({"scope": SCOPE, "status": "held", "reason": str(exc)}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
