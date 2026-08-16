#!/usr/bin/env python3
"""ADR-009 closing step — dispose of the orphan residue the flip could not reach.

THE POPULATION. After the A9-4 seller-of-record flip emptied the sentinel
bucket, the independent grader (scripts/verify_seller_rekey.py) still failed on
sentinel rows in product-scoped tables. Reconnaissance
(scripts/recon_sentinel_orphans.py, docs/ADR009_ORPHAN_RESIDUE_RECON_2026-08-16.md)
classified every one; the founder gated a disposition per table. This executes
exactly the two that need a write, and nothing else:

  catalog_offers   12 rows  RE-KEY to the product's CURRENT merchant.
      These are `us_market_capture` offers (scripts/capture_us_market_offers.py)
      written 2026-08-14 02:09-02:23. That capture selects candidates ONCE
      (carrying cp.merchant_id), then probes each storefront over HTTP for
      minutes, then upserts the snapshot value — so the flip moved the products
      and cascaded catalog_offers in between, and the upsert landed after the
      cascade with a stale merchant. A TOCTOU, not a stray writer. The product
      still exists and still has its own moved mirror offer, so the fix is to
      follow the product, exactly as the flip's cascade would have.
      (The writer is hardened in the same PR. Hardening alone cannot repair
      these: OFFER_UPSERT_SQL's ON CONFLICT DO UPDATE does not touch
      merchant_id, so a re-run would leave all 12 as they are.)

  product_reviews   9 rows  DELETE, with every row dumped first.
      The 2026-05-20 moderation-canary run: source_type native /
      source_system accounts / unverified / no author. The reviews service
      keys `merchant|platform|id` in its OWN namespace
      (services/reviews_service.py build_product_key), which is why the flip's
      catalog product_key cascade could never touch them and why the recon
      reports 0/9 of their scope keys resolving to a catalog row. Re-keying
      would publish QA fixtures onto a real seller's PDP. They are already out
      of every public reader (8 `removed`, 1 `under_review`).

Deliberately NOT here, each for a stated reason:
  * evidence_items / action_plan_items / niche_target_outcomes (324 rows) —
    NULL scope key on every row; their merchant_id is the audit's TENANT. The
    verifier's `unscoped` bucket (same PR) stops failing them. No row moves.
  * product_enrichment (8) — the existing primitive already does this:
    scripts/reattribute_orphaned_enrichment.py H0. Re-run it; do not fork it.
  * pdp_identity_listing (103) — founder deferred. Nothing serves them
    (n_seeds_active = 0), so the flip's _migrate_listing_refs can run later.

DOORS, every one evaluated in BOTH modes and ALL before any write (exit 2).
A door that cannot fail is not a door, so each is stated as the measurement
that would stop the run:
  D1  the sentinel bucket is EMPTY. While catalog_products still holds
      sentinel rows the dependents legitimately share it and nothing here is
      an orphan yet.
  D2  every offer row's product EXISTS. A missing product is a different
      disposition (tombstone/delete), not this one.
  D3  every offer row's product is OFF the sentinel, and its merchant is not
      the sentinel. Re-keying onto the bucket would be a no-op that reports
      success.
  D4  every offer row's target merchant EXISTS in catalog_merchants. Re-keying
      onto an id no merchant row backs would strand the offer.
  D5  no review row's product_key resolves to a catalog row. If one does, the
      reviews are NOT orphans and deleting them would destroy live content.
  D6  no review row is publicly visible (status <> 'active'). Deleting serving
      content is a different decision than cleaning up a retired canary.
The residue for the tables this touches must be ZERO afterwards, asserted
inside the transaction — not printed, asserted.

DRY-RUN by default: prints the population, the door verdicts, and the exact
row-level plan, and stops. Nothing is written without --apply. The tool never
grades its own writes: run scripts/verify_seller_rekey.py after.

Usage
    python -m scripts.dispose_sentinel_orphans                    # dry-run
    python -m scripts.dispose_sentinel_orphans --tables catalog_offers
    python -m scripts.dispose_sentinel_orphans --apply            # founder-gated
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill_seller_of_record import (  # noqa: E402
    BANNED_BUCKET_MERCHANT_ID,
    assert_sig_frozen_sql,
)

ALL_TABLES = ("catalog_offers", "product_reviews")

# -- populations ------------------------------------------------------------

OFFERS_SQL = """
SELECT o.offer_id, o.product_key, o.sku_key, o.merchant_id, o.source_system,
       o.source_domain, o.currency, o.list_price, o.created_at,
       cp.merchant_id AS target_merchant,
       (cp.product_key IS NOT NULL) AS product_exists,
       (cp.suppression_reason IS NOT NULL) AS product_tombstoned,
       EXISTS (SELECT 1 FROM catalog_merchants m WHERE m.merchant_id = cp.merchant_id)
         AS target_merchant_exists
  FROM catalog_offers o
  LEFT JOIN catalog_products cp ON cp.product_key = o.product_key
 WHERE o.merchant_id = :banned
 ORDER BY o.offer_id
"""

REVIEWS_SQL = """
SELECT r.*,
       EXISTS (SELECT 1 FROM catalog_products cp WHERE cp.product_key = r.product_key)
         AS scope_resolves
  FROM product_reviews r
 WHERE r.merchant_id = :banned
 ORDER BY r.id
"""

# Children that FK to product_reviews with ON DELETE CASCADE (migration 040).
# Dumped before the delete so the run log alone is enough to re-insert.
REVIEW_CHILD_TABLES = ("media_assets", "review_replies", "review_interactions",
                       "review_featured")

BUCKET_SQL = "SELECT count(*) AS c FROM catalog_products WHERE merchant_id = :banned"

# -- writes -----------------------------------------------------------------

# Follows the product, never a literal merchant: the target is read from the
# catalog row in the same statement, so it cannot drift from the door's check.
OFFERS_REKEY_SQL = """
UPDATE catalog_offers o
   SET merchant_id = cp.merchant_id, updated_at = NOW()
  FROM catalog_products cp
 WHERE cp.product_key = o.product_key
   AND o.offer_id = ANY(:offer_ids)
   AND o.merchant_id = :banned
   AND cp.merchant_id <> :banned
"""

REVIEWS_DELETE_SQL = "DELETE FROM product_reviews WHERE id = ANY(:ids) AND merchant_id = :banned"

for _sql in (OFFERS_REKEY_SQL, REVIEWS_DELETE_SQL):
    assert_sig_frozen_sql(_sql)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return str(v)


async def _rows(db, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return [dict(r) for r in await db.fetch_all(sql, params or {}) or []]


async def _count(db, sql: str, params: Dict[str, Any]) -> int:
    row = await db.fetch_one(sql, params)
    return int(dict(row)["c"]) if row else 0


def offer_doors(bucket: int, offers: List[Dict[str, Any]]) -> List[str]:
    """D1-D4. Returns the failures; empty means every door passed."""
    bad = []
    if bucket != 0:
        bad.append(f"D1 sentinel bucket not empty: catalog_products={bucket}")
    missing = [o["offer_id"] for o in offers if not o["product_exists"]]
    if missing:
        bad.append(f"D2 offer rows whose product does not exist: {missing}")
    on_bucket = [o["offer_id"] for o in offers
                 if o["product_exists"] and o["target_merchant"] == BANNED_BUCKET_MERCHANT_ID]
    if on_bucket:
        bad.append(f"D3 offer rows whose product is STILL on the sentinel: {on_bucket}")
    unbacked = [o["offer_id"] for o in offers
                if o["product_exists"] and not o["target_merchant_exists"]]
    if unbacked:
        bad.append(f"D4 offer rows whose target merchant has no catalog_merchants row: {unbacked}")
    return bad


def review_doors(bucket: int, reviews: List[Dict[str, Any]]) -> List[str]:
    """D1, D5, D6."""
    bad = []
    if bucket != 0:
        bad.append(f"D1 sentinel bucket not empty: catalog_products={bucket}")
    resolves = [r["id"] for r in reviews if r.get("scope_resolves")]
    if resolves:
        bad.append(f"D5 review rows whose product_key RESOLVES to a catalog row "
                   f"(not orphans — deleting these would destroy live content): {resolves}")
    live = [r["id"] for r in reviews if str(r.get("status") or "") == "active"]
    if live:
        bad.append(f"D6 review rows that are publicly visible (status='active'): {live}")
    return bad


async def plan(db, tables: List[str]) -> Dict[str, Any]:
    """READ-ONLY. The population, the doors, and the exact per-row plan."""
    banned = {"banned": BANNED_BUCKET_MERCHANT_ID}
    bucket = await _count(db, BUCKET_SQL, banned)
    out: Dict[str, Any] = {"banned": BANNED_BUCKET_MERCHANT_ID,
                           "catalog_products_under_sentinel": bucket,
                           "tables": {}, "doors_failed": []}

    if "catalog_offers" in tables:
        offers = await _rows(db, OFFERS_SQL, banned)
        doors = offer_doors(bucket, offers)
        out["tables"]["catalog_offers"] = {
            "action": "rekey_to_product_current_merchant",
            "rows": len(offers),
            "plan": [{"offer_id": o["offer_id"], "product_key": o["product_key"],
                      "from": o["merchant_id"], "to": o["target_merchant"],
                      "source_system": o["source_system"],
                      "product_tombstoned": o["product_tombstoned"]} for o in offers],
            "doors_failed": doors,
        }
        out["doors_failed"].extend(doors)

    if "product_reviews" in tables:
        reviews = await _rows(db, REVIEWS_SQL, banned)
        doors = review_doors(bucket, reviews)
        ids = [int(r["id"]) for r in reviews]
        children: Dict[str, List[Dict[str, Any]]] = {}
        for child in REVIEW_CHILD_TABLES:
            try:
                children[child] = await _rows(
                    db, f"SELECT * FROM {child} WHERE review_id = ANY(:ids)", {"ids": ids})
            except Exception as exc:  # noqa: BLE001
                # A child table absent from this database is a schema fact, not
                # a reason to delete blind: record it, and let the door layer
                # decide. It is never silently treated as "no rows".
                children[child] = [{"__unreadable__": str(exc)[:160]}]
        out["tables"]["product_reviews"] = {
            "action": "delete_with_row_dump",
            "rows": len(reviews),
            "dump": [_jsonable(r) for r in reviews],
            "cascaded_children": {k: _jsonable(v) for k, v in children.items()},
            "doors_failed": doors,
        }
        out["doors_failed"].extend(doors)

    return _jsonable(out)


async def apply(db, tables: List[str], run_id: str) -> Dict[str, Any]:
    """Per-table transaction. Residue for the touched table must be zero
    INSIDE the transaction, or the whole table's change rolls back."""
    banned = BANNED_BUCKET_MERCHANT_ID
    result: Dict[str, Any] = {"run_id": run_id, "applied": {}}

    if "catalog_offers" in tables:
        offers = await _rows(db, OFFERS_SQL, {"banned": banned})
        ids = [str(o["offer_id"]) for o in offers]
        async with db.transaction():
            await db.execute(OFFERS_REKEY_SQL, {"offer_ids": ids, "banned": banned})
            left = await _count(
                db, "SELECT count(*) AS c FROM catalog_offers WHERE merchant_id = :banned",
                {"banned": banned})
            if left:
                raise RuntimeError(
                    f"catalog_offers still holds {left} sentinel row(s) after re-key "
                    f"(rolled back)")
        result["applied"]["catalog_offers"] = {"rekeyed": len(ids)}

    if "product_reviews" in tables:
        reviews = await _rows(db, REVIEWS_SQL, {"banned": banned})
        ids = [int(r["id"]) for r in reviews]
        async with db.transaction():
            await db.execute(REVIEWS_DELETE_SQL, {"ids": ids, "banned": banned})
            left = await _count(
                db, "SELECT count(*) AS c FROM product_reviews WHERE merchant_id = :banned",
                {"banned": banned})
            if left:
                raise RuntimeError(
                    f"product_reviews still holds {left} sentinel row(s) after delete "
                    f"(rolled back)")
        result["applied"]["product_reviews"] = {"deleted": len(ids)}

    return result


async def _run(tables: List[str], do_apply: bool) -> int:
    from db.database import database

    await database.connect()
    try:
        p = await plan(database, tables)
        if p["doors_failed"]:
            print(json.dumps({"mode": "abort", "reason": "doors failed", **p}, indent=1))
            print("DOORS FAILED — nothing was written:", file=sys.stderr)
            for d in p["doors_failed"]:
                print(f"  {d}", file=sys.stderr)
            return 2
        if not do_apply:
            print(json.dumps({"mode": "dry-run", **p}, indent=1))
            return 0
        run_id = str(uuid.uuid4())
        # The dump is printed BEFORE the write, so a run log that ends in a
        # crash still contains everything needed to re-insert.
        print(json.dumps({"mode": "apply", "run_id": run_id, "pre_write_plan": p}, indent=1))
        res = await apply(database, tables, run_id)
        post = await plan(database, tables)
        remaining = {t: d["rows"] for t, d in post["tables"].items() if d["rows"]}
        print(json.dumps({"mode": "applied", **res, "remaining": remaining}, indent=1))
        if remaining:
            print(f"FAIL: rows still under the sentinel after apply: {remaining}", file=sys.stderr)
            return 1
        return 0
    finally:
        await database.disconnect()


def _parse(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tables", default=",".join(ALL_TABLES),
                    help=f"comma-separated subset of {','.join(ALL_TABLES)}")
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    args = ap.parse_args(argv)
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    unknown = [t for t in tables if t not in ALL_TABLES]
    if unknown:
        ap.error(f"--tables names {unknown}, which this tool has no disposition for")
    if not tables:
        ap.error("--tables selected nothing")
    args.table_list = tables
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse(argv)
    return asyncio.run(_run(args.table_list, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
