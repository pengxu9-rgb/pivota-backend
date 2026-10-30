#!/usr/bin/env python3
"""Audit storefront currency hints without rewriting money.

A storefront's base currency cannot prove the currency of a captured amount.
An amount may be a Shopify Markets price or a converted seed snapshot. Relabelling
it without its own source amount changes its value, so this command refuses
--apply. Use repair_catalog_variant_prices with exact variant evidence instead.
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

# The suppressed-row scope switch. Composed into the SQL as a literal fragment
# rather than a bind parameter DELIBERATELY: an untyped `:flag = FALSE` bind is the
# exact defect class that took the feed down on 2026-07-26 (#1588's untyped
# `concat` parameter — Postgres cannot infer the type, SQLite never notices). A
# constant clause chosen in Python has no type to infer.
_LIVE_ONLY_FILTER = "AND o.suppressed_at IS NULL"
_ALL_ROWS_FILTER = "AND TRUE  /* suppressed rows IN SCOPE; see module docstring */"


def _suppressed_filter(live_only: bool) -> str:
    return _LIVE_ONLY_FILTER if live_only else _ALL_ROWS_FILTER


# Candidate domains: those with DEFAULT-USD-stamped external-seed offers. Keyed on
# source_domain, else the attached seed's most-recent domain (the mirror path never
# writes source_domain — the audit blind spot). Only USD-stamped rows are counted,
# so a domain already fully corrected drops out (idempotent).
#
# The live/suppressed split is reported per domain so the operator can see exactly
# what a --apply would touch: a domain that is entirely suppressed is a store we
# already took off the surface, and relabelling it changes no served price.
_DOMAINS_SQL_TEMPLATE = """
    SELECT domain,
           count(*) AS usd_offers,
           count(*) FILTER (WHERE NOT is_suppressed) AS live_offers,
           count(*) FILTER (WHERE is_suppressed) AS suppressed_offers
    FROM (
        SELECT o.offer_id,
               (o.suppressed_at IS NOT NULL) AS is_suppressed,
               coalesce(nullif(btrim(o.source_domain), ''),
                        (SELECT nullif(btrim(eps.domain), '')
                         FROM external_product_seeds eps
                         WHERE eps.attached_product_key = o.product_key
                         ORDER BY eps.updated_at DESC, eps.id DESC LIMIT 1)) AS domain
        FROM catalog_offers o
        WHERE o.list_price > 0
          {suppressed_filter}
          AND o.source_system = ANY(:sources)
          AND upper(trim(coalesce(o.currency, ''))) = 'USD'
    ) t
    WHERE coalesce(domain, '') <> ''
    GROUP BY domain
    HAVING count(*) >= :min_offers
    ORDER BY count(*) DESC
"""

# Rewrite ONLY the default-USD-stamped rows on the domain. The currency='USD' guard
# makes this idempotent and structurally unable to touch a row already bearing a
# real currency (mixed-currency-safe). That guard is INDEPENDENT of the suppressed
# scope above — widening which rows are VISIBLE must never widen which rows are
# WRITABLE.
_UPDATE_OFFERS_SQL_TEMPLATE = """
    UPDATE catalog_offers o
       SET currency = :cur, market = :mkt, updated_at = NOW()
     WHERE FALSE /* domain base currency is not amount evidence */
       AND o.source_system = ANY(:sources)
       AND o.list_price > 0
       {suppressed_filter}
       AND upper(trim(coalesce(o.currency,''))) = 'USD'
       AND coalesce(nullif(btrim(o.source_domain), ''),
                    (SELECT nullif(btrim(eps.domain), '') FROM external_product_seeds eps
                     WHERE eps.attached_product_key = o.product_key
                     ORDER BY eps.updated_at DESC, eps.id DESC LIMIT 1)) = :domain
    RETURNING o.offer_id
"""


def domains_sql(live_only: bool = False) -> str:
    return _DOMAINS_SQL_TEMPLATE.format(suppressed_filter=_suppressed_filter(live_only))


def update_offers_sql(live_only: bool = False) -> str:
    return _UPDATE_OFFERS_SQL_TEMPLATE.format(
        suppressed_filter=_suppressed_filter(live_only)
    )


# Default-scope (suppressed rows included) renderings, kept as module constants so
# the guard-rail tests can assert on the SQL that actually ships.
_DOMAINS_SQL = domains_sql()
_UPDATE_OFFERS_SQL = update_offers_sql()


async def _run(args: argparse.Namespace) -> int:
    if args.apply:
        raise ValueError("domain_currency_is_not_price_evidence: use repair_catalog_variant_prices")
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        live_only = bool(getattr(args, "live_only", False))
        rows = [dict(r) for r in await database.fetch_all(
            domains_sql(live_only),
            {"sources": list(_SEED_SOURCES), "min_offers": args.min_offers})]
        scope = "live offers only" if live_only else "live + suppressed offers"
        print(f"external-seed domains with USD-stamped offers "
              f"(>= {args.min_offers}, {scope}): {len(rows)}\n")

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

        # --only-domain: apply to a REVIEWED subset instead of all-or-nothing.
        # The dry-run routinely surfaces domains at different confidence levels in
        # one report — a suppressed foreign-primary store with zero serving impact
        # next to a live storefront whose relabel WILL change what serves once the
        # currency-derived US-buyable gate is on. Forcing those into a single
        # atomic apply is what pushes an operator to either over-write or skip the
        # run entirely. Filtering AFTER classification, so every domain is still
        # resolved against its storefront and the narrowing is explicit in the
        # output. NOTE the main `=== N domains to relabel ===` report counts only
        # the SELECTION; what was dropped is recoverable from the HELD BACK lines
        # printed just above it.
        only = {d.strip().lower() for d in (args.only_domain or []) if d.strip()}
        if only:
            skipped = [c for c in corrections if c["domain"].lower() not in only]
            corrections = [c for c in corrections if c["domain"].lower() in only]
            print(f"--only-domain: {len(corrections)} selected, {len(skipped)} held back")
            for c in skipped:
                print(f"  HELD BACK {c['domain']} ({c['usd_offers']} offers)")
            unmatched = only - {c["domain"].lower() for c in corrections}
            if unmatched:
                # Loud, because the silent failure mode is "operator typed a
                # domain, saw APPLIED: 0, and read it as already-corrected".
                print(f"  WARNING: no correction candidate matched: {sorted(unmatched)}")

        corrections.sort(key=lambda x: -x["usd_offers"])
        total = sum(c["usd_offers"] for c in corrections)
        print(f"=== {len(corrections)} domains requiring variant-level price review ({total} USD-stamped offers); "
              f"{unresolved} domains unresolved (left as-is) ===")
        for c in corrections:
            print(f"  {c['domain']:32} USD -> {c['true_currency']}/{c['true_market']} "
                  f"offers={c['usd_offers']} "
                  f"(live={c.get('live_offers', 0)} suppressed={c.get('suppressed_offers', 0)})")
        if not corrections:
            print("\nno domain-level discrepancies found.")
            return 0
        if not args.apply:
            print(f"\n(READ-ONLY — {total} offers across {len(corrections)} domains need "
                  "variant-level amount and currency evidence; use repair_catalog_variant_prices)")
            return 0
        if args.max_domains and len(corrections) > args.max_domains:
            print(f"\nREFUSED: {len(corrections)} domains exceeds --max-domains "
                  f"{args.max_domains}. Inspect the dry-run before widening.")
            return 2

        written = 0
        for c in corrections:
            # RETURNING + fetch_all: database.execute() yields None for an
            # UPDATE without RETURNING, so counting its result reports 0.
            updated = await database.fetch_all(update_offers_sql(live_only), {
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
    p = argparse.ArgumentParser(description="Audit domain currency hints; amount corrections require exact variant evidence.")
    p.add_argument("--min-offers", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--max-domains", type=int, default=25,
                   help="legacy compatibility option; --apply is always refused")
    p.add_argument("--only-domain", action="append", metavar="DOMAIN",
                   help="restrict the report to this domain (repeatable); held-back domains are listed")
    p.add_argument("--live-only", action="store_true",
                   help="pre-2026-07-27 scope: skip suppressed offers. Leaves rows "
                        "suppressed FOR a currency defect permanently mislabelled — "
                        "see the module docstring before using this.")
    p.add_argument("--apply", action="store_true", help="disabled: domain currency is not evidence of a captured amount's denomination")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
