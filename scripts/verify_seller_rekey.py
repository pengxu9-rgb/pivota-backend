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
  SELECT ref_id AS pk, observed_id
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
  SELECT ref_id AS pk, observed_id
  FROM a9_4_backfill_checkpoint WHERE phase = 'catalog' AND status = 'done'),
spids AS (
  -- A listing may key on the catalog row's source_product_id (Path B) OR the
  -- attached seed's external_product_id (Path C rows carry a NAME SLUG in
  -- source_product_id — measured 2026-08-07: 0 Path C listings under the
  -- slug). The tool migrates BOTH candidates; the verifier must check both,
  -- or new_refs reads 0 for a perfectly migrated Path C cohort.
  SELECT m.pk, m.observed_id, cp.source_product_id AS spid
  FROM moved m JOIN catalog_products cp ON cp.product_key = m.pk
  UNION
  SELECT m.pk, m.observed_id, e.external_product_id AS spid
  FROM moved m
  JOIN catalog_products cp ON cp.product_key = m.pk
  JOIN external_product_seeds e
    ON e.attached_product_key = cp.product_key AND e.status = 'active'
  WHERE e.external_product_id IS NOT NULL)
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

# GLOBAL sentinel residue — every table that carries a seller column, counted
# without any checkpoint scope. The catalog phase's Q1/Q2 grade the moved
# cohort; the rig-retirement step (which never enters the checkpoint) and any
# future stray writer are graded HERE. The table set is REFLECTED with the
# flip's own discover_cascade_tables — a hand-picked list here would be
# exactly the drift the flip's reflection exists to prevent — plus the two
# tables reflection cannot see (no product-scope column). Wanted 0 across the
# board once catalog_products is empty; reported (not failed) before that,
# because dependents of an unmoved row legitimately share the sentinel.
UNREFLECTED = ("product_group_members", "pdp_identity_listing")


async def global_residue(database) -> dict:
    from scripts.backfill_seller_of_record import SellerBackfill

    bf = SellerBackfill(database=database, si_mod=None, execute=False, batch_size=1)
    cascade = await bf.discover_cascade_tables()
    cols = {"catalog_products": "merchant_id", **{t: "merchant_id" for t in UNREFLECTED},
            **{t["table"]: t["seller_col"] for t in cascade}}
    out = {}
    for t in cols:
        row = await database.fetch_one(
            f"SELECT count(*) AS c FROM {t} WHERE {cols[t]} = 'external_seed'")
        out[t] = int(dict(row)["c"]) if row else 0
    return out


async def main() -> int:
    from db.database import database

    await database.connect()
    try:
        r1 = dict(await database.fetch_one(Q1))
        r2 = dict(await database.fetch_one(Q2))
        lanes = {dict(r)["lane"]: dict(r)["remaining"]
                 for r in await database.fetch_all(SENTINEL_LANES)}
        glob = await global_residue(database)
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
    # Once catalog_products holds no sentinel rows, NOTHING else may either:
    # a dependent still on the sentinel is an orphan no re-key can reach.
    if glob["catalog_products"] == 0:
        for table, n in glob.items():
            if n:
                failures.append(f"global_residue:{table}={n}")

    print(json.dumps({"verdict": "FAIL" if failures else "OK",
                      "failures": failures, "rows": r1, "cascades": r2,
                      "sentinel_lanes_remaining": lanes,
                      "global_sentinel_residue": glob}, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
