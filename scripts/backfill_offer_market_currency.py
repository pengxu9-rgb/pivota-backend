#!/usr/bin/env python3
"""Derive external-seed offer market+currency from the real storefront (/meta.json).

Ingest stamps external-seed offers `market='US', currency='USD'` from a default, not
from the store: `mintree.us` is an Indian store (INR), `upcirclebeauty.com` is UK
(GBP). This corrects the corpus by asking each storefront what market+currency it
actually uses (Shopify /meta.json), keyed on the offer's source_domain (falling back
to the seed's domain for the source_domain-less mirror rows).

Correct-only + provenance-safe:
  * NEVER fabricates. Only writes when the store's real currency is KNOWN and DIFFERS
    from what is stamped. An unresolvable storefront is left exactly as-is + reported.
  * Only touches external-seed source_systems (never a real merchant's own sync, whose
    currency already comes from its Shopify /shop.json).
  * One cached /meta.json fetch per distinct domain — NOT per offer, and NOT inside any
    ingest write. Safe to run on a schedule so new offers are corrected each pass.

After correcting an offer to a non-USD currency it no longer satisfies the US-buyable
gate (index_pipeline_state.has_us_offer, currency-derived per #1568), so --apply
recomputes serving for every affected content_key.

Dry-run by default; --apply writes.

  DATABASE_URL=... python -m scripts.backfill_offer_market_currency --min-offers 3
  DATABASE_URL=... python -m scripts.backfill_offer_market_currency --min-offers 3 --apply
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
from services.index_pipeline_state_service import recompute_serving_eligibility  # noqa: E402
from services.storefront_currency import (  # noqa: E402
    fetch_storefront_meta,
    normalize_domain,
)

# external-seed writers only; a real merchant's own sync already carries true currency.
_SEED_SOURCES = (
    "external_product_seeds_mirror_v1",
    "catalog_enrichment_agent_v1",
    "public_source_pdp_repair_v1",
    "public_source_pdp_content_repair_v1",
)

# Offers grouped by the domain we can resolve: source_domain, else the attached
# seed's domain (the mirror path never writes source_domain — the audit blind spot).
_DOMAINS_SQL = """
    SELECT domain, count(*) AS offers,
           array_agg(DISTINCT upper(coalesce(currency, ''))) AS currencies
    FROM (
        SELECT o.offer_id,
               coalesce(nullif(btrim(o.source_domain), ''),
                        (SELECT nullif(btrim(eps.domain), '')
                         FROM external_product_seeds eps
                         WHERE eps.attached_product_key = o.product_key
                         ORDER BY eps.updated_at DESC LIMIT 1)) AS domain,
               o.currency
        FROM catalog_offers o
        WHERE o.suppressed_at IS NULL
          AND o.list_price > 0
          AND o.source_system = ANY(:sources)
    ) t
    WHERE coalesce(domain, '') <> ''
    GROUP BY domain
    HAVING count(*) >= :min_offers
    ORDER BY count(*) DESC
"""

_AFFECTED_CKS_SQL = """
    SELECT DISTINCT cp.content_key
    FROM catalog_offers o
    JOIN catalog_products cp ON cp.product_key = o.product_key
    WHERE cp.content_key IS NOT NULL
      AND o.source_system = ANY(:sources)
      AND coalesce(nullif(btrim(o.source_domain), ''),
                   (SELECT nullif(btrim(eps.domain), '') FROM external_product_seeds eps
                    WHERE eps.attached_product_key = o.product_key
                    ORDER BY eps.updated_at DESC LIMIT 1)) = :domain
"""

# Correct-only: WHERE the stamped value still differs, so re-runs are idempotent and a
# concurrent correction is never clobbered.
_UPDATE_OFFERS_SQL = """
    UPDATE catalog_offers o
       SET currency = :cur, market = :mkt, updated_at = NOW()
     WHERE o.source_system = ANY(:sources)
       AND (upper(coalesce(o.currency,'')) <> :cur OR coalesce(o.market,'') <> :mkt)
       AND coalesce(nullif(btrim(o.source_domain), ''),
                    (SELECT nullif(btrim(eps.domain), '') FROM external_product_seeds eps
                     WHERE eps.attached_product_key = o.product_key
                     ORDER BY eps.updated_at DESC LIMIT 1)) = :domain
"""


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        rows = [dict(r) for r in await database.fetch_all(
            _DOMAINS_SQL, {"sources": list(_SEED_SOURCES), "min_offers": args.min_offers})]
        print(f"external-seed domains (>= {args.min_offers} offers): {len(rows)}\n")

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
            country = meta.get("country")
            stamped = [c for c in (row.get("currencies") or []) if c]
            if cur and any(c != cur for c in stamped):
                corrections.append({**row, "true_currency": cur, "true_market": country})

        await asyncio.gather(*(classify(r) for r in rows))

        corrections.sort(key=lambda x: -x["offers"])
        total_offers = sum(c["offers"] for c in corrections)
        print(f"=== {len(corrections)} domains need currency correction "
              f"({total_offers} offers); {unresolved} domains unresolved ===")
        for c in corrections:
            print(f"  {c['domain'][:32]:32} stamped={','.join(c['currencies'])} -> "
                  f"{c['true_currency']}/{c['true_market']} offers={c['offers']}")
        if not corrections:
            print("\nnothing to correct.")
            return 0
        if not args.apply:
            print(f"\n(DRY-RUN — would correct {total_offers} offers across "
                  f"{len(corrections)} domains; pass --apply)")
            return 0

        written = recomputed = 0
        for c in corrections:
            cks = [r["content_key"] for r in await database.fetch_all(
                _AFFECTED_CKS_SQL, {"sources": list(_SEED_SOURCES), "domain": c["domain"]})]
            n = await database.execute(_UPDATE_OFFERS_SQL, {
                "cur": c["true_currency"], "mkt": c["true_market"] or "US",
                "sources": list(_SEED_SOURCES), "domain": c["domain"]})
            written += int(n or 0)
            for ck in cks:
                try:
                    await recompute_serving_eligibility(
                        str(ck), reason="market_currency_from_meta_v1")
                    recomputed += 1
                except Exception as exc:  # noqa: BLE001 — one ck must not stop the run
                    print(f"  WARN recompute ck={ck}: {str(exc)[:100]}")
            print(f"  corrected {c['domain']}: {n} offers -> {c['true_currency']}, "
                  f"{len(cks)} cks recomputed", flush=True)
        print(f"\nAPPLIED: offers_corrected={written} content_keys_recomputed={recomputed}")
        return 0
    finally:
        if own:
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Derive external-seed offer market+currency from /meta.json.")
    p.add_argument("--min-offers", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--apply", action="store_true", help="write corrections (else dry-run)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
