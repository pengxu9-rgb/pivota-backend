#!/usr/bin/env python3
"""DETECTIVE: find offers whose stamped currency contradicts the real storefront.

Ingest stamps external-seed offers `currency='USD'` without checking the store. A
`.us` domain is not a currency — `mintree.us` is an Indian store pricing in INR, so
a Rs.1,999 hand-cream 5-pack was served to agents as $1,999. This scans every live
source_domain, asks the storefront what currency it actually uses (`/meta.json`),
and reports the domains we are provably mispricing. Also flags `wholesale.*`
subdomains (B2B pallet/case listings that should not sit in a consumer index).

This is OBSERVE-FIRST. It never edits offers or prices. `--apply` quarantines the
offending domains through the standard reversible source-quarantine path, and — like
scripts/manage_source_quarantine.py — requires an explicit --confirm token because a
domain quarantine suppresses that domain's entire inventory from serving.

  DATABASE_URL=... python -m scripts.audit_offer_currency
  DATABASE_URL=... python -m scripts.audit_offer_currency \
      --apply --confirm AUDIT_OFFER_CURRENCY_QUARANTINE --created-by you@x [--max-quarantine 30]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.source_quarantine import MATCH_TYPE_DOMAIN, create_quarantine  # noqa: E402
from services.storefront_currency import (  # noqa: E402
    currency_mismatch,
    fetch_storefront_meta,
    normalize_domain,
)

CONFIRM_TOKEN = "AUDIT_OFFER_CURRENCY_QUARANTINE"

# Per (domain, currency) so a mixed-currency domain is not collapsed by min(): a
# store with both USD and INR rows must surface the INR rows as wrong, not hide
# them behind a single aggregate. array_agg exposes every stamped currency.
_DOMAINS_SQL = """
    SELECT domain,
           array_agg(DISTINCT currency) AS currencies,
           sum(offers) AS offers,
           max(max_price) AS max_price
    FROM (
        SELECT o.source_domain AS domain,
               upper(coalesce(o.currency, '')) AS currency,
               count(*) AS offers,
               max(o.list_price) AS max_price
        FROM catalog_offers o
        WHERE o.suppressed_at IS NULL
          AND coalesce(o.source_domain, '') <> ''
          AND o.list_price > 0
        GROUP BY o.source_domain, upper(coalesce(o.currency, ''))
    ) per_currency
    GROUP BY domain
    HAVING sum(offers) >= :min_offers
    ORDER BY sum(offers) DESC
"""


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        rows = [dict(r) for r in await database.fetch_all(
            _DOMAINS_SQL, {"min_offers": args.min_offers})]
        # NOTE: offers written without source_domain (historically the whole
        # external_product_seeds_mirror_v1 lane) are invisible to this
        # domain-keyed scan. scripts/audit_domainless_offer_currency.py covers
        # them by deriving the domain from seed provenance, and its --apply
        # backfills source_domain so this count trends to zero.
        no_domain = await database.fetch_val(
            "SELECT count(*) FROM catalog_offers WHERE suppressed_at IS NULL "
            "AND list_price > 0 AND coalesce(source_domain,'') = ''")
        print(f"scanning {len(rows)} source domains (>= {args.min_offers} live offers)")
        print(f"NOTE: {no_domain} live offers have no source_domain (not scanned here — "
              f"run scripts/audit_domainless_offer_currency.py for that cohort)\n")

        mismatches: List[Dict[str, Any]] = []
        wholesale: List[Dict[str, Any]] = []
        sem = asyncio.Semaphore(args.concurrency)

        async def check(row: Dict[str, Any]) -> None:
            domain = row["domain"]
            if normalize_domain(domain).split(".")[0] == "wholesale":
                wholesale.append(row)
                return
            async with sem:
                meta = await fetch_storefront_meta(domain)
            if not meta:
                return
            # every distinct stamped currency on the domain must match the store
            wrong = [c for c in (row.get("currencies") or [])
                     if c and currency_mismatch(c, meta)]
            if wrong:
                row["actual_currency"] = meta.get("currency")
                row["country"] = meta.get("country")
                row["wrong_currencies"] = wrong
                mismatches.append(row)

        await asyncio.gather(*(check(r) for r in rows))

        print(f"=== CURRENCY MISMATCH: {len(mismatches)} domains ===")
        for m in sorted(mismatches, key=lambda x: -x["offers"]):
            print(f"  {m['domain'][:34]:34} stamped={','.join(m['wrong_currencies'])} "
                  f"actual={m['actual_currency']} ({m.get('country')}) "
                  f"offers={m['offers']:>4} max={m['max_price']:.0f}")
        print(f"\n=== WHOLESALE/B2B DOMAINS: {len(wholesale)} ===")
        for w in wholesale:
            print(f"  {w['domain'][:34]:34} offers={w['offers']:>4} max={w['max_price']:.0f}")

        offenders = mismatches + wholesale
        if not offenders:
            print("\nno defects found.")
            return 0
        if not args.apply:
            print(f"\n(DRY-RUN — would quarantine {len(offenders)} domains; "
                  f"pass --apply --confirm {CONFIRM_TOKEN})")
            return 0
        if args.confirm != CONFIRM_TOKEN:
            print(f"\nREFUSED: --apply requires --confirm {CONFIRM_TOKEN} "
                  f"(quarantine suppresses a domain's ENTIRE inventory).")
            return 2
        if args.max_quarantine and len(offenders) > args.max_quarantine:
            print(f"\nREFUSED: {len(offenders)} offenders exceeds --max-quarantine "
                  f"{args.max_quarantine}; inspect before widening.")
            return 2

        for o in offenders:
            reason = (
                f"currency mismatch: stamped {','.join(o.get('wrong_currencies', []))} "
                f"but storefront is {o.get('actual_currency')}" if o.get("actual_currency")
                else "wholesale/B2B channel not valid for the consumer index"
            )
            try:
                res = await create_quarantine(
                    match_type=MATCH_TYPE_DOMAIN,
                    match_value=normalize_domain(o["domain"]) or o["domain"],
                    reason=reason, created_by=args.created_by, db=database)
                print(f"  quarantined {o['domain']} -> id={getattr(res, 'quarantine_id', res)}")
            except Exception as exc:  # noqa: BLE001 — one domain must not stop the sweep
                print(f"  WARN quarantine failed {o['domain']}: {str(exc)[:120]}")
        return 0
    finally:
        if own:
            await database.disconnect()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Audit offer currency vs the real storefront.")
    p.add_argument("--min-offers", type=int, default=5)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--created-by", default="audit_offer_currency")
    p.add_argument("--apply", action="store_true", help="quarantine offending domains")
    p.add_argument("--confirm", default="", help=f"required with --apply: {CONFIRM_TOKEN}")
    p.add_argument("--max-quarantine", type=int, default=50,
                   help="refuse --apply if more than this many domains would be quarantined (0=off)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
