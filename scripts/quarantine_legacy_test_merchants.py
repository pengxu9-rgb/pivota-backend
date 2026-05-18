#!/usr/bin/env python3
"""Quarantine legacy test merchants without deleting historical rows.

Usage:
  # Dry-run
  python3 scripts/quarantine_legacy_test_merchants.py --merchant-id <merchant_id>

  # Apply
  python3 scripts/quarantine_legacy_test_merchants.py --merchant-id <merchant_id> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# This is a short-lived operator script; keep the production pool tiny.
os.environ.setdefault("DB_POOL_MIN_SIZE", "1")
os.environ.setdefault("DB_POOL_MAX_SIZE", "2")
if os.getenv("DATABASE_PUBLIC_URL"):
    os.environ["DATABASE_URL"] = os.getenv("DATABASE_PUBLIC_URL", "")

from db.database import database  # noqa: E402


logger = logging.getLogger("quarantine_legacy_test_merchants")


COUNT_SQL = {
    "merchant_stores_active": """
        SELECT COUNT(*) AS n
        FROM merchant_stores
        WHERE merchant_id = :merchant_id
          AND lower(coalesce(status, '')) IN ('active', 'connected')
    """,
    "catalog_merchants_active": """
        SELECT COUNT(*) AS n
        FROM catalog_merchants
        WHERE merchant_id = :merchant_id
          AND lower(coalesce(status, 'active')) = 'active'
    """,
    "products_cache_rows": """
        SELECT COUNT(*) AS n
        FROM products_cache
        WHERE merchant_id = :merchant_id
    """,
    "catalog_products_rows": """
        SELECT COUNT(*) AS n
        FROM catalog_products
        WHERE merchant_id = :merchant_id
    """,
}


APPLY_SQL = (
    """
    UPDATE merchant_stores
    SET status = 'inactive'
    WHERE merchant_id = :merchant_id
      AND lower(coalesce(status, '')) IN ('active', 'connected')
    """,
    """
    UPDATE catalog_merchants
    SET status = 'inactive',
        updated_at = NOW()
    WHERE merchant_id = :merchant_id
      AND lower(coalesce(status, 'active')) = 'active'
    """,
)


async def _count(label: str, sql: str, merchant_id: str) -> Optional[int]:
    try:
        row = await database.fetch_one(sql, {"merchant_id": merchant_id})
        return int(dict(row).get("n") or 0) if row else 0
    except Exception as exc:
        logger.warning("%s count skipped for merchant=%s err=%s", label, merchant_id, exc)
        return None


async def _drive(args: argparse.Namespace) -> None:
    merchant_ids = [value.strip() for value in args.merchant_id if value.strip()]
    if not merchant_ids:
        raise SystemExit("--merchant-id is required")

    await database.connect()
    try:
        print("=== Pre-quarantine counts ===")
        for merchant_id in merchant_ids:
            print(f"\nmerchant_id={merchant_id}")
            for label, sql in COUNT_SQL.items():
                value = await _count(label, sql, merchant_id)
                print(f"  {label}: {'skipped' if value is None else value}")

        if not args.apply:
            print("\nDRY-RUN - no writes. Re-run with --apply to quarantine.")
            return

        for merchant_id in merchant_ids:
            for sql in APPLY_SQL:
                await database.execute(sql, {"merchant_id": merchant_id})

        print("\nAPPLY complete.")
        print("\n=== Post-quarantine active counts ===")
        for merchant_id in merchant_ids:
            store_count = await _count("merchant_stores_active", COUNT_SQL["merchant_stores_active"], merchant_id)
            catalog_count = await _count(
                "catalog_merchants_active",
                COUNT_SQL["catalog_merchants_active"],
                merchant_id,
            )
            print(f"merchant_id={merchant_id} active_stores={store_count} active_catalog_merchants={catalog_count}")
    finally:
        try:
            await database.disconnect()
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merchant-id", action="append", default=[], help="Merchant ID to quarantine.")
    parser.add_argument("--apply", action="store_true", help="Write quarantine updates. Default is dry-run.")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_drive(_parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
