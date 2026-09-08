"""Reconcile `catalog_offers` against the three states it must never be in.

MEASURED ON PROD 2026-09-08, which is what each pass is sized against:

  (a) 2,139 offers (6.5%) name a `sku_key` with no `catalog_skus` row; 647 of
      them are LIVE. All external-referral, minted by `us_market_capture`
      (529 live) and `retailer_offer_attach_v1` (118 live), all with
      `<product_key>::canonical` sku_keys. An orphan offer is invisible to
      every sku-joined read lane (`pivot_query_service` INNER JOINs on
      `catalog_skus`) while still counting as supply to everything that reads
      `catalog_offers` alone — a row that is simultaneously there and not.
  (b) 1,636 duplicate offers across 462 `(sku_key, channel, market)` groups.
      There is no unique index on that tuple, so two writers (or one writer
      under two offer_id namespaces) can both claim the same shelf and the
      surface that reads "the offer" picks one by sort order.
  (c) 2,171 suppressed products with UNsuppressed offers — see
      `services/catalog_offer_suppression`, which is what stops that class
      regrowing at the writers. This pass drains the standing stock.

SUPPRESS, NEVER DELETE. Every pass sets `suppressed_at` AND
`suppression_reason` (the gate column and the label; a row with one and not the
other is the `suppression_reason_without_timestamp` invariant's subject) and
writes what it did into `suppression_metadata`. Nothing here issues a DELETE:
an orphan offer is evidence of a writer defect and its row is the only record of
what that writer produced. `--revert-batch` puts a batch back.

THE UNIQUE INDEX IS PARTIAL, AND IT HAS TO BE. `--create-unique-index` builds

    UNIQUE (sku_key, channel, market) WHERE suppressed_at IS NULL

and NOT the full-table index the inventory asked for. Because pass (b)
SUPPRESSES the losing rows rather than deleting them, those rows stay in the
table forever; a full unique index on the tuple could never be built on this
data — not after this script runs, not after any number of runs. The live
predicate is also the only shape that matches what the tuple means: two
suppressed rows on one shelf are history, two LIVE rows on one shelf are the
defect. Anything that reads the index must therefore read live rows.

WHY THE INDEX IS HERE AND NOT IN db/migrations/. A migration creating this index
would run at deploy against a database that still has 1,636 duplicates, fail,
and take the deploy with it. The order has to be: land this script, run it with
`--apply` on prod, run it with `--create-unique-index`, THEN land a migration
carrying the same `CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS` so a
fresh/rebuilt database gets it too (that migration is a no-op against prod,
which already has the index by then). The migration is deliberately NOT in this
PR. See the PR body.

`CREATE INDEX CONCURRENTLY` must run OUTSIDE a transaction, so this script never
wraps it in one. The repo's migration runner classifies CONCURRENTLY by a regex
over the whole file including prose — irrelevant here, because this is a script
and not a migration, but worth stating so the next reader does not go looking.

Usage
-----
  python3 scripts/reconcile_catalog_offers.py                       # dry run, all passes
  python3 scripts/reconcile_catalog_offers.py --apply
  python3 scripts/reconcile_catalog_offers.py --apply --limit 500
  python3 scripts/reconcile_catalog_offers.py --apply --pass duplicates
  python3 scripts/reconcile_catalog_offers.py --apply --create-unique-index
  python3 scripts/reconcile_catalog_offers.py --apply --revert-batch <batch_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_offer_writer_guard import (  # noqa: E402
    WriterAuditAccumulator,
    make_batch_id,
    write_writer_audit_log,
)

WRITER_NAME = "reconcile_catalog_offers"

#: The three labels this script writes. They are the vocabulary a reader of
#: `catalog_offers.suppression_reason` will meet, and `--revert-batch` keys on
#: them, so they are constants rather than inline strings.
REASON_ORPHAN = "orphan_no_sku"
REASON_DUPLICATE = "duplicate_offer"
REASON_PRODUCT_SUPPRESSED = "product_suppressed"

PASSES = ("orphans", "duplicates", "cascade")

#: The report is ONE LINE and fenced. `scripts/ops/run_oneoff_job.sh` retrieves a
#: job's output from Cloud Logging, which DROPS LINES — a pretty-printed report
#: arrives with arbitrary keys missing and nothing saying anything is gone. A
#: single line cannot be partially dropped, and the sentinels let a caller cut it
#: out of the surrounding log:
#:
#:   ... | grep -o 'RECONREPORT>>>{.*}<<<RECONREPORT' \
#:       | sed 's/^RECONREPORT>>>//; s/<<<RECONREPORT$//' | python3 -m json.tool
REPORT_BEGIN = "RECONREPORT>>>"
REPORT_END = "<<<RECONREPORT"

#: The index `--create-unique-index` builds, and the one the follow-up migration
#: must name. Partial on the live predicate — see the module docstring.
UNIQUE_INDEX_NAME = "idx_catalog_offers_live_sku_channel_market"


# ── pass (a): orphan offers ──────────────────────────────────────────────────
# LEFT-anti-join by NOT EXISTS on catalog_skus.sku_key, the exact join
# `pivot_query_service` makes. `LIMIT CAST(:limit AS bigint)` with a NULL bind is
# Postgres for "no limit", so one statement serves both --limit N and the
# unlimited default; a separate unlimited copy of each statement is how two
# spellings of the same predicate drift apart.
ORPHAN_SELECT_SQL = """
    SELECT co.offer_id, co.sku_key, co.product_key,
           coalesce(co.source_system, '') AS source_system
      FROM catalog_offers co
     WHERE co.suppressed_at IS NULL
       AND NOT EXISTS (
             SELECT 1 FROM catalog_skus s WHERE s.sku_key = co.sku_key
           )
     ORDER BY co.offer_id
     LIMIT CAST(:limit AS bigint)
"""

# ── pass (b): duplicate (sku_key, channel, market) ───────────────────────────
# The keeper is rn = 1: newest `updated_at` first, NULLS LAST so a row that has
# never been updated never beats one that has, then the LOWEST offer_id as the
# tie-break. Only rn > 1 is suppressed, so exactly one live row survives per
# group. Both ORDER BY terms are load-bearing and both are pinned by tests: with
# `updated_at ASC` the reconciler keeps the STALEST price, and with no offer_id
# tie-break two rows sharing a timestamp make the survivor depend on scan order.
DUPLICATE_SELECT_SQL = """
    SELECT offer_id, sku_key, channel, market, keeper_offer_id
      FROM (
        SELECT co.offer_id, co.sku_key, co.channel, co.market,
               row_number() OVER w AS rn,
               first_value(co.offer_id) OVER w AS keeper_offer_id
          FROM catalog_offers co
         WHERE co.suppressed_at IS NULL
           AND NOT (co.offer_id = ANY(:excluded))
        WINDOW w AS (
                 PARTITION BY co.sku_key, co.channel, co.market
                 ORDER BY co.updated_at DESC NULLS LAST, co.offer_id ASC
               )
      ) ranked
     WHERE rn > 1
     ORDER BY sku_key, channel, market, offer_id
     LIMIT CAST(:limit AS bigint)
"""

#: The gate `--create-unique-index` reads. Counts GROUPS with more than one live
#: row — the thing the index forbids — not excess rows, because a group of three
#: is one index violation and two excess rows, and the index cares about the
#: former.
#:
#: `:excluded` is how a DRY RUN answers the same question a real run would.
#: Under --apply it is always empty — the gate must measure the table as it IS
#: before building a unique index on it, never as a plan believes it will be.
DUPLICATE_GROUP_COUNT_SQL = """
    SELECT count(*) AS c
      FROM (
        SELECT co.sku_key, co.channel, co.market
          FROM catalog_offers co
         WHERE co.suppressed_at IS NULL
           AND NOT (co.offer_id = ANY(:excluded))
         GROUP BY co.sku_key, co.channel, co.market
        HAVING count(*) > 1
      ) groups
"""

# ── pass (c): suppression cascade ────────────────────────────────────────────
CASCADE_SELECT_SQL = """
    SELECT co.offer_id, co.product_key
      FROM catalog_offers co
      JOIN catalog_products cp ON cp.product_key = co.product_key
     WHERE co.suppressed_at IS NULL
       AND cp.suppressed_at IS NOT NULL
     ORDER BY co.offer_id
     LIMIT CAST(:limit AS bigint)
"""

# ── the write ────────────────────────────────────────────────────────────────
# BOTH columns, plus a metadata stamp carrying the batch id so --revert-batch can
# find exactly this run's rows. `suppressed_at IS NULL` in the WHERE keeps the
# statement idempotent and keeps it from re-stamping a row another lane
# tombstoned between the SELECT and the UPDATE.
SUPPRESS_OFFERS_SQL = """
    UPDATE catalog_offers
       SET suppressed_at = NOW(),
           suppression_reason = CAST(:reason AS text),
           suppression_metadata = coalesce(suppression_metadata, '{}'::jsonb)
                                  || CAST(:meta AS jsonb),
           updated_at = NOW()
     WHERE offer_id = ANY(:offer_ids)
       AND suppressed_at IS NULL
    RETURNING offer_id
"""

#: Revert OUR batch only, and only rows still carrying the reason we set. An
#: offer another lane re-tombstoned after us keeps its gate.
REVERT_BATCH_SQL = """
    UPDATE catalog_offers
       SET suppressed_at = NULL,
           suppression_reason = NULL,
           suppression_metadata = suppression_metadata - 'reconcile_batch_id'
                                                       - 'reconcile_pass'
                                                       - 'reconcile_keeper_offer_id',
           updated_at = NOW()
     WHERE suppression_metadata->>'reconcile_batch_id' = CAST(:batch_id AS text)
       AND suppressed_at IS NOT NULL
       AND suppression_reason = ANY(:reasons)
    RETURNING offer_id
"""

#: CONCURRENTLY, IF NOT EXISTS, PARTIAL. Runs outside any transaction — see the
#: module docstring for why it is a script and not a migration.
CREATE_UNIQUE_INDEX_SQL = f"""
    CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {UNIQUE_INDEX_NAME}
        ON catalog_offers (sku_key, channel, market)
     WHERE suppressed_at IS NULL
"""

#: A CONCURRENTLY build that loses a race leaves an INVALID index behind that
#: enforces nothing and is not reported by the CREATE. Asked explicitly, so the
#: report can never say "created" about an index that is not enforcing.
INDEX_STATE_SQL = """
    SELECT i.indisvalid AS valid, i.indisready AS ready
      FROM pg_class c
      JOIN pg_index i ON i.indexrelid = c.oid
     WHERE c.relname = CAST(:index_name AS text)
"""


def _limit_bind(limit: int) -> Optional[int]:
    """`--limit 0` (the default) means every row. Postgres reads `LIMIT NULL` as
    unlimited, so the same statement serves both."""
    return int(limit) if limit and limit > 0 else None


async def _fetch(sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(sql, params)
    return [dict(row) for row in (rows or [])]


async def _suppress(
    offer_ids: List[str], *, reason: str, batch_id: str, pass_name: str,
    keepers: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Apply one pass's suppression. Returns the offer ids ACTUALLY moved.

    `RETURNING offer_id`, never a rowcount: `databases` + asyncpg returns no
    rowcount from `execute()` (SQLite does, which is how a caller comes to think
    it has one), so a count read any other way would be invented.

    The duplicate pass carries a per-row `reconcile_keeper_offer_id`, so a reader
    of a suppressed row can see WHICH row won without re-deriving the ranking —
    which is the only way to tell a correct dedupe from one that kept the wrong
    row after the fact. That forces one statement per row for that pass; the
    other two suppress their whole batch in one.
    """
    if not offer_ids:
        return []
    moved: List[str] = []
    if keepers:
        for offer_id in offer_ids:
            meta = {
                "reconcile_batch_id": batch_id,
                "reconcile_pass": pass_name,
                "reconcile_keeper_offer_id": keepers.get(offer_id),
            }
            rows = await database.fetch_all(
                SUPPRESS_OFFERS_SQL,
                {"offer_ids": [offer_id], "reason": reason,
                 "meta": json.dumps(meta, ensure_ascii=False)},
            )
            moved.extend(str(r["offer_id"]) for r in (rows or []))
        return moved
    meta = {"reconcile_batch_id": batch_id, "reconcile_pass": pass_name}
    rows = await database.fetch_all(
        SUPPRESS_OFFERS_SQL,
        {"offer_ids": offer_ids, "reason": reason,
         "meta": json.dumps(meta, ensure_ascii=False)},
    )
    return [str(r["offer_id"]) for r in (rows or [])]


def _unclaimed(rows: List[Dict[str, Any]], claimed: Set[str]) -> List[Dict[str, Any]]:
    """Drop rows an EARLIER pass has already taken.

    Load-bearing in DRY RUN, and only there. Under --apply an earlier pass has
    already set `suppressed_at`, so every pass's own `suppressed_at IS NULL`
    filter excludes those rows and this is a no-op. A dry run writes nothing, so
    without it the later passes re-count rows the earlier ones claimed and the
    plan disagrees with the run it is supposed to predict — measured: a shelf
    holding two duplicates plus one offer of a suppressed product reported
    `excess_rows_found: 2` in the plan and 1 on apply.
    """
    return [row for row in rows if str(row["offer_id"]) not in claimed]


async def run_orphan_pass(
    *, apply: bool, limit: int, batch_id: str, claimed: Set[str],
) -> Tuple[Dict[str, Any], List[str]]:
    rows = _unclaimed(
        await _fetch(ORPHAN_SELECT_SQL, {"limit": _limit_bind(limit)}), claimed)
    by_writer: Dict[str, int] = {}
    for row in rows:
        writer = str(row.get("source_system") or "(null)")
        by_writer[writer] = by_writer.get(writer, 0) + 1
    offer_ids = [str(r["offer_id"]) for r in rows]
    moved = await _suppress(
        offer_ids, reason=REASON_ORPHAN, batch_id=batch_id, pass_name="orphans",
    ) if apply else []
    return (
        {"found": len(rows), "suppressed": len(moved), "by_source_system": by_writer,
         "sample": offer_ids[:5], "claimed": offer_ids},
        moved,
    )


async def run_duplicate_pass(
    *, apply: bool, limit: int, batch_id: str, claimed: Set[str],
) -> Tuple[Dict[str, Any], List[str]]:
    # EXCLUDED FROM THE RANKING, not filtered out of its result. A row an
    # earlier pass has claimed is about to leave the shelf, and a keeper election
    # that still ranks it elects a row --apply would not: measured on the demo
    # fixture, a shelf of three whose newest row belongs to a suppressed product
    # planned `excess_rows_found: 2` and applied 1. Under --apply the claimed
    # rows already carry suppressed_at, so this is a no-op there.
    excluded = sorted(claimed)
    rows = await _fetch(
        DUPLICATE_SELECT_SQL, {"limit": _limit_bind(limit), "excluded": excluded})
    keepers = {str(r["offer_id"]): str(r["keeper_offer_id"]) for r in rows}
    offer_ids = [str(r["offer_id"]) for r in rows]
    groups = {(r["sku_key"], r["channel"], r["market"]) for r in rows}
    moved = await _suppress(
        offer_ids, reason=REASON_DUPLICATE, batch_id=batch_id,
        pass_name="duplicates", keepers=keepers,
    ) if apply else []
    # Re-measured AFTER the writes, not derived from them: this is the number
    # --create-unique-index gates on, and computing it as `groups_before -
    # groups_touched` would report zero for a run that only suppressed some of
    # each group's excess rows (which --limit can do).
    # In a dry run, the rows this pass WOULD suppress are excluded too, so the
    # predicted `live_duplicate_groups_after` is the number --apply would leave.
    remaining = await database.fetch_one(
        DUPLICATE_GROUP_COUNT_SQL,
        {"excluded": excluded if apply else sorted(set(excluded) | set(offer_ids))},
    )
    return (
        {"excess_rows_found": len(rows), "groups_found": len(groups),
         "suppressed": len(moved),
         "live_duplicate_groups_after": int((remaining["c"] if remaining else 0) or 0),
         "sample": offer_ids[:5], "claimed": offer_ids},
        moved,
    )


async def run_cascade_pass(
    *, apply: bool, limit: int, batch_id: str, claimed: Set[str],
) -> Tuple[Dict[str, Any], List[str]]:
    rows = _unclaimed(
        await _fetch(CASCADE_SELECT_SQL, {"limit": _limit_bind(limit)}), claimed)
    offer_ids = [str(r["offer_id"]) for r in rows]
    products = {str(r["product_key"]) for r in rows}
    moved = await _suppress(
        offer_ids, reason=REASON_PRODUCT_SUPPRESSED, batch_id=batch_id,
        pass_name="cascade",
    ) if apply else []
    return (
        {"found": len(rows), "products": len(products), "suppressed": len(moved),
         "sample": offer_ids[:5], "claimed": offer_ids},
        moved,
    )


async def create_unique_index(*, apply: bool) -> Dict[str, Any]:
    """Build the partial unique index, but ONLY on a table that currently has no
    live duplicate group.

    The gate is re-read here rather than passed in from pass (b): the passes are
    individually selectable and `--limit`-able, so "pass (b) ran" is not the same
    claim as "there are no duplicates left", and the index build is the one
    operation that cannot be half-right.
    """
    # `excluded` is EMPTY here, always. The index build has to be gated on the
    # table as it is, not on what some pass in this run believes it will become.
    remaining_row = await database.fetch_one(
        DUPLICATE_GROUP_COUNT_SQL, {"excluded": []})
    remaining = int((remaining_row["c"] if remaining_row else 0) or 0)
    if remaining:
        return {"attempted": False, "reason": "live_duplicate_groups_remain",
                "live_duplicate_groups": remaining}
    if not apply:
        return {"attempted": False, "reason": "dry_run", "live_duplicate_groups": 0}
    # NO `async with database.transaction()` anywhere on this path. Postgres
    # refuses CREATE INDEX CONCURRENTLY inside a transaction block outright.
    await database.execute(CREATE_UNIQUE_INDEX_SQL)
    state = await database.fetch_one(INDEX_STATE_SQL, {"index_name": UNIQUE_INDEX_NAME})
    valid = bool(state["valid"]) if state else False
    return {"attempted": True, "index": UNIQUE_INDEX_NAME,
            "exists": state is not None, "valid": valid,
            # An INVALID index enforces nothing. Saying so is the difference
            # between "the constraint is on" and "a CREATE returned".
            "enforcing": bool(state is not None and valid),
            "live_duplicate_groups": 0}


async def revert_batch(batch_id: str, *, apply: bool) -> Dict[str, Any]:
    reasons = [REASON_ORPHAN, REASON_DUPLICATE, REASON_PRODUCT_SUPPRESSED]
    if not apply:
        rows = await _fetch(
            """
            SELECT offer_id
              FROM catalog_offers
             WHERE suppression_metadata->>'reconcile_batch_id' = CAST(:batch_id AS text)
               AND suppressed_at IS NOT NULL
               AND suppression_reason = ANY(:reasons)
             ORDER BY offer_id
            """,
            {"batch_id": batch_id, "reasons": reasons},
        )
        return {"batch_id": batch_id, "would_restore": len(rows),
                "restored": 0, "sample": [str(r["offer_id"]) for r in rows[:5]]}
    rows = await _fetch(REVERT_BATCH_SQL, {"batch_id": batch_id, "reasons": reasons})
    return {"batch_id": batch_id, "would_restore": len(rows),
            "restored": len(rows), "sample": [str(r["offer_id"]) for r in rows[:5]]}


async def run(
    *, apply: bool, limit: int, passes: Tuple[str, ...],
    create_index: bool, revert: str = "",
) -> Dict[str, Any]:
    audit = WriterAuditAccumulator(
        writer_name=WRITER_NAME, batch_id=make_batch_id(WRITER_NAME)
    )
    report: Dict[str, Any] = {
        "writer": WRITER_NAME,
        "batch_id": audit.batch_id,
        "applied": 1 if apply else 0,
        "limit": int(limit or 0),
        "passes": list(passes),
    }

    if revert:
        report["revert"] = await revert_batch(revert, apply=apply)
        report["passes"] = []
        if apply:
            audit.record_applied(int(report["revert"]["restored"]))
            audit.record_info({"reverted_batch_rows": int(report["revert"]["restored"])})
            await write_writer_audit_log(audit)
        return report

    total_moved = 0
    # What earlier passes have taken. Under --apply the rows are already gated
    # and each pass's own filter would exclude them anyway; in a dry run this is
    # the only thing that keeps the plan equal to the run.
    claimed: Set[str] = set()
    if "orphans" in passes:
        detail, moved = await run_orphan_pass(
            apply=apply, limit=limit, batch_id=audit.batch_id, claimed=claimed)
        report["orphans"] = detail
        claimed.update(detail["claimed"])
        total_moved += len(moved)
    if "cascade" in passes:
        # BEFORE duplicates, on purpose: a suppressed product's offers stop being
        # duplicate candidates once cascaded, so running cascade first means the
        # duplicate pass never picks a row that was about to be gated anyway and
        # never labels a `product_suppressed` row `duplicate_offer`.
        detail, moved = await run_cascade_pass(
            apply=apply, limit=limit, batch_id=audit.batch_id, claimed=claimed)
        report["cascade"] = detail
        claimed.update(detail["claimed"])
        total_moved += len(moved)
    if "duplicates" in passes:
        detail, moved = await run_duplicate_pass(
            apply=apply, limit=limit, batch_id=audit.batch_id, claimed=claimed)
        report["duplicates"] = detail
        claimed.update(detail["claimed"])
        total_moved += len(moved)

    if create_index:
        report["unique_index"] = await create_unique_index(apply=apply)

    for name in PASSES:
        if isinstance(report.get(name), dict):
            report[name].pop("claimed", None)
    report["suppressed_total"] = total_moved
    if apply:
        audit.record_applied(total_moved)
        counters = {
            f"{name}_suppressed": int((report.get(name) or {}).get("suppressed") or 0)
            for name in PASSES if name in report
        }
        # record_info drops <= 0, so a measured zero would be indistinguishable
        # from never-measured in writer_audit_log.reasons. Send the zeros under
        # their own key instead of losing them.
        audit.record_info({k: v for k, v in counters.items() if v > 0})
        audit.reasons["zero_counters"] = sorted(k for k, v in counters.items() if v == 0)
        if create_index:
            audit.reasons["unique_index"] = report["unique_index"]
        await write_writer_audit_log(audit)
    return report


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true",
                   help="write; the default is a dry run that changes nothing")
    p.add_argument("--limit", type=int, default=0,
                   help="cap rows per pass (0 = every row)")
    p.add_argument("--pass", dest="passes", action="append", choices=PASSES,
                   help="run only this pass; repeatable. Default: all three")
    p.add_argument("--create-unique-index", action="store_true",
                   help=f"build {UNIQUE_INDEX_NAME} — a PARTIAL unique index on "
                        "(sku_key, channel, market) WHERE suppressed_at IS NULL. "
                        "Refuses while any live duplicate group remains.")
    p.add_argument("--revert-batch", default="",
                   help="restore the offers one batch_id suppressed")
    args = p.parse_args(argv)
    if args.limit < 0:
        p.error("--limit must be >= 0")
    if args.revert_batch and (args.passes or args.create_unique_index):
        p.error("--revert-batch runs alone")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    passes = tuple(args.passes) if args.passes else PASSES

    async def _go() -> Dict[str, Any]:
        await database.connect()
        try:
            return await run(
                apply=args.apply, limit=args.limit, passes=passes,
                create_index=args.create_unique_index, revert=args.revert_batch,
            )
        finally:
            await database.disconnect()

    report = asyncio.run(_go())
    print(REPORT_BEGIN + json.dumps(report, sort_keys=True, default=str) + REPORT_END,
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
