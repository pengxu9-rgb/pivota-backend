"""Repair mirror variant offers from stored evidence; dry-run by default.

Writes only missing offers. Does not fetch merchants, refresh prices/proofs, create
SKUs, or enable checkout. Apply is restricted to the audited staging database.
"""

import argparse
import asyncio
import collections
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
CONTRACT = "missing-mirror-variant-offers-v1"


def validate_target():
    url = urlsplit(os.environ.get("DATABASE_URL", ""))
    if (
        os.environ.get("PIVOTA_ENV") != "staging"
        or url.scheme not in ("postgres", "postgresql")
        or url.hostname != "10.122.0.3"
        or url.path != "/pivota"
        or "," in url.netloc.rpartition("@")[2]
        or {k.lower() for k, v in parse_qsl(url.query, keep_blank_values=True)}
        & {"host", "hostaddr", "options", "service"}
    ):
        raise RuntimeError("staging_catalog_target_refused")


async def run(*, apply=False, product_key=None, limit=0):
    validate_target()
    from db.database import database
    from services.catalog_variant_offer_projection import project_missing_variant_offers, MIRROR

    await database.connect()
    counts = collections.Counter(products=0, planned=0, inserted=0)
    cursor = ""
    report = []
    try:
        identity = await database.fetch_one("SELECT current_database() AS db,host(inet_server_addr()) AS addr")
        if identity["db"] != "pivota" or identity["addr"] != "10.122.0.3":
            raise RuntimeError("staging_catalog_server_target_refused")
        while True:
            page = await database.fetch_all(
                """SELECT product_key FROM catalog_products
             WHERE source_system=:src AND suppressed_at IS NULL AND suppression_reason IS NULL
             AND (CAST(:selected AS TEXT) IS NOT NULL OR EXISTS (SELECT 1 FROM catalog_skus s
              WHERE s.product_key=catalog_products.product_key AND s.sku_key NOT LIKE '%::canonical'
              AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
              AND NOT EXISTS(SELECT 1 FROM catalog_offers o WHERE o.product_key=s.product_key AND o.sku_key=s.sku_key)))
             AND product_key>:cursor AND (CAST(:selected AS TEXT) IS NULL OR product_key=CAST(:selected AS TEXT))
             ORDER BY product_key LIMIT 100""",
                {"src": MIRROR, "cursor": cursor, "selected": product_key},
            )
            if not page:
                break
            for p in page:
                cursor = p["product_key"]
                result = await project_missing_variant_offers(cursor, apply=apply, db=database)
                counts["products"] += 1
                counts["planned"] += result["planned"]
                counts["inserted"] += result["inserted"]
                for reason, n in result["skips"].items():
                    counts["skip_" + reason] += n
                if counts["products"] % 100 == 0:
                    print(json.dumps({"repair_progress": dict(counts)}), flush=True)
                if product_key:
                    report.append({"product_key": cursor, **result})
                if limit and counts["products"] >= limit:
                    break
            if limit and counts["products"] >= limit:
                break
        return {
            "kind": "mirror_variant_offer_repair",
            "environment": "staging",
            "apply": apply,
            "contract": CONTRACT,
            "counts": dict(counts),
            "selected_product": report,
            "last_product_key": cursor,
            "merchant_calls": False,
            "price_or_cart_proof_freshness_changed": False,
            "checkout_enabled": False,
        }
    finally:
        await database.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-contract")
    parser.add_argument("--product-key")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.apply and args.expect_contract != CONTRACT:
        parser.error("--apply requires --expect-contract " + CONTRACT)
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    print(
        json.dumps(asyncio.run(run(apply=args.apply, product_key=args.product_key, limit=args.limit)), sort_keys=True)
    )


if __name__ == "__main__":
    main()
