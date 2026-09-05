#!/usr/bin/env python3
"""Withdraw named catalog rows from serving, reversibly. Never deletes.

WHY THIS EXISTS. The curated-brand lane (`scripts/onboard_curated_brands.py`)
upserts and never prunes, so a row it should not have minted — the $0.01
"Free Travel … (TikTok Shop)" promo it ingested from stilacosmetics.com on
2026-09-05, before the price floor existed — keeps serving after the lane is
fixed and re-run. The other takedown runner, `remediate_unpublished_crawl_rows`,
selects its own cohort (crawl rows absent from the merchant sitemap); nothing
takes down a row by NAME. This does.

WHAT IT WRITES, per product_key, in this order:
  1. catalog_products, catalog_skus, catalog_offers — suppression_reason +
     suppressed_at + suppression_metadata{script, reason} in ONE statement per
     table. The serving classifier reads `suppressed_at` (blocker_code
     'suppressed'); the label alone leaves the row serving, and writing the two
     as separate autocommitted statements is how reason-only rows were minted
     before (see tests/test_canonical_feed_tombstoned_flag_postgres.py). The
     metadata is what makes the withdrawal OWNED: --revert touches only rows
     whose metadata names this script, on all three tables, so a SKU or offer
     another lane tombstoned earlier (catalog_sync_service's stale sweep,
     merge_duplicate_canonicals) is neither re-gated here nor un-gated later.
  2. external_product_seeds — status 'inactive' for the seeds attached to the
     key that were ACTIVE at withdrawal time; their ids are recorded in the
     product's metadata (`deactivated_seed_ids`) so --revert re-activates
     exactly those and never a seed another lane retired (the destination
     sweep's 404 retirements, identity-resolution losers).
  3. index_pipeline_state — recompute_serving_eligibility per content_key, then
     the state is READ BACK from the table (never inferred: the recompute
     swallows exceptions and returns False for "went dark" and "blew up" alike).
  4. catalog_row_trust — upsert for the touched product_keys, the column public
     readers gate on (the promotion lane writes it for the same reason).

CONTENT_KEY GRAIN, stated because it is the obvious trap: index_pipeline_state
is keyed on content_key and takes the MAX across the key's rows. Withdrawing one
row on a key that has live siblings leaves the KEY serving-eligible — the
withdrawn row itself is no longer advertised, but the read-back will say
'serving'. The report prints "live rows on key" so that reading is not a surprise.

Every write is guarded on the column it changes (suppressed_at IS NULL /
status = 'active' / ownership), so a re-run is a no-op.

Usage:
  python3 scripts/withdraw_catalog_rows.py --product-key <pk> [--product-key ...] \
      [--reason token_price_promo]               # dry-run: reports, writes nothing
  python3 scripts/withdraw_catalog_rows.py --product-key <pk> --reason token_price_promo --apply
  python3 scripts/withdraw_catalog_rows.py --revert [--product-key <pk>] --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many  # noqa: E402
from services.index_pipeline_state_service import recompute_serving_eligibility  # noqa: E402

SCRIPT_NAME = "withdraw_catalog_rows"
DEFAULT_REASON = "manual_withdrawal"

# The three tables a withdrawal gates. One statement shape each way, applied per
# table, so the reason+timestamp+ownership invariant cannot drift between them.
_SUPPRESSIBLE_TABLES = ("catalog_products", "catalog_skus", "catalog_offers")

_ROW_COLUMNS_SQL = """
SELECT cp.product_key, cp.content_key, cp.title, cp.brand, cp.source_system, cp.source_domain,
       cp.suppression_reason, cp.suppressed_at, cp.suppression_metadata,
       (SELECT count(*) FROM catalog_skus s WHERE s.product_key = cp.product_key) AS skus,
       (SELECT count(*) FROM catalog_offers o WHERE o.product_key = cp.product_key) AS offers,
       (SELECT count(*) FROM external_product_seeds e
         WHERE e.attached_product_key = cp.product_key) AS seeds,
       (SELECT count(*) FROM external_product_seeds e
         WHERE e.attached_product_key = cp.product_key AND e.status = 'active') AS active_seeds,
       (SELECT count(*) FROM catalog_products c2
         WHERE c2.content_key = cp.content_key AND c2.suppressed_at IS NULL) AS rows_on_key
FROM catalog_products cp
"""
_LOAD_SQL = _ROW_COLUMNS_SQL + "WHERE cp.product_key = ANY(:pks)"
_LOAD_OURS_SQL = _ROW_COLUMNS_SQL + (
    "WHERE cp.suppressed_at IS NOT NULL "
    "  AND cp.suppression_metadata->>'script' = CAST(:script AS text)"
)


async def _load_rows(product_keys: List[str]) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(_LOAD_SQL, {"pks": list(product_keys)})
    found = [dict(r) for r in rows]
    missing = sorted(set(product_keys) - {r["product_key"] for r in found})
    for pk in missing:
        print(f"  ! no catalog_products row for {pk}")
    return found


async def _load_ours(product_keys: Optional[List[str]]) -> List[Dict[str, Any]]:
    rows = [dict(r) for r in await database.fetch_all(_LOAD_OURS_SQL, {"script": SCRIPT_NAME})]
    if product_keys:
        keep = set(product_keys)
        ours = {r["product_key"] for r in rows}
        for pk in sorted(keep - ours):
            print(f"  ! {pk}: not withdrawn by this script (or not suppressed) — skipped")
        rows = [r for r in rows if r["product_key"] in keep]
    return rows


async def _recompute_and_verify(content_keys: List[str], reason: str) -> Dict[str, str]:
    """Recompute each content_key, then read the state back from the table.
    Returns {content_key: 'dark' | 'serving' | 'unknown'}."""
    out: Dict[str, str] = {}
    for ck in content_keys:
        await recompute_serving_eligibility(ck, reason=reason)
        row = await database.fetch_one(
            "SELECT serving_eligible FROM index_pipeline_state WHERE content_key = :ck", {"ck": ck}
        )
        if row is None:
            out[ck] = "unknown"
        else:
            out[ck] = "serving" if dict(row).get("serving_eligible") else "dark"
    return out


async def _withdraw(rows: List[Dict[str, Any]], reason: str) -> Dict[str, str]:
    """Suppress product + skus + offers under our name, deactivate the seeds that
    were active (recording which), recompute, refresh trust."""
    for row in rows:
        pk = row["product_key"]
        # Which seeds are we about to deactivate? Recorded BEFORE the write so
        # --revert can restore exactly these and nothing another lane retired.
        seed_rows = await database.fetch_all(
            "SELECT id FROM external_product_seeds "
            " WHERE attached_product_key = :pk AND status = 'active'",
            {"pk": pk},
        )
        seed_ids = [str(dict(r)["id"]) for r in seed_rows]
        for table in _SUPPRESSIBLE_TABLES:
            # Reason, timestamp and provenance in ONE statement — never a reason
            # without its gate column. Guarded on suppressed_at so a re-run and a
            # row another lane already withdrew are both left alone. CASTs are
            # load-bearing: jsonb_build_object is variadic "any" and Postgres
            # cannot infer a bind's type from position (fails at PREPARE).
            await database.execute(
                f"UPDATE {table} "
                "   SET suppression_reason = COALESCE(suppression_reason, CAST(:reason AS text)), "
                "       suppressed_at = NOW(), "
                "       suppression_metadata = COALESCE(suppression_metadata, '{}'::jsonb) "
                "         || jsonb_build_object("
                "              'script', CAST(:script AS text), "
                "              'reason', CAST(:reason AS text), "
                "              'deactivated_seed_ids', CAST(:seed_ids AS jsonb)), "
                "       updated_at = NOW() "
                " WHERE product_key = :pk AND suppressed_at IS NULL",
                {"pk": pk, "reason": reason, "script": SCRIPT_NAME, "seed_ids": json.dumps(seed_ids)},
            )
        await database.execute(
            "UPDATE external_product_seeds SET status = 'inactive', updated_at = NOW() "
            " WHERE id = ANY(:ids) AND status = 'active'",
            {"ids": seed_ids},
        )
    keys = sorted({r["content_key"] for r in rows if r.get("content_key")})
    states = await _recompute_and_verify(keys, f"{SCRIPT_NAME}:{reason}")
    await upsert_catalog_row_trust_many(db=database, product_keys=[r["product_key"] for r in rows])
    return states


def _meta(row: Dict[str, Any]) -> Dict[str, Any]:
    meta = row.get("suppression_metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    return meta if isinstance(meta, dict) else {}


async def _revert(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Undo OUR withdrawals only. Every table is guarded on the ownership stamp
    in its own metadata, so a SKU/offer another lane tombstoned before we came
    along keeps its gate column and its reason. Seeds come back only if we were
    the ones that put them to sleep."""
    for row in rows:
        pk = row["product_key"]
        meta = _meta(row)
        if meta.get("script") != SCRIPT_NAME:
            print(f"  ! {pk}: suppressed by {meta.get('script') or 'another lane'}, not reverting")
            continue
        reason = str(meta.get("reason") or DEFAULT_REASON)
        seed_ids = [str(s) for s in (meta.get("deactivated_seed_ids") or [])]
        for table in _SUPPRESSIBLE_TABLES:
            await database.execute(
                f"UPDATE {table} "
                "   SET suppressed_at = NULL, "
                "       suppression_reason = CASE WHEN suppression_reason = CAST(:reason AS text) "
                "                                 THEN NULL ELSE suppression_reason END, "
                "       suppression_metadata = suppression_metadata "
                "                              - 'script' - 'reason' - 'deactivated_seed_ids', "
                "       updated_at = NOW() "
                " WHERE product_key = :pk AND suppressed_at IS NOT NULL "
                "   AND suppression_metadata->>'script' = CAST(:script AS text)",
                {"pk": pk, "reason": reason, "script": SCRIPT_NAME},
            )
        await database.execute(
            "UPDATE external_product_seeds SET status = 'active', updated_at = NOW() "
            " WHERE id = ANY(:ids) AND status = 'inactive'",
            {"ids": seed_ids},
        )
    keys = sorted({r["content_key"] for r in rows if r.get("content_key")})
    states = await _recompute_and_verify(keys, f"{SCRIPT_NAME}:revert")
    await upsert_catalog_row_trust_many(db=database, product_keys=[r["product_key"] for r in rows])
    return states


def _report(rows: List[Dict[str, Any]], *, revert: bool) -> None:
    for r in rows:
        state = "SUPPRESSED" if r.get("suppressed_at") else "live"
        print(
            f"  {r['product_key']}\n"
            f"      {r.get('brand')} | {r.get('title')} | {r.get('source_system')} @ {r.get('source_domain')}\n"
            f"      {state}"
            + (f" ({r.get('suppression_reason')})" if r.get("suppression_reason") else "")
            + f" · content_key={r.get('content_key')} · skus={r.get('skus')} offers={r.get('offers')}"
            f" seeds={r.get('seeds')} (active {r.get('active_seeds')}) · live rows on key={r.get('rows_on_key')}"
        )
    if not revert:
        multi = [r for r in rows if int(r.get("rows_on_key") or 0) > 1]
        if multi:
            print(
                f"  note: {len(multi)} row(s) share a content_key with live siblings — the key stays "
                "serving-eligible for the siblings; only the withdrawn row stops being advertised."
            )


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        if args.revert:
            rows = await _load_ours(args.product_key or None)
            if not rows:
                print(f"nothing to revert: no rows carry suppression_metadata.script={SCRIPT_NAME!r}")
                return 0
            print(f"{len(rows)} row(s) withdrawn by this script:")
            _report(rows, revert=True)
            if not args.apply:
                print("DRY-RUN — re-run with --apply to revert.")
                return 0
            states = await _revert(rows)
        else:
            if not args.product_key:
                print("--product-key is required unless --revert", file=sys.stderr)
                return 2
            rows = await _load_rows(args.product_key)
            actionable = [r for r in rows if not r.get("suppressed_at")]
            print(f"{len(rows)} row(s) found, {len(actionable)} live:")
            _report(rows, revert=False)
            if not actionable:
                print("nothing to withdraw.")
                return 0
            if not args.apply:
                print(f"DRY-RUN — re-run with --apply to withdraw {len(actionable)} row(s) as {args.reason!r}.")
                return 0
            states = await _withdraw(actionable, args.reason)
        for ck, state in sorted(states.items()):
            print(f"  {ck}: {state}")
        print(f"done: {len(states)} content_key(s) recomputed, "
              f"{sum(1 for s in states.values() if s == 'dark')} dark, "
              f"{sum(1 for s in states.values() if s == 'serving')} still serving, "
              f"{sum(1 for s in states.values() if s == 'unknown')} unknown")
        return 0
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--product-key", action="append", help="catalog_products.product_key (repeatable)")
    p.add_argument("--reason", default=DEFAULT_REASON, help="suppression_reason to stamp (default: manual_withdrawal)")
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    p.add_argument("--revert", action="store_true",
                   help="undo withdrawals made by this script (optionally limited to --product-key)")
    args = p.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
