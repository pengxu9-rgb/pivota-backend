"""Run the Stage 2b-ii variant promoter.

Service: services.catalog_variant_promoter.promote_variants_all
Plan: plans/rosy-mixing-bengio.md Stage 2b-ii

Usage:
  # Dry-run scoped to a single product_group (deepest spot-check)
  python3 scripts/run_variant_promoter.py \
      --product-group-id pg_a363cbe4bc721b724168df4282713e6c

  # Dry-run scoped to a single merchant
  python3 scripts/run_variant_promoter.py --merchant-id external_seed

  # Apply on that scope
  python3 scripts/run_variant_promoter.py --merchant-id external_seed --apply

  # Full sweep across all multi-member product_groups
  python3 scripts/run_variant_promoter.py --apply --limit 0

Output is JSON. The per_group array lists each processed group with
its variants_found / variants_promoted counts + sample titles.
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
from services.catalog_variant_promoter import promote_variants_all  # noqa: E402

logger = logging.getLogger(__name__)


async def _drive(args: argparse.Namespace) -> dict:
    if not getattr(database, "is_connected", False):
        await database.connect()

    effective_limit = args.limit if args.limit > 0 else 100000

    report = await promote_variants_all(
        product_group_id=args.product_group_id,
        merchant_id=args.merchant_id,
        apply=args.apply,
        limit=effective_limit,
    )
    return {
        "mode": "apply" if args.apply else "dry_run",
        "scope": {
            "product_group_id": args.product_group_id,
            "merchant_id": args.merchant_id,
            "limit": args.limit,
        },
        "totals": {
            "groups_considered": report.groups_considered,
            "groups_promoted": report.groups_promoted,
            "groups_skipped_no_real_variants": report.groups_skipped_no_real_variants,
            "groups_skipped_no_primary": report.groups_skipped_no_primary,
            "skus_upserted_total": report.skus_upserted_total,
        },
        "per_group": [asdict(g) for g in report.per_group],
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually UPSERT catalog_skus rows. Default: dry-run.",
    )
    p.add_argument(
        "--product-group-id", type=str, default=None,
        help="Scope to one product_group (deepest spot-check).",
    )
    p.add_argument(
        "--merchant-id", type=str, default=None,
        help="Scope to groups owned by this merchant.",
    )
    p.add_argument(
        "--limit", type=int, default=100,
        help="Cap on groups processed (0 = all). Default 100.",
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
