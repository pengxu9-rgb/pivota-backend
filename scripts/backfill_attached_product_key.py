#!/usr/bin/env python3
"""Backfill external_product_seeds.attached_product_key by DETERMINISTIC-EXACT
seed→PDP matching (convergence P1.3).

Populates the identity-attachment key that was never a write-time invariant (the
seed→PDP matcher is a batch CLI with no live caller). EXACT signals only
(source_product_id / canonical_url); fuzzy title-trigram is left for HITL review.
Candidates are scope-gated to pdp_scope='multi_merchant_canonical'.

  !!! CO-GATE WARNING !!!
  Attaching a seed removes it from the LIVE mainline external-seed serving lane
  (fetch_external_seed_rows only_unattached=True). Its canonical replacement is
  served ONLY by the pivot lane, which is OFF in prod — so applying this BEFORE
  the Phase-2 pivot serve cutover DROPS those products from serving. Run --apply
  ONLY in lockstep with the pivot cutover. Dry-run is always safe.

Dry-run is the default (reports what WOULD attach, writes nothing):
  python scripts/backfill_attached_product_key.py
Apply (Phase-2 co-gated; production only with explicit user authorization):
  DATABASE_URL=... python scripts/backfill_attached_product_key.py --apply

--force bypasses the SEED_IDENTITY_ATTACHMENT_ENABLED flag (the backfill is a
deliberate operator action); --limit bounds the batch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.seed_identity_attachment import attach_seed_identity_exact  # noqa: E402

logger = logging.getLogger("backfill_attached_product_key")

# Active, still-unattached seeds — the exact population the batch matcher targets.
UNATTACHED_SEEDS_SQL = """
    SELECT id, external_product_id, canonical_url, destination_url, title,
           seed_data, attached_product_key
    FROM external_product_seeds
    WHERE status = 'active'
      AND attached_product_key IS NULL
    ORDER BY updated_at DESC NULLS LAST
    LIMIT :limit
"""


async def run_backfill(*, apply: bool, limit: int) -> Dict[str, Any]:
    rows = await database.fetch_all(UNATTACHED_SEEDS_SQL, {"limit": limit})
    report: Dict[str, Any] = {
        "apply": apply,
        "seeds_scanned": len(rows),
        "would_attach": 0,
        "attached": 0,
        "by_matcher": {},
        "sample": [],
    }
    for row in rows:
        seed = dict(row)
        match = await attach_seed_identity_exact(seed, dry_run=not apply, force=True)
        if not match:
            continue
        matcher = str(match.get("matcher"))
        report["by_matcher"][matcher] = report["by_matcher"].get(matcher, 0) + 1
        if apply:
            report["attached"] += 1
        else:
            report["would_attach"] += 1
        if len(report["sample"]) < 10:
            report["sample"].append({
                "seed_id": str(seed.get("id")),
                "product_key": match.get("product_key"),
                "matcher": matcher,
                "confidence": match.get("confidence"),
            })
    return report


async def _main(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        return await run_backfill(apply=args.apply, limit=args.limit)
    finally:
        await database.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write attachments (default: dry-run)")
    parser.add_argument("--limit", type=int, default=5000, help="max seeds to scan")
    cli_args = parser.parse_args()

    result = asyncio.run(_main(cli_args))
    print(json.dumps(result, indent=2, default=str))
    if not cli_args.apply:
        print(
            "\nDRY-RUN — nothing written. Re-run with --apply ONLY in lockstep with "
            "the Phase-2 pivot serve cutover (see co-gate warning).",
            file=sys.stderr,
        )
