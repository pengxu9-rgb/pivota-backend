#!/usr/bin/env python3
"""Post-batch verification for the A9-4 seller-of-record flip. READ-ONLY.

The flip tool's own parity report is necessary but not sufficient — the tool
must never be the only grader of its own writes. This script re-derives the
verdict from the checkpoint table and the live rows, independently:

  moved            products checkpointed done in the catalog phase
  still_sentinel   moved rows whose catalog row still carries the banned bucket   (want 0)
  mismatched       moved rows whose merchant differs from the checkpointed target (want 0)
  servable         moved rows admitted by the D2 public gate                      (want == moved)
  old_refs_left    identity listings still under 'external_seed:' for moved rows  (want 0)
  new_refs         listings under the new '<observed>:<pid>' refs
  overrides_on_new operator overrides attached to the NEW refs (work preserved)
  queue_on_new     review-queue rows attached to the NEW refs
  pgm_banned_left  group-membership rows still under the banned bucket            (want 0)
  pgm_moved        group-membership rows under the checkpointed target

Exit 0 when every "want" holds; exit 1 with the offending counts otherwise.
Runs in-container by name (railway ssh ... -- python -m scripts.verify_seller_rekey)
so the flaky public proxy is never in the loop.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

Q1 = """
WITH moved AS (
  SELECT subject AS pk, observed_id
  FROM a9_4_backfill_checkpoint WHERE phase = 'catalog' AND status = 'done')
SELECT count(*) AS moved,
       count(*) FILTER (WHERE cp.merchant_id = 'external_seed') AS still_sentinel,
       count(*) FILTER (WHERE cp.merchant_id <> m.observed_id) AS mismatched,
       count(*) FILTER (WHERE cm.indexable IS TRUE
                          AND cm.status IN ('active','observed')
                          AND cp.content_key IS NOT NULL) AS servable
FROM moved m
JOIN catalog_products cp ON cp.product_key = m.pk
LEFT JOIN catalog_merchants cm ON cm.merchant_id = cp.merchant_id
"""

Q2 = """
WITH moved AS (
  SELECT subject AS pk, observed_id
  FROM a9_4_backfill_checkpoint WHERE phase = 'catalog' AND status = 'done'),
spids AS (
  SELECT m.pk, m.observed_id, cp.source_product_id AS spid
  FROM moved m JOIN catalog_products cp ON cp.product_key = m.pk)
SELECT
  (SELECT count(*) FROM pdp_identity_listing pil
     JOIN spids s ON pil.product_id = s.spid
    WHERE pil.merchant_id = 'external_seed') AS old_refs_left,
  (SELECT count(*) FROM pdp_identity_listing pil
     JOIN spids s ON pil.source_listing_ref = s.observed_id || ':' || s.spid) AS new_refs,
  (SELECT count(*) FROM pdp_identity_override o
     JOIN spids s ON o.source_listing_ref = s.observed_id || ':' || s.spid) AS overrides_on_new,
  (SELECT count(*) FROM pdp_identity_review_queue q
     JOIN spids s ON q.source_listing_ref = s.observed_id || ':' || s.spid) AS queue_on_new,
  (SELECT count(*) FROM product_group_members pgm
     JOIN spids s ON pgm.platform_product_id = s.spid
    WHERE pgm.merchant_id = 'external_seed') AS pgm_banned_left,
  (SELECT count(*) FROM product_group_members pgm
     JOIN spids s ON pgm.platform_product_id = s.spid
      AND pgm.merchant_id = s.observed_id) AS pgm_moved
"""

SENTINEL_LANES = """
SELECT coalesce(source_system, '?') AS lane, count(*) AS remaining
FROM catalog_products WHERE merchant_id = 'external_seed' GROUP BY 1 ORDER BY 2 DESC
"""


async def main() -> int:
    from db.database import database

    await database.connect()
    try:
        r1 = dict(await database.fetch_one(Q1))
        r2 = dict(await database.fetch_one(Q2))
        lanes = {dict(r)["lane"]: dict(r)["remaining"]
                 for r in await database.fetch_all(SENTINEL_LANES)}
    finally:
        await database.disconnect()

    failures = []
    if r1["still_sentinel"]:
        failures.append(f"still_sentinel={r1['still_sentinel']}")
    if r1["mismatched"]:
        failures.append(f"mismatched={r1['mismatched']}")
    if r1["servable"] != r1["moved"]:
        failures.append(f"servable={r1['servable']} != moved={r1['moved']}")
    if r2["old_refs_left"]:
        failures.append(f"old_refs_left={r2['old_refs_left']}")
    if r2["pgm_banned_left"]:
        failures.append(f"pgm_banned_left={r2['pgm_banned_left']}")

    print(json.dumps({"verdict": "FAIL" if failures else "OK",
                      "failures": failures, "rows": r1, "cascades": r2,
                      "sentinel_lanes_remaining": lanes}, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
