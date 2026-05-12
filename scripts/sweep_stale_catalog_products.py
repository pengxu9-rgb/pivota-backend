"""Sweep stale catalog_products rows (Stage 2a).

Tombstones rows whose upstream Shopify products were deleted by the
merchant. This is the cleanest fix for the "old expired caches"
duplicate class — the MOYU cohort where one product accumulated 26
sequential Shopify IDs as it was deleted/recreated during catalog
setup.

Mechanic (industry-standard catalog hygiene, similar to Algolia /
Searchspring):

  1. catalog_products.last_seen_in_sync_at — bumped to NOW() by Path A
     on every UPSERT (services.catalog_sync_service.ingest_standard_products).
  2. catalog_merchants.last_full_sync_at — bumped to NOW() at the end
     of each successful ingest_standard_products run.
  3. This sweep: for each merchant with a non-NULL last_full_sync_at,
     find rows where last_seen_in_sync_at < (last_full_sync_at -
     GRACE_HOURS). Flip sync_status='stale'. Stale rows older than
     ARCHIVE_DAYS go to 'archived'.

GRACE_HOURS (default 24h) gives a buffer so a single failed sync run
doesn't tombstone everything — only when a row has missed multiple
syncs does it actually get flagged. ARCHIVE_DAYS (default 7) is the
"definitely deleted" window.

Scope:
  - Excludes external_seed (merchant_id='external_seed') — Path B/C
    rows have a different lifecycle and aren't sync-tombstoned.
  - The catalog_merchants.last_full_sync_at IS NOT NULL filter already
    excludes external_seed naturally (no Path A sync ever ran for it).

Usage:
  # Dry-run: report what would change, no writes
  python3 scripts/sweep_stale_catalog_products.py

  # Apply
  python3 scripts/sweep_stale_catalog_products.py --apply

  # Scope to one merchant for spot-checking
  python3 scripts/sweep_stale_catalog_products.py --merchant-id merch_efbc46b4619cfbdf

  # Tweak thresholds
  python3 scripts/sweep_stale_catalog_products.py \
      --grace-hours 48 --archive-days 14 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

logger = logging.getLogger(__name__)


# Live merchants (anything with a real Path A sync). external_seed
# never gets a last_full_sync_at because it's Path B/C only, so the
# IS NOT NULL filter excludes it. Belt-and-suspenders: also explicit
# != 'external_seed' for clarity.
SELECT_MERCHANTS_SQL = """
    SELECT merchant_id, last_full_sync_at
    FROM catalog_merchants
    WHERE last_full_sync_at IS NOT NULL
      AND merchant_id != 'external_seed'
"""


# Per-merchant find: rows that haven't been seen in the most recent
# full sync. Two cases:
#   (a) last_seen_in_sync_at IS NULL — legacy rows predating mig 084.
#       Conservative: only tombstone these if they're older than the
#       grace window AND haven't been touched recently. We use the
#       row's created_at as a fallback "last activity" signal.
#   (b) last_seen_in_sync_at < merchant's last_full_sync_at - grace.
#       Clear signal: this row was supposed to appear in the recent
#       sync but didn't. Upstream deleted it.
FIND_STALE_SQL = """
    SELECT product_key,
           last_seen_in_sync_at,
           sync_status,
           created_at
    FROM catalog_products
    WHERE merchant_id = :merchant_id
      AND sync_status = 'live'
      AND (
        (last_seen_in_sync_at IS NOT NULL
         AND last_seen_in_sync_at < :stale_before)
        OR
        (last_seen_in_sync_at IS NULL
         AND created_at < :stale_before)
      )
"""


# For tombstoned rows that have been stale long enough, archive them.
# 'archived' means: out of recall, but the row is preserved for sig_*
# URL redirect compatibility (an LLM might still cite the old URL).
FIND_ARCHIVE_SQL = """
    SELECT product_key, updated_at
    FROM catalog_products
    WHERE merchant_id = :merchant_id
      AND sync_status = 'stale'
      AND updated_at < :archive_before
"""


UPDATE_TO_STALE_SQL = """
    UPDATE catalog_products
    SET sync_status = 'stale',
        updated_at = NOW()
    WHERE product_key = :product_key
      AND sync_status = 'live'
"""


UPDATE_TO_ARCHIVED_SQL = """
    UPDATE catalog_products
    SET sync_status = 'archived',
        updated_at = NOW()
    WHERE product_key = :product_key
      AND sync_status = 'stale'
"""


async def _sweep_merchant(
    *,
    merchant_id: str,
    last_full_sync_at: Any,
    grace_hours: int,
    archive_days: int,
    apply: bool,
) -> Dict[str, Any]:
    """Sweep one merchant. Returns counters + sample rows."""
    # GRACE_HOURS before last_full_sync_at is the "stale" threshold.
    # We compute it in Python with an interval expression in the SQL
    # to avoid timezone surprises across Python/PG boundaries.
    import datetime as _dt
    if last_full_sync_at is None:
        return {"merchant_id": merchant_id, "skipped_reason": "no_sync_yet"}
    if not isinstance(last_full_sync_at, _dt.datetime):
        return {"merchant_id": merchant_id, "skipped_reason": "bad_sync_timestamp"}

    stale_before = last_full_sync_at - _dt.timedelta(hours=grace_hours)
    # Archive cutoff is independent: stale rows that have been stale
    # for archive_days get archived. We approximate "how long has it
    # been stale" by the row's updated_at (the sweep bumps updated_at
    # when it sets sync_status='stale').
    archive_before = _dt.datetime.now(tz=last_full_sync_at.tzinfo) - _dt.timedelta(days=archive_days)

    stale_candidates = await database.fetch_all(
        FIND_STALE_SQL,
        {"merchant_id": merchant_id, "stale_before": stale_before},
    )
    archive_candidates = await database.fetch_all(
        FIND_ARCHIVE_SQL,
        {"merchant_id": merchant_id, "archive_before": archive_before},
    )

    stale_count = 0
    archived_count = 0
    samples_stale: List[Dict[str, Any]] = []
    samples_archive: List[Dict[str, Any]] = []

    for row in stale_candidates or []:
        row_dict = dict(row)
        if len(samples_stale) < 3:
            samples_stale.append(row_dict)
        if not apply:
            stale_count += 1
            continue
        rc = await database.execute(UPDATE_TO_STALE_SQL, {"product_key": row_dict["product_key"]})
        if rc is None or rc:
            stale_count += 1

    for row in archive_candidates or []:
        row_dict = dict(row)
        if len(samples_archive) < 3:
            samples_archive.append(row_dict)
        if not apply:
            archived_count += 1
            continue
        rc = await database.execute(UPDATE_TO_ARCHIVED_SQL, {"product_key": row_dict["product_key"]})
        if rc is None or rc:
            archived_count += 1

    return {
        "merchant_id": merchant_id,
        "last_full_sync_at": last_full_sync_at.isoformat(),
        "stale_threshold_before": stale_before.isoformat(),
        "marked_stale": stale_count,
        "marked_archived": archived_count,
        "samples_stale": samples_stale,
        "samples_archive": samples_archive,
    }


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    if args.merchant_id:
        merchants = await database.fetch_all(
            "SELECT merchant_id, last_full_sync_at FROM catalog_merchants "
            "WHERE merchant_id = :m AND last_full_sync_at IS NOT NULL",
            {"m": args.merchant_id},
        )
    else:
        merchants = await database.fetch_all(SELECT_MERCHANTS_SQL, {})

    per_merchant: List[Dict[str, Any]] = []
    totals = {"marked_stale": 0, "marked_archived": 0, "merchants_swept": 0}

    for m in merchants or []:
        m_dict = dict(m)
        result = await _sweep_merchant(
            merchant_id=m_dict["merchant_id"],
            last_full_sync_at=m_dict["last_full_sync_at"],
            grace_hours=args.grace_hours,
            archive_days=args.archive_days,
            apply=args.apply,
        )
        per_merchant.append(result)
        if "marked_stale" in result:
            totals["marked_stale"] += result["marked_stale"]
            totals["marked_archived"] += result["marked_archived"]
            totals["merchants_swept"] += 1

    return {
        "mode": "apply" if args.apply else "dry_run",
        "grace_hours": args.grace_hours,
        "archive_days": args.archive_days,
        "totals": totals,
        "per_merchant": per_merchant,
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually UPDATE sync_status. Default: dry-run.",
    )
    p.add_argument(
        "--merchant-id", type=str, default=None,
        help="Scope to one merchant (e.g. for spot-checking MOYU). "
        "Omit to sweep all merchants with last_full_sync_at IS NOT NULL.",
    )
    p.add_argument(
        "--grace-hours", type=int, default=24,
        help="Rows whose last_seen_in_sync_at is older than (last_full_sync_at - grace) "
        "become 'stale'. Default 24h. Set higher to be more forgiving of intermittent "
        "sync failures.",
    )
    p.add_argument(
        "--archive-days", type=int, default=7,
        help="Stale rows older than this many days become 'archived' (out of recall, "
        "kept for redirect). Default 7.",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
