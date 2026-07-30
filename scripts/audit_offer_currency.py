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

Besides the per-domain currency check, the report ends with an OBSERVE-ONLY
implausible-price list (USD-stamped offers >= --price-alert). It exists because
the currency check is structurally blind to one class: a Shopify-Markets store
whose /meta.json base currency IS 'USD' but whose crawled price carries another
currency's magnitude — the confirmed member being Oiad's ₩400,000 crawled as
$400,000 (oiad.us, KR store, base currency genuinely USD). When the label and
the base currency agree, price magnitude is the only remaining smoke. The list
never feeds --apply; per the standing lesson, price is a detector, not a
decision basis — a human verifies each row against the storefront (quadthera.us
$1,200 proved genuine the same day Oiad proved fake).

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

# Observe-only price-magnitude list. Deliberately NOT restricted to the
# suppressed_at IS NULL scope of the domain scan: a row suppressed FOR a price
# defect must stay visible to price tooling (the 2026-07-27 backfill lesson),
# so the split is reported instead. trim() not btrim() — the SQL-standard form
# runs on both Postgres and the SQLite suite (#1568's inverse-trap lesson).
# LIMIT bounds a pathological fleet; the print caps at 20 rows anyway.
_PRICE_ALERT_SQL = """
    SELECT o.offer_id,
           o.source_domain AS domain,
           o.list_price,
           (o.suppressed_at IS NOT NULL) AS is_suppressed
    FROM catalog_offers o
    WHERE upper(trim(coalesce(o.currency, ''))) = 'USD'
      AND o.list_price >= :price_alert
      AND coalesce(o.source_domain, '') <> ''
    ORDER BY o.list_price DESC, o.offer_id
    LIMIT 200
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

        # Observe-only: catches the Markets-store class the currency check is
        # blind to (label == base currency, magnitude from another currency).
        # Never joins `offenders` — a big number is a review signal, not proof.
        if args.price_alert > 0:
            def _price(r: Dict[str, Any]) -> float:
                try:
                    return float(r.get("list_price") or 0)
                except (TypeError, ValueError):
                    return 0.0

            alerts = [dict(r) for r in await database.fetch_all(
                _PRICE_ALERT_SQL, {"price_alert": args.price_alert})]
            print(f"\n=== IMPLAUSIBLE-PRICE REVIEW LIST: {len(alerts)} USD-stamped "
                  f"rows >= {args.price_alert:.0f} (observe-only) ===")
            for r in alerts[:20]:
                print(f"  {str(r.get('domain') or '?')[:30]:30} {_price(r):>12.2f} USD "
                      f"{'suppressed' if r.get('is_suppressed') else 'live':>10} "
                      f"offer={r.get('offer_id')}")
            if len(alerts) > 20:
                print(f"  ... and {len(alerts) - 20} more (raise --price-alert to narrow)")

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
    p.add_argument("--price-alert", type=float, default=1000.0,
                   help="observe-only: list USD-stamped offers priced >= this "
                        "(magnitude smoke for Markets stores whose base currency "
                        "matches the stamp; 0 disables)")
    p.add_argument("--created-by", default="audit_offer_currency")
    p.add_argument("--apply", action="store_true", help="quarantine offending domains")
    p.add_argument("--confirm", default="", help=f"required with --apply: {CONFIRM_TOKEN}")
    p.add_argument("--max-quarantine", type=int, default=50,
                   help="refuse --apply if more than this many domains would be quarantined (0=off)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
