#!/usr/bin/env python3
"""Attach INCI to enrichment-minted brand-official canonicals (Path-C cohort).

The mint lane (source_system='catalog_enrichment_agent_v1') never crawls INCI, so
every minted canonical lands with zero beauty_sku_ingredients / beauty enrichment —
no verified actives, no ingredient evidence, no substantiated "Contains {X}" claims.
This is the SAME gap the external_seed INCI crawl already solved; the existing
resolver + ingest are reused verbatim. Only the POPULATION differs: this selects the
minted cohort by its own canonical_url instead of the external_seed junk tail.

Per row: resolve_inci_from_urls([canonical_url]) (PDP text + Shopify-JSON fallback),
then ingest_crawled_inci_items(source_system='pdp_crawl') — which UPSERTs raw_inci
under ADR-001 source precedence (never downgrades a higher-authority INCI),
looks up the AUTHORITATIVE seller-of-record from catalog_products (no key-parsing),
runs enrich_and_persist_product -> actives + concerns + claims, and the enrich path
auto-refreshes agent_pdp_view. Idempotent + resumable: rows that already carry
raw_inci are excluded by the population query.

Dry-run (default): resolve-only, report yield, write nothing.
    DATABASE_URL=... python3 scripts/attach_inci_minted_canonicals.py --limit 40
Apply:
    DATABASE_URL=... python3 scripts/attach_inci_minted_canonicals.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.canonical_inci_resolver import http_fetch, resolve_inci_from_urls  # noqa: E402
from services.crawled_inci_ingest import ingest_crawled_inci_items  # noqa: E402

# Population: live minted canonicals with a canonical_url and NO clean INCI yet
# (resumable). sku_key prefers the row's ::canonical catalog_sku; when the mint
# lane never wrote a catalog_sku (the broken sub-batch), synthesize the canonical
# key from product_key so the beauty_sku_ingredients PK is still well-formed.
_SELECT = """
    SELECT product_key, sku_key, canonical_url FROM (
      SELECT DISTINCT ON (cp.product_key)
             cp.product_key,
             coalesce(s.sku_key, cp.product_key || '::canonical') AS sku_key,
             cp.canonical_url
      FROM catalog_products cp
      LEFT JOIN catalog_skus s ON s.product_key = cp.product_key
      LEFT JOIN beauty_sku_ingredients bsi
        ON bsi.product_key = cp.product_key AND coalesce(bsi.raw_inci,'') <> ''
      WHERE cp.source_system = 'catalog_enrichment_agent_v1'
        AND cp.sync_status = 'live'
        AND cp.canonical_url IS NOT NULL AND cp.canonical_url <> ''
        AND bsi.product_key IS NULL
      ORDER BY cp.product_key, (s.sku_key LIKE '%::canonical') DESC
    ) t
    ORDER BY product_key
    LIMIT :limit
"""

RESOLVE_CONCURRENCY = 6


async def _connect_with_retry(tries: int = 6) -> None:
    for i in range(tries):
        try:
            await database.connect()
            return
        except Exception as e:  # noqa: BLE001
            print(f"  (db connect attempt {i+1} failed: {type(e).__name__} — retry 20s)", flush=True)
            await asyncio.sleep(20)
    raise SystemExit("could not connect to DB")


async def _resolve_one(row: Dict[str, Any], sem: asyncio.Semaphore) -> Optional[Dict[str, Any]]:
    async with sem:
        try:
            res = await resolve_inci_from_urls([row["canonical_url"]], fetch=http_fetch)
        except Exception:  # noqa: BLE001
            res = None
    if res and getattr(res, "raw_inci", None):
        return {"product_key": row["product_key"], "sku_key": row["sku_key"], "raw_inci": res.raw_inci}
    return None


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await _connect_with_retry()
    try:
        rows = [dict(r) for r in await database.fetch_all(_SELECT, {"limit": args.limit})]
        print(f"[population] minted canonicals without INCI to crawl: {len(rows)} (apply={args.apply})", flush=True)
        if not rows:
            return 0

        sem = asyncio.Semaphore(RESOLVE_CONCURRENCY)
        items: List[Dict[str, Any]] = []
        misses = 0
        done = 0
        for chunk_start in range(0, len(rows), 50):
            chunk = rows[chunk_start:chunk_start + 50]
            results = await asyncio.gather(*[_resolve_one(r, sem) for r in chunk])
            for res in results:
                if res:
                    items.append(res)
                else:
                    misses += 1
            done += len(chunk)
            print(f"  [resolve] {done}/{len(rows)}  hits={len(items)} misses={misses}", flush=True)

        print(f"[resolved] clean INCI: {len(items)} | misses: {misses}", flush=True)
        if not items:
            return 0

        if not args.apply:
            print(f"(DRY-RUN — would ingest {len(items)} resolved INCI lists)", flush=True)
            return 0

        # Ingest in DB-sized batches; ingest_crawled_inci_items reuses the live
        # connection and enriches + refreshes agent_pdp_view per item.
        agg = {"n": 0, "inci_written": 0, "actives_filled": 0, "claims_written": 0,
               "skipped": 0, "skipped_outranked": 0, "skipped_unresolved_seller": 0}
        abandoned = 0
        for i in range(0, len(items), 40):
            batch = items[i:i + 40]
            for attempt in range(3):
                try:
                    rep = await ingest_crawled_inci_items(batch, source_system="pdp_crawl", db=database)
                    for k in agg:
                        agg[k] += rep.get(k, 0)
                    print(f"  [ingest] {min(i+40, len(items))}/{len(items)}  "
                          f"inci={agg['inci_written']} actives={agg['actives_filled']} "
                          f"claims={agg['claims_written']} outranked={agg['skipped_outranked']} "
                          f"no_seller={agg['skipped_unresolved_seller']}", flush=True)
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  [ingest] batch {i} attempt {attempt+1} failed: {type(e).__name__} — reconnect", flush=True)
                    try:
                        await database.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.sleep(15)
                    await _connect_with_retry()
            else:
                abandoned += 1
                print(f"  [ingest] batch {i} ABANDONED after 3 attempts ({len(batch)} items skipped)", flush=True)
        print(f"[done] {agg} | abandoned_batches: {abandoned}", flush=True)
        return 1 if abandoned else 0
    finally:
        if own and bool(getattr(database, "is_connected", False)):
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Attach INCI to enrichment-minted canonicals.")
    p.add_argument("--limit", type=int, default=2000)
    p.add_argument("--apply", action="store_true", help="ingest + enrich + serve (else resolve-only dry-run)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
