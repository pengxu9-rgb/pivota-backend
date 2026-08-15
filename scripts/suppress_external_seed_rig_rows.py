#!/usr/bin/env python3
"""ADR-009 closing step — retire the 50 test-rig rows left in the sentinel bucket.

The A9-4 flip (verifier verdict OK at 11,099 moved, 2026-08-14) deliberately
skipped one cohort: mirror rows whose source_domain is the retired founder
test rig's Shopify dev store (EXCLUDED_SOURCE_DOMAINS in
scripts/backfill_seller_of_record.py). Re-keying them onto the rig merchant
inside the flip would have both dropped them from serving and exposed them to
the stale-product sweep — so they got their own founder-gated step. This is it.

Precedent: scripts/retire_test_rig_merch_efbc.py (founder decision
2026-07-10) — the house retirement is a REVERSIBLE tombstone
(catalog_products.suppression_reason + suppressed_at + run-id metadata,
reason 'step5_test_rig_retirement'), which the trust policy reads as
ROW_TOMBSTONED (no public surface) and the sync layer preserves. No hard
deletes, money untouched.

What --apply does, in ONE transaction:
  1. tombstone every sentinel row whose source_domain is a rig domain
     (reason 'step5_test_rig_retirement', metadata carries the run id and the
     ADR-009 decision) — the row is out of every public surface from here;
  2. THEN re-key those rows off the sentinel onto the rig's own merchant so
     the bucket reaches ZERO — safe only because step 1 landed first: a
     tombstoned row is not serving and the sweep exposure the flip feared is
     moot. The rig merchant is DERIVED from the row's own store connection
     (merchant_stores.domain = source_domain), never a literal — and it must
     be a KNOWN test merchant (services/test_merchant_policy) or the step
     refuses: retiring rows onto a live tenant would be a wrong seller.
  3. cascade the merchant re-key through the two tables reflection cannot
     see, exactly as the flip does (product_group_members + identity
     listings), by reusing the flip tool's own methods.

DRY-RUN by default: prints the cohort (count, distinct source domains, the
derived rig merchant, whether it is a known test merchant, listing / pgm
footprint) and stops. Nothing is written without --apply.

Runs in the same GitHub-runner channel as the flip; the verifier
(scripts/verify_seller_rekey.py) grades the result — expect the mirror lane
at 0 and pgm_banned_left / old_refs_left still 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill_seller_of_record import (  # noqa: E402
    BANNED_BUCKET_MERCHANT_ID,
    EXCLUDED_SOURCE_DOMAINS,
    SellerBackfill,
)
from services.test_merchant_policy import static_test_merchant_ids  # noqa: E402

SUPPRESSION_REASON = "step5_test_rig_retirement"  # the precedent's reason, on purpose

COHORT_SQL = """
SELECT cp.product_key, cp.content_key, cp.source_domain, cp.platform,
       cp.source_product_id, cp.suppression_reason,
       (SELECT array_agg(e2.external_product_id) FROM external_product_seeds e2
         WHERE e2.attached_product_key = cp.product_key AND e2.status = 'active') AS seed_listing_ids
  FROM catalog_products cp
 WHERE cp.merchant_id = :banned AND cp.source_domain = ANY(:domains)
 ORDER BY cp.product_key
"""

RIG_MERCHANT_SQL = """
SELECT DISTINCT merchant_id, status
  FROM merchant_stores
 WHERE domain = ANY(:domains)
"""

TOMBSTONE_SQL = """
UPDATE catalog_products
   SET suppression_reason = :reason,
       suppressed_at = COALESCE(suppressed_at, NOW()),
       suppression_metadata = CAST(:meta AS jsonb),
       updated_at = NOW()
 WHERE product_key = ANY(:pks) AND merchant_id = :banned
"""

REKEY_SQL = """
UPDATE catalog_products
   SET merchant_id = :rig, updated_at = NOW()
 WHERE product_key = ANY(:pks) AND merchant_id = :banned
   AND suppression_reason = :reason
"""


async def _run(apply: bool) -> int:
    from db.database import database

    await database.connect()
    try:
        domains = sorted(EXCLUDED_SOURCE_DOMAINS)
        rows = [dict(r) for r in await database.fetch_all(
            COHORT_SQL, {"banned": BANNED_BUCKET_MERCHANT_ID, "domains": domains})]
        rig_rows = [dict(r) for r in await database.fetch_all(
            RIG_MERCHANT_SQL, {"domains": domains})]
        rig_ids = sorted({str(r["merchant_id"]) for r in rig_rows if r.get("merchant_id")})
        known = static_test_merchant_ids()
        recon: Dict[str, Any] = {
            "rig_domains": domains,
            "cohort_rows": len(rows),
            "already_tombstoned": sum(1 for r in rows if r.get("suppression_reason")),
            "distinct_source_domains": sorted({str(r.get("source_domain")) for r in rows}),
            "rig_merchant_candidates": rig_rows,
            "rig_merchant_is_known_test_merchant": {m: (m in known) for m in rig_ids},
        }
        print(json.dumps({"mode": "apply" if apply else "dry_run", "recon": recon},
                         indent=2, default=str))
        if not rows:
            print("Nothing to retire — the cohort is empty.")
            return 0
        # Every door answers both ways: exactly ONE rig merchant, and it MUST
        # be a known test merchant. Anything else aborts before any write.
        if len(rig_ids) != 1:
            print(f"ABORT: expected exactly one rig merchant for {domains}, got {rig_ids}",
                  file=sys.stderr)
            return 2
        rig = rig_ids[0]
        if rig not in known:
            print(f"ABORT: {rig} is not a known test merchant (services/test_merchant_policy); "
                  "refusing to retire rows onto a live tenant", file=sys.stderr)
            return 2
        if not apply:
            print("DRY-RUN — no changes written. Re-run with --apply to execute.")
            return 0

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        meta = json.dumps({
            "script": "suppress_external_seed_rig_rows",
            "run_id": run_id,
            "decision": "founder 2026-08-14: ADR-009 closing step — retire the test-rig "
                        "rows the A9-4 flip excluded, and empty the sentinel bucket",
            "rekeyed_from": BANNED_BUCKET_MERCHANT_ID,
            "rekeyed_to": rig,
        })
        pks = [str(r["product_key"]) for r in rows]
        bf = SellerBackfill(database=database, si_mod=None, execute=True, batch_size=len(pks))
        pgm_totals = {"moved": 0, "retired": 0}
        async with database.transaction():
            await database.execute(TOMBSTONE_SQL, {"reason": SUPPRESSION_REASON, "meta": meta,
                                                   "pks": pks, "banned": BANNED_BUCKET_MERCHANT_ID})
            await database.execute(REKEY_SQL, {"rig": rig, "pks": pks,
                                               "banned": BANNED_BUCKET_MERCHANT_ID,
                                               "reason": SUPPRESSION_REASON})
            for r in rows:
                b = {**r, "listing_product_ids": [str(v) for v in dict.fromkeys(
                    [r.get("source_product_id"), *(r.get("seed_listing_ids") or [])]) if v]}
                st = await bf._resubject_group_membership(b, rig)
                pgm_totals["moved"] += st["moved"]
                pgm_totals["retired"] += st["retired"]
                await bf._migrate_listing_refs(b, rig)
            left = await database.fetch_one(
                "SELECT count(*) AS c FROM catalog_products WHERE merchant_id = :banned "
                "AND source_domain = ANY(:domains)",
                {"banned": BANNED_BUCKET_MERCHANT_ID, "domains": domains})
            if int(dict(left)["c"]) != 0:
                raise RuntimeError(f"rig rows still under the sentinel after re-key: {dict(left)['c']}")
        post = dict(await database.fetch_one(
            "SELECT count(*) AS c FROM catalog_products WHERE merchant_id = :banned",
            {"banned": BANNED_BUCKET_MERCHANT_ID}))
        print(json.dumps({"run_id": run_id, "rows_retired": len(pks), "rekeyed_to": rig,
                          "pgm": pgm_totals, "sentinel_rows_remaining_total": post["c"]},
                         indent=2, default=str))
        return 0
    finally:
        await database.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    return asyncio.run(_run(apply=ap.parse_args().apply))


if __name__ == "__main__":
    raise SystemExit(main())
