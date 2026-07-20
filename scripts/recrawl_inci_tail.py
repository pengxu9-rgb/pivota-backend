"""Re-crawl clean INCI for external-seed skincare via canonical_url.

Two cohorts (both external_seed skincare, both resumable, both fed through the
same production ingest):

  default          -- the JUNK-SOURCE tail: rows whose stored inci_list is
                      prose/blob junk (unenrichable). Original use case.
  --missing-inci   -- the MISSING-INCI set: rows with a Shopify /products/
                      canonical_url and NO clean INCI at all. Measured
                      recoverable yield ~10% via the static Shopify-JSON path
                      (headless render adds 0 over this — spike 2026-07-20).

Per product: re-fetch the PDP, extract + validate a clean INCI via
canonical_inci_resolver (Shopify-JSON preferred, PDP text fallback), and feed the
production ingest (ingest_crawled_inci_items → enrich → substantiated claims →
agent_pdp_view auto-refresh). The ingest's _is_skippable_inci is the authoritative
gate, so an occasional prose false-positive from the resolver is rejected, not
served; ADR-001 precedence (canonical_inci_intake.may_write) means a listing-tier
crawl never downgrades brand-official / supplier INCI.

Dry-run by default; resumable (skips products that already have raw_inci, so a
long --apply run is safe to re-invoke after a crash). --apply flushes resolved
INCI to the ingest every --flush-every hits so progress is durable mid-run.

  DATABASE_URL=... python -m scripts.recrawl_inci_tail --limit 100                        # junk-tail dry-run
  DATABASE_URL=... python -m scripts.recrawl_inci_tail --missing-inci --limit 200         # missing-set dry-run
  DATABASE_URL=... python -m scripts.recrawl_inci_tail --missing-inci --limit 4000 --apply
"""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
from typing import Dict, List, Optional

from db.database import database
from services.canonical_inci_resolver import http_fetch, resolve_inci_from_urls
from services.crawled_inci_ingest import ingest_crawled_inci_items

# JUNK-SOURCE tail: has an inci_list but it's a keyword-blob or marketing prose.
_SELECT = """
    SELECT product_key, sku_key, canonical_url FROM (
      SELECT DISTINCT ON (cp.product_key)
             cp.product_key, s.sku_key, cp.canonical_url
      FROM catalog_products cp
      JOIN catalog_skus s ON s.product_key = cp.product_key
      LEFT JOIN beauty_sku_ingredients bsi
        ON bsi.product_key = cp.product_key AND coalesce(bsi.raw_inci,'') <> ''
      WHERE cp.merchant_id='external_seed' AND cp.category_kind='skincare'
        AND cp.brand ~* :brand
        AND coalesce(cp.product_payload->>'inci_list','') <> ''
        -- junk source: keyword-blob (no delimiter) or marketing prose
        AND (position(',' in cp.product_payload->>'inci_list')=0
             OR cp.product_payload->>'inci_list' ~* '\\y(promot|improv|reduc|boost|helps|soothe|calm|nourish|brighten|clinically|dermatologist|visibly|wrinkle)\\y|anti-?aging')
        AND cp.canonical_url IS NOT NULL
        AND bsi.product_key IS NULL            -- not yet has clean INCI → resumable
      ORDER BY cp.product_key, (s.sku_key LIKE '%::canonical') DESC
    ) t
    ORDER BY random()
    LIMIT :limit
"""

# MISSING-INCI set: Shopify /products/ PDP, no clean INCI yet. No inci_list
# requirement (that's the whole point) — the resolver reads the .json body_html.
_SELECT_MISSING = """
    SELECT product_key, sku_key, canonical_url FROM (
      SELECT DISTINCT ON (cp.product_key)
             cp.product_key, s.sku_key, cp.canonical_url
      FROM catalog_products cp
      JOIN catalog_skus s ON s.product_key = cp.product_key
      LEFT JOIN beauty_sku_ingredients bsi
        ON bsi.product_key = cp.product_key AND coalesce(bsi.raw_inci,'') <> ''
      WHERE cp.merchant_id='external_seed' AND cp.category_kind='skincare'
        AND cp.brand ~* :brand
        AND cp.canonical_url ~ '/products/'    -- Shopify-JSON path is what recovers
        AND bsi.product_key IS NULL            -- no clean INCI yet → resumable
      ORDER BY cp.product_key, (s.sku_key LIKE '%::canonical') DESC
    ) t
    ORDER BY random()
    LIMIT :limit
"""


async def _flush(items: List[Dict], totals: Dict[str, int]) -> None:
    """Ingest a batch of resolved INCI and fold the report into running totals."""
    if not items:
        return
    rep = await ingest_crawled_inci_items(items, source_system="pdp_recrawl", db=database)
    for k in ("n", "inci_written", "claims_written", "skipped"):
        totals[k] = totals.get(k, 0) + int(rep.get(k, 0))
    print(f"  flushed {len(items)} → inci_written={rep.get('inci_written')} "
          f"claims={rep.get('claims_written')} skipped={rep.get('skipped')} "
          f"| cumulative inci_written={totals['inci_written']}", flush=True)
    items.clear()


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        select = _SELECT_MISSING if args.missing_inci else _SELECT
        cohort = "missing-inci set" if args.missing_inci else "junk-source tail"
        rows = await database.fetch_all(select, {"brand": args.brand, "limit": args.limit})
        print(f"{cohort}: {len(rows)} products to re-crawl (apply={args.apply})")
        # Shorter per-fetch timeout: real Shopify .json responds in <2s, so a
        # tight bound mostly trims the dead/bot-blocked hosts that otherwise hang
        # to the 10s default (×2 for .json + page) and dominate wall time.
        fetch = partial(http_fetch, timeout=float(args.fetch_timeout))
        items: List[dict] = []
        hits = misses = 0
        totals: Dict[str, int] = {"n": 0, "inci_written": 0, "claims_written": 0, "skipped": 0}
        for i, r in enumerate(rows, 1):
            try:
                res = await resolve_inci_from_urls([r["canonical_url"]], fetch=fetch)
            except Exception:
                res = None
            if res and res.raw_inci:
                hits += 1
                items.append({"product_key": r["product_key"], "sku_key": r["sku_key"],
                              "raw_inci": res.raw_inci})
                if args.apply and len(items) >= args.flush_every:
                    await _flush(items, totals)
            else:
                misses += 1
            if i % 100 == 0:
                print(f"  ...{i}/{len(rows)} scanned | hits={hits} misses={misses} "
                      f"({hits/i:.0%})", flush=True)

        print(f"resolved clean INCI: {hits} | misses: {misses} "
              f"({hits/max(len(rows),1):.0%} yield)")
        if args.apply:
            await _flush(items, totals)          # final partial batch
            print(f"INGEST TOTALS: {totals}")
        else:
            print(f"(DRY-RUN — would ingest {hits} resolved INCI)")
        return 0
    finally:
        if own:
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Re-crawl clean INCI for external-seed skincare.")
    p.add_argument("--brand", default=".*", help="brand regex (default: all)")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--missing-inci", action="store_true",
                   help="target the missing-INCI set (Shopify PDPs, no INCI yet) "
                        "instead of the junk-source tail")
    p.add_argument("--flush-every", type=int, default=50,
                   help="on --apply, ingest resolved INCI every N hits (durable progress)")
    p.add_argument("--fetch-timeout", type=float, default=6.0,
                   help="per-fetch timeout seconds (tight bound trims dead hosts)")
    p.add_argument("--apply", action="store_true", help="ingest + serve (else resolve-only dry-run)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
