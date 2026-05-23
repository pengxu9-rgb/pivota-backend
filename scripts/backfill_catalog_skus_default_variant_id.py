#!/usr/bin/env python3
"""Backfill catalog_skus.source_variant_id rows that still use 'default'.

FIX-04 changes the SKU identity unique index to include product_key and changes
new writes to use product_key when no source variant id exists. This one-time
backfill rewrites legacy rows to the same shape.

Dry-run is the default:
  python scripts/backfill_catalog_skus_default_variant_id.py

Apply on staging first, then production only with explicit user authorization:
  DATABASE_URL=... python scripts/backfill_catalog_skus_default_variant_id.py --apply

Idempotency: only rows where source_variant_id = 'default' are updated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402


logger = logging.getLogger("backfill_catalog_skus_default_variant_id")


COUNT_DEFAULT_SQL = """
    SELECT COUNT(*) AS count
    FROM catalog_skus
    WHERE source_variant_id = 'default'
"""


UPDATE_DEFAULT_SQL = """
    UPDATE catalog_skus
    SET source_variant_id = product_key,
        updated_at = NOW()
    WHERE source_variant_id = 'default'
"""


CONFIRMATION_PHRASE = "UPDATE catalog_skus default variant ids"


def _count_from_row(row: Any) -> int:
    if row is None:
        return 0
    data = dict(row)
    return int(data.get("count") or data.get("COUNT(*)") or 0)


async def _count_default_rows() -> int:
    row = await database.fetch_one(COUNT_DEFAULT_SQL)
    return _count_from_row(row)


def _prompt_for_confirmation(count: int) -> bool:
    print()
    print(f"About to UPDATE {count} catalog_skus rows where source_variant_id = 'default'.")
    print("Run this on staging FIRST, then production only with explicit user authorization.")
    typed = input(f"Type '{CONFIRMATION_PHRASE}' to continue: ").strip()
    return typed == CONFIRMATION_PHRASE


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    pre_count = await _count_default_rows()
    report: Dict[str, Any] = {
        "apply": bool(args.apply),
        "pre_default_count": pre_count,
        "post_default_count": pre_count,
        "updated_count": 0,
        "cancelled": False,
    }

    if not args.apply:
        report["mode"] = "dry_run"
        return report

    if pre_count == 0:
        report["mode"] = "apply"
        return report

    if not _prompt_for_confirmation(pre_count):
        report["mode"] = "apply"
        report["cancelled"] = True
        return report

    async with database.transaction():
        pre_count_in_tx = await _count_default_rows()
        await database.execute(UPDATE_DEFAULT_SQL)
        post_count = await _count_default_rows()

    report["mode"] = "apply"
    report["pre_default_count_in_transaction"] = pre_count_in_tx
    report["post_default_count"] = post_count
    report["updated_count"] = max(0, pre_count_in_tx - post_count)
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually UPDATE catalog_skus. Default is dry-run.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, sort_keys=True))
    if report.get("cancelled"):
        logger.warning("cancelled before UPDATE; no rows changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
