#!/usr/bin/env python3
"""DETECTIVE: currency-audit the offers scripts/audit_offer_currency.py cannot see.

That audit keys on `catalog_offers.source_domain`, but the external-seed mirror
lane (`external_product_seeds_mirror_v1`) never wrote the column, so ~4,705 live
offers are structurally invisible to every domain-keyed scan — the audit's own
"NOTE: N live offers have no source_domain" line. The class is not hypothetical:
"Oiad ODDY BUNNY" (prod::merch_obs_be16d153c2e337bd::external_seed::
oiad_us_9145536774400) carries an offer stamped 400000.00 USD that is actually
KRW (~$290).

This script derives each domain-less offer's storefront from its OWN seed-side
provenance — never from a heuristic:

  1. offer_seed      catalog_offers.source_ref = external_product_seeds.id
                     (the dual-write stamps the seed id it projected from)
  2. offer_payload   offer_payload->>'domain' / canonical_url / destination_url
                     (the projection's write-time snapshot of the same seed)
  3. product_seed    catalog_products.source_ref = external_product_seeds.id
                     (the mirror's product-level provenance link)
  4. attached_seed   external_product_seeds.attached_product_key — only when
                     every attached seed agrees on ONE domain; multiple domains
                     (the Path-C sibling-attribution problem) = unresolved.

An offer none of those resolve stays unresolved and is only counted — the
market/currency provenance in our own rows is fabricated at ingest, so nothing
here is ever guessed (the source_product_id prefix convention, e.g. `oiad_us_*`,
is deliberately NOT used: it names a brand slug, not a storefront host).

Derived domains are then checked the same way as the main audit: the store's own
Shopify /meta.json (services.storefront_currency) versus every stamped currency,
plus the `wholesale.*` channel flag.

Dry-run by default; it never edits anything. `--apply` (gated on an explicit
--confirm token, like the sibling scripts) performs the DURABLE fix: backfill
`source_domain` onto the confidently-derived offers. That makes the weekly
"Derive Offer Market/Currency from Storefront" audit and scripts/
audit_offer_currency.py's domain-keyed scan cover this cohort permanently, and
lets the trust upserter's `domain`-quarantine matching reach these offers
through the offers column directly (today it reaches them only via its
eps.domain fallback; note the serving-side anti-join keys on
catalog_products.source_domain, which this script does not fill — that
product/sku-level backfill is scripts/backfill_catalog_source_domain.py's job).
`--quarantine-mismatched` additionally quarantines the storefronts proven
mispriced, mirroring the main audit's --apply (and, per the standing lesson:
quarantine alone never clears the served surface — live_read is a separate
lever).

  DATABASE_URL=... python -m scripts.audit_domainless_offer_currency
  DATABASE_URL=... python -m scripts.audit_domainless_offer_currency \
      --apply --confirm AUDIT_DOMAINLESS_OFFER_CURRENCY --created-by you@x \
      [--include-attached] [--quarantine-mismatched --max-quarantine 20]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.source_quarantine import MATCH_TYPE_DOMAIN, create_quarantine  # noqa: E402
from services.storefront_currency import (  # noqa: E402
    currency_mismatch,
    fetch_storefront_meta,
    normalize_domain,
    plausible_domain,
)

CONFIRM_TOKEN = "AUDIT_DOMAINLESS_OFFER_CURRENCY"

# Same scope as scripts/backfill_offer_market_currency._SEED_SOURCES (asserted
# equal by the unit tests): external-seed writers only. A real merchant's own
# sync stamps source_domain at ingest and is never in this cohort anyway.
_SEED_SOURCES = (
    "external_product_seeds_mirror_v1",
    "catalog_enrichment_agent_v1",
    "public_source_pdp_repair_v1",
    "public_source_pdp_content_repair_v1",
)

# Rules that tie the offer to ITS OWN seed. attached_seed_unique is excluded by
# default: attachment is product-level and Path-C attaches sibling seeds with
# other domains to one product_key — unanimous-domain agreement makes it usable,
# but the operator opts in (--include-attached).
CONFIDENT_RULES = ("offer_seed", "offer_payload", "product_seed")

# One row per domain-less external-seed offer, carrying every provenance field
# the Python-side derivation needs. Suppressed rows are IN SCOPE (the
# 2026-07-27 lesson: rows suppressed FOR a currency defect must not be invisible
# to currency tooling); the report keeps the live/suppressed split visible.
# Postgres-only (= ANY(:sources), ->>, LATERAL) — gated by the
# tests/test_*_postgres.py CI job, not the SQLite suite.
_SCAN_SQL = """
    SELECT o.offer_id,
           o.product_key,
           upper(coalesce(o.currency, '')) AS currency,
           o.list_price,
           (o.suppressed_at IS NOT NULL) AS is_suppressed,
           nullif(btrim(o.offer_payload->>'domain'), '') AS payload_domain,
           nullif(btrim(o.offer_payload->>'canonical_url'), '') AS payload_canonical_url,
           nullif(btrim(o.offer_payload->>'destination_url'), '') AS payload_destination_url,
           nullif(btrim(seed_o.domain), '') AS offer_seed_domain,
           nullif(btrim(seed_o.canonical_url), '') AS offer_seed_canonical_url,
           nullif(btrim(seed_o.destination_url), '') AS offer_seed_destination_url,
           nullif(btrim(seed_p.domain), '') AS product_seed_domain,
           nullif(btrim(seed_p.canonical_url), '') AS product_seed_canonical_url,
           nullif(btrim(seed_p.destination_url), '') AS product_seed_destination_url,
           attached.domains AS attached_domains
    FROM catalog_offers o
    LEFT JOIN external_product_seeds seed_o
           ON seed_o.id = o.source_ref
    LEFT JOIN catalog_products cp
           ON cp.product_key = o.product_key
          AND cp.source_system = ANY(:sources)
    LEFT JOIN external_product_seeds seed_p
           ON seed_p.id = cp.source_ref
    LEFT JOIN LATERAL (
        SELECT array_agg(DISTINCT lower(btrim(eps.domain))) AS domains
        FROM external_product_seeds eps
        WHERE eps.attached_product_key = o.product_key
          AND nullif(btrim(eps.domain), '') IS NOT NULL
    ) attached ON TRUE
    WHERE o.source_system = ANY(:sources)
      AND coalesce(btrim(o.source_domain), '') = ''
    ORDER BY o.offer_id
"""

# The write is driven by the EXACT offer_id -> domain mapping the dry-run
# printed, not by re-deriving inside the UPDATE — so what the operator reviewed
# is what lands, even if a seed refreshes in between. The NULL/empty guard makes
# it fill-only and idempotent: a row that already carries a source_domain
# (ingest-written, or backfilled by an earlier run) is structurally untouchable.
#
# updated_at is deliberately NOT touched. source_domain is provenance metadata,
# not offer truth — and the PDP reconciler treats MAX(co.updated_at) as
# truth-changed, so bumping it on ~4,000 rows would put every touched
# content_key into multi-day drift (300 keys per 6h cron pass) and hold the
# drift count above the alert threshold the whole time, for a write that
# changes nothing any PDP renders.
_BACKFILL_SQL = """
    WITH mapping AS (
        SELECT offer_id, source_domain
        FROM jsonb_to_recordset(CAST(:mappings AS jsonb))
             AS m(offer_id text, source_domain text)
    )
    UPDATE catalog_offers o
       SET source_domain = m.source_domain
      FROM mapping m
     WHERE o.offer_id = m.offer_id
       AND o.source_system = ANY(:sources)
       AND coalesce(btrim(o.source_domain), '') = ''
    RETURNING o.offer_id
"""

# Domain-less offers under OTHER source_systems are called out, not silently
# skipped (the lesson this whole lane keeps re-learning). They are out of scope
# deliberately: e.g. retailer_offer_attach_v1 rows carry operator-curated
# currency from a trusted feed — relabelling or domain-quarantining those from a
# /meta.json base-currency check would be wrong (a retailer's US price is
# legitimately USD even when its base currency is not).
_OUT_OF_SCOPE_SQL = """
    SELECT coalesce(source_system, '<null>') AS source_system,
           count(*) AS offers
    FROM catalog_offers
    WHERE coalesce(btrim(source_domain), '') = ''
      AND (source_system IS NULL OR NOT (source_system = ANY(:sources)))
    GROUP BY coalesce(source_system, '<null>')
    ORDER BY count(*) DESC
"""


def derive_offer_domain(row: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """(normalized domain, rule) for one scanned offer row.

    Provenance precedence: the offer's own seed (current data) beats the
    write-time payload snapshot beats the product's seed; within each rule the
    seed's `domain` column beats the canonical/destination URL host. Returns
    (None, 'attached_seed_ambiguous'|'unresolved') when nothing proves a single
    storefront — never a guess.
    """
    for rule, fields in (
        ("offer_seed", ("offer_seed_domain", "offer_seed_canonical_url",
                        "offer_seed_destination_url")),
        ("offer_payload", ("payload_domain", "payload_canonical_url",
                           "payload_destination_url")),
        ("product_seed", ("product_seed_domain", "product_seed_canonical_url",
                          "product_seed_destination_url")),
    ):
        for field in fields:
            # plausible_domain gates every candidate: the backfill is fill-only,
            # so a placeholder that parses ('N/A' -> 'n', 'unknown') would be a
            # PERMANENT junk write — and it must not shadow a good URL host in a
            # later field either.
            domain = normalize_domain(row.get(field))
            if plausible_domain(domain):
                return domain, rule
    attached = sorted({normalize_domain(d) for d in (row.get("attached_domains") or [])
                       if plausible_domain(normalize_domain(d))})
    if len(attached) == 1:
        return attached[0], "attached_seed_unique"
    if len(attached) > 1:
        return None, "attached_seed_ambiguous"
    return None, "unresolved"


def _confident(rule: str, include_attached: bool) -> bool:
    return rule in CONFIDENT_RULES or (include_attached and rule == "attached_seed_unique")


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        rows = [dict(r) for r in await database.fetch_all(
            _SCAN_SQL, {"sources": list(_SEED_SOURCES)})]
        print(f"domain-less external-seed offers: {len(rows)}")
        out_of_scope = await database.fetch_all(
            _OUT_OF_SCOPE_SQL, {"sources": list(_SEED_SOURCES)})
        for r in out_of_scope:
            print(f"NOTE: {r['offers']} domain-less offers under "
                  f"source_system={r['source_system']} are OUT OF SCOPE here "
                  f"(not an ingest-defaulted currency lane)")

        # Derive per offer, group per derived domain.
        groups: Dict[str, Dict[str, Any]] = {}
        unresolved = Counter()
        for row in rows:
            domain, rule = derive_offer_domain(row)
            if not domain:
                unresolved[rule] += 1
                continue
            g = groups.setdefault(domain, {
                "domain": domain, "offers": 0, "live": 0, "suppressed": 0,
                "currencies": set(), "rules": Counter(), "max_price": 0.0,
                "offer_ids": [], "backfill_ids": [],
            })
            g["offers"] += 1
            g["suppressed" if row["is_suppressed"] else "live"] += 1
            if row.get("currency"):
                g["currencies"].add(row["currency"])
            g["rules"][rule] += 1
            try:
                g["max_price"] = max(g["max_price"], float(row.get("list_price") or 0))
            except (TypeError, ValueError):
                pass
            g["offer_ids"].append(row["offer_id"])
            if _confident(rule, args.include_attached):
                g["backfill_ids"].append(row["offer_id"])

        print(f"derived {sum(g['offers'] for g in groups.values())} offers across "
              f"{len(groups)} storefront domains; unresolved: {dict(unresolved) or 0}\n")

        # Verify the derived storefronts, same shape as the main audit.
        checkable = [g for g in groups.values() if g["offers"] >= args.min_offers]
        mismatches: List[Dict[str, Any]] = []
        wholesale: List[Dict[str, Any]] = []
        unresolvable_meta = 0
        sem = asyncio.Semaphore(args.concurrency)

        async def check(g: Dict[str, Any]) -> None:
            nonlocal unresolvable_meta
            if g["domain"].split(".")[0] == "wholesale":
                wholesale.append(g)
                return
            async with sem:
                meta = await fetch_storefront_meta(g["domain"])
            if not meta:
                unresolvable_meta += 1
                return
            wrong = [c for c in sorted(g["currencies"]) if currency_mismatch(c, meta)]
            if wrong:
                g["actual_currency"] = meta.get("currency")
                g["country"] = meta.get("country")
                g["wrong_currencies"] = wrong
                mismatches.append(g)

        await asyncio.gather(*(check(g) for g in checkable))

        print(f"=== CURRENCY MISMATCH (derived domains): {len(mismatches)} ===")
        for m in sorted(mismatches, key=lambda x: -x["offers"]):
            print(f"  {m['domain'][:34]:34} stamped={','.join(m['wrong_currencies'])} "
                  f"actual={m['actual_currency']} ({m.get('country')}) "
                  f"offers={m['offers']:>4} (live={m['live']} suppressed={m['suppressed']}) "
                  f"max={m['max_price']:.0f} rules={dict(m['rules'])}")
        print(f"=== WHOLESALE/B2B (derived domains): {len(wholesale)} ===")
        for w in wholesale:
            print(f"  {w['domain'][:34]:34} offers={w['offers']:>4} max={w['max_price']:.0f}")
        print(f"(checked {len(checkable)} domains with >= {args.min_offers} offers; "
              f"{unresolvable_meta} storefronts unresolvable via /meta.json)")

        # Observe-only price-magnitude alert. The confirmed member of this class
        # — Oiad's ₩400,000 crawled into a USD-stamped price — is INVISIBLE to
        # the stamped-vs-base-currency check above, because oiad.us is a KR
        # Shopify-Markets store whose /meta.json base currency IS 'USD'. When
        # the label and the base currency agree, the only remaining smoke is a
        # price whose magnitude belongs to another currency. This never decides
        # anything (per the standing lesson: price is a detector, not a decision
        # basis) — it lists the rows a human should eyeball.
        def _price(r: Dict[str, Any]) -> float:
            try:
                return float(r.get("list_price") or 0)
            except (TypeError, ValueError):
                return 0.0

        alerts = [(r, derive_offer_domain(r)[0]) for r in rows
                  if r.get("currency") == "USD" and _price(r) >= args.price_alert]
        alerts.sort(key=lambda t: -_price(t[0]))
        print(f"\n=== IMPLAUSIBLE-PRICE REVIEW LIST: {len(alerts)} USD-stamped rows "
              f">= {args.price_alert:.0f} ===")
        for r, domain in alerts[:20]:
            print(f"  {str(domain or '?')[:30]:30} {_price(r):>12.2f} USD "
                  f"{'suppressed' if r['is_suppressed'] else 'live':>10} "
                  f"offer={r['offer_id']}")
        if len(alerts) > 20:
            print(f"  ... and {len(alerts) - 20} more (raise --price-alert to narrow)")

        # The durable fix: what a backfill would write.
        mapping = [{"offer_id": oid, "source_domain": g["domain"]}
                   for g in groups.values() for oid in g["backfill_ids"]]
        backfill_domains = sum(1 for g in groups.values() if g["backfill_ids"])
        print(f"\nsource_domain backfill candidates: {len(mapping)} offers across "
              f"{backfill_domains} domains "
              f"(confident rules: {', '.join(CONFIDENT_RULES)}"
              f"{' + attached_seed_unique' if args.include_attached else ''})")

        if not args.apply:
            print(f"\n(DRY-RUN — pass --apply --confirm {CONFIRM_TOKEN} to backfill "
                  f"source_domain; add --quarantine-mismatched to also quarantine "
                  f"the {len(mismatches) + len(wholesale)} offender domains)")
            return 0
        if args.confirm != CONFIRM_TOKEN:
            print(f"\nREFUSED: --apply requires --confirm {CONFIRM_TOKEN}")
            return 2
        if args.max_offers and len(mapping) > args.max_offers:
            print(f"\nREFUSED: {len(mapping)} offers exceeds --max-offers "
                  f"{args.max_offers}; inspect the dry-run before widening.")
            return 2

        written = 0
        if mapping:
            # RETURNING + fetch_all: database.execute() yields None for an
            # UPDATE without RETURNING — counting it would report 0 forever.
            updated = await database.fetch_all(_BACKFILL_SQL, {
                "mappings": json.dumps(mapping, sort_keys=True),
                "sources": list(_SEED_SOURCES)})
            written = len(updated)
        print(f"\nAPPLIED: source_domain backfilled on {written} offers "
              f"(of {len(mapping)} mapped; the difference is rows another writer "
              f"filled first — fill-only, never overwritten)")
        print("NOTE: these offers are now covered by scripts/audit_offer_currency.py "
              "and the weekly derive-offer-market-currency workflow.")

        if not args.quarantine_mismatched:
            return 0
        offenders = mismatches + wholesale
        if not offenders:
            print("no offender domains to quarantine.")
            return 0
        if args.max_quarantine and len(offenders) > args.max_quarantine:
            print(f"REFUSED quarantine: {len(offenders)} offenders exceeds "
                  f"--max-quarantine {args.max_quarantine}; inspect before widening.")
            return 2
        for o in offenders:
            reason = (
                f"currency mismatch on seed-derived domain: stamped "
                f"{','.join(o.get('wrong_currencies', []))} but storefront is "
                f"{o.get('actual_currency')}" if o.get("actual_currency")
                else "wholesale/B2B channel not valid for the consumer index"
            )
            try:
                res = await create_quarantine(
                    match_type=MATCH_TYPE_DOMAIN,
                    match_value=o["domain"],
                    reason=reason, created_by=args.created_by, db=database)
                print(f"  quarantined {o['domain']} -> id={getattr(res, 'quarantine_id', res)}")
            except Exception as exc:  # noqa: BLE001 — one domain must not stop the sweep
                print(f"  WARN quarantine failed {o['domain']}: {str(exc)[:120]}")
        print("REMINDER: quarantine alone never clears the served surface — "
              "live_read/overrides are a separate, explicit step.")
        return 0
    finally:
        if own:
            await database.disconnect()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Currency-audit domain-less external-seed offers via seed provenance.")
    p.add_argument("--min-offers", type=int, default=1,
                   help="only /meta.json-check derived domains with at least this many offers")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--price-alert", type=float, default=1000.0,
                   help="report (never act on) USD-stamped rows priced at or above "
                        "this — catches wrong-magnitude prices the base-currency "
                        "check cannot see (Markets stores with a USD base)")
    p.add_argument("--created-by", default="audit_domainless_offer_currency")
    p.add_argument("--include-attached", action="store_true",
                   help="treat a UNANIMOUS attached-seed domain as confident for the "
                        "backfill (attachment is product-level; default excludes it)")
    p.add_argument("--apply", action="store_true",
                   help="backfill source_domain onto confidently-derived offers")
    p.add_argument("--confirm", default="", help=f"required with --apply: {CONFIRM_TOKEN}")
    p.add_argument("--max-offers", type=int, default=6000,
                   help="refuse --apply if more than this many offers would be backfilled (0=off)")
    p.add_argument("--quarantine-mismatched", action="store_true",
                   help="with --apply: also quarantine domains proven mispriced/wholesale")
    p.add_argument("--max-quarantine", type=int, default=20,
                   help="refuse quarantine if more than this many domains (0=off)")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
