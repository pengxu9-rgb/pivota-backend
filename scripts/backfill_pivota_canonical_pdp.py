#!/usr/bin/env python3
"""
Backfill catalog_products.pivota_signature_id + pivota_canonical_url
for rows that predate Phase C-1 (PR #327).

Phase C-1 wired sig minting into ingest_product_payloads + lazy-mint
at audit time, but rows synced before #327 still have NULL sigs. This
script populates them in one pass so:
  - the canonical resolver (/api/canonical/products/{sig}) returns
    the merchant product instead of 404
  - sitemap-products.xml includes them and Google can begin indexing

Idempotent — only touches rows where pivota_signature_id IS NULL.
Defaults to dry-run; pass --apply to actually persist.

Usage (production is Cloud Run, pivota-prod/us-west1, since the 2026-08-22
cutover; Railway is RETIRED (#1872), so a `railway run` backfill would write to a
database nobody is served from). There is no `railway run` equivalent — use a
throwaway job on the production image; the helper mounts the DATABASE_URL secret
(a job inherits NO env and NO secrets) and takes its verdict from the exit code:

  # Dry run, all merchants:
  scripts/ops/run_oneoff_job.sh scripts/backfill_pivota_canonical_pdp.py

  # Apply, with explicit chunk size + filter:
  scripts/ops/run_oneoff_job.sh scripts/backfill_pivota_canonical_pdp.py \\
    --apply --merchant-id merch_38fa56d5118b9974 --chunk-size 500

Full pattern and its footguns: docs/runbooks/operating_on_gcp_production.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.catalog_sync_service import make_pivota_canonical_fields


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill Pivota canonical PDP signature + URL for "
            "catalog_products rows missing them (idempotent)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually UPDATE rows. Default is dry-run (preview only).",
    )
    parser.add_argument(
        "--merchant-id",
        help="Limit backfill to a single merchant_id.",
    )
    parser.add_argument(
        "--platform",
        help="Limit backfill to a single platform (e.g. shopify, wix).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="Rows per fetch + UPDATE batch (default 500).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Stop after backfilling this many rows total (0 = unlimited).",
    )
    return parser.parse_args()


def _build_where_clause(args: argparse.Namespace) -> Tuple[str, Dict[str, Any]]:
    conditions: List[str] = ["pivota_signature_id IS NULL"]
    values: Dict[str, Any] = {}
    if args.merchant_id:
        conditions.append("merchant_id = :merchant_id")
        values["merchant_id"] = args.merchant_id
    if args.platform:
        conditions.append("platform = :platform")
        values["platform"] = args.platform
    return f"WHERE {' AND '.join(conditions)}", values


async def _fetch_chunk(
    where_clause: str,
    values: Dict[str, Any],
    chunk_size: int,
) -> List[Dict[str, Any]]:
    """Pull the next batch of rows missing a sig. Order by product_key
    so successive iterations don't repeat work between chunks."""
    rows = await database.fetch_all(
        f"""
        SELECT product_key, merchant_id, platform, source_product_id
        FROM catalog_products
        {where_clause}
        ORDER BY product_key ASC
        LIMIT :chunk_size
        """,
        {**values, "chunk_size": chunk_size},
    )
    return [dict(r) for r in rows]


async def _persist_chunk(rows_with_sigs: List[Dict[str, Any]]) -> int:
    """UPDATE rows in a single transaction. Returns count actually
    written. Uses parameterized statement-per-row for portability —
    chunks are small enough that the overhead is fine."""
    if not rows_with_sigs:
        return 0

    written = 0
    async with database.transaction():
        for row in rows_with_sigs:
            await database.execute(
                """
                UPDATE catalog_products
                SET pivota_signature_id = :sig,
                    pivota_canonical_url = :url
                WHERE product_key = :product_key
                  AND pivota_signature_id IS NULL
                """,
                {
                    "sig": row["pivota_signature_id"],
                    "url": row["pivota_canonical_url"],
                    "product_key": row["product_key"],
                },
            )
            written += 1
    return written


async def _run(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        where_clause, values = _build_where_clause(args)

        # First, total candidate count for an honest progress report.
        total_row = await database.fetch_one(
            f"SELECT COUNT(*) AS n FROM catalog_products {where_clause}",
            values,
        )
        total_candidates = int(total_row["n"]) if total_row else 0
        print(
            f"[backfill] candidates with NULL sig: {total_candidates} "
            f"(filter: merchant_id={args.merchant_id or '*'}, "
            f"platform={args.platform or '*'})"
        )
        if total_candidates == 0:
            print("[backfill] nothing to do.")
            return 0

        if not args.apply:
            print("[backfill] DRY RUN — pass --apply to persist.")

        scanned = 0
        written = 0
        skipped_invalid = 0
        max_rows = args.max_rows or total_candidates

        # Loop until either: we hit max_rows, or there are no more
        # NULL-sig rows to fetch. Because UPDATE flips the NULL → sig,
        # successive chunks naturally drain without us tracking offset.
        while scanned < max_rows:
            remaining = max_rows - scanned
            this_chunk = min(args.chunk_size, remaining)
            batch = await _fetch_chunk(where_clause, values, this_chunk)
            if not batch:
                break

            shaped: List[Dict[str, Any]] = []
            for row in batch:
                merchant_id = (row.get("merchant_id") or "").strip()
                platform = (row.get("platform") or "").strip()
                source_product_id = (row.get("source_product_id") or "").strip()
                if not (merchant_id and platform and source_product_id):
                    skipped_invalid += 1
                    continue
                fields = make_pivota_canonical_fields(
                    merchant_id, platform, source_product_id
                )
                shaped.append(
                    {
                        "product_key": row["product_key"],
                        "pivota_signature_id": fields["pivota_signature_id"],
                        "pivota_canonical_url": fields["pivota_canonical_url"],
                    }
                )

            scanned += len(batch)

            if args.apply:
                wrote = await _persist_chunk(shaped)
                written += wrote
                print(
                    f"[backfill] chunk: scanned={len(batch)} "
                    f"wrote={wrote} cumulative_wrote={written}"
                )
            else:
                # Dry-run: emit the first 3 of the chunk so the operator
                # can sanity-check the sig shape before running --apply.
                preview = shaped[:3]
                for p in preview:
                    print(
                        f"[backfill][preview] {p['product_key']} → "
                        f"{p['pivota_signature_id']}"
                    )
                # In dry-run we'd loop forever (NULL rows never drain),
                # so break after the first chunk.
                break

        print(
            f"[backfill] done. scanned={scanned} wrote={written} "
            f"skipped_invalid_identity={skipped_invalid} "
            f"applied={args.apply}"
        )
        return 0
    finally:
        await database.disconnect()


def main() -> None:
    args = _parse_args()
    code = asyncio.run(_run(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
