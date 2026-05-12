#!/usr/bin/env python3
"""Read-only PDP identity graph coverage audit.

Outputs JSON for:
  - verified merchant anchors vs suspect/historical cache cohorts
  - mirrored external_seed catalog products missing product_group_members
  - prod:: and ext:* attachment coverage
  - review-only exact title+brand multi-domain candidates
  - high-confidence recovery proposals the writer would apply
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
    DEFAULT_SUSPECT_MERCHANT_IDS,
    build_identity_graph_audit_report,
)


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        report = await build_identity_graph_audit_report(
            limit=args.limit,
            offset=args.offset,
            suspect_merchant_ids=args.suspect_merchant_id,
        )
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--suspect-merchant-id",
        action="append",
        default=list(DEFAULT_SUSPECT_MERCHANT_IDS),
        help="Merchant IDs to treat as suspect/historical cache cohorts. May be repeated.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
