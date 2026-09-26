#!/usr/bin/env python3
"""Relink existing retailer listings onto the brand's own product (identity engine attach_membership).

See services/identity_brand_link.py for the rule. Each step is explicit; nothing is written by default.

  Dry run (default): print what would be proposed.
    python -m scripts.link_retailer_listings_to_brand_products
  Write the proposals (status 'proposed'):
    python -m scripts.link_retailer_listings_to_brand_products --propose
  Approve this strategy's proposed rows:
    python -m scripts.link_retailer_listings_to_brand_products --approve --by "<who>"
  Apply the approved ones (drift-guarded, one transaction), then rebuild the touched views:
    python -m scripts.link_retailer_listings_to_brand_products --apply
  Undo a run (per listing, only when both its content_key and group still hold this run's values),
  then rebuild the views:
    python -m scripts.link_retailer_listings_to_brand_products --revert <run_id>
  Re-run only the view rebuild for an applied run (after a counted refresh error):
    python -m scripts.link_retailer_listings_to_brand_products --refresh <run_id>

Apply only once PR #2384 is live: a moved listing stays is_primary (as every grouped retailer listing
is), and #2384's brand-store rank is what keeps the brand's own row the served canonical in its group.

Needs DATABASE_URL: run it through scripts/ops/run_oneoff_job.sh.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.identity_brand_link import STRATEGY, load_and_build, refresh_after_move  # noqa: E402
from services.identity_resolution import (  # noqa: E402
    RUN_EVENTS_SQL,
    apply_approved,
    approve_proposals,
    revert_run,
    upsert_proposals,
)

REVERT_EVENT_SQL = """
SELECT detail FROM identity_resolution_events WHERE run_id = $1 AND action = 'reverted'
ORDER BY id DESC LIMIT 1
"""

PROPOSED_IDS_SQL = """
SELECT proposal_id FROM identity_resolution_proposals
WHERE strategy = $1 AND kind = 'attach_membership' AND status = 'proposed'
ORDER BY created_at
"""


def _details(rows: List[Any]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        d = r["detail"]
        out.append(json.loads(d or "{}") if isinstance(d, str) else dict(d or {}))
    return [d for d in out if d.get("attached")]


async def _refresh(details: List[Dict[str, Any]], source: str) -> Dict[str, int]:
    from db.database import database

    await database.connect()
    try:
        return await refresh_after_move(details, source=source)
    finally:
        await database.disconnect()


async def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--propose", action="store_true")
    mode.add_argument("--approve", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--revert", metavar="RUN_ID")
    mode.add_argument("--refresh", metavar="RUN_ID")
    ap.add_argument("--by", help="who approves (with --approve)")
    args = ap.parse_args(argv)
    if args.approve and not (args.by or "").strip():
        ap.error("--approve needs --by")

    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=30, command_timeout=300)
    try:
        if args.refresh:
            reverted = await conn.fetchrow(REVERT_EVENT_SQL, args.refresh)
            if reverted:  # a reverted run: rebuild in the revert's order, from what the revert moved back
                d = reverted["detail"]
                d = json.loads(d or "{}") if isinstance(d, str) else dict(d or {})
                details = [x for x in d.get("detached") or [] if x.get("attached")]
            else:
                details = _details(await conn.fetch(RUN_EVENTS_SQL, args.refresh))
            result = {"run_id": args.refresh, "refresh": await _refresh(details, STRATEGY)}
        elif args.revert:
            result = await revert_run(conn, args.revert)
            result["refresh"] = await _refresh(result.get("detached") or [], f"{STRATEGY}_revert")
        elif args.apply:
            result = await apply_approved(conn, strategies=[STRATEGY])
            details = _details(await conn.fetch(RUN_EVENTS_SQL, result["run_id"]))
            result["refresh"] = await _refresh(details, STRATEGY)
        elif args.approve:
            ids = [r["proposal_id"] for r in await conn.fetch(PROPOSED_IDS_SQL, STRATEGY)]
            result = {"approved": await approve_proposals(conn, ids, args.by.strip()) if ids else []}
        else:
            proposals, counts = await load_and_build(conn)
            result = {"counts": counts, "sample": [
                {k: p["evidence"].get(k) for k in ("listing_title", "brand", "listing_product_key")}
                | {"joins": p["keeper_product_key"]} for p in proposals[:60]]}
            if args.propose:
                result["written"] = await upsert_proposals(conn, proposals)
        print("RESULT " + json.dumps(result, default=str), flush=True)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
