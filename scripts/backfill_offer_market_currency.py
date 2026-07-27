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

SUPPRESSED ROWS ARE IN SCOPE — and until 2026-07-27 they were not, which was a
SELF-DEFEATING SCOPE BUG. Both the candidate scan and the UPDATE carried
`o.suppressed_at IS NULL`, so the rows suppressed *for* a currency defect were
structurally invisible to the tool built to correct currency defects. MEASURED on
prod the day it was found: Mintree (213 offers) and RED DANE (71) — the two stores
whose INR/ZAR-priced-as-USD listings started this whole workstream — were still
stamped `currency='USD', market='US'` months after being suppressed under
`source_currency_or_channel_defect`. They had been HIDDEN, never CORRECTED, and no
scheduled pass could ever reach them.

Why including them is safe, and why it is the honest thing to do:
  * A suppressed row does not serve, so relabelling it cannot change what a buyer
    or an agent sees. The write is strictly data-honesty.
  * It removes a live landmine: `has_us_offer` now derives from `currency='USD'`
    (#1568), so a suppressed row left stamped USD would re-enter the index as
    "US-buyable" the instant suppression is lifted — reintroducing the original
    defect through the back door.
  * The correct-only guard is UNCHANGED and is what keeps this safe: the UPDATE
    still requires `upper(coalesce(currency,'')) = 'USD'`, so a row already
    bearing a real currency remains structurally untouchable, suppressed or not.
    Widening WHICH rows are visible must never widen WHICH rows are writable.
`--live-only` restores the pre-fix behaviour if an operator ever wants it.

CAVEAT the operator must weigh: base currency != a US buyer's price for a Shopify
Markets store that sells to the US in USD (the stamped USD number may be a genuine
US-converted price). currency-mismatch cannot tell that apart from a mislabelled
foreign price, so the WEEKLY cron is DRY-RUN ONLY — a human reviews the by-domain
report and runs --apply (workflow_dispatch) for the writes.

KNOWN LIMIT OF THE DOMAIN ATTRIBUTION, stated because the correct-only guard does
NOT cover it. "Correct-only" guarantees we only ever overwrite a DEFAULT-`USD`
row; it does NOT guarantee the replacement currency is the right one, because the
domain an offer is attributed to may not be its own. When `source_domain` is NULL
the domain comes from a seed joined on `attached_product_key`, and the Path-C lane
(`catalog_enrichment_agent_v1`) attaches SEVERAL seeds — with different domains —
to one product_key. An offer can therefore be grouped under, and stamped with the
currency of, a SIBLING seed's storefront.

`ORDER BY eps.updated_at DESC, eps.id DESC` at least makes the choice
deterministic and identical in the scan and the UPDATE. Without the `eps.id`
tiebreaker two seeds sharing an `updated_at` could resolve differently between the
dry-run a human reviewed and the --apply they then dispatched. It does not make
the attribution CORRECT. Two consequences to hold in mind:
  * a seed refresh between the dry-run and the apply can move offers between
    domain groups — re-run the dry-run if the two are far apart in time;
  * a domain's offer count can cross the `--min-offers` floor in either direction
    as offers migrate, so a storefront reviewed last week can silently stop
    appearing this week.
The real fix is to attribute each offer to ITS OWN seed (`catalog_offers.
source_ref` holds the seed id the dual-write projected from) rather than to the
most-recent sibling. That changes which rows land in which group, so it needs its
own measured change — it is not a scope tweak.

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
     WHERE o.source_system = ANY(:sources)
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
        # run entirely. Filtering AFTER classification, so the dry-run still shows
        # the full picture and the narrowing is visible in the output.
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
        print(f"=== {len(corrections)} domains to relabel ({total} USD-stamped offers); "
              f"{unresolved} domains unresolved (left as-is) ===")
        for c in corrections:
            print(f"  {c['domain']:32} USD -> {c['true_currency']}/{c['true_market']} "
                  f"offers={c['usd_offers']} "
                  f"(live={c.get('live_offers', 0)} suppressed={c.get('suppressed_offers', 0)})")
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
    p = argparse.ArgumentParser(description="Relabel external-seed offer currency/market from /meta.json.")
    p.add_argument("--min-offers", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--max-domains", type=int, default=25,
                   help="refuse --apply if more than this many domains would be relabelled (0=off)")
    p.add_argument("--only-domain", action="append", metavar="DOMAIN",
                   help="restrict --apply to this domain (repeatable). Classification still "
                        "runs over every domain; the selection and the writes are narrowed, "
                        "and held-back domains are listed.")
    p.add_argument("--live-only", action="store_true",
                   help="pre-2026-07-27 scope: skip suppressed offers. Leaves rows "
                        "suppressed FOR a currency defect permanently mislabelled — "
                        "see the module docstring before using this.")
    p.add_argument("--apply", action="store_true", help="write corrections (else dry-run)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
