"""Backfill catalog_products.gtin from catalog_skus.barcode (ADR-011 step 4).

Migration 178 added catalog_products.gtin (the GS1 GTIN-14 match-attribute the
resolve-or-attach primitive keys on — services/intake_identity.py). Intake doors
populate it going forward; this fills the legacy rows so the Tier-0 GTIN matcher
works against the EXISTING catalog, not just post-deploy rows.

Why it matters (the SPU convergence): legacy first-party rows were minted with
GTIN FOLDED INTO content_key (catalog_sync_service made
make_content_key(brand, title, barcode)), while new rows key on brand+title only.
Those two forms don't share a content_key — the GTIN attribute is what bridges
them, so a legacy row and a fresh observation of the same product converge
instead of fragmenting. That bridge only exists once legacy rows carry the
attribute; this script is that step.

What it does:
  1. Window catalog_products by product_key (stable, offset-paginated).
  2. For each row, gather its catalog_skus barcodes and pick the canonical
     GTIN-14 the SAME way the serving view does — services.agent_pdp_view_
     assembler.pick_gtin13 (modal value, lex tiebreak, malformed 15+ dropped).
     Reusing it guarantees catalog_products.gtin agrees with agent_pdp_view.gtin13.
  3. UPDATE catalog_products.gtin, guarded by `gtin IS NULL` — never overwrites a
     value a door already wrote, and re-runnable.

What it does NOT do:
  - Touch content_key, product_group_id, or any identity/serving field. This is
    purely populating the match attribute. It performs NO merges — surfacing the
    GTIN just lets the (separately enabled, flag-gated) intake primitive converge
    future observations. Retro-merging existing rows is the ADR-010 resolver /
    D-2 backlog, a separate reviewed step.
  - Invent GTINs. A product with no SKU barcode (store-less, thin crawl rows)
    stays gtin-NULL — honest absence, exactly like content_key on a no-identity
    row.

Usage (mirrors scripts/backfill_content_key.py):
  Dry-run (default; per-batch counts + first samples):
    python3 scripts/backfill_catalog_products_gtin.py --limit 200
  Apply one chunk:
    python3 scripts/backfill_catalog_products_gtin.py --apply --limit 200 --offset 0
  Full backfill (small DBs / staging only):
    python3 scripts/backfill_catalog_products_gtin.py --apply --limit 0

--limit 0 = no limit. Ordering is product_key ASC (write-stable — UPDATE doesn't
change product_key). The window is NOT filtered on gtin IS NULL so offset
pagination stays stable across --apply chunks; the UPDATE stays guarded by
gtin IS NULL.
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
from services.agent_pdp_view_assembler import pick_gtin13  # noqa: E402

logger = logging.getLogger(__name__)


def _select_sql(*, limit: int, offset: int) -> str:
    """Stable product_key-ordered window over catalog_products, with each row's
    catalog_skus barcodes gathered into an array (barcode is the only GTIN
    source; many legacy rows have no SKU row and get an empty array → stay NULL).

    Do NOT filter gtin IS NULL here — filtering NULL rows makes offset chunks
    skip legacy rows once earlier chunks have populated some (same reasoning as
    backfill_content_key). The UPDATE below stays guarded by gtin IS NULL."""
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    offset_clause = "OFFSET :offset" if offset > 0 else ""
    return f"""
        SELECT
          cp.product_key,
          cp.gtin,
          COALESCE(
            (SELECT array_agg(s.barcode)
               FROM catalog_skus s
              WHERE s.product_key = cp.product_key
                AND s.barcode IS NOT NULL),
            ARRAY[]::varchar[]
          ) AS barcodes
        FROM catalog_products cp
        ORDER BY cp.product_key ASC
        {limit_clause}
        {offset_clause}
    """


UPDATE_SQL = """
    UPDATE catalog_products
    SET gtin = :gtin,
        updated_at = NOW()
    WHERE product_key = :product_key
      AND gtin IS NULL
"""


async def _fetch_candidates(args: argparse.Namespace) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {}
    if args.limit > 0:
        params["limit"] = int(args.limit)
    if args.offset > 0:
        params["offset"] = int(args.offset)
    rows = await database.fetch_all(
        _select_sql(limit=args.limit, offset=args.offset), params
    )
    return [dict(r) for r in rows or []]


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    rows = await _fetch_candidates(args)
    logger.info(
        "loaded %d candidate rows (limit=%d offset=%d)",
        len(rows), args.limit, args.offset,
    )

    outcomes: Dict[str, int] = {
        "considered": len(rows),
        "gtin_computed": 0,
        "no_barcode_or_malformed": 0,
        "skipped_already_gtin": 0,
        "updated": 0,
        "skipped_no_op_in_dry_run": 0,
    }
    samples: List[Dict[str, Any]] = []

    for row in rows:
        if row.get("gtin"):
            outcomes["skipped_already_gtin"] += 1
            continue
        barcodes = row.get("barcodes") or []
        # Reuse the serving view's picker so catalog_products.gtin and
        # agent_pdp_view.gtin13 converge on the SAME GTIN-14 (modal, lex
        # tiebreak, malformed dropped).
        gtin = pick_gtin13([{"barcode": b} for b in barcodes])
        if not gtin:
            outcomes["no_barcode_or_malformed"] += 1
            continue
        outcomes["gtin_computed"] += 1
        if len(samples) < 5:
            samples.append({
                "product_key": row["product_key"],
                "barcodes": list(barcodes),
                "gtin": gtin,
            })
        if not args.apply:
            outcomes["skipped_no_op_in_dry_run"] += 1
            continue
        rc = await database.execute(
            UPDATE_SQL, {"product_key": row["product_key"], "gtin": gtin}
        )
        if rc is None or rc:
            outcomes["updated"] += 1

    return {"outcome_counts": outcomes, "samples": samples}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually UPDATE catalog_products.gtin. Default: dry-run.",
    )
    p.add_argument(
        "--limit", type=int, default=200,
        help="Max candidate rows to process this run (0 = all). Default 200.",
    )
    p.add_argument(
        "--offset", type=int, default=0,
        help="Skip the first N candidate rows. Use to paginate across chunks.",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
