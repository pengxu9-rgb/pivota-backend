"""Capture genuine Shopify-Markets USD offers for no_us_offer-blocked products.

THE COHORT (measured prod 2026-08-14). 963 content_keys carry blocker_code
'no_us_offer'; every one has an unsuppressed, priced offer in a genuine foreign
currency (GBP 680, EUR 149, JPY 53, ...) across 36 storefront domains — the
honest residue of the currency-relabeling arc (#1636/#1642). All 775 with a
quality snapshot score >= 71.4: high-quality content blocked purely on market.
A local probe of all 36 domains found 19 (573 keys) present REAL merchant-set
USD prices to US buyers via Shopify Markets.

WHAT THIS WRITES. A SIBLING first-party offer row per captured product —
market='US', currency='USD', the price the store itself quotes a US buyer —
alongside the untouched foreign offer. Identity/offer separation is the
architecture (see scripts/attach_retailer_offer.py): US-buyability arrives as
an attached offer, never by rewriting the home-market offer (that would undo
the currency-honesty work). `has_us_offer` then passes through the existing
gate with no gate change.

CAPTURE MECHANICS (probed, not assumed). A Shopify Markets store switches a
session's country via POST /localization as MULTIPART form data with
`_method=put` — a urlencoded POST without it silently no-ops and the session
stays home-country. Verification is positive-only: the session's /cart.js must
report currency == 'USD' BEFORE any product price is read, and every price is
read from /products/<handle>.js inside that session. A store that will not
localize to USD contributes nothing (counted, never guessed).

SAFETY RULES:
  * The foreign offer row is never modified. Only a NEW offer_id namespace
    ("offer:us_market:" + sha) is written; it cannot collide with the mirror's
    per-product first-party id ("offer:external_seed:" + sha).
  * Prices come only from the store's own US-context quote. No conversion, no
    estimate, no fabrication. A product whose handle 404s (delisted) or whose
    US price is unavailable/zero is skipped and counted.
  * ON CONFLICT (offer_id) refreshes the mutable price/availability columns, so
    re-runs are idempotent price refreshes, not duplicates.
  * Dry-run is the default and prints per-domain coverage + a full plan of what
    --apply would write. --apply also republishes each touched content_key via
    the canonical refresh and recomputes serving eligibility.

THE SKU PRECONDITION (added 2026-09-08). This lane wrote 529 LIVE ORPHAN OFFERS
on prod — rows naming `<product_key>::canonical` with no `catalog_skus` row
behind it — because `plan_offer` DERIVES that sku_key and the upsert then
ASSUMES the mirror already wrote it. It often has not: the candidate query
selects any product carrying a foreign-priced offer, and several lanes
(Path C ingest, the crawl onboarder, the enrichment apply) produce a
catalog_products row and a catalog_offers row without ever minting the mirror's
canonical SKU spelling. An orphan offer is invisible to every sku-joined read
lane (`pivot_query_service` INNER JOINs `catalog_skus`) while still counting as
supply to everything that reads `catalog_offers` alone.

THE FIX IS TO WRITE THE SKU, NOT TO REFUSE THE OFFER, and the choice is
different from the one `scripts/attach_retailer_offer.py` makes. Here the SKU is
fully derivable from data we already hold and have already validated: the
catalog_products row supplies merchant_id, platform, source_product_id, title
and source_domain, and the identity is the product itself (one canonical
"the product is the variant" SKU, `source_variant_id = product_key`) — the same
shape ingestion writes. Refusing instead would drop US-buyability for the entire
`no_us_offer` cohort this lane exists to recover, over a row we can write
correctly. `attach_retailer_offer` refuses because its product_key is
operator-supplied and its merchant is the RETAILER, so it has no such identity
to mint; see that file.

`ON CONFLICT` targets the IDENTITY INDEX (merchant_id, platform, product_key,
source_variant_id) and never `(sku_key)` — #2135's rationale: the identity tuple
is what "the same variant" means, so an existing row under another lane's
spelling must be ADOPTED (its sku_key comes back from RETURNING and the offer is
written against THAT key), not shadowed by a rival row for the same variant.
When the mint returns nothing the identity exists on a SUPPRESSED row, and the
offer is REFUSED and counted as `offers_refused_no_sku` — attaching live supply
to a gated identity creates supply nothing can surface.

A SUPPRESSED PRODUCT IS REFUSED OUTRIGHT (added after review). The lane read
`catalog_products.suppressed_at` nowhere: not in the candidate scan, not in the
mint, not in the offer upsert. A product somebody had withdrawn therefore went
all the way through — `skus_minted: 1`, live offer written — and re-created
`suppressed_product_with_live_offer`, the class the reconciler's cascade pass had
just drained. All three statements now carry `cp.suppressed_at IS NULL`, and a
product retired during the minutes of HTTP probing is counted as
`offers_refused_product_suppressed` rather than written.

Usage
-----
  python3 scripts/capture_us_market_offers.py                 # dry run
  python3 scripts/capture_us_market_offers.py --apply
  python3 scripts/capture_us_market_offers.py --apply --domain palmofferonia.com

THROUGH THE JOB RUNNER, PASS SUBNET=pivota-crawl. This script fetches `/`, `/localization`,
`/cart.js` and `/products/<handle>.js` from 36 merchant storefronts at REQUEST_GAP_SECONDS=0.6,
which is close to the shape that was measured tripping a cross-domain IP-level 429 lasting ~15
minutes (~50 requests over 37 Cloudflare-fronted domains in about a minute). prod's `default`
subnet egresses from 8.231.167.230, the address given to payment partners for allowlisting, and
NAT port exhaustion is per-IP — so a burst here can starve payment egress even with clean
reputation. `pivota-crawl` egresses from 34.82.199.35 instead:

  SUBNET=pivota-crawl scripts/ops/run_oneoff_job.sh scripts/capture_us_market_offers.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_offer_writer_guard import (  # noqa: E402
    ORPHAN_NO_SKU,
    WriterAuditAccumulator,
    fetch_live_catalog_sku_keys,
    guard_catalog_offer_rows,
    make_batch_id,
    write_writer_audit_log,
)
from services.external_offer_dual_write import (  # noqa: E402
    derive_mirror_sku_key,
)
from services.variant_identity import PRODUCT_DERIVED  # noqa: E402

REFRESH_SOURCE = "us_market_offer_capture"
SOURCE_SYSTEM = "us_market_capture"
OFFER_ID_PREFIX = "offer:us_market:"

#: One line, fenced. `scripts/ops/run_oneoff_job.sh` reads a job's output from
#: Cloud Logging, which DROPS LINES — a multi-line report arrives with arbitrary
#: keys missing and nothing saying so. The per-domain progress prints stay (they
#: are progress, not the result); this line is the result.
REPORT_BEGIN = "USMKTREPORT>>>"
REPORT_END = "<<<USMKTREPORT"
REQUEST_GAP_SECONDS = 0.6
# Re-verify the session is still presenting USD every N product reads; a
# decayed localization cookie would silently relabel home-currency prices as
# USD — the exact defect class (#1636/#1642) this lane must never reintroduce.
SESSION_RECHECK_EVERY = 25

# Blocked products with a foreign-priced offer, one row per (product, domain).
# canonical_url supplies the Shopify handle (its final path segment).
#
# `cp.suppressed_at IS NULL` IS PART OF THE COHORT, not a tidy-up. A suppressed
# product is one somebody withdrew; capturing a US price for it and writing a
# LIVE offer against it re-creates `suppressed_product_with_live_offer` — the
# exact class `services/catalog_offer_suppression` and the reconciler's cascade
# pass exist to drain (2,171 rows on prod 2026-09-08). Measured before this
# line existed: a suppressed product went through the whole lane and reported
# `skus_minted: 1` with a live offer written, so the morning after the
# reconciler drained the class it regrew from here.
CANDIDATES_SQL = """
    SELECT DISTINCT ips.content_key, cp.product_key, cp.merchant_id,
           cp.canonical_url, co.source_domain
    FROM index_pipeline_state ips
    JOIN catalog_products cp ON cp.content_key = ips.content_key
    JOIN catalog_offers co ON co.product_key = cp.product_key
    WHERE ips.blocker_code = 'no_us_offer'
      AND cp.suppressed_at IS NULL
      AND co.suppressed_at IS NULL
      AND coalesce(co.merchant_effective_price, co.list_price) > 0
      AND upper(trim(coalesce(co.currency, ''))) <> 'USD'
      AND co.source_domain IS NOT NULL
      AND cp.canonical_url IS NOT NULL
    ORDER BY co.source_domain, cp.product_key
"""

#: The products among a planned batch that are SUPPRESSED. CANDIDATES_SQL
#: already excludes them, but minutes of HTTP probing separate that scan from
#: the write and a nightly retirement lane runs in between — the same race that
#: produced the ADR-009 sentinel orphans. Read in BOTH modes so a plan cannot
#: promise offers --apply then refuses.
SUPPRESSED_PRODUCT_PROBE_SQL = """
    SELECT cp.product_key
      FROM catalog_products cp
     WHERE cp.product_key = ANY(:product_keys)
       AND cp.suppressed_at IS NOT NULL
"""

# Same column set as the mirror/attach writers; ON CONFLICT refreshes only the
# mutable price/availability facts — identity/provenance columns stay put.
#
# merchant_id is READ FROM THE CATALOG ROW IN THIS STATEMENT, not carried from
# the candidate row. CANDIDATES_SQL is fetched once, then every domain is
# probed over HTTP for minutes before any upsert runs, so a candidate's
# merchant_id is a snapshot that a concurrent re-key invalidates. That happened:
# the A9-4 flip moved these products and cascaded catalog_offers while the
# 2026-08-14 02:09-02:23 capture was probing, and 12 offers landed after the
# cascade carrying the retired sentinel — orphans no cascade could reach
# (ADR-009; repaired by scripts/dispose_sentinel_orphans.py, which was needed
# precisely because ON CONFLICT DO UPDATE below does not touch merchant_id, so
# re-running the capture could never have fixed them).
#
# The subselect makes the write self-consistent: whatever the catalog says at
# COMMIT time is what the offer carries. A product that vanished between the
# scan and the write inserts nothing rather than inventing a seller — the
# INSERT ... SELECT yields no row, which is the fail-closed direction.
#
# `cp.suppressed_at IS NULL` in the SELECT source is the FAIL-CLOSED backstop for
# the cohort filter: a product retired between the scan and the write yields no
# row and no offer, rather than a live offer on a withdrawn product. The write is
# counted from `RETURNING offer_id`, never assumed — `databases` + asyncpg gives
# no rowcount from `execute()`, so a caller that incremented a counter beside the
# call would report `written: 1` for a statement that inserted nothing.
#
# THE DO UPDATE REPOINTS `sku_key` AND LIFTS EXACTLY ONE TOMBSTONE. Measured:
# after the reconciler suppressed this lane's 529 orphans as `orphan_no_sku`, a
# re-run refreshed price and reported `written: 1` while the row stayed gated on
# the dead key — the offer was fixed everywhere except where it counts.
# `orphan_no_sku` is the ONE label whose cause this very statement has just
# removed (the SKU now exists; `ensure_skus_for_planned` proved it), so it is the
# only one cleared. A `product_suppressed`, `duplicate_offer` or currency-
# quarantine tombstone is somebody else's live decision and stays put — reviving
# those would make this writer a blanket un-suppressor.
OFFER_UPSERT_SQL = """
    INSERT INTO catalog_offers
      (offer_id, sku_key, product_key, merchant_id,
       catalog_track, truth_tier, readiness_tier,
       offer_type, is_first_party, offer_mode,
       market, channel, availability, currency,
       list_price, price_confidence,
       source_system, source_domain, offer_payload)
    SELECT
       :offer_id, :sku_key, CAST(:product_key AS text), cp.merchant_id,
       'external_referral', 'observed', 'referral_only',
       'brand_direct', TRUE, 'redirect',
       'US', 'external_referral', :availability, 'USD',
       :list_price, 0.9,
       :source_system, :source_domain, CAST(:offer_payload AS jsonb)
      FROM catalog_products cp
     WHERE cp.product_key = CAST(:product_key AS text)
       AND cp.suppressed_at IS NULL
    ON CONFLICT (offer_id) DO UPDATE SET
      availability = EXCLUDED.availability,
      list_price = EXCLUDED.list_price,
      offer_payload = EXCLUDED.offer_payload,
      -- The seller follows the catalog on refresh too; without this an offer
      -- written before a re-key keeps the stale merchant forever.
      merchant_id = EXCLUDED.merchant_id,
      -- The KEY the offer hangs on follows too: an adoption (or a mint after a
      -- reconciler sweep) changes which catalog_skus row is correct, and a
      -- refresh that left the old spelling in place would refresh an orphan.
      sku_key = EXCLUDED.sku_key,
      suppressed_at = CASE
          WHEN catalog_offers.suppression_reason = 'orphan_no_sku'
          THEN NULL ELSE catalog_offers.suppressed_at END,
      suppression_reason = CASE
          WHEN catalog_offers.suppression_reason = 'orphan_no_sku'
          THEN NULL ELSE catalog_offers.suppression_reason END,
      -- The reconciler's own stamp goes with the tombstone it belongs to; a
      -- `reconcile_batch_id` left behind would make `--revert-batch` count a row
      -- it can no longer restore.
      suppression_metadata = CASE
          WHEN catalog_offers.suppression_reason = 'orphan_no_sku'
          THEN coalesce(catalog_offers.suppression_metadata, '{}'::jsonb)
               - 'reconcile_batch_id' - 'reconcile_pass'
               - 'reconcile_keeper_offer_id'
          ELSE catalog_offers.suppression_metadata END,
      updated_at = NOW()
    RETURNING offer_id
"""


# The canonical SKU this lane's offer needs behind it, written in the SHAPE
# INGESTION WRITES (services/external_offer_dual_write + the Path B mirror):
# one synthetic "the product is the variant" row per product, with
# `source_variant_id = product_key`.
#
# INSERT ... SELECT FROM catalog_products for the same reason the offer upsert
# does it: every identity column is read from the catalog row AT WRITE TIME, so a
# product that was re-keyed (or vanished) during the minutes of HTTP probing
# between the scan and the write yields NO row rather than a SKU minted under a
# stale seller. `merchant_id`, `platform` and `source_product_id` are NOT NULL on
# catalog_skus, so a bind-carried snapshot of them is exactly the ADR-009 orphan
# this lane already produced once.
#
# `cp.suppressed_at IS NULL` for the same reason CANDIDATES_SQL carries it: a
# product withdrawn between the scan and the write must mint NOTHING, so this
# lane can never be the thing that re-creates a live SKU (and then a live offer)
# under a tombstoned product. THROUGH THIS MODULE'S OWN CALL PATH IT IS
# RACE-ONLY: `ensure_skus_for_planned` runs SUPPRESSED_PRODUCT_PROBE_SQL first
# and drops a suppressed product's rows before any mint, so this predicate is
# reached only when the product is suppressed in the window between that probe
# and this INSERT. The test that pins it stubs the probe to return nothing, which
# is the only way to drive this statement against a suppressed product.
#
# THE DO UPDATE's WHERE IS THE FIRST OF TWO GUARDS on a suppressed identity, and
# the two are pinned separately. Here, a suppressed identity row returns NO
# `sku_key`, so the caller refuses BEFORE adopting it (`skus_adopted_other_key`
# stays 0). If this WHERE were lost, the caller would adopt the suppressed row
# and `guard_catalog_offer_rows(live_only=True)` would then refuse the offer —
# same outcome, `skus_adopted_other_key` 1. Both tests exist because with only
# "the offer was refused" asserted, either guard could be deleted alone and every
# test would still pass.
#
# ON CONFLICT targets the 4-column identity index, never the sku_key primary key
# — see the module docstring. DO UPDATE touches only `updated_at`: adopting
# another lane's existing identity row must not restamp its title, payload or
# provenance with ours. The WHERE makes a SUPPRESSED identity return NOTHING, and
# the caller turns that into a refusal.
MINT_CANONICAL_SKU_SQL = """
    INSERT INTO catalog_skus
      (sku_key, product_key, merchant_id, platform,
       source_product_id, source_variant_id, source_domain,
       sku, barcode, title, currency, image_url,
       visible_attributes, visible_option_labels, ingredient_ids,
       sku_payload, readiness_tier, updated_at)
    SELECT
       CAST(:sku_key AS text), cp.product_key, cp.merchant_id, cp.platform,
       cp.source_product_id, cp.product_key, cp.source_domain,
       NULL, NULL, cp.title, 'USD', NULL,
       '{}'::jsonb, '[]'::jsonb, '[]'::jsonb,
       CAST(:sku_payload AS jsonb), 'referral_only', NOW()
      FROM catalog_products cp
     WHERE cp.product_key = CAST(:product_key AS text)
       AND cp.suppressed_at IS NULL
    ON CONFLICT (merchant_id, platform, product_key, source_variant_id) DO UPDATE SET
       updated_at = NOW()
     WHERE catalog_skus.suppressed_at IS NULL
       AND catalog_skus.suppression_reason IS NULL
    RETURNING sku_key
"""

# The DRY-RUN twin of the refusal above. A plan that reported only "N SKUs to
# mint" would silently promise offers that --apply then refuses, so the identities
# already sitting on a suppressed row are counted read-only here too.
SUPPRESSED_IDENTITY_PROBE_SQL = """
    SELECT s.product_key
      FROM catalog_skus s
     WHERE s.product_key = ANY(:product_keys)
       AND s.source_variant_id = s.product_key
       AND (s.suppressed_at IS NOT NULL OR s.suppression_reason IS NOT NULL)
"""


def derive_us_offer_id(product_key: str) -> str:
    """Deterministic id in a namespace disjoint from the mirror's
    "offer:external_seed:" prefix — same product_key, different offer row."""
    digest = hashlib.sha256(product_key.encode("utf-8")).hexdigest()[:32]
    return f"{OFFER_ID_PREFIX}{digest}"


def handle_from_url(url: Optional[str]) -> Optional[str]:
    """The Shopify product handle: final non-empty path segment of the product
    URL, query/fragment stripped. None when the URL has no usable path — a
    caller must skip, never guess."""
    if not url or not isinstance(url, str):
        return None
    path = urlsplit(url.strip()).path
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    handle = segments[-1]
    # A bare domain or a collections index is not a product URL.
    if handle in ("products", "collections") or not handle.strip():
        return None
    return handle


def _host(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    host = (urlsplit(url.strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else (host or None)


def select_capturable(candidates: List[Dict[str, Any]],
                      ) -> Tuple[Dict[str, List[Dict[str, Any]]],
                                 List[Tuple[str, str]]]:
    """Partition candidates into {domain: rows} safe to capture, plus counted
    skips. Two refusals, both wrong-price-publish vectors:

      * domain_mismatch — the handle comes from canonical_url but the fetch
        goes to the offer's source_domain; Path-C attaches sibling-domain
        seeds, so a mismatched pair would price the WRONG store's product.
      * ambiguous_domains — one product reachable via several domains would
        plan the same offer_id more than once and let sort order pick the
        surviving price; like the reattribution matcher, ambiguity is
        rejected, never resolved by accident.
    """
    by_product: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        by_product.setdefault(c["product_key"], []).append(c)

    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    skipped: List[Tuple[str, str]] = []
    for product_key, rows in sorted(by_product.items()):
        # Normalize BOTH sides: a www./case/whitespace variant in the stored
        # source_domain must not fake a mismatch or split one domain into two.
        domains = {_host(f"https://{r['source_domain']}") for r in rows}
        if len(domains) > 1:
            skipped.append((product_key, "ambiguous_domains"))
            continue
        row = rows[0]
        domain = domains.pop()
        if not domain or _host(row["canonical_url"]) != domain:
            skipped.append((product_key, "domain_mismatch"))
            continue
        by_domain.setdefault(domain, []).append(row)
    return by_domain, skipped


def plan_offer(candidate: Dict[str, Any], price_cents: int,
               available: bool) -> Optional[Dict[str, Any]]:
    """Build the upsert params for one captured US price, or None when the
    price is not a real buyable quote (<= 0). Prices arrive in cents from
    /products/<handle>.js and are stored in currency units."""
    if price_cents is None or price_cents <= 0:
        return None
    product_key = candidate["product_key"]
    payload = {
        "capture": "shopify_markets_us_localization",
        "captured_from": candidate["source_domain"],
        "destination_url": candidate["canonical_url"],
        "price_cents": price_cents,
    }
    return {
        "offer_id": derive_us_offer_id(product_key),
        # The one canonical SKU: retailer/US offers deliberately SHARE the
        # mirror's sku row — a dangling sku_key would hide this offer from
        # every sku-joined read lane (pivot_query_service INNER JOINs on it).
        "sku_key": derive_mirror_sku_key(product_key),
        "product_key": product_key,
        # NO merchant_id. The upsert reads the seller from the catalog row at
        # write time, and `databases`/SQLAlchemy text() rejects a params key
        # the statement does not name — passing the stale snapshot here would
        # raise ArgumentError on every write, not merely re-introduce the bug.
        "availability": "in_stock" if available else "out_of_stock",
        "list_price": price_cents / 100.0,
        "source_system": SOURCE_SYSTEM,
        "source_domain": candidate["source_domain"],
        "offer_payload": json.dumps(payload, ensure_ascii=False),
    }


async def _fetch_json(client: Any, url: str) -> Optional[Any]:
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:  # noqa: BLE001 — a storefront hiccup is a skip, not a crash
        return None


async def establish_us_session(client: Any, domain: str) -> bool:
    """Localize the session to the US market and PROVE it took effect: the
    positive signal is /cart.js reporting USD, never the POST's status."""
    try:
        await client.get(f"https://{domain}/")
        await client.post(
            f"https://{domain}/localization",
            files={
                "_method": (None, "put"),
                "country_code": (None, "US"),
                "return_to": (None, "/"),
            },
        )
    except Exception:  # noqa: BLE001
        return False
    cart = await _fetch_json(client, f"https://{domain}/cart.js")
    return bool(cart) and cart.get("currency") == "USD"


async def _session_is_usd(client: Any, domain: str) -> bool:
    cart = await _fetch_json(client, f"https://{domain}/cart.js")
    return bool(cart) and cart.get("currency") == "USD"


async def capture_domain(domain: str, candidates: List[Dict[str, Any]],
                         ) -> Dict[str, Any]:
    """Capture US prices for one domain's candidates inside one US session.

    Prices captured since the last PASSING /cart.js check are held in a
    pending buffer and committed only when the next check still reports USD —
    /products/<handle>.js carries no currency field, so a decayed session can
    only be detected out-of-band, and everything read since the last proof
    must be discarded, never trusted."""
    import httpx

    planned: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    skipped: List[Tuple[str, str]] = []

    async def checkpoint(client: Any) -> None:
        nonlocal planned, pending
        if not pending:
            return
        if await _session_is_usd(client, domain):
            planned.extend(pending)
        else:
            skipped.extend((r["product_key"], "us_session_lost") for r in pending)
        pending = []

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=20.0,
        headers={"User-Agent": "Mozilla/5.0 (compatible; PivotaBot/1.0)"},
    ) as client:
        if not await establish_us_session(client, domain):
            return {"domain": domain, "us_market": False,
                    "planned": [], "skipped": [(c["product_key"], "no_us_session")
                                               for c in candidates]}
        since_check = 0
        for c in candidates:
            handle = handle_from_url(c["canonical_url"])
            if not handle:
                skipped.append((c["product_key"], "no_handle_in_url"))
                continue
            pjs = await _fetch_json(
                client, f"https://{domain}/products/{handle}.js")
            await asyncio.sleep(REQUEST_GAP_SECONDS)
            if not pjs:
                skipped.append((c["product_key"], "product_js_unavailable"))
                continue
            row = plan_offer(c, pjs.get("price"), bool(pjs.get("available")))
            if row is None:
                skipped.append((c["product_key"], "no_positive_us_price"))
                continue
            row["content_key"] = c["content_key"]
            pending.append(row)
            since_check += 1
            if since_check >= SESSION_RECHECK_EVERY:
                await checkpoint(client)
                since_check = 0
        await checkpoint(client)
    return {"domain": domain, "us_market": True,
            "planned": planned, "skipped": skipped}


def _sku_payload(product_key: str, batch_id: str) -> str:
    """The provenance blob for a minted canonical SKU.

    `variant_id_provenance` is PRODUCT_DERIVED and nothing else: this identity is
    the product key, not an id the merchant issued. Stamping it `merchant_issued`
    to make the provenance share look better is the exact fabrication the
    `variant_id_provenance` vocabulary exists to prevent, and the new
    `skus_without_merchant_issued_identity_share` invariant would then read a
    number this writer invented.
    """
    return json.dumps(
        {
            "synthetic_canonical_variant": True,
            "source": SOURCE_SYSTEM,
            "variant_id_provenance": PRODUCT_DERIVED,
            "batch_id": batch_id,
        },
        ensure_ascii=False,
    )


async def ensure_skus_for_planned(
    planned: List[Dict[str, Any]], *, apply: bool, batch_id: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Guarantee every planned offer names a LIVE catalog_skus row on a LIVE product.

    Returns the rows that may be written (with `sku_key` rewritten to whatever
    the identity actually resolved to) plus the counters.

    THE ORDER IS: drop the suppressed products, look, then mint only what is
    missing, then re-read at the shared guard.

      0. `SUPPRESSED_PRODUCT_PROBE_SQL` — a suppressed product's offer must not be
         written at all, whatever its SKU situation. CANDIDATES_SQL already
         excludes them; this catches the ones retired during the minutes of HTTP
         probing, and it has to be a refusal of its OWN and not a side effect of
         the mint, because the `skus_existing` branch below never reaches the
         mint.
      1. `fetch_live_catalog_sku_keys` — the planned key may already exist, in
         which case there is nothing to mint. LIVE, not merely existing: a
         SUPPRESSED row holding the derived `<pk>::canonical` key counted as
         `existing` here and the offer was written against it (measured). Only
         the other-spelling case refused, and `%::canonical` is 39.4% of
         catalog_skus — so the refusal was decided by which spelling the
         suppressed row happened to carry. This also avoids the one case
         `ON CONFLICT` cannot help with: a row that already OWNS the sku_key
         under a DIFFERENT identity tuple (a re-key moved the merchant) would hit
         the sku_key PRIMARY KEY, which is not the arbiter, and raise.
      2. mint the rest, adopting the returned key.
      3. `guard_catalog_offer_rows(..., live_only=True)`, the shared chokepoint
         that already knows how to say ORPHAN_NO_SKU. THIS LANE BYPASSED IT
         ENTIRELY — it builds and executes its own INSERT and never went near the
         guard, which is why the guard's existence did not stop 529 live orphans.
         It is a BACKSTOP, not the gate that catches what nothing else does: when
         steps 1-2 are correct it re-reads the same keys and refuses nothing, by
         construction. What it buys is that a future mint bug is REFUSED rather
         than written, and refused in the vocabulary every other writer already
         reports. `live_only=True` so it asks the same question step 1 does.
         Pinned on its own: a test runs the mint WITHOUT its DO UPDATE WHERE (the
         future bug) and expects this step to refuse what that mint adopted.
    """
    counts = {"skus_existing": 0, "skus_to_mint": 0, "skus_minted": 0,
              "skus_adopted_other_key": 0, "offers_refused_no_sku": 0,
              "offers_refused_product_suppressed": 0}
    if not planned:
        return [], counts

    suppressed_products = {
        str(row["product_key"]) for row in (await database.fetch_all(
            SUPPRESSED_PRODUCT_PROBE_SQL,
            {"product_keys": sorted({r["product_key"] for r in planned})},
        ) or [])
    }
    if suppressed_products:
        counts["offers_refused_product_suppressed"] = sum(
            1 for r in planned if r["product_key"] in suppressed_products)
        for key in sorted(suppressed_products):
            print(f"  [SKIP] product suppressed: {key[:60]}", flush=True)
        planned = [r for r in planned if r["product_key"] not in suppressed_products]
    if not planned:
        return [], counts

    existing = await fetch_live_catalog_sku_keys([r["sku_key"] for r in planned])
    missing = [r for r in planned if r["sku_key"] not in existing]
    counts["skus_existing"] = len(planned) - len(missing)
    counts["skus_to_mint"] = len(missing)

    if not apply:
        # Read-only: the identities that ALREADY exist suppressed are the ones
        # --apply will refuse. Reporting them here keeps the plan and the run
        # from disagreeing about how many offers land.
        rows = await database.fetch_all(
            SUPPRESSED_IDENTITY_PROBE_SQL,
            {"product_keys": sorted({r["product_key"] for r in missing})},
        )
        blocked = {str(row["product_key"]) for row in (rows or [])}
        counts["offers_refused_no_sku"] = sum(
            1 for r in missing if r["product_key"] in blocked
        )
        return [r for r in planned if r["product_key"] not in blocked], counts

    accepted: List[Dict[str, Any]] = []
    for row in planned:
        if row["sku_key"] in existing:
            accepted.append(row)
            continue
        written_key = await database.fetch_val(
            MINT_CANONICAL_SKU_SQL,
            {"sku_key": row["sku_key"], "product_key": row["product_key"],
             "sku_payload": _sku_payload(row["product_key"], batch_id)},
        )
        if written_key is None:
            # Either the product vanished or was suppressed between the probe
            # above and now (the INSERT ... SELECT yielded no row) or the
            # identity lives on a suppressed SKU. All are "no live identity to
            # hang this on".
            counts["offers_refused_no_sku"] += 1
            print(f"  [SKIP] no live SKU identity for {row['product_key'][:60]}",
                  flush=True)
            continue
        if str(written_key) != row["sku_key"]:
            # The identity already existed under another lane's spelling. Adopt
            # it — writing our spelling would be a second identity row for one
            # variant, which is what the 4-column index exists to forbid. AN
            # ADOPTION IS NOT A MINT: no catalog_skus row was created, and
            # counting it as one overstates what this writer produced (and would
            # make `skus_minted` disagree with a COUNT of the table after a run).
            counts["skus_adopted_other_key"] += 1
            row = dict(row, sku_key=str(written_key))
        else:
            counts["skus_minted"] += 1
        accepted.append(row)

    guarded, reasons, rejected = await guard_catalog_offer_rows(
        accepted, live_only=True)
    if rejected:
        counts["offers_refused_no_sku"] += int(reasons.get(ORPHAN_NO_SKU, 0))
        for bad in rejected:
            print(f"  [REFUSE] {bad.get('offer_id')}: {bad.get('reasons')}", flush=True)
    return guarded, counts


async def apply_offers(planned: List[Dict[str, Any]]) -> Dict[str, Any]:
    from services.agent_pdp_view_assembler import (
        refresh_agent_pdp_view_for_content_key,
    )
    from services.index_pipeline_state_service import (
        recompute_serving_eligibility,
    )

    written = 0
    not_written: List[str] = []
    republish_failed: List[str] = []
    for row in planned:
        params = {k: v for k, v in row.items() if k != "content_key"}
        # `fetch_val` for the RETURNING, not `execute`: the statement's SELECT
        # source can legitimately yield NO ROW (the product was retired or
        # re-keyed during the probe), and `databases` + asyncpg reports no
        # rowcount, so an unconditional `written += 1` beside an `execute()` is a
        # count of ATTEMPTS being reported as a count of WRITES.
        landed = await database.fetch_val(OFFER_UPSERT_SQL, params)
        if landed is None:
            not_written.append(row["product_key"])
            print(f"  [SKIP] no live product row at write time for "
                  f"{row['product_key'][:60]}", flush=True)
            continue
        written += 1
        try:
            await refresh_agent_pdp_view_for_content_key(
                row["content_key"], refresh_source=REFRESH_SOURCE)
            await recompute_serving_eligibility(
                row["content_key"], reason=REFRESH_SOURCE)
        except Exception as exc:  # noqa: BLE001 — recorded for a manual retry
            republish_failed.append(row["content_key"])
            print(f"  [WARN] republish failed for {row['content_key']}: "
                  f"{type(exc).__name__}: {str(exc)[:80]}", flush=True)
    return {"written": written, "republish_failed": republish_failed,
            "not_written": not_written}


async def _drive(apply: bool, only_domain: Optional[str],
                 limit: Optional[int]) -> int:
    await database.connect()
    try:
        rows = [dict(r) for r in await database.fetch_all(CANDIDATES_SQL)]
    finally:
        await database.disconnect()

    by_domain, pre_skipped = select_capturable(rows)
    if only_domain:
        by_domain = {d: c for d, c in by_domain.items() if d == only_domain}
    pre_reasons: Dict[str, int] = {}
    for _, why in pre_skipped:
        pre_reasons[why] = pre_reasons.get(why, 0) + 1
    print(f"candidates={len(rows)} across {len(by_domain)} domains "
          f"(pre-skipped: {pre_reasons})", flush=True)

    all_planned: List[Dict[str, Any]] = []
    for domain in sorted(by_domain):
        cands = by_domain[domain]
        if limit is not None:
            remaining = limit - len(all_planned)
            if remaining <= 0:
                break
            cands = cands[:remaining]
        result = await capture_domain(domain, cands)
        reasons: Dict[str, int] = {}
        for _, why in result["skipped"]:
            reasons[why] = reasons.get(why, 0) + 1
        print(f"  {domain:<40} us_market={result['us_market']} "
              f"captured={len(result['planned'])} skipped={reasons}", flush=True)
        all_planned.extend(result["planned"])

    print(f"PLAN: {len(all_planned)} US offers", flush=True)
    for row in all_planned:
        print(f"  {row['product_key'][:60]:<62} ${row['list_price']:.2f}")

    audit = WriterAuditAccumulator(
        writer_name=SOURCE_SYSTEM, batch_id=make_batch_id(SOURCE_SYSTEM)
    )
    report: Dict[str, Any] = {
        "writer": SOURCE_SYSTEM,
        "batch_id": audit.batch_id,
        "applied": 1 if apply else 0,
        "candidates": len(rows),
        "domains": len(by_domain),
        "pre_skipped": pre_reasons,
        "planned": len(all_planned),
    }

    # The SKU precondition runs in BOTH modes: a dry run that skipped it would
    # report a plan of N offers while --apply writes fewer, which is the same
    # class of lie as a dropped report line.
    await database.connect()
    try:
        writable, sku_counts = await ensure_skus_for_planned(
            all_planned, apply=apply, batch_id=audit.batch_id
        )
        report.update(sku_counts)
        report["writable"] = len(writable)
        if not apply:
            report["written"] = 0
            report["not_written"] = 0
            report["republish_failed"] = 0
        else:
            summary = await apply_offers(writable)
            report["written"] = summary["written"]
            report["not_written"] = len(summary["not_written"])
            report["republish_failed"] = len(summary["republish_failed"])
            report["republish_failed_keys"] = summary["republish_failed"][:5]
            audit.record_applied(summary["written"])
            # EVERY `offers_refused_*` counter is a SKIP, matched by prefix. A
            # membership test against one name silently files the next refusal
            # reason under `record_info`, where it stops counting toward
            # `skipped_rows` and reads as progress.
            audit.record_skips({
                k: v for k, v in sku_counts.items()
                if k.startswith("offers_refused_") and v > 0
            })
            if summary["not_written"]:
                audit.record_skips({"offers_not_written": len(summary["not_written"])})
            audit.record_info({
                k: v for k, v in sku_counts.items()
                if not k.startswith("offers_refused_") and v > 0
            })
            audit.reasons["zero_counters"] = sorted(
                k for k, v in sku_counts.items() if v == 0
            )
            await write_writer_audit_log(audit)
    finally:
        await database.disconnect()

    if not apply:
        print("DRY-RUN — pass --apply to write the planned offers.")
    print(REPORT_BEGIN + json.dumps(report, sort_keys=True, default=str) + REPORT_END,
          flush=True)
    return 1 if report.get("republish_failed") else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true")
    p.add_argument("--domain", help="capture only this source_domain")
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of captured products")
    args = p.parse_args()
    if args.limit is not None and args.limit <= 0:
        p.error("--limit must be a positive integer")
    return asyncio.run(_drive(args.apply, args.domain, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
