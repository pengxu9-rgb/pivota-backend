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

Usage
-----
  python3 scripts/capture_us_market_offers.py                 # dry run
  python3 scripts/capture_us_market_offers.py --apply
  python3 scripts/capture_us_market_offers.py --apply --domain palmofferonia.com
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

REFRESH_SOURCE = "us_market_offer_capture"
SOURCE_SYSTEM = "us_market_capture"
OFFER_ID_PREFIX = "offer:us_market:"
REQUEST_GAP_SECONDS = 0.6

# Blocked products with a foreign-priced offer, one row per (product, domain).
# canonical_url supplies the Shopify handle (its final path segment).
CANDIDATES_SQL = """
    SELECT DISTINCT ips.content_key, cp.product_key, cp.merchant_id,
           cp.canonical_url, co.source_domain
    FROM index_pipeline_state ips
    JOIN catalog_products cp ON cp.content_key = ips.content_key
    JOIN catalog_offers co ON co.product_key = cp.product_key
    WHERE ips.blocker_code = 'no_us_offer'
      AND co.suppressed_at IS NULL
      AND coalesce(co.merchant_effective_price, co.list_price) > 0
      AND upper(trim(coalesce(co.currency, ''))) <> 'USD'
      AND co.source_domain IS NOT NULL
      AND cp.canonical_url IS NOT NULL
    ORDER BY co.source_domain, cp.product_key
"""

# Same column set as the mirror/attach writers; ON CONFLICT refreshes only the
# mutable price/availability facts — identity/provenance columns stay put.
OFFER_UPSERT_SQL = """
    INSERT INTO catalog_offers
      (offer_id, sku_key, product_key, merchant_id,
       catalog_track, truth_tier, readiness_tier,
       offer_type, is_first_party, offer_mode,
       market, channel, availability, currency,
       list_price, price_confidence,
       source_system, source_domain, offer_payload)
    VALUES
      (:offer_id, :sku_key, :product_key, :merchant_id,
       'external_referral', 'observed', 'referral_only',
       'brand_direct', TRUE, 'redirect',
       'US', 'online', :availability, 'USD',
       :list_price, 0.9,
       :source_system, :source_domain, CAST(:offer_payload AS jsonb))
    ON CONFLICT (offer_id) DO UPDATE SET
      availability = EXCLUDED.availability,
      list_price = EXCLUDED.list_price,
      offer_payload = EXCLUDED.offer_payload,
      updated_at = NOW()
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
        "sku_key": f"{product_key}::us_market",
        "product_key": product_key,
        "merchant_id": candidate["merchant_id"],
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


async def capture_domain(domain: str, candidates: List[Dict[str, Any]],
                         ) -> Dict[str, Any]:
    """Capture US prices for one domain's candidates inside one US session."""
    import httpx

    planned: List[Dict[str, Any]] = []
    skipped: List[Tuple[str, str]] = []
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=20.0,
        headers={"User-Agent": "Mozilla/5.0 (compatible; PivotaBot/1.0)"},
    ) as client:
        if not await establish_us_session(client, domain):
            return {"domain": domain, "us_market": False,
                    "planned": [], "skipped": [(c["product_key"], "no_us_session")
                                               for c in candidates]}
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
            planned.append(row)
    return {"domain": domain, "us_market": True,
            "planned": planned, "skipped": skipped}


async def apply_offers(planned: List[Dict[str, Any]]) -> Dict[str, Any]:
    from services.agent_pdp_view_assembler import (
        refresh_agent_pdp_view_for_content_key,
    )
    from services.index_pipeline_state_service import (
        recompute_serving_eligibility,
    )

    written = 0
    republish_failed: List[str] = []
    for row in planned:
        params = {k: v for k, v in row.items() if k != "content_key"}
        await database.execute(OFFER_UPSERT_SQL, params)
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
    return {"written": written, "republish_failed": republish_failed}


async def _drive(apply: bool, only_domain: Optional[str],
                 limit: Optional[int]) -> int:
    await database.connect()
    try:
        rows = [dict(r) for r in await database.fetch_all(CANDIDATES_SQL)]
    finally:
        await database.disconnect()

    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_domain.setdefault(r["source_domain"], []).append(r)
    if only_domain:
        by_domain = {d: c for d, c in by_domain.items() if d == only_domain}
    print(f"candidates={len(rows)} across {len(by_domain)} domains", flush=True)

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
    if not apply:
        print("DRY-RUN — pass --apply to write the planned offers.")
        return 0

    await database.connect()
    try:
        summary = await apply_offers(all_planned)
    finally:
        await database.disconnect()
    print(f"DONE: {json.dumps(summary)}")
    return 1 if summary["republish_failed"] else 0


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
