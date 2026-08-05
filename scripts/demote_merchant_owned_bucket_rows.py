#!/usr/bin/env python3
"""Remediate the parked `merchant_owned` BUCKET cohort — round-14 finding on
PR #1681.

THE COHORT (measured 2026-08-05): 5,357 bucket rows
(merchant_id='external_seed') at pdp_scope='merchant_owned' — 3,337 stamped
`backfill_2026_05` (before the CLI backfill excluded bucket rows), 2,015
`external_brand_crawl` (before the onboard lane's bucket guard), 5
`kbeauty_cohort_promote` (a one-off whose script no longer exists). By the
workstream's own doctrine (PR #1680 F1/F2), `merchant_owned` is an AFFIRMED
single-merchant resolution that a bucket row — whose merchant_id is not a
seller at all — must never hold: it exits the `WHERE pdp_scope='unverified'`
gate every promotion writer checks, and P4's demotion does not select it (that
script filters `multi_merchant_canonical`). These rows are parked outside
every scope transition, permanently, unless demoted back to 'unverified'.

WHY DEMOTION IS SERVING-SAFE, STRUCTURALLY. The serving gate
(services/index_pipeline_state_service.py:256) is
`identity_resolved = has product_group_members row OR resolved pdp_scope`.
Measured 2026-08-05: every one of the 5,357 has a group row — demotion changes
identity_resolved for ZERO rows. This script makes that measurement a RAIL,
not an assumption: the selection requires the group row to exist, the
write-time quals re-check it (a row whose group vanishes mid-run is skipped,
not unserved), and rows WITHOUT a group are reported as `held_no_group` and
left untouched for a separate decision. Ranking is unaffected either way —
the +200 term is `multi_merchant_canonical` only
(services/pivot_query_service.py:1048); `merchant_owned` and `'unverified'`
rank identically.

SUPPRESSED ROWS ARE INCLUDED (delta from P4, deliberate): scope should state
the truth independently of suppression. A suppressed row neither serves nor
gets promoted (the D3 cron gates `suppressed_at IS NULL`), so demoting it is
invisible today — but leaving it `merchant_owned` re-parks the defect the
moment someone unsuppresses it. P4 excluded suppressed rows because its
cohort's ranking exposure was the point; this cohort's point is reachability.

AFTER THE APPLY: demoted rows are back inside continuous re-qualification.
The genuinely multi-seller ones are promoted by the next D3 cron tick — that
is the system working, not a leak; the dry run reports the expected count as
`would_promote_next_tick` (the shared CANONICAL_SCOPE_PREDICATE, interpolated,
never retyped, over the unsuppressed candidates).

Dry-run by default — `--apply` to write. Every planned row's previous
(scope, source, set_at) is reported BEFORE any write; `--report-file` for the
reversibility record. The `--apply` run against prod is an OPERATOR action.

Usage:
  python -m scripts.demote_merchant_owned_bucket_rows
  python -m scripts.demote_merchant_owned_bucket_rows --apply --report-file mo.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from services.pdp_identity_recovery import CANONICAL_SCOPE_PREDICATE  # noqa: E402
from services.pdp_scope_classifier import NON_SELLER_MERCHANT_ID  # noqa: E402

logger = logging.getLogger("demote_merchant_owned_bucket_rows")

# pdp_scope_source marker (varchar(32)). Greppable back to this script.
DEMOTION_SOURCE = "mo_bucket_demotion"

# The row→group identity join, spelled exactly as the promotion predicate and
# every other consumer spell it (merchant_id + platform + platform_product_id).
_HAS_GROUP_SQL = """EXISTS (
        SELECT 1 FROM product_group_members pgm
        WHERE pgm.merchant_id = cp.merchant_id
          AND pgm.platform = cp.platform
          AND pgm.platform_product_id = cp.source_product_id
      )"""

_SELECT_SQL = """
    SELECT cp.product_key, cp.pdp_scope, cp.pdp_scope_source, cp.pdp_scope_set_at,
           (cp.suppressed_at IS NOT NULL) AS is_suppressed,
           {has_group} AS has_group,
           (cp.suppressed_at IS NULL AND (
{predicate}
           )) AS would_promote
    FROM catalog_products cp
    WHERE cp.merchant_id = :bucket
      AND cp.pdp_scope = 'merchant_owned'
    ORDER BY cp.product_key
    LIMIT :limit
""".format(has_group=_HAS_GROUP_SQL, predicate=CANONICAL_SCOPE_PREDICATE)


async def fetch_demotion_candidates(limit: int = 10_000) -> List[Dict[str, Any]]:
    """ALL merchant_owned bucket rows — suppressed included (module docstring).
    `has_group` decides demote-vs-hold; `would_promote` is the next-tick
    forecast under the shared predicate."""
    rows = await database.fetch_all(
        _SELECT_SQL, {"bucket": NON_SELLER_MERCHANT_ID, "limit": limit}
    )
    return [dict(r) for r in rows or []]


async def demote_row(product_key: str) -> bool:
    """CAS demotion of one row. Returns True if the row was written.

    EVERY load-bearing condition is in the WRITE-TIME quals — the PR #1680
    round-11 lesson (EvalPlanQual re-evaluates only the outer quals after a
    lock wait): the pre-image scope, the bucket merchant, AND the group's
    existence are all re-checked at the write. A row promoted, re-merchanted,
    or de-grouped between select and write is skipped, not clobbered — and
    never unserved.
    """
    row = await database.fetch_one(
        """
        UPDATE catalog_products cp
        SET pdp_scope = 'unverified',
            pdp_scope_source = :src,
            pdp_scope_set_at = NOW()
        WHERE cp.product_key = :pk
          AND cp.pdp_scope = 'merchant_owned'
          AND cp.merchant_id = :bucket
          AND {has_group}
        RETURNING cp.product_key
        """.format(has_group=_HAS_GROUP_SQL),
        {"pk": product_key, "src": DEMOTION_SOURCE,
         "bucket": NON_SELLER_MERCHANT_ID},
    )
    return row is not None


async def run_demotion(*, apply: bool, limit: int = 10_000) -> Dict[str, Any]:
    """One pass. Dry-run returns the full plan; apply executes it with CAS.
    Group-less rows are NEVER written — held and reported."""
    candidates = await fetch_demotion_candidates(limit)
    planned = [c for c in candidates if c["has_group"]]
    held = [c for c in candidates if not c["has_group"]]

    by_source: Dict[str, int] = {}
    for c in candidates:
        src = str(c["pdp_scope_source"])
        by_source[src] = by_source.get(src, 0) + 1

    report: Dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "candidates": len(candidates),
        "planned": len(planned),
        "by_source": by_source,
        "suppressed_in_plan": sum(1 for c in planned if c["is_suppressed"]),
        "would_promote_next_tick": sum(1 for c in planned if c["would_promote"]),
        "held_no_group": [c["product_key"] for c in held],
        "previous_values": [
            {
                "product_key": c["product_key"],
                "pdp_scope": c["pdp_scope"],
                "pdp_scope_source": c["pdp_scope_source"],
                "pdp_scope_set_at": str(c["pdp_scope_set_at"]),
            }
            for c in planned
        ],
        "demoted": 0,
        "cas_skipped": 0,
    }
    if not apply:
        return report
    for c in planned:
        if await demote_row(c["product_key"]):
            report["demoted"] += 1
        else:
            report["cas_skipped"] += 1
    return report


async def _main(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        report = await run_demotion(apply=args.apply, limit=args.limit)
    finally:
        await database.disconnect()

    out = json.dumps(report, indent=1)
    if args.report_file:
        Path(args.report_file).write_text(out)
        summary = {k: v for k, v in report.items() if k != "previous_values"}
        print(json.dumps(summary, indent=1))
        print(f"previous values -> {args.report_file}")
    else:
        print(out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="execute the demotion (default: dry run)")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--report-file", default=None,
                        help="write the full reversibility record here")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
