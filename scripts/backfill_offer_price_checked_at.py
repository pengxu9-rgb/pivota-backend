#!/usr/bin/env python3
"""Date the offers whose price IS the one the seed refresh last read (migration 246).

`catalog_offers.price_checked_at` starts NULL everywhere. From the deploy on, the refresh stamps
every listing offer it re-prices (services/external_offer_dual_write). This fills in the rows
whose current price the refresh ALREADY read, so they need not wait for their turn in the nightly
rotation. It never stamps a price nobody read.

AN OFFER IS STAMPED ONLY WHEN ALL OF THESE HOLD:
  * its seed was read: `external_product_seeds.last_crawled_at` ("we actually read the price,
    from the URL we serve", migration 202), and that stamp is the date written;
  * the seed's stored price is the one that read produced: `price_amount` equals the snapshot's
    `price_amount`, so a later employee edit is not dated by an older crawl;
  * the offer is the seed's own listing (`is_listing_offer`, the attached lane's rule), live, and
    its product-level row (`<product_key>::canonical`). A variant row carries its variant's price,
    which this does not compare;
  * the offer's price and currency ARE the seed's, to the cent;
  * the offer has no stamp yet (a write is fill-only and never overwrites a later read).
The UPDATE re-checks price, currency and suppression, so a row that moved after the read is
skipped rather than dated. `updated_at` is deliberately left alone: this changes no offer truth.

    python -m scripts.backfill_offer_price_checked_at             # dry run: counts only
    python -m scripts.backfill_offer_price_checked_at --apply

Refuses to run until migration 246's column and trigger exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List, Optional

from db.database import database
from services.external_offer_dual_write import SKU_SUFFIX, is_listing_offer

PRICE_TOLERANCE = 0.005

PREFLIGHT_SQL = """
SELECT
  EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('catalog_offers')
          AND attname = 'price_checked_at' AND attnum > 0 AND NOT attisdropped) AS has_column,
  EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('catalog_offers')
          AND tgname = 'trg_catalog_offers_forget_unread_price_check' AND NOT tgisinternal) AS has_trigger
"""

SEEDS_SQL = """
SELECT id, destination_url, attached_product_key, price_amount, price_currency, last_crawled_at,
       seed_data -> 'snapshot' ->> 'price_amount' AS snapshot_price_amount
FROM external_product_seeds
WHERE last_crawled_at IS NOT NULL
  AND attached_product_key IS NOT NULL
  AND id > :after
ORDER BY id
LIMIT :limit
"""

OFFERS_SQL = """
SELECT o.offer_id, o.product_key, o.sku_key, o.merchant_id, o.currency, o.source_ref,
       o.list_price, o.merchant_effective_price, o.price_checked_at,
       o.offer_payload ->> 'destination_url' AS payload_destination_url,
       o.offer_payload ->> 'external_seed_id' AS payload_seed_id,
       o.suppressed_at IS NOT NULL AS suppressed
FROM catalog_offers o
WHERE o.product_key = ANY(:keys)
"""

# Fill-only and re-checked on the row as it is NOW. RETURNING + fetch_all so the count is of rows
# that changed, not of statements sent (tests/test_write_accounting_tripwire.py).
STAMP_SQL = """
UPDATE catalog_offers
SET price_checked_at = :checked_at
WHERE offer_id = :offer_id
  AND price_checked_at IS NULL
  AND suppressed_at IS NULL
  AND upper(trim(coalesce(currency, ''))) = :currency
  AND abs(coalesce(merchant_effective_price, list_price) - :price) < 0.005
RETURNING offer_id
"""


def _price(value: Any) -> Optional[float]:
    try:
        price = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return price if price is not None and price > 0 else None


def plan_seed(seed: Dict[str, Any], offers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure: which of this seed's offers the backfill dates, and why the rest are left alone.

    Returns {"writes": [{offer_id, checked_at, price, currency}], "skips": {reason: n}}."""
    skips: Dict[str, int] = {}

    def skip(reason: str, n: int = 1) -> Dict[str, Any]:
        skips[reason] = skips.get(reason, 0) + n
        return {"writes": [], "skips": skips}

    checked_at = seed.get("last_crawled_at")
    if checked_at is None:
        return skip("seed_never_read")
    seed_price = _price(seed.get("price_amount"))
    currency = str(seed.get("price_currency") or "").strip().upper()
    if seed_price is None or not currency:
        return skip("seed_has_no_price")
    snapshot_price = _price(seed.get("snapshot_price_amount"))
    if snapshot_price is None or abs(snapshot_price - seed_price) >= PRICE_TOLERANCE:
        return skip("seed_price_not_from_its_read")

    product_key = str(seed.get("attached_product_key") or "")
    writes = []
    listing = [o for o in offers if o.get("product_key") == product_key and is_listing_offer(o, seed)]
    if not listing:
        return skip("no_listing_offer")
    for offer in listing:
        if offer.get("suppressed"):
            skip("suppressed")
        elif offer.get("sku_key") != f"{product_key}{SKU_SUFFIX}":
            skip("variant_row")
        elif offer.get("price_checked_at") is not None:
            skip("already_stamped")
        elif str(offer.get("currency") or "").strip().upper() != currency:
            skip("currency_differs")
        else:
            offer_price = _price(offer.get("merchant_effective_price")) or _price(offer.get("list_price"))
            if offer_price is None or abs(offer_price - seed_price) >= PRICE_TOLERANCE:
                skip("price_differs")
            else:
                writes.append({"offer_id": offer["offer_id"], "checked_at": checked_at,
                               "price": seed_price, "currency": currency})
    return {"writes": writes, "skips": skips}


async def run_backfill(db, *, apply: bool, batch: int = 500, max_seeds: int = 0) -> Dict[str, Any]:
    """Plan (and with `apply`, write) over every read seed. Returns the summary; `refused` when
    migration 246 is not in place, because without its trigger a stamp could outlive its price."""
    preflight = dict(await db.fetch_one(PREFLIGHT_SQL))
    if not (preflight["has_column"] and preflight["has_trigger"]):
        return {"refused": "migration_246_not_applied", **preflight}
    totals: Dict[str, Any] = {"apply": apply, "seeds": 0, "planned": 0, "written": 0,
                              "changed_since_read": 0, "skips": {}}
    after = ""
    while True:
        seeds = [dict(r) for r in await db.fetch_all(SEEDS_SQL, {"after": after, "limit": batch})]
        if not seeds:
            break
        after = seeds[-1]["id"]
        keys = sorted({s["attached_product_key"] for s in seeds})
        by_key: Dict[str, List[Dict[str, Any]]] = {}
        for row in await db.fetch_all(OFFERS_SQL, {"keys": keys}):
            by_key.setdefault(row["product_key"], []).append(dict(row))
        for seed in seeds:
            totals["seeds"] += 1
            plan = plan_seed(seed, by_key.get(seed["attached_product_key"], []))
            for reason, n in plan["skips"].items():
                totals["skips"][reason] = totals["skips"].get(reason, 0) + n
            totals["planned"] += len(plan["writes"])
            if not apply:
                continue
            for write in plan["writes"]:
                if await db.fetch_all(STAMP_SQL, write):
                    totals["written"] += 1
                else:
                    totals["changed_since_read"] += 1
        if max_seeds and totals["seeds"] >= max_seeds:
            break
    return totals


async def _run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        summary = await run_backfill(database, apply=args.apply, batch=args.batch, max_seeds=args.max_seeds)
    finally:
        await database.disconnect()
    # A text prefix keeps the line in textPayload, where run_oneoff_job.sh prints it.
    print("PRICE_CHECK_BACKFILL " + json.dumps(summary, sort_keys=True, default=str))
    return 2 if summary.get("refused") else 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="write the stamps (else dry-run counts)")
    p.add_argument("--batch", type=int, default=500, help="seeds per read")
    p.add_argument("--max-seeds", type=int, default=0, help="stop after this many seeds (0 = all)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
