#!/usr/bin/env python3
"""One-shot cleanup for the (market, currency) and (market, domain)
inconsistencies in external_product_seeds + their downstream
catalog_offers / catalog_products rows.

Background: routes/employee_products.py CSV imports historically
accepted whatever values appeared in the spreadsheet, even when the
combination was nonsense (US market with EUR currency; US market with
.kr/.co.kr domain). This left ~469 EUR/US offer rows + ~12 KR-domain
US-market product rows in prod, surfacing as "$0 price" and
"Korean-language PDP" results in the shopping-agent chat.

Going-forward fix: validators in routes/employee_products.py reject
new mismatched rows at CSV import time. This script heals what's
already in the catalog.

What it does (in --apply mode):
  1. NULL price + currency on catalog_offers JOINed to external_product_seeds
     where eps.market='US' AND offer.currency != 'USD'. The chat will
     show "price unavailable" instead of EUR for US users.
  2. Set catalog_products.pdp_lifecycle_stage='archived' for rows
     whose external_product_seeds.domain is in a non-US TLD (.kr,
     .co.kr, .jp, .co.jp) but market='US'. The O-5 recall filter
     (validated|published) excludes archived → these rows stop
     appearing in chat.

Both operations are reversible (the source data in
external_product_seeds is untouched). Re-extracting after fixing the
CSV at the source restores correct values.

Usage:
  # Dry-run shows row counts that would be touched.
  python3 scripts/cleanup_csv_import_market_inconsistencies.py

  # Apply.
  python3 scripts/cleanup_csv_import_market_inconsistencies.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402


logger = logging.getLogger("cleanup_csv_import_market_inconsistencies")


COUNT_CURRENCY_MISMATCH_SQL = """
    SELECT COUNT(*) AS n
    FROM catalog_offers o
    JOIN catalog_products cp ON o.product_key = cp.product_key
    JOIN external_product_seeds eps ON eps.external_product_id = cp.source_product_id
    WHERE cp.merchant_id = 'external_seed'
      AND eps.market = 'US'
      AND o.currency IS NOT NULL
      AND o.currency != 'USD'
"""


COUNT_KR_DOMAIN_SQL = """
    SELECT COUNT(*) AS n
    FROM catalog_products cp
    JOIN external_product_seeds eps ON eps.external_product_id = cp.source_product_id
    WHERE cp.merchant_id = 'external_seed'
      AND eps.market = 'US'
      AND (eps.domain LIKE '%.kr' OR eps.domain LIKE '%.co.kr'
           OR eps.domain LIKE '%.jp' OR eps.domain LIKE '%.co.jp')
      AND (cp.pdp_lifecycle_stage IS NULL OR cp.pdp_lifecycle_stage != 'archived')
"""


# NULL out wrong-currency prices. Setting currency='USD' afterward keeps
# downstream JOINs stable; the price columns are NULL so the chat shows
# "price unavailable" rather than misleading "$X EUR for a US user".
FIX_CURRENCY_SQL = """
    UPDATE catalog_offers o
    SET list_price = NULL,
        merchant_effective_price = NULL,
        estimated_best_price = NULL,
        price_confidence = NULL,
        currency = 'USD',
        updated_at = NOW()
    FROM catalog_products cp, external_product_seeds eps
    WHERE o.product_key = cp.product_key
      AND eps.external_product_id = cp.source_product_id
      AND cp.merchant_id = 'external_seed'
      AND eps.market = 'US'
      AND o.currency IS NOT NULL
      AND o.currency != 'USD'
"""


# Archive non-US-domain products mistakenly mirrored under market=US.
# pdp_lifecycle_stage='archived' is excluded by the O-5 recall filter.
FIX_KR_DOMAIN_SQL = """
    UPDATE catalog_products cp
    SET pdp_lifecycle_stage = 'archived',
        updated_at = NOW()
    FROM external_product_seeds eps
    WHERE eps.external_product_id = cp.source_product_id
      AND cp.merchant_id = 'external_seed'
      AND eps.market = 'US'
      AND (eps.domain LIKE '%.kr' OR eps.domain LIKE '%.co.kr'
           OR eps.domain LIKE '%.jp' OR eps.domain LIKE '%.co.jp')
      AND (cp.pdp_lifecycle_stage IS NULL OR cp.pdp_lifecycle_stage != 'archived')
"""


async def _drive(args: argparse.Namespace) -> None:
    await database.connect()
    try:
        currency_count_row = await database.fetch_one(COUNT_CURRENCY_MISMATCH_SQL)
        domain_count_row = await database.fetch_one(COUNT_KR_DOMAIN_SQL)

        currency_count = int(dict(currency_count_row).get("n") or 0)
        domain_count = int(dict(domain_count_row).get("n") or 0)

        print()
        print("=== Pre-cleanup counts ===")
        print(f"  US-market offers with non-USD currency: {currency_count}")
        print(f"  US-market products on non-US domains:   {domain_count}")
        print()

        if not args.apply:
            print("DRY-RUN — no writes. Re-run with --apply to heal.")
            return

        # Apply currency fix
        await database.execute(FIX_CURRENCY_SQL)
        await database.execute(FIX_KR_DOMAIN_SQL)
        print("APPLY complete.")
        print()

        # Verify
        currency_after = await database.fetch_one(COUNT_CURRENCY_MISMATCH_SQL)
        domain_after = await database.fetch_one(COUNT_KR_DOMAIN_SQL)
        print("=== Post-cleanup counts ===")
        print(f"  US-market offers with non-USD currency: {int(dict(currency_after).get('n') or 0)} (expect 0)")
        print(f"  US-market products on non-US domains:   {int(dict(domain_after).get('n') or 0)} (expect 0)")
    finally:
        try:
            await database.disconnect()
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the cleanup. Default is dry-run.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    asyncio.run(_drive(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
