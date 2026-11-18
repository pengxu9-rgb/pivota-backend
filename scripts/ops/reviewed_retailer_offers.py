"""Plan or refresh the eight already published US referrals for reviewed cohort C."""

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

from scripts.ops import reviewed_job_pins as pins
from services import crawl_politeness, shopify_edge_pacer
from services.catalog_enrichment_agent.reviewed_retailer_apply import apply_reviewed_retailer
from services.catalog_identity import validated_source_gtin
from services.curated_brand_feed import _note_response, shopify_product_to_record
from services.exact_retailer_cohorts import COHORT_C_CONFIG, COHORT_C_WRITE_FLAG, RETAILER_COHORT_C
from services.exact_retailer_shades import read_offers
from services.outbound_links_service import is_destination_domain_allowed
from services.shopify_presentment import no_cookie_client, presentment_currency


SCOPE = "reviewed_retailer_c_us_referrals"
JOB_PREFIX = "reviewed-retailer-c-us-"
RELEASE_COMMIT_ENV = "REVIEWED_RETAILER_C_RELEASE_COMMIT"
PROD_PROJECT_SHA256 = "39ebed7b03c90293ee9ae6abfa6f003f9327ef8fff2d311e2813f5a4d308239c"
AGENT = "PivotaCommerceIndex/1.0 (+https://pivota.cc; catalog coverage)"


def image_url(value):
    raw = value.get("src") if isinstance(value, dict) else value
    if not isinstance(raw, str):
        return None
    url = "https:" + raw if raw.startswith("//") else raw
    parsed = urlsplit(url)
    return url if parsed.scheme == "https" and parsed.hostname else None


def storefront_record(payload, observed_at, *, listing, config):
    if (not isinstance(payload, dict) or str(payload.get("id")) != listing.native_product_id
            or payload.get("handle") != listing.product_path.rsplit("/", 1)[-1]
            or str(payload.get("vendor", "")).strip().casefold() != config["vendor"].casefold()
            or not isinstance(payload.get("variants"), list)):
        raise ValueError("reviewed storefront product or maker changed")
    image = next((url for raw in payload.get("images") or [] if (url := image_url(raw))), None)
    if not image:
        raise ValueError("reviewed storefront image is unavailable")
    variants = {}
    for row in payload["variants"]:
        if not isinstance(row, dict):
            raise ValueError("reviewed storefront returned an invalid variant")
        native = str(row.get("id"))
        if native not in listing.variants:
            continue
        price, stock = row.get("price"), row.get("available")
        if (native in variants or validated_source_gtin(row.get("barcode")) != listing.variants[native][1]
                or row.get("sku") != config["sku_by_variant"][native]
                or type(price) is not int or not 0 < price < 1_000_000 or type(stock) is not bool):
            raise ValueError("reviewed retailer variant identity, price or stock is unproven")
        variants[native] = {**row, "price": price / 100,
                            "featured_image": {"src": image_url(row.get("featured_image")) or image}}
    if variants.keys() != listing.variants.keys():
        raise ValueError("reviewed storefront is missing a scoped variant")

    record = shopify_product_to_record(
        {**payload, "product_type": payload.get("type"), "body_html": payload.get("description") or "",
         "variants": [variants[native] for native in listing.variants], "images": [{"src": image}]},
        domain=listing.host, category_path=config["category_path"], brand_override=config["vendor"],
        emit_native_variants=True, currency="USD", source_role="retailer", retailer_name=config["retailer_name"],
    )
    if not record or len(record.get("offers") or []) != 1 or record["offers"][0]["canonical_url"] != listing.family_url:
        raise ValueError("stock mapper did not retain the reviewed retailer URL")
    record["offers"][0]["validated_at"] = observed_at.isoformat()
    return record


async def fetch_storefront(*, listing, config):
    url = f"https://{config['storefront']}{listing.product_path}.js?country=US"
    shopify_edge_pacer.mark_shopify_host(config["storefront"])
    await crawl_politeness.before_request(url, user_agent=AGENT, max_wait=10)
    async with no_cookie_client(follow_redirects=False, trust_env=False, timeout=15, headers={
        "User-Agent": AGENT, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }) as client:
        response = await client.get(url)
    _note_response(url, response)
    currency, issue = presentment_currency(response.headers.get_list("set-cookie"))
    if response.status_code != 200 or str(response.url) != url or currency != "USD" or issue:
        raise ValueError("reviewed storefront did not prove US dollar presentment")
    return storefront_record(response.json(), datetime.now(timezone.utc), listing=listing, config=config)


def check_environment(*, write):
    job = os.getenv("CLOUD_RUN_JOB", "")
    revision = Path("/app/.image_commit_sha").read_text().strip()
    if (os.getenv("PIVOTA_ENV") != "production"
            or os.getenv("DB_AUTO_MIGRATE") != "false"
            or not job.startswith(JOB_PREFIX)
            or not os.getenv("CLOUD_RUN_EXECUTION", "").startswith(job + "-")
            or not re.fullmatch(r"[0-9a-f]{40}", revision) or os.getenv(RELEASE_COMMIT_ENV) != revision):
        raise ValueError("expected the reviewed production refresh job and backend image")
    pins.require_pins(cloud_project=(os.getenv("GOOGLE_CLOUD_PROJECT"), PROD_PROJECT_SHA256))
    pins.require_dsn_pins(urlsplit(os.environ["DATABASE_URL"]), host_sha256=pins.PROD_DB_HOST_SHA256,
                          name_sha256=pins.PROD_DB_NAME_SHA256)
    if write and os.getenv(COHORT_C_WRITE_FLAG) != "1":
        raise ValueError("reviewed US retailer refresh writes are disabled")
    return revision


async def refresh_existing(db, record, *, write, cohort):
    (listing_key, listing), = cohort.listings.items()
    async with db.transaction(force_rollback=not write):
        await db.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE" + ("" if write else " READ ONLY"))
        await pins.require_server_pins(db, host_sha256=pins.PROD_DB_HOST_SHA256, name_sha256=pins.PROD_DB_NAME_SHA256,
                                       reason="connection did not reach the reviewed production primary")
        if not await db.fetch_val("""
            SELECT indisvalid AND indisready FROM pg_index
            WHERE indexrelid=to_regclass('idx_catalog_skus_barcode')
        """):
            raise ValueError("canonical barcode index is not valid")
        domains = [row["domain"] for row in await db.fetch_all("""
            SELECT domain FROM outbound_link_allowed_domains WHERE market='US' AND status='active'
        """)]
        if not is_destination_domain_allowed(destination_url=listing.family_url, allowed_domains=domains):
            raise ValueError("reviewed retailer domain is outside the US signed outbound policy")
        planned = await apply_reviewed_retailer(db, record, write=False, cohort=cohort)
        expected = (listing_key, listing.merchant_id, len(listing.variants), len(listing.variants))
        if (planned["product_key"], planned["seller_ref"], planned["existing"], len(planned["offers"])) != expected:
            raise ValueError("refresh requires every reviewed offer to be already published")
        if not write:
            return planned
        applied = await apply_reviewed_retailer(db, record, write=True, cohort=cohort)
        if applied != planned:
            raise ValueError("refresh differs from its plan")
        # The reader queries in a child task. Keep it on the current transaction's connection.
        connection = db.connection()
        for offer_id, proof in applied["offers"].items():
            rows = await read_offers(connection, proof["canonical_native_variant_id"], timeout=5, cohort=cohort)
            seen = [(row["merchant_id"], row["offer_id"], row["destination_url"], Decimal(str(row["price_amount"])) * 100)
                    for row in rows]
            wanted = [(listing.merchant_id, offer_id, proof["retailer_landing_url"], proof["price_minor"])]
            if seen != (wanted if proof["availability"] == "in_stock" else []):
                raise ValueError("exact retailer reader disagrees with refreshed price, stock or destination")
        return applied


async def run(mode):
    if COHORT_C_CONFIG is None or RETAILER_COHORT_C is None:
        raise ValueError("reviewed retailer cohort configuration is unavailable")
    write = mode == "refresh"
    revision = check_environment(write=write)
    from db.database import database

    listing, = RETAILER_COHORT_C.listings.values()
    record = await fetch_storefront(listing=listing, config=COHORT_C_CONFIG["refresh"])
    await database.connect()
    try:
        return {"scope": SCOPE, "mode": mode, "revision": revision,
                "listing": await refresh_existing(database, record, write=write, cohort=RETAILER_COHORT_C)}
    finally:
        await database.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "refresh"))
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args().mode)), sort_keys=True), flush=True)
    except (ValueError, httpx.HTTPError, crawl_politeness.CrawlPaced, crawl_politeness.RobotsDisallowed) as exc:
        print(json.dumps({"scope": SCOPE, "status": "held", "reason": str(exc)}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
