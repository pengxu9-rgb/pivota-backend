"""
Product Enrichment Worker

CLI job to backfill or refresh product_enrichment + quality snapshots
for products already present in products_cache.

Typical usage (from repo root):

    cd pivota-backend
    python -m jobs.product_enrichment_worker --merchant merch_xxx --platform wix

This will:
- Connect to the database
- Load all products_cache rows for the merchant+platform
- For each row, run the enrichment pipeline
"""

from __future__ import annotations

import asyncio
import argparse
import logging
from typing import Optional

from db.database import database
from db.products import get_cached_products
from services.product_enrichment_pipeline import run_enrichment_for_product


logger = logging.getLogger(__name__)


async def _process_merchant(
  merchant_id: str,
  platform: str,
  limit: Optional[int] = None,
) -> None:
  rows = await get_cached_products(
    merchant_id=merchant_id,
    platform=platform,
    include_expired=False,
  )

  if limit is not None:
    rows = rows[:limit]

  logger.info(
    "Starting enrichment for merchant=%s platform=%s, total products=%d",
    merchant_id,
    platform,
    len(rows),
  )

  for idx, row in enumerate(rows, start=1):
    platform_product_id = row.get("platform_product_id")
    if not platform_product_id:
      continue
    try:
      logger.info(
        "[%d/%d] Enriching %s/%s/%s",
        idx,
        len(rows),
        merchant_id,
        platform,
        platform_product_id,
      )
      await run_enrichment_for_product(
        merchant_id=merchant_id,
        platform=platform,
        platform_product_id=platform_product_id,
        geo_code="default",
      )
    except Exception as exc:
      logger.exception(
        "Failed to enrich %s/%s/%s: %s",
        merchant_id,
        platform,
        platform_product_id,
        exc,
      )

  logger.info("Enrichment worker finished for merchant=%s platform=%s", merchant_id, platform)


async def _main_async(args: argparse.Namespace) -> None:
  await database.connect()
  try:
    await _process_merchant(
      merchant_id=args.merchant,
      platform=args.platform,
      limit=args.limit,
    )
  finally:
    await database.disconnect()


def main() -> None:
  parser = argparse.ArgumentParser(description="Product Enrichment Worker")
  parser.add_argument(
    "--merchant",
    required=True,
    help="Merchant ID (e.g. merch_efbc46b4619cfbdf)",
  )
  parser.add_argument(
    "--platform",
    required=True,
    help="Platform (e.g. shopify, wix)",
  )
  parser.add_argument(
    "--limit",
    type=int,
    default=None,
    help="Optional limit on number of products to process",
  )
  args = parser.parse_args()

  logging.basicConfig(level=logging.INFO)
  asyncio.run(_main_async(args))


if __name__ == "__main__":
  main()

