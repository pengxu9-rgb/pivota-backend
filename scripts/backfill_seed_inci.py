"""Backfill: feed already-crawled INCI (sitting in external_product_seeds.seed_data)
into the evidence pipeline, so the trust layer reflects INCI we ALREADY have.

The crawl captured INCI into seed_data.inci_list / pdp_ingredients_raw for ~3,300
serving products, but only ~135 were ever ingested into beauty_sku_ingredients.
This feeds the rest through the SAME pipeline (ingest_crawled_inci_items ->
enrich_and_persist_product -> INCI-substantiated claims), then re-materializes
agent_pdp_view so the evidence reaches the served PDP (enrich does not auto-refresh it).

Idempotent: skips product_keys that already carry raw_inci; ADR-001 may_write guards
against downgrading a higher-authority INCI source. Chunk with --limit for the large run.

Usage:
  python3 scripts/backfill_seed_inci.py                 # dry-run (extraction preview)
  python3 scripts/backfill_seed_inci.py --apply --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key  # noqa: E402
from services.crawled_inci_ingest import ingest_crawled_inci_items  # noqa: E402

REFRESH_SOURCE = "seed_inci_backfill"
MIN_INCI_LEN = 20
# A real INCI list is a comma-separated enumeration of many ingredients. Marketing
# "key ingredients" bullets and cross-references ("see X for ingredients") are not
# — gate them out so junk never lands as raw_inci (which ADR-001 would then let
# block a future clean list).
MIN_INCI_COMMAS = 4

# seed_data text often carries marketing preamble ("Key Ingredients ... The wax
# formula glides ...") before the actual list, labelled "Ingredients:".
_INGREDIENTS_MARKER = re.compile(r"ingredients?\s*[:\-]", re.IGNORECASE)


def extract_inci(raw: object) -> str:
    """Pull the INCI list out of seed_data text that may carry marketing preamble.

    Splits on the LAST 'Ingredients:'/'Ingredient -' marker (so a 'Key Ingredients'
    prose block is dropped and the real list kept); if no marker, uses the text
    as-is (many inci_list fields are already clean). Collapses whitespace.
    """
    if not raw:
        return ""
    text = str(raw)
    matches = list(_INGREDIENTS_MARKER.finditer(text))
    if matches:
        text = text[matches[-1].end():]
    return re.sub(r"\s+", " ", text).strip()


def looks_like_inci(text: str) -> bool:
    """A comma-dense enumeration — filters marketing bullets / cross-references."""
    return len(text) >= MIN_INCI_LEN and text.count(",") >= MIN_INCI_COMMAS


async def _fetch(limit: int):
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    sql = f"""
        SELECT s.attached_product_key AS product_key, cp.content_key,
               COALESCE(NULLIF(s.seed_data->>'inci_list',''), s.seed_data->>'pdp_ingredients_raw') AS raw_inci
        FROM external_product_seeds s
        JOIN catalog_products cp ON cp.product_key = s.attached_product_key
        JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
        WHERE s.status = 'active' AND ips.serving_eligible IS TRUE
          AND length(COALESCE(s.seed_data->>'inci_list', s.seed_data->>'pdp_ingredients_raw', '')) > 30
          AND s.attached_product_key NOT IN (
              SELECT product_key FROM beauty_sku_ingredients WHERE raw_inci <> ''
          )
        ORDER BY s.attached_product_key
        {limit_clause}
    """
    return await database.fetch_all(sql)


async def _drive(args: argparse.Namespace) -> None:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        rows = [dict(r) for r in await _fetch(args.limit)]
        items = []
        content_keys = []
        thin = 0
        for r in rows:
            inci = extract_inci(r["raw_inci"])
            if not looks_like_inci(inci):
                thin += 1
                continue
            pk = r["product_key"]
            items.append({"product_key": pk, "sku_key": f"{pk}::canonical", "raw_inci": inci})
            content_keys.append(r["content_key"])

        print(f"{'APPLY' if args.apply else 'DRY'} :: candidates={len(rows)} feedable={len(items)} skipped_thin={thin}")
        if not args.apply:
            for it in items[:6]:
                print(f"  {it['product_key'][-26:]}: {it['raw_inci'][:100]}")
            return

        report = await ingest_crawled_inci_items(items, dry_run=False, db=database)
        print(
            f"ingest: inci_written={report['inci_written']} actives={report['actives_filled']} "
            f"claims={report['claims_written']} skipped={report['skipped']} outranked={report['skipped_outranked']}"
        )

        refreshed = 0
        for ck in dict.fromkeys(content_keys):  # dedup, preserve order
            try:
                if await refresh_agent_pdp_view_for_content_key(ck, refresh_source=REFRESH_SOURCE):
                    refreshed += 1
            except Exception as e:  # never let one bad row abort the batch
                print(f"  refresh err {str(ck)[:20]}: {str(e)[:60]}")
        print(f"agent_pdp_view refreshed: {refreshed}")
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    p.add_argument("--limit", type=int, default=0, help="max products this run (0=all). Chunk the big run.")
    asyncio.run(_drive(p.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
