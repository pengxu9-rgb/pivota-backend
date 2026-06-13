"""Batch-ingest crawled INCI into the beauty_* tables, then enrich + persist.

The scaled INCI crawl (services/scripts: Claude crawls each PDP with WebFetch and
extracts the published ingredient list) feeds this writer. Per (product_key,
sku_key, raw_inci):
  1. UPSERT raw_inci into beauty_sku_ingredients (source_system='pdp_crawl', PK
     sku_key), fill-only-when-empty so it is idempotent + re-runnable and never
     clobbers a merchant/verified row.
  2. enrich_and_persist_product -> INCI-verified actives (source="inci"),
     text-derived concerns, and substantiated "Contains {X}" claims (justify).

Reads a JSON array on stdin (or --file): [{"product_key","sku_key","raw_inci"}, ...].
merchant_id is taken from the product_key's 2nd segment (external_seed for seeds).
Prints a per-item + aggregate summary. --dry-run writes nothing.

Usage:
  DATABASE_URL=... python -m scripts.ingest_crawled_inci --file batch.json
  echo '[{...}]' | DATABASE_URL=... python -m scripts.ingest_crawled_inci
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List

from db.database import database
from services.crawled_inci_ingest import (  # noqa: F401 -- _merchant_id re-exported for callers/tests
    ingest_crawled_inci_items,
    merchant_id_from_product_key as _merchant_id,
)


async def _drive(items: List[Dict[str, Any]], *, dry_run: bool, db: Any = database) -> Dict[str, Any]:
    """Thin CLI wrapper over the shared services.crawled_inci_ingest implementation."""
    return await ingest_crawled_inci_items(items, dry_run=dry_run, db=db)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="JSON file of [{product_key,sku_key,raw_inci}]")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    raw = open(args.file).read() if args.file else sys.stdin.read()
    items = json.loads(raw)
    report = asyncio.run(_drive(items, dry_run=args.dry_run))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
