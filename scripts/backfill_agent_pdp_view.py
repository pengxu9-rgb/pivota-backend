"""Stage 3a-ii backfill — populate agent_pdp_view from existing catalog tables.

Stage 3a-i (migration 085) added the denormalized agent_pdp_view table;
this script is the one-shot backfill that seeds it from
catalog_products × catalog_skus × catalog_offers × product_group_members
× external_product_seeds. Stage 3a-iii adds the writer hook that keeps
it fresh on every seed_data commit; Stage 3a-iv ships the read endpoint.

The pure-Python assembly logic lives in
services/agent_pdp_view_assembler.py — both this script and the Stage
3a-iii writer hook call it. This script owns: the content_key window
SELECT, the per-key fetches, and the UPSERT loop.

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
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.agent_pdp_view_assembler import (  # noqa: E402
    BACKFILL_REFRESH_SOURCE,
    UPSERT_SQL,
    assemble_row,
    row_to_upsert_params,
)

logger = logging.getLogger("backfill_agent_pdp_view")


# ---------------------------------------------------------------------
# DB fetch helpers
# ---------------------------------------------------------------------

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


async def _fetch_products_for_key(content_key: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT
          cp.product_key,
          cp.merchant_id,
          cp.platform,
          cp.source_product_id,
          cp.title,
          cp.description,
          cp.brand,
          cp.product_type,
          cp.category,
          cp.image_url,
          cp.product_payload,
          cp.tags,
          cp.price_tier,
          cp.use_case_tags,
          cp.lifestyle_tags,
          cp.demographic,
          cp.pdp_lifecycle_stage,
          cp.pivota_signature_id,
          cp.canonical_url,
          cp.sync_status,
          cp.created_at,
          pgm.product_group_id,
          pgm.is_primary AS group_is_primary
        FROM catalog_products cp
        LEFT JOIN product_group_members pgm
          ON pgm.merchant_id = cp.merchant_id
         AND pgm.platform = cp.platform
         AND pgm.platform_product_id = cp.source_product_id
        WHERE cp.content_key = :ck
        """,
        {"ck": content_key},
    )
    return [dict(r) for r in rows or []]


async def _fetch_skus_for_keys(product_keys: List[str]) -> List[Dict[str, Any]]:
    if not product_keys:
        return []
    rows = await database.fetch_all(
        """
        SELECT
          sku_key, product_key, merchant_id, source_variant_id, source_product_id,
          sku, barcode, title, currency, image_url, visible_attributes,
          visible_option_labels
        FROM catalog_skus
        WHERE product_key = ANY(:keys)
        """,
        {"keys": product_keys},
    )
    return [dict(r) for r in rows or []]


async def _fetch_offers_for_keys(product_keys: List[str]) -> List[Dict[str, Any]]:
    if not product_keys:
        return []
    rows = await database.fetch_all(
        """
        SELECT
          o.offer_id, o.sku_key, o.product_key, o.merchant_id,
          o.availability, o.currency, o.list_price,
          o.merchant_effective_price, o.estimated_best_price,
          m.merchant_name
        FROM catalog_offers o
        LEFT JOIN catalog_merchants m ON m.merchant_id = o.merchant_id
        WHERE o.product_key = ANY(:keys)
        """,
        {"keys": product_keys},
    )
    return [dict(r) for r in rows or []]


async def _fetch_external_seed_for_keys(product_keys: List[str]) -> Optional[Dict[str, Any]]:
    """First active external_product_seed attached to any of these
    product_keys. We only need it as a content fallback, so any one row
    is fine.
    """
    if not product_keys:
        return None
    row = await database.fetch_one(
        """
        SELECT id, attached_product_key, title, image_url, seed_data,
               canonical_url, destination_url
        FROM external_product_seeds
        WHERE attached_product_key = ANY(:keys)
          AND status = 'active'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"keys": product_keys},
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------

async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    content_keys = await _fetch_content_keys(limit=args.limit, offset=args.offset)
    logger.info(
        "loaded %d content_keys (limit=%d offset=%d)",
        len(content_keys), args.limit, args.offset,
    )

    outcomes: Dict[str, int] = {
        "content_keys_considered": len(content_keys),
        "rows_assembled": 0,
        "rows_skipped_no_title": 0,
        "rows_upserted": 0,
        "rows_skipped_no_op_in_dry_run": 0,
    }
    samples: List[Dict[str, Any]] = []

    for ck in content_keys:
        products = await _fetch_products_for_key(ck)
        if not products:
            continue
        product_keys = [p["product_key"] for p in products]
        skus = await _fetch_skus_for_keys(product_keys)
        offers = await _fetch_offers_for_keys(product_keys)
        external_seed = await _fetch_external_seed_for_keys(product_keys)

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

        if not args.apply:
            outcomes["rows_skipped_no_op_in_dry_run"] += 1
            continue
        await database.execute(UPSERT_SQL, row_to_upsert_params(row))
        outcomes["rows_upserted"] += 1

    return {"outcome_counts": outcomes, "samples": samples}


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
