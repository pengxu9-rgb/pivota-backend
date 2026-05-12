#!/usr/bin/env python3
"""Review-gated PDP identity graph repair.

Dry-run is read-only. Apply mode writes only high-confidence deterministic
identity edges: product_group_members, external_product_seeds.attached_product_key,
and catalog_products.pdp_scope. It never writes external_product_seeds.seed_data
or catalog_products.product_payload.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from services.pdp_identity_recovery import (  # noqa: E402
    DEFAULT_PROPOSER,
    apply_recovery_proposals,
    build_recovery_proposals,
)


async def _run(args: argparse.Namespace) -> int:
    if args.apply and args.dry_run:
        raise SystemExit("--apply and --dry-run are mutually exclusive")
    if not args.apply and not args.dry_run:
        raise SystemExit("Choose --dry-run or --apply")

    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        report = await build_recovery_proposals(limit=args.limit, offset=args.offset)
        high_confidence = [p for p in report.get("proposals", []) if p.get("high_confidence")]
        report["dry_run"] = bool(args.dry_run)
        report["apply"] = bool(args.apply)
        report["proposer"] = args.proposer
        report["high_confidence_count"] = len(high_confidence)
        if args.apply:
            report["apply_result"] = await apply_recovery_proposals(
                high_confidence,
                proposer=args.proposer,
            )
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--proposer", default=DEFAULT_PROPOSER)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
