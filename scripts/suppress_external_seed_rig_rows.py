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

What --apply does, in ONE transaction, on the cohort the flip's OWN
_is_excluded_rig_row selects (never an exact-domain match — the flip's rule
is wider):
  1. tombstone every rig row (reason 'step5_test_rig_retirement'; metadata
     carries the run id, the ADR-009 decision, and any PRIOR tombstone's
     reason/metadata — provenance is carried, not discarded) — the row is out
     of every public surface from here;
  2. THEN re-key those rows off the sentinel onto the rig's own merchant so
     the bucket reaches ZERO — safe only because step 1 landed first: a
     tombstoned row is not serving and the sweep exposure the flip feared is
     moot. The rig merchant is DERIVED from the row's own store connection
     (merchant_stores.domain), never a literal;
  3. cascade the flip's FULL contract, in the flip's order: every reflected
     dependent table (discover_cascade_tables), then product_group_members and
     the identity listings, then the residue audit inside the transaction —
     any residue rolls the whole step back.

Doors, all evaluated in BOTH modes and all BEFORE any write (exit 2):
  - exactly one rig merchant derives from the rig domains;
  - it is a KNOWN test merchant (services/test_merchant_policy) — retiring
    rows onto a live tenant would be a wrong seller-of-record;
  - no listing new-ref conflict (a listing already under <rig>:<pid> would
    make the migration merge operator work onto unreviewed content and delete
    the old listing silently — the flip pre-checks this before every write,
    and so does this step).
After apply the sentinel bucket must be EMPTY — asserted (exit 1), not
printed: a non-zero count is a row the flip's exclusion missed.

DRY-RUN by default: prints the recon (cohort, rig merchant, door verdicts,
cascade tables, conflicts) and stops. Nothing is written without --apply.

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
    _is_excluded_rig_row,
    assert_sig_frozen_sql,
)
from services.test_merchant_policy import static_test_merchant_ids  # noqa: E402

SUPPRESSION_REASON = "step5_test_rig_retirement"  # the precedent's reason, on purpose

# Every row still under the sentinel; the cohort is then selected with the
# flip's OWN _is_excluded_rig_row (source_domain / canonical_url host / seed
# domain, www.-stripped) — an exact-source_domain match would leave behind
# the very rows the flip excluded by the wider rule.
SENTINEL_ROWS_SQL = """
SELECT cp.product_key, cp.content_key, cp.source_domain, cp.canonical_url, cp.platform,
       cp.source_product_id, cp.suppression_reason, cp.suppression_metadata,
       e.domain AS seed_domain, e.destination_url AS seed_destination_url,
       (SELECT array_agg(e2.external_product_id) FROM external_product_seeds e2
         WHERE e2.attached_product_key = cp.product_key AND e2.status = 'active') AS seed_listing_ids
  FROM catalog_products cp
  LEFT JOIN LATERAL (
        SELECT domain, destination_url FROM external_product_seeds
         WHERE attached_product_key = cp.product_key AND status = 'active'
         ORDER BY (coalesce(domain, '') = '') ASC, updated_at DESC NULLS LAST
         LIMIT 1) e ON TRUE
 WHERE cp.merchant_id = :banned
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

# Sig-frozen at import, on the constants THEMSELVES (an assert over string
# copies stays green when the constant is edited — review nit on #1752).
for _sql in (TOMBSTONE_SQL, REKEY_SQL):
    assert_sig_frozen_sql(_sql)


async def _run(apply: bool) -> int:
    from db.database import database

    await database.connect()
    try:
        domains = sorted(EXCLUDED_SOURCE_DOMAINS)
        all_sentinel = [dict(r) for r in await database.fetch_all(
            SENTINEL_ROWS_SQL, {"banned": BANNED_BUCKET_MERCHANT_ID})]
        rows = [r for r in all_sentinel if _is_excluded_rig_row(r)]
        non_rig_left = len(all_sentinel) - len(rows)
        rig_rows = [dict(r) for r in await database.fetch_all(
            RIG_MERCHANT_SQL, {"domains": domains})]
        rig_ids = sorted({str(r["merchant_id"]) for r in rig_rows if r.get("merchant_id")})
        known = static_test_merchant_ids()
        for r in rows:
            r["listing_product_ids"] = [str(v) for v in dict.fromkeys(
                [r.get("source_product_id"), *(r.get("seed_listing_ids") or [])]) if v]
        bf = SellerBackfill(database=database, si_mod=None, execute=apply,
                            batch_size=max(1, len(rows)))
        cascade = await bf.discover_cascade_tables()
        # The listing new-ref conflict pre-check the flip runs before EVERY
        # write: a listing already under <rig>:<pid> would make the copy
        # no-op, repoint operator overrides onto unreviewed content, and
        # delete the old listing silently. A door, evaluated in BOTH modes.
        conflicts: List[Dict[str, Any]] = []
        if rig_ids and len(rig_ids) == 1:
            for r in rows:
                c = await bf._listing_new_ref_conflict(r, rig_ids[0])
                if c:
                    conflicts.append({"product_key": r["product_key"], "conflicting_ref": c})
        recon: Dict[str, Any] = {
            "rig_domains": domains,
            "sentinel_rows_total": len(all_sentinel),
            "cohort_rows": len(rows),
            "non_rig_sentinel_rows_left_behind": non_rig_left,
            "already_tombstoned": sum(1 for r in rows if r.get("suppression_reason")),
            "distinct_source_domains": sorted({str(r.get("source_domain")) for r in rows}),
            "rig_merchant_candidates": rig_rows,
            "rig_merchant_is_known_test_merchant": {m: (m in known) for m in rig_ids},
            "cascade_tables": [t["table"] for t in cascade],
            "listing_new_ref_conflicts": conflicts,
        }
        print(json.dumps({"mode": "apply" if apply else "dry_run", "recon": recon},
                         indent=2, default=str))
        if not rows:
            print("Nothing to retire — the cohort is empty.")
            return 0
        # Every door answers both ways, before ANY write.
        if len(rig_ids) != 1:
            print(f"ABORT: expected exactly one rig merchant for {domains}, got {rig_ids}",
                  file=sys.stderr)
            return 2
        rig = rig_ids[0]
        if rig not in known:
            print(f"ABORT: {rig} is not a known test merchant (services/test_merchant_policy); "
                  "refusing to retire rows onto a live tenant", file=sys.stderr)
            return 2
        if conflicts:
            print(f"ABORT: {len(conflicts)} listing new-ref conflict(s) — migrating would "
                  "silently merge operator work onto unreviewed content", file=sys.stderr)
            return 2
        if not apply:
            print("DRY-RUN — no changes written. Re-run with --apply to execute.")
            return 0

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        pks = [str(r["product_key"]) for r in rows]
        # Prior tombstone provenance is CARRIED, not discarded (the re-key
        # predicate needs one uniform reason, so a prior reason is overwritten
        # in place and preserved here).
        def _as_obj(v):
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except ValueError:
                    return v
            return v
        prior = {str(r["product_key"]): {"reason": r.get("suppression_reason"),
                                         "metadata": _as_obj(r.get("suppression_metadata"))}
                 for r in rows if r.get("suppression_reason")}
        meta = json.dumps({
            "script": "suppress_external_seed_rig_rows",
            "run_id": run_id,
            "decision": "founder 2026-08-14: ADR-009 closing step — retire the test-rig "
                        "rows the A9-4 flip excluded, and empty the sentinel bucket",
            "rekeyed_from": BANNED_BUCKET_MERCHANT_ID,
            "rekeyed_to": rig,
            "prior_tombstones": prior,
        }, default=str)
        pgm_totals = {"moved": 0, "retired": 0}
        async with database.transaction():
            await database.execute(TOMBSTONE_SQL, {"reason": SUPPRESSION_REASON, "meta": meta,
                                                   "pks": pks, "banned": BANNED_BUCKET_MERCHANT_ID})
            await database.execute(REKEY_SQL, {"rig": rig, "pks": pks,
                                               "banned": BANNED_BUCKET_MERCHANT_ID,
                                               "reason": SUPPRESSION_REASON})
            # The flip's full cascade, in the flip's order: reflected
            # dependents, then the two tables reflection cannot see.
            for r in rows:
                pk, ck = str(r["product_key"]), r.get("content_key")
                sku_keys = await bf._sku_keys_for_product(pk)
                for t in cascade:
                    await bf._resubject_dependent(t, rig, pk, ck, sku_keys)
                st = await bf._resubject_group_membership(r, rig)
                pgm_totals["moved"] += st["moved"]
                pgm_totals["retired"] += st["retired"]
                await bf._migrate_listing_refs(r, rig)
            # Residue audit INSIDE the txn, exactly as the flip: any enumerated
            # dependent still on the sentinel for these rows = cascade miss.
            residue = await bf._residue_audit(pks, cascade)
            if any(residue.values()):
                raise RuntimeError(f"cascade residue after rig re-key (rolled back): {residue}")
            left = await database.fetch_one(
                "SELECT count(*) AS c FROM catalog_products WHERE merchant_id = :banned "
                "AND product_key = ANY(:pks)",
                {"banned": BANNED_BUCKET_MERCHANT_ID, "pks": pks})
            if int(dict(left)["c"]) != 0:
                raise RuntimeError(f"rig rows still under the sentinel after re-key: {dict(left)['c']}")
        post = int(dict(await database.fetch_one(
            "SELECT count(*) AS c FROM catalog_products WHERE merchant_id = :banned",
            {"banned": BANNED_BUCKET_MERCHANT_ID}))["c"])
        print(json.dumps({"run_id": run_id, "rows_retired": len(pks), "rekeyed_to": rig,
                          "pgm": pgm_totals, "residue": residue,
                          "sentinel_rows_remaining_total": post},
                         indent=2, default=str))
        # The bucket must be EMPTY — asserted, not printed. Non-zero here is a
        # row the flip's exclusion missed and the flip left behind: exit loud.
        if post != 0:
            print(f"FAIL: {post} row(s) still under the sentinel after retirement", file=sys.stderr)
            return 1
        return 0
    finally:
        await database.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    return asyncio.run(_run(apply=ap.parse_args().apply))


if __name__ == "__main__":
    raise SystemExit(main())
