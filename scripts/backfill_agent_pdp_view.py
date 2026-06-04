"""Stage 3a-ii backfill — populate agent_pdp_view from existing catalog tables.

Stage 3a-i (migration 085) added the denormalized agent_pdp_view table;
this script is the one-shot backfill that seeds it from
catalog_products × catalog_skus × catalog_offers × product_group_members
× external_product_seeds. Stage 3a-iii adds the writer hook that keeps
it fresh on every seed_data commit; Stage 3a-iv ships the read endpoint.

The pure-Python assembly logic lives in
services/agent_pdp_view_assembler.py — both this script and the Stage
3a-iii writer hook call it. This script owns the content_key window
SELECT and the UPSERT loop; the shared service module owns the per-key
source-row reads.

Grouping model and tiebreak ladder are documented in
services/agent_pdp_view_assembler.py.

Mock/synthetic boundary (memory: feedback_mock_data_never_to_merchant):
the assembler never synthesizes description prose; this script never
backfills rows with fabricated content. Every field originates in a
primary catalog table or external_product_seeds.seed_data (employee-
authored bootstrap data — memory: project_pivota_external_seed_bootstrap).

Usage
-----
Dry-run (default):
  python3 scripts/backfill_agent_pdp_view.py --limit 200

Apply:
  python3 scripts/backfill_agent_pdp_view.py --apply --limit 200 --offset 0

Full backfill in one shot (only on small / staging DBs):
  python3 scripts/backfill_agent_pdp_view.py --apply --limit 0
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
from services.agent_pdp_view_assembler import (  # noqa: E402
    BACKFILL_REFRESH_SOURCE,
    UPSERT_SQL,
    assemble_row,
    fetch_external_seed_for_keys,
    fetch_offers_for_keys,
    fetch_products_for_key,
    fetch_skus_for_keys,
    row_to_upsert_params,
)

logger = logging.getLogger("backfill_agent_pdp_view")


async def _fetch_content_keys(*, limit: int, offset: int) -> List[str]:
    """Stable content_key window. We page by content_key ASC so each
    chunk processes a disjoint slice — no double-writes, safe to resume
    on partial failures.
    """
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    offset_clause = "OFFSET :offset" if offset > 0 else ""
    sql = f"""
        SELECT DISTINCT content_key
        FROM catalog_products
        WHERE content_key IS NOT NULL
        ORDER BY content_key ASC
        {limit_clause}
        {offset_clause}
    """
    params: Dict[str, Any] = {}
    if limit > 0:
        params["limit"] = int(limit)
    if offset > 0:
        params["offset"] = int(offset)
    rows = await database.fetch_all(sql, params)
    return [r["content_key"] for r in rows or []]


async def _fetch_stale_content_keys(*, limit: int = 0) -> List[str]:
    """content_keys that need (re)materialization: either missing from
    agent_pdp_view entirely, or whose catalog_products row changed after
    the last agent_pdp_view refresh. Drives the daily incremental sweep so
    it only touches rows that actually need work (vs the full-table
    _fetch_content_keys window used by the one-shot backfill).
    """
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    sql = f"""
        SELECT DISTINCT cp.content_key
        FROM catalog_products cp
        LEFT JOIN agent_pdp_view apv ON apv.content_key = cp.content_key
        WHERE cp.content_key IS NOT NULL
          AND (
            apv.content_key IS NULL
            OR (
              cp.updated_at IS NOT NULL
              AND (apv.refreshed_at IS NULL OR cp.updated_at > apv.refreshed_at)
            )
          )
        ORDER BY cp.content_key ASC
        {limit_clause}
    """
    params: Dict[str, Any] = {}
    if limit > 0:
        params["limit"] = int(limit)
    rows = await database.fetch_all(sql, params)
    return [r["content_key"] for r in rows or []]


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------

async def _materialize_content_keys(
    content_keys: List[str], *, apply: bool
) -> Dict[str, Any]:
    """Assemble + UPSERT agent_pdp_view rows for the given content_keys.

    Shared by the one-shot backfill (_drive) and the incremental scheduled
    sweep (run_agent_pdp_view_sweep). A per-row sig collision (a different
    content_key already owns this pivota_signature_id) is skipped, never
    fatal, so one duplicate cannot block the rest of the run.
    """
    outcomes: Dict[str, int] = {
        "content_keys_considered": len(content_keys),
        "rows_assembled": 0,
        "rows_skipped_no_title": 0,
        "rows_upserted": 0,
        "rows_skipped_no_op_in_dry_run": 0,
        "rows_skipped_sig_collision": 0,
    }
    samples: List[Dict[str, Any]] = []

    for ck in content_keys:
        products = await fetch_products_for_key(ck)
        if not products:
            continue
        product_keys = [p["product_key"] for p in products]
        skus = await fetch_skus_for_keys(product_keys)
        offers = await fetch_offers_for_keys(product_keys)
        external_seed = await fetch_external_seed_for_keys(product_keys)

        row = assemble_row(
            content_key=ck,
            products=products,
            skus=skus,
            offers=offers,
            external_seed=external_seed,
            refresh_source=BACKFILL_REFRESH_SOURCE,
        )
        if row is None:
            outcomes["rows_skipped_no_title"] += 1
            continue
        outcomes["rows_assembled"] += 1
        if len(samples) < 5:
            samples.append({
                "content_key": ck,
                "title": row["title"],
                "brand": row["brand"],
                "offer_count": row["offer_count"],
                "variants_count": row["variants_count"],
                "primary_merchant_id": row["primary_merchant_id"],
            })

        if not apply:
            outcomes["rows_skipped_no_op_in_dry_run"] += 1
            continue
        try:
            await database.execute(UPSERT_SQL, row_to_upsert_params(row))
            outcomes["rows_upserted"] += 1
        except Exception as exc:  # noqa: BLE001 - skip sig collisions; never abort the whole run
            msg = str(exc).lower()
            if "unique" in msg or "duplicate key" in msg:
                outcomes["rows_skipped_sig_collision"] += 1
                logger.warning("skip sig-collision content_key=%s: %s", ck, exc)
                continue
            raise

    return {"outcome_counts": outcomes, "samples": samples}


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    content_keys = await _fetch_content_keys(limit=args.limit, offset=args.offset)
    logger.info(
        "loaded %d content_keys (limit=%d offset=%d)",
        len(content_keys), args.limit, args.offset,
    )
    return await _materialize_content_keys(content_keys, apply=args.apply)


async def run_agent_pdp_view_sweep(*, limit: int = 0) -> Dict[str, Any]:
    """Scheduler entry point (registered on a 30-minute interval in
    services/audit_scheduler.py).

    Incrementally materializes agent_pdp_view for content_keys that are
    missing or stale, so newly-crawled products — mirrored into
    catalog_products by the 15-min external_seed_catalog_materialization
    tick, which does NOT write agent_pdp_view — enter the serving
    projection within ~30 min. The Stage 3a-iii inline writer keeps apv
    fresh on seed-edit/authoring paths; this sweep covers the bulk
    materialization path the inline writer misses (the 2026-05/06 incident,
    where ~1,800 products fell out of serving because the manual apv
    backfill stopped on 2026-05-21).

    Never raises — errors are caught and returned in the summary so a
    failure surfaces in logs rather than killing the scheduler.
    """
    summary: Dict[str, Any] = {
        "job": "agent_pdp_view_sweep",
        "content_keys_considered": 0,
        "rows_assembled": 0,
        "rows_upserted": 0,
        "rows_skipped_sig_collision": 0,
        "error": None,
    }
    try:
        if not getattr(database, "is_connected", False):
            await database.connect()
        content_keys = await _fetch_stale_content_keys(limit=limit)
        logger.info(
            "agent_pdp_view_sweep: %d stale/missing content_keys",
            len(content_keys),
        )
        result = await _materialize_content_keys(content_keys, apply=True)
        summary.update(result["outcome_counts"])
        logger.info("agent_pdp_view_sweep done: %s", summary)
    except Exception as exc:  # noqa: BLE001 - scheduler entry point must never raise
        logger.error("agent_pdp_view_sweep failed: %s", exc, exc_info=True)
        summary["error"] = repr(exc)
    return summary


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually UPSERT agent_pdp_view rows. Default: dry-run.",
    )
    p.add_argument(
        "--limit", type=int, default=200,
        help="Max content_keys to process this run (0 = all). Default 200.",
    )
    p.add_argument(
        "--offset", type=int, default=0,
        help="Skip the first N content_keys. Use to paginate across chunks.",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
