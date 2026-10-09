"""Repair mirror variant offers from stored evidence; dry-run by default.

Writes only missing offers. Does not fetch merchants, refresh prices/proofs, create
SKUs, or enable checkout. Apply requires an explicit environment and is pinned to its audited database.
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


TARGETS = {"staging": ("10.122.0.3", "pivota"), "production": ("10.25.0.2", "pivota_08220842_Zsqlgz")}

def validate_target(environment="staging"):
    host, name = TARGETS[environment]
    url = urlsplit(os.environ.get("DATABASE_URL", ""))
    if (
        os.environ.get("PIVOTA_ENV") != environment
        or url.scheme not in ("postgres", "postgresql")
        or url.hostname != host
        or url.path != "/" + name
        or "," in url.netloc.rpartition("@")[2]
        or {k.lower() for k, v in parse_qsl(url.query, keep_blank_values=True)}
        & {"host", "hostaddr", "options", "service"}
    ):
        raise RuntimeError("catalog_target_refused")


async def run(*, apply=False, product_key=None, limit=0, environment="staging", include_manifest=False, expected_plan_hash=None):
    validate_target(environment)
    from db.database import database
    from services.catalog_variant_offer_projection import project_missing_variant_offers, MIRROR

    from services.catalog_offer_link_repair import plan_hash
    await database.connect()
    counts = collections.Counter(products=0, planned=0, inserted=0)
    cursor = ""
    report = []
    plans = []
    try:
        identity = await database.fetch_one("SELECT current_database() AS db,host(inet_server_addr()) AS addr")
        if (identity["addr"], identity["db"]) != TARGETS[environment]:
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
                result = await project_missing_variant_offers(cursor, apply=False, db=database, include_manifest=True)
                plans.append({"product_key": cursor, "plan_sha256": result.get("plan_sha256"), "manifest": result.get("manifest", [])})
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
        digest = plan_hash(plans)
        if apply:
            if not expected_plan_hash or expected_plan_hash != digest:
                raise RuntimeError("variant_offer_batch_plan_changed")
            async with database.transaction():
                for plan in plans:
                    if not plan["manifest"]:
                        continue
                    result = await project_missing_variant_offers(plan["product_key"], apply=True, db=database,
                        expected_plan_hash=plan["plan_sha256"])
                    counts["inserted"] += result["inserted"]
        if include_manifest:
            for plan in plans:
                for row in plan["manifest"]:
                    print(json.dumps({"kind": "mirror_variant_offer_repair_manifest", "row": row}), flush=True)
        return {
            "plan_sha256": digest,
            "kind": "mirror_variant_offer_repair",
            "environment": environment,
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
    parser.add_argument("--environment", choices=sorted(TARGETS), default="staging")
    parser.add_argument("--expect-contract")
    parser.add_argument("--expect-plan-sha256")
    parser.add_argument("--manifest", action="store_true")
    parser.add_argument("--product-key")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.apply and (args.expect_contract != CONTRACT or not args.expect_plan_sha256):
        parser.error("--apply requires --expect-contract " + CONTRACT + " and --expect-plan-sha256")
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    print(
        json.dumps(asyncio.run(run(apply=args.apply, product_key=args.product_key, limit=args.limit, environment=args.environment, include_manifest=args.manifest, expected_plan_hash=args.expect_plan_sha256)), sort_keys=True)
    )


if __name__ == "__main__":
    main()
