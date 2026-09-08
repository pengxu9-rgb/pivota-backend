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

THERE IS NO UNIQUE INDEX IN THIS SCRIPT, AND THAT IS A DECISION. An earlier
draft carried a `--create-unique-index` flag building

    UNIQUE (sku_key, channel, market) WHERE suppressed_at IS NULL

It was removed, because building it would take the next writer down. EVERY
`INSERT INTO catalog_offers` in this repo arbitrates on `(offer_id)` alone —
`services/external_offer_dual_write`, `scripts/capture_us_market_offers`,
`scripts/attach_retailer_offer`, `catalog_enrichment_agent/apply`,
`scripts/backfill_variant_identity_skus`,
`scripts/backfill_canonical_chain_for_agent_seeds`,
`scripts/source_pdp_offer_image_repair` — and the mirror, the capture and the
attach lanes write THE SAME SHELF under THREE DIFFERENT offer_id namespaces
("offer:external_seed:", "offer:us_market:", "offer:retailer:"). That is what
the 462 duplicate groups ARE. With the index in place, `ON CONFLICT (offer_id)`
never fires for the rival row, so the second lane's INSERT raises 23505 and
dies mid-batch instead of updating — reproduced on real Postgres.

THE PREREQUISITE IS CONVERGENCE, NOT A BUILD ORDER. The index becomes safe once
the three lanes agree on ONE offer_id namespace per shelf (or arbitrate on the
shelf tuple rather than on offer_id) — a separate change to those writers, not
something this reconciler can do by draining rows. Until then the alarm is
`duplicate_offers_per_sku_channel_market` in `services/catalog_invariant_checks`
at threshold 0: it counts the excess LIVE rows and goes red the moment two lanes
claim one shelf again, which is the same signal the index would give without the
failure mode. Pass (b) here is what drains the standing stock.

Usage
-----
  python3 scripts/reconcile_catalog_offers.py                       # dry run, all passes
  python3 scripts/reconcile_catalog_offers.py --apply
  python3 scripts/reconcile_catalog_offers.py --apply --limit 500
  python3 scripts/reconcile_catalog_offers.py --apply --pass duplicates
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
from services.catalog_offer_suppression import (  # noqa: E402
    PRODUCT_SUPPRESSED_REASON,
)
from services.catalog_offer_writer_guard import (  # noqa: E402
    ORPHAN_NO_SKU,
    WriterAuditAccumulator,
    make_batch_id,
    write_writer_audit_log,
)

WRITER_NAME = "reconcile_catalog_offers"

#: The three labels this script writes. They are the vocabulary a reader of
#: `catalog_offers.suppression_reason` will meet, and `--revert-batch` keys on
#: them, so they are constants rather than inline strings.
#:
#: TWO OF THE THREE ARE IMPORTED, not re-spelled here. `orphan_no_sku` is the
#: vocabulary `services/catalog_offer_writer_guard` refuses with at the writers,
#: and `product_suppressed` is the label `services/catalog_offer_suppression`
#: cascades with — this script's passes must carry the SAME strings, and two
#: independent literals are two things that can drift while every test still
#: passes.
REASON_ORPHAN = ORPHAN_NO_SKU
REASON_DUPLICATE = "duplicate_offer"
REASON_PRODUCT_SUPPRESSED = PRODUCT_SUPPRESSED_REASON

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

# EVERY pass carries `AND NOT (co.offer_id = ANY(:excluded))` INSIDE ITS OWN
# SQL, before the LIMIT. Filtering the result set in Python instead was a
# measured under-report: `--limit 2` planned `cascade.found: 0` and `--apply`
# then moved 1, because the two rows the LIMIT returned were both already claimed
# by the orphan pass and the Python filter had nothing left to hand back. The SQL
# is where the exclusion has to live, because the LIMIT is applied by Postgres.
#
# Under `--apply` an earlier pass has already set `suppressed_at`, so each pass's
# own `suppressed_at IS NULL` filter would exclude the same rows and the clause
# is a no-op there. It is load-bearing in DRY RUN, which writes nothing — and
# under `--limit`, where the plan and the run must still agree.
#
# THE PREDICATE IS WRITTEN OUT IN EACH STATEMENT rather than interpolated from a
# shared fragment. Composing these with an f-string makes every one of them an
# `ast.JoinedStr`, and tests/test_repo_sql_prepare_postgres.py's module-constant
# scan accepts only `ast.Constant` — the four statements would silently leave the
# repo-wide PREPARE sweep. Four copies of one line is the cheaper failure.

# ── pass (a): orphan offers ──────────────────────────────────────────────────
# LEFT-anti-join by NOT EXISTS on catalog_skus.sku_key, the exact join
# `pivot_query_service` makes. THE ANTI-JOIN ASKS ONLY WHETHER THE ROW EXISTS and
# deliberately does not filter on `s.suppressed_at`: an offer whose SKU is merely
# suppressed is not an orphan, it is a cascade subject, and labelling it
# `orphan_no_sku` would name the wrong defect on a row whose writer did nothing
# wrong. Pinned by a test.
#
# `LIMIT CAST(:limit AS bigint)` with a NULL bind is Postgres for "no limit", so
# one statement serves both --limit N and the unlimited default; a separate
# unlimited copy of each statement is how two spellings of the same predicate
# drift apart.
ORPHAN_SELECT_SQL = """
    SELECT co.offer_id, co.sku_key, co.product_key,
           coalesce(co.source_system, '') AS source_system
      FROM catalog_offers co
     WHERE co.suppressed_at IS NULL
       AND NOT (co.offer_id = ANY(:excluded))
       AND NOT EXISTS (
             SELECT 1 FROM catalog_skus s WHERE s.sku_key = co.sku_key
           )
     ORDER BY co.offer_id
     LIMIT CAST(:limit AS bigint)
"""

# ── pass (b): duplicate (sku_key, channel, market) ───────────────────────────
# The keeper is rn = 1: newest `updated_at` first, then the LOWEST offer_id as
# the tie-break. Only rn > 1 is suppressed, so exactly one live row survives per
# group. Both ORDER BY terms are load-bearing and both are pinned by tests: with
# `updated_at ASC` the reconciler keeps the STALEST price, and with no offer_id
# tie-break two rows sharing a timestamp make the survivor depend on scan order.
#
# NO `NULLS LAST`. `catalog_offers.updated_at` is NOT NULL with a
# `server_default=now()` (db/catalog.py) — there is no null to order, so a
# `NULLS LAST` here would be an ordering term no test could ever pin and no row
# could ever exercise, i.e. a claim about behaviour the data cannot produce.
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
                 ORDER BY co.updated_at DESC, co.offer_id ASC
               )
      ) ranked
     WHERE rn > 1
     ORDER BY sku_key, channel, market, offer_id
     LIMIT CAST(:limit AS bigint)
"""

#: What the pass reports as `live_duplicate_groups_after`. Counts GROUPS with
#: more than one live row — a group of three is ONE contested shelf and two
#: excess rows, and "is this shelf still contested" is the question an operator
#: reads after a run.
#:
#: `:excluded` is how a DRY RUN answers the same question a real run would: the
#: rows the plan would suppress are excluded, so the predicted number is the one
#: --apply would leave. Under --apply it carries only the earlier passes' claims,
#: because this pass's own writes are already in the table by the time it runs.
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
       AND NOT (co.offer_id = ANY(:excluded))
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

#: The DRY-RUN twin of REVERT_BATCH_SQL, and a MODULE-LEVEL CONSTANT rather than
#: a literal inside `revert_batch()`. The repo's PREPARE sweep
#: (tests/test_repo_sql_prepare_postgres.py) follows `database.<accessor>(NAME,
#: ...)` by AST and can only ask Postgres to plan a statement it can name; a
#: statement built in a function body is invisible to it. Its predicate is
#: REVERT_BATCH_SQL's WHERE, verbatim, so the count a plan reports is the count
#: the UPDATE would move.
REVERT_PREVIEW_SQL = """
    SELECT offer_id
      FROM catalog_offers
     WHERE suppression_metadata->>'reconcile_batch_id' = CAST(:batch_id AS text)
       AND suppressed_at IS NOT NULL
       AND suppression_reason = ANY(:reasons)
     ORDER BY offer_id
"""


def _limit_bind(limit: int) -> Optional[int]:
    """`--limit 0` (the default) means every row. Postgres reads `LIMIT NULL` as
    unlimited, so the same statement serves both."""
    return int(limit) if limit and limit > 0 else None


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


async def run_orphan_pass(
    *, apply: bool, limit: int, batch_id: str, claimed: Set[str],
) -> Tuple[Dict[str, Any], List[str]]:
    # `:excluded` goes INTO the statement, so the LIMIT is applied to rows this
    # pass can actually take. Filtering after the LIMIT under-reported: see
    # the banner above ORPHAN_SELECT_SQL.
    rows = [dict(row) for row in (await database.fetch_all(
        ORPHAN_SELECT_SQL,
        {"limit": _limit_bind(limit), "excluded": sorted(claimed)},
    ) or [])]
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
    rows = [dict(row) for row in (await database.fetch_all(
        DUPLICATE_SELECT_SQL,
        {"limit": _limit_bind(limit), "excluded": excluded},
    ) or [])]
    keepers = {str(r["offer_id"]): str(r["keeper_offer_id"]) for r in rows}
    offer_ids = [str(r["offer_id"]) for r in rows]
    groups = {(r["sku_key"], r["channel"], r["market"]) for r in rows}
    moved = await _suppress(
        offer_ids, reason=REASON_DUPLICATE, batch_id=batch_id,
        pass_name="duplicates", keepers=keepers,
    ) if apply else []
    # Re-measured AFTER the writes, not derived from them: this is the number an
    # operator reads to decide whether the sweep is finished — and the one
    # `duplicate_offers_per_sku_channel_market` will be asserting on next — so
    # computing it as `groups_before - groups_touched` would report zero for a run
    # that only suppressed some of each group's excess rows (which --limit can do).
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
    rows = [dict(row) for row in (await database.fetch_all(
        CASCADE_SELECT_SQL,
        {"limit": _limit_bind(limit), "excluded": sorted(claimed)},
    ) or [])]
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


async def revert_batch(batch_id: str, *, apply: bool) -> Dict[str, Any]:
    reasons = [REASON_ORPHAN, REASON_DUPLICATE, REASON_PRODUCT_SUPPRESSED]
    if not apply:
        rows = [dict(row) for row in (await database.fetch_all(
            REVERT_PREVIEW_SQL, {"batch_id": batch_id, "reasons": reasons},
        ) or [])]
        return {"batch_id": batch_id, "would_restore": len(rows),
                "restored": 0, "sample": [str(r["offer_id"]) for r in rows[:5]]}
    rows = [dict(row) for row in (await database.fetch_all(
        REVERT_BATCH_SQL, {"batch_id": batch_id, "reasons": reasons},
    ) or [])]
    return {"batch_id": batch_id, "would_restore": len(rows),
            "restored": len(rows), "sample": [str(r["offer_id"]) for r in rows[:5]]}


async def run(
    *, apply: bool, limit: int, passes: Tuple[str, ...], revert: str = "",
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
    # What earlier passes have taken, handed to every later pass as `:excluded`
    # INSIDE its SQL. Under --apply the rows are already gated and each pass's
    # own filter would exclude them anyway; in a dry run — and under --limit,
    # where the exclusion has to happen before Postgres applies the LIMIT — this
    # is the only thing that keeps the plan equal to the run.
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
    p.add_argument("--revert-batch", default="",
                   help="restore the offers one batch_id suppressed")
    args = p.parse_args(argv)
    if args.limit < 0:
        p.error("--limit must be >= 0")
    if args.revert_batch and args.passes:
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
                revert=args.revert_batch,
            )
        finally:
            await database.disconnect()

    report = asyncio.run(_go())
    print(REPORT_BEGIN + json.dumps(report, sort_keys=True, default=str) + REPORT_END,
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
