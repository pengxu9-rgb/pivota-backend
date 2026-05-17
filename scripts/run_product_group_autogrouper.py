"""Run the Stage 2b-i content_key → product_group_members autogrouper.

Service: services.product_group_autogrouper.autogroup_clusters
Plan: plans/rosy-mixing-bengio.md Stage 2b-i

Usage:
  # Dry-run scoped to a single merchant (recommended first pass)
  python3 scripts/run_product_group_autogrouper.py --merchant-id <merchant_id>

  # Apply on that merchant
  python3 scripts/run_product_group_autogrouper.py --merchant-id <merchant_id> --apply

  # Dry-run scoped to a single content_key (deepest spot-check)
  python3 scripts/run_product_group_autogrouper.py \
      --content-key ck_32de31827aded89c8d0339895b6a2786

  # Full catalog (NO scope) — only after spot-checks pass on samples
  python3 scripts/run_product_group_autogrouper.py --apply --limit 0

Output is JSON. The per_cluster array lists each grouped cluster with
its derived product_group_id, member_count, primary_product_key, and
whether the run was dry or applied.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.product_group_autogrouper import autogroup_clusters  # noqa: E402

logger = logging.getLogger(__name__)


async def _drive(args: argparse.Namespace) -> dict:
    if not getattr(database, "is_connected", False):
        await database.connect()

    # limit=0 means "all clusters" — pass a large sentinel that the
    # SELECT's ORDER BY content_key + LIMIT will still page sensibly.
    # We deliberately don't drop the LIMIT entirely: a runaway dataset
    # is better to cap and warn than to OOM.
    effective_limit = args.limit if args.limit > 0 else 100000

    report = await autogroup_clusters(
        content_key=args.content_key,
        merchant_id=args.merchant_id,
        apply=args.apply,
        limit=effective_limit,
    )
    return {
        "mode": "apply" if args.apply else "dry_run",
        "scope": {
            "content_key": args.content_key,
            "merchant_id": args.merchant_id,
            "limit": args.limit,
        },
        "totals": {
            "clusters_considered": report.clusters_considered,
            "clusters_grouped": report.clusters_grouped,
            "members_upserted_total": report.members_upserted_total,
        },
        "per_cluster": [asdict(o) for o in report.per_cluster],
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually UPSERT product_group_members. Default: dry-run.",
    )
    p.add_argument(
        "--content-key", type=str, default=None,
        help="Scope to one content_key (deepest spot-check; ignores --merchant-id).",
    )
    p.add_argument(
        "--merchant-id", type=str, default=None,
        help="Scope to clusters where this merchant has >=2 rows.",
    )
    p.add_argument(
        "--limit", type=int, default=100,
        help="Cap on clusters processed (0 = all). Default 100.",
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
