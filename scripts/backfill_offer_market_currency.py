#!/usr/bin/env python3
"""Correct external-seed offer currency/market from the real storefront (/meta.json).

Ingest stamps external-seed offers `market='US', currency='USD'` from a DEFAULT, not
from the store: `mintree.us` is an Indian store (INR), `upcirclebeauty.com` is UK
(GBP). This asks each storefront what currency it actually uses (Shopify /meta.json,
keyed on the offer's source_domain, falling back to the attached seed's domain — which
reaches the source_domain-less mirror rows) and relabels the default-stamped offers.

SCOPE — deliberately narrow (this is a DATA-honesty fix, not a serving change):
  * Only rewrites offers still stamped the default `currency='USD'`. A row already
    bearing a real non-USD currency is NEVER touched — so this cannot corrupt a
    correctly-labelled offer, nor collapse a mixed-currency domain.
  * Only touches external-seed source_systems — never a real merchant's own sync,
    whose currency already comes from its Shopify /shop.json.
  * Never fabricates: writes only when the store's real currency is KNOWN (from
    /meta.json) and is non-USD. An unresolvable storefront is left as-is + counted.
  * Does NOT recompute serving. Serving off a currency change is reconciled by the
    existing index/trust drift machinery (and only gates once the currency-derived
    US-buyable gate is enabled). Keeping this data-only avoids a partial-run leaving
    serving half-updated.

CAVEAT the operator must weigh: base currency != a US buyer's price for a Shopify
Markets store that sells to the US in USD (the stamped USD number may be a genuine
US-converted price). currency-mismatch cannot tell that apart from a mislabelled
foreign price, so the WEEKLY cron is DRY-RUN ONLY — a human reviews the by-domain
report and runs --apply (workflow_dispatch) for the writes.

Dry-run by default; --apply writes (guarded by --max-domains).

  DATABASE_URL=... python -m scripts.backfill_offer_market_currency
  DATABASE_URL=... python -m scripts.backfill_offer_market_currency --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.storefront_currency import fetch_storefront_meta  # noqa: E402

# external-seed writers only; a real merchant's own sync already carries true currency.
_SEED_SOURCES = (
    "external_product_seeds_mirror_v1",
    "catalog_enrichment_agent_v1",
    "public_source_pdp_repair_v1",
    "public_source_pdp_content_repair_v1",
)

# Candidate domains: those with DEFAULT-USD-stamped external-seed offers. Keyed on
# source_domain, else the attached seed's most-recent domain (the mirror path never
# writes source_domain — the audit blind spot). Only USD-stamped rows are counted,
# so a domain already fully corrected drops out (idempotent).
_DOMAINS_SQL = """
    SELECT domain, count(*) AS usd_offers
    FROM (
        SELECT o.offer_id,
               coalesce(nullif(btrim(o.source_domain), ''),
                        (SELECT nullif(btrim(eps.domain), '')
                         FROM external_product_seeds eps
                         WHERE eps.attached_product_key = o.product_key
                         ORDER BY eps.updated_at DESC LIMIT 1)) AS domain
        FROM catalog_offers o
        WHERE o.suppressed_at IS NULL
          AND o.list_price > 0
          AND o.source_system = ANY(:sources)
          AND upper(coalesce(o.currency, '')) = 'USD'
    ) t
    WHERE coalesce(domain, '') <> ''
    GROUP BY domain
    HAVING count(*) >= :min_offers
    ORDER BY count(*) DESC
"""

# Rewrite ONLY the default-USD-stamped rows on the domain. The currency='USD' guard
# makes this idempotent and structurally unable to touch a row already bearing a
# real currency (mixed-currency-safe).
_UPDATE_OFFERS_SQL = """
    UPDATE catalog_offers o
       SET currency = :cur, market = :mkt, updated_at = NOW()
     WHERE o.source_system = ANY(:sources)
       AND o.suppressed_at IS NULL
       AND o.list_price > 0
       AND upper(coalesce(o.currency,'')) = 'USD'
       AND coalesce(nullif(btrim(o.source_domain), ''),
                    (SELECT nullif(btrim(eps.domain), '') FROM external_product_seeds eps
                     WHERE eps.attached_product_key = o.product_key
                     ORDER BY eps.updated_at DESC LIMIT 1)) = :domain
    RETURNING o.offer_id
"""


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        rows = [dict(r) for r in await database.fetch_all(
            _DOMAINS_SQL, {"sources": list(_SEED_SOURCES), "min_offers": args.min_offers})]
        print(f"external-seed domains with USD-stamped offers "
              f"(>= {args.min_offers}): {len(rows)}\n")

        sem = asyncio.Semaphore(args.concurrency)
        corrections: List[Dict[str, Any]] = []
        unresolved = 0

        async def classify(row: Dict[str, Any]) -> None:
            nonlocal unresolved
            async with sem:
                meta = await fetch_storefront_meta(row["domain"])
            if not meta:
                unresolved += 1
                return
            cur = meta.get("currency")
            # only a KNOWN, non-USD store currency is a correction.
            if cur and cur != "USD":
                corrections.append({**row, "true_currency": cur, "true_market": meta.get("country")})

        await asyncio.gather(*(classify(r) for r in rows))

        corrections.sort(key=lambda x: -x["usd_offers"])
        total = sum(c["usd_offers"] for c in corrections)
        print(f"=== {len(corrections)} domains to relabel ({total} USD-stamped offers); "
              f"{unresolved} domains unresolved (left as-is) ===")
        for c in corrections:
            print(f"  {c['domain'][:32]:32} USD -> {c['true_currency']}/{c['true_market']} "
                  f"offers={c['usd_offers']}")
        if not corrections:
            print("\nnothing to correct.")
            return 0
        if not args.apply:
            print(f"\n(DRY-RUN — would relabel {total} offers across "
                  f"{len(corrections)} domains; pass --apply)")
            return 0
        if args.max_domains and len(corrections) > args.max_domains:
            print(f"\nREFUSED: {len(corrections)} domains exceeds --max-domains "
                  f"{args.max_domains}. Inspect the dry-run before widening.")
            return 2

        written = 0
        for c in corrections:
            # RETURNING + fetch_all: database.execute() yields None for an
            # UPDATE without RETURNING, so counting its result reports 0.
            updated = await database.fetch_all(_UPDATE_OFFERS_SQL, {
                "cur": c["true_currency"], "mkt": c["true_market"] or "US",
                "sources": list(_SEED_SOURCES), "domain": c["domain"]})
            got = len(updated)
            written += got
            print(f"  relabelled {c['domain']}: {got} offers -> {c['true_currency']}", flush=True)
        print(f"\nAPPLIED: offers_relabelled={written}")
        print("NOTE: serving is reconciled by the index/trust drift machinery, not here.")
        return 0
    finally:
        if own:
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Relabel external-seed offer currency/market from /meta.json.")
    p.add_argument("--min-offers", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--max-domains", type=int, default=25,
                   help="refuse --apply if more than this many domains would be relabelled (0=off)")
    p.add_argument("--apply", action="store_true", help="write corrections (else dry-run)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
