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
  D7  every cascade child named by the FK graph could be READ. A child that
      cannot be dumped is a hole in the reversal record, and a delete whose
      children vanish unrecorded is not reversible.
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
import re
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

# Children of product_reviews, DERIVED FROM THE FK GRAPH — never a list.
#
# The first cut hardcoded the four tables migration 040 creates and called the
# delete "reversible by re-insert". It was not: `buyer_review_ownership`
# (migration 042) and `buyer_review_user_subject` (created at runtime by
# services/ugc_capabilities_service.py) also CASCADE, and
# `buyer_review_idempotency_keys.review_id` is ON DELETE SET NULL — mutated,
# not deleted, and recorded nowhere. Two of the three are invisible to anyone
# reading migration 040, which is exactly why the house rule says derive from
# the schema rather than write the identities down.
#
# confdeltype is carried so the dump says WHAT will happen to each child:
# 'c' cascade (row disappears), 'n' set null (row survives, FK cleared),
# 'a'/'r' no action/restrict (the delete would fail — worth seeing).
REVIEW_CHILDREN_SQL = """
SELECT n.nspname            AS child_schema,
       cl.relname           AS child_table,
       a.attname            AS fk_column,
       c.confdeltype::text  AS on_delete
  FROM pg_constraint c
  JOIN pg_class cl     ON cl.oid = c.conrelid
  JOIN pg_namespace n  ON n.oid = cl.relnamespace
  -- conkey and confkey are PARALLEL arrays: unnesting them together is what
  -- keeps a multi-column FK's OTHER columns out. Unnesting conkey alone
  -- yielded e.g. the `tenant` half of (review_id, tenant), and the tool then
  -- queried `WHERE tenant = ANY(<review ids>)` — a type error that surfaced as
  -- an unreadable child and aborted the whole run on a legitimate schema.
  JOIN unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(attnum, refattnum, ord) ON TRUE
  JOIN pg_attribute a  ON a.attrelid = c.conrelid  AND a.attnum = k.attnum
  JOIN pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = k.refattnum
 WHERE c.contype = 'f'
   AND c.confrelid = 'product_reviews'::regclass
   AND ra.attname = 'id'
 ORDER BY 1, 2, 3
"""

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> str:
    """Names come from pg_constraint, but they are still interpolated, so they
    still have to be checked — a name that cannot be safely interpolated aborts
    rather than being skipped."""
    if not _IDENT.match(name or ""):
        raise RuntimeError(f"refusing to interpolate identifier {name!r}")
    return name

BUCKET_SQL = "SELECT count(*) AS c FROM catalog_products WHERE merchant_id = :banned"

# The same populations, row-locked, for the in-transaction re-check. The id set
# agreeing is NOT enough: a review that is `removed` at plan time and `active`
# at write time keeps its id, so a set comparison waves it through while D5/D6
# would both have refused it (reproduced, review round 2). The doors are
# therefore re-evaluated inside the transaction on these rows, and FOR UPDATE
# holds them so nothing can change between the re-check and the write.
OFFERS_LOCKED_SQL = OFFERS_SQL + "\n FOR UPDATE OF o"
REVIEWS_LOCKED_SQL = REVIEWS_SQL + "\n FOR UPDATE OF r"

_ON_DELETE = {"c": "cascade", "n": "set null", "a": "no action", "r": "restrict", "d": "set default"}

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
        children: Dict[str, Any] = {}
        unreadable: List[str] = []
        for fk in await _rows(db, REVIEW_CHILDREN_SQL):
            schema, table, col = fk["child_schema"], fk["child_table"], fk["fk_column"]
            # A child outside `public` is a door, not a crash: _ident would
            # reject the qualified name with an uncaught traceback, which on a
            # destructive tool reads as a bug rather than a refusal.
            if schema != "public":
                unreadable.append(f"{schema}.{table}.{col} (outside the public schema)")
                children[f"{schema}.{table}.{col}"] = {
                    "fk_column": col, "on_delete": _ON_DELETE.get(fk["on_delete"], fk["on_delete"]),
                    "unreadable": "child table is not in the public schema"}
                continue
            table, col = _ident(table), _ident(col)
            # Keyed by table.COLUMN: one child table can hold TWO FKs to
            # product_reviews (review_id and parent_review_id, both CASCADE),
            # and keying by table alone let the second overwrite the first —
            # the rows reached only by the losing FK were deleted with no
            # reversal record, which is the exact failure deriving from
            # pg_constraint was meant to end.
            key = f"{table}.{col}"
            entry: Dict[str, Any] = {"fk_column": col,
                                     "on_delete": _ON_DELETE.get(fk["on_delete"], fk["on_delete"])}
            try:
                entry["rows"] = await _rows(
                    db, f"SELECT * FROM {table} WHERE {col} = ANY(:ids)", {"ids": ids})
            except Exception as exc:  # noqa: BLE001
                # A child the FK graph names but that cannot be read is a hole
                # in the reversal record, so it ABORTS. Recording it and
                # deleting anyway is the silent-fallback shape: the rows would
                # be destroyed with nothing written down.
                entry["unreadable"] = str(exc)[:200]
                unreadable.append(key)
            children[key] = _jsonable(entry)
        if unreadable:
            doors = doors + [f"D7 cascade children could not be read, so the delete would "
                             f"destroy rows with no reversal record: {unreadable}"]
        out["tables"]["product_reviews"] = {
            "action": "delete_with_row_dump",
            "rows": len(reviews),
            "dump": [_jsonable(r) for r in reviews],
            "cascaded_children": children,
            "doors_failed": doors,
        }
        out["doors_failed"].extend(doors)

    return _jsonable(out)


async def apply(db, tables: List[str], run_id: str, plan_result: Dict[str, Any]) -> Dict[str, Any]:
    """Write exactly the rows `plan_result` described, in a per-table transaction.

    THE POPULATION COMES FROM THE PLAN, NEVER FROM A FRESH READ. The doors and
    the dump both belong to `plan()`; re-reading here would write rows that no
    door examined and no dump recorded — measured on a real engine
    (review 2026-08-17): a review inserted after the plan, still SERVING and
    with a product_key that resolves, was deleted by a re-reading apply(), and
    it appeared in no dump, so the run log could not even restore it. Two
    doors would have refused it and neither ran.

    So: the id list is the plan's, and inside the transaction the CURRENT
    residue set must still equal the planned set. A row that joined or left
    the population in between aborts the table and rolls it back — the
    disagreement is the signal, not something to reconcile silently.
    """
    banned = BANNED_BUCKET_MERCHANT_ID
    result: Dict[str, Any] = {"run_id": run_id, "applied": {}}
    # plan() records verdicts in TWO places; reading only the top-level list
    # left a gap a test was already walking through.
    failed = list(plan_result.get("doors_failed") or [])
    for t in tables:
        failed.extend((plan_result.get("tables", {}).get(t) or {}).get("doors_failed") or [])
    if failed:
        raise RuntimeError(f"refusing to apply: doors failed {sorted(set(failed))}")

    if "catalog_offers" in tables:
        planned = plan_result["tables"]["catalog_offers"]["plan"]
        ids = [str(p["offer_id"]) for p in planned]
        async with db.transaction():
            rows = await _rows(db, OFFERS_LOCKED_SQL, {"banned": banned})
            current = {str(o["offer_id"]) for o in rows}
            if current != set(ids):
                raise RuntimeError(
                    f"catalog_offers residue changed since the plan (rolled back): "
                    f"added={sorted(current - set(ids))} removed={sorted(set(ids) - current)}")
            # Same ids can still mean different ROWS — re-run the doors on the
            # locked rows, not just their keys.
            redoors = offer_doors(0, rows)
            if redoors:
                raise RuntimeError(
                    f"catalog_offers rows changed since the plan (rolled back): {redoors}")
            await db.execute(OFFERS_REKEY_SQL, {"offer_ids": ids, "banned": banned})
            left = await _count(
                db, "SELECT count(*) AS c FROM catalog_offers WHERE merchant_id = :banned",
                {"banned": banned})
            # Same standing as the reviews guard below: unreachable while the
            # doors pass, kept for the READ COMMITTED window.
            if left:
                raise RuntimeError(
                    f"catalog_offers still holds {left} sentinel row(s) after re-key "
                    f"(rolled back)")
        result["applied"]["catalog_offers"] = {"rekeyed": len(ids)}

    if "product_reviews" in tables:
        planned = plan_result["tables"]["product_reviews"]["dump"]
        ids = [int(r["id"]) for r in planned]
        async with db.transaction():
            rows = await _rows(db, REVIEWS_LOCKED_SQL, {"banned": banned})
            current = {int(r["id"]) for r in rows}
            if current != set(ids):
                raise RuntimeError(
                    f"product_reviews residue changed since the plan (rolled back): "
                    f"added={sorted(current - set(ids))} removed={sorted(set(ids) - current)}")
            # A row that turned `active`, or whose product_key started
            # resolving, keeps its id — so re-run D5/D6 on the locked rows.
            redoors = review_doors(0, rows)
            if redoors:
                raise RuntimeError(
                    f"product_reviews rows changed since the plan (rolled back): {redoors}")
            await db.execute(REVIEWS_DELETE_SQL, {"ids": ids, "banned": banned})
            left = await _count(
                db, "SELECT count(*) AS c FROM product_reviews WHERE merchant_id = :banned",
                {"banned": banned})
            # Defense in depth, and honestly so: with the set check and the
            # door re-check above passing, the DELETE removes every residue
            # row, so a single session cannot make this fire and its mutant is
            # unkillable from one connection. It still guards the READ
            # COMMITTED window in which another session commits a new sentinel
            # review between the locked re-read and the write. The offers twin
            # is unreachable for the same reason, and says so there too.
            if left:
                raise RuntimeError(
                    f"product_reviews still holds {left} sentinel row(s) after delete "
                    f"(rolled back)")
        result["applied"]["product_reviews"] = {"deleted": len(ids)}

    return result


# The review columns that are safe to PRINT. An ALLOWLIST, not a denylist: a
# denylist leaks every column added after it is written (a future
# `reviewer_email` would print by default), and the house rule from the
# security-test review is allowlist-never-denylist. The FULL row still goes to
# the dump FILE — that is the reversal record; only the copy printed to the run
# log is reduced, because a CI log is readable by everyone with repo access and
# this tool is reusable on rows that are not QA canaries.
_LOGGABLE_REVIEW_FIELDS = frozenset({
    "id", "product_key", "sku_key", "merchant_id", "platform", "platform_product_id",
    "status", "source_type", "source_system", "verification", "rating", "group_id",
    "media_count", "created_at", "updated_at", "scope_resolves",
})


def _redacted_for_log(p: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(p, default=str))
    rev = out.get("tables", {}).get("product_reviews")
    if not rev:
        return out
    for row in rev.get("dump", []):
        for f in list(row):
            if f not in _LOGGABLE_REVIEW_FIELDS and row.get(f) is not None:
                row[f] = f"<redacted {len(str(row[f]))} chars — see the dump file>"
    for child in rev.get("cascaded_children", {}).values():
        if isinstance(child, dict) and isinstance(child.get("rows"), list):
            child["rows"] = f"<{len(child['rows'])} row(s) — see the dump file>"
    return out


def _write_dump(path: Optional[str], payload: Dict[str, Any]) -> Optional[str]:
    """The reversal record. Written and FLUSHED before any write happens; a
    log-only dump is subject to log retention, which is not a place to keep the
    only copy of rows a hard DELETE is about to remove."""
    if not path:
        return None
    f = Path(path)
    if f.exists():
        # The docstring calls this the ONLY reversal record for a hard DELETE;
        # a second run silently overwriting the first is how that record is
        # lost. Refuse rather than clobber.
        raise RuntimeError(f"refusing to overwrite an existing dump file: {f}")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return str(f)


async def _run(tables: List[str], do_apply: bool, dump_file: Optional[str]) -> int:
    from db.database import database

    await database.connect()
    try:
        p = await plan(database, tables)
        if p["doors_failed"]:
            written = _write_dump(dump_file, {"mode": "abort", **p})
            print(json.dumps({"mode": "abort", "reason": "doors failed",
                              "dump_file": written, **_redacted_for_log(p)}, indent=1))
            print("DOORS FAILED — nothing was written:", file=sys.stderr)
            for d in p["doors_failed"]:
                print(f"  {d}", file=sys.stderr)
            return 2
        if not do_apply:
            written = _write_dump(dump_file, {"mode": "dry-run", **p})
            print(json.dumps({"mode": "dry-run", "dump_file": written,
                              **_redacted_for_log(p)}, indent=1))
            return 0
        run_id = str(uuid.uuid4())
        # The dump lands BEFORE the write, so a run that dies mid-apply still
        # leaves everything needed to re-insert.
        written = _write_dump(dump_file, {"mode": "apply", "run_id": run_id, "pre_write_plan": p})
        print(json.dumps({"mode": "apply", "run_id": run_id, "dump_file": written,
                          "pre_write_plan": _redacted_for_log(p)}, indent=1))
        res = await apply(database, tables, run_id, p)
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
    ap.add_argument("--dump-file", default="dispose_sentinel_orphans_dump.json",
                    help="where the FULL (unredacted) plan is written; the reversal "
                         "record. Empty string disables it.")
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
    return asyncio.run(_run(args.table_list, args.apply, args.dump_file or None))


if __name__ == "__main__":
    raise SystemExit(main())
