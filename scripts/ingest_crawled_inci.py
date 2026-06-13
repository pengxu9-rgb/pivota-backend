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
from services.beauty_enrichment_persist import enrich_and_persist_product

_UPSERT = """
    INSERT INTO beauty_sku_ingredients
        (sku_key, product_key, merchant_id, raw_inci, source_system, created_at, updated_at)
    VALUES (:sk, :pk, :mid, :inci, 'pdp_crawl', NOW(), NOW())
    ON CONFLICT (sku_key) DO UPDATE SET
        raw_inci = EXCLUDED.raw_inci,
        source_system = EXCLUDED.source_system,
        updated_at = NOW()
    WHERE beauty_sku_ingredients.raw_inci IS NULL
       OR length(trim(beauty_sku_ingredients.raw_inci)) = 0
"""


def _merchant_id(product_key: str) -> str:
    parts = product_key.split("::")
    return parts[1] if len(parts) > 1 and parts[1] else "external_seed"


async def _drive(items: List[Dict[str, Any]], *, dry_run: bool, db: Any = database) -> Dict[str, Any]:
    was_connected = bool(getattr(db, "is_connected", False))
    if not was_connected:
        await db.connect()
    report = {"n": 0, "inci_written": 0, "actives_filled": 0, "claims_written": 0, "skipped": 0, "items": []}
    try:
        for it in items:
            pk = str(it.get("product_key") or "").strip()
            sk = str(it.get("sku_key") or "").strip()
            inci = str(it.get("raw_inci") or "").strip()
            report["n"] += 1
            if not (pk and sk and inci) or inci.upper().startswith("NO INGREDIENT"):
                report["skipped"] += 1
                report["items"].append({"product_key": pk, "status": "skipped_no_inci"})
                continue
            if not dry_run:
                await db.execute(_UPSERT, {"sk": sk, "pk": pk, "mid": _merchant_id(pk), "inci": inci})
            res = await enrich_and_persist_product(pk, db=db, dry_run=dry_run)
            wrote = res.get("written", {})
            if not dry_run:
                report["inci_written"] += 1
            if wrote.get("actives_skus"):
                report["actives_filled"] += 1
            if wrote.get("evidence_claims"):
                report["claims_written"] += 1
            report["items"].append({
                "product_key": pk[-26:],
                "active_source": res.get("derived", {}).get("active_source"),
                "claims": res.get("derived", {}).get("substantiated_claims"),
            })
    finally:
        if not was_connected and bool(getattr(db, "is_connected", False)):
            await db.disconnect()
    return report


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
