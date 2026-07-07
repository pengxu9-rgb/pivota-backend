"""Onboard a crawled (external-seed) brand's products into the live catalog so
they land BOTH decision-grade and SERVABLE, in one pass.

Complements scripts/ingest_crawled_inci.py (#855), which adds INCI to products
that already exist by product_key. This runner is the other entry point: it
CREATES the external-seed catalog product from crawl output and walks it all the
way to serving_eligible.

Per product it does:
  1. upsert an external_product_seeds row
  2. run the external_seed -> catalog_products mirror
  3. set category_kind + mark the brand-direct offer first-party
  4. INCI + enrichment via the SHARED crawled-INCI ingest
     (services.crawled_inci_ingest) -- one upsert-raw_inci + enrich_and_persist
     implementation for both crawl tools, so they don't drift
  5. make_external_seed_servable: attached_product_key back-link + quality
     snapshot + agent_pdp_view refresh + recompute eligibility

Idempotent throughout. Input: a JSON array on --file or stdin; each item:
  {external_product_id, brand, title, category_kind, product_type,
   destination_url, image_url, price_amount, description, raw_inci,
   offer_type?("brand_direct"|"retailer", default brand_direct),
   market?(default "US"), price_currency?(default "USD")}

market/price_currency carry the offer's real locale (e.g. a Korean D2C brand
sells in KRW) instead of being forced to US/USD -- baking a foreign price as USD
would corrupt the shared index. The mirror already propagates seed.market and
seed.price_currency to catalog_offers, so threading them here is sufficient.

Usage:
  python -m scripts.onboard_external_brand_from_crawl --file cohort.json --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from scripts.mirror_external_seeds_to_catalog_products import _apply as mirror_apply
from services.brand_claim_service import normalize_host
from services.catalog_sync_service import make_catalog_product_key
from services.crawled_inci_ingest import ingest_crawled_inci_items
from services.external_seed_servability import (
    build_servable_quality_payload,
    make_external_seed_servable,
)
from services.pdp_scope_classifier import ScopeSignals, classify
# ADR-009 D2/D3 (docs/adr/ADR-009-seller-of-record-identity.md; IDENTITY_REFERENCE
# §3 Trap T3, §4): a crawl-onboarded brand mints its own per-brand observed
# seller-of-record instead of landing under the banned 'external_seed' bucket,
# and the seed row records that seller (seller_ref/seed_kind) so T2 attribution
# keys conversions by seller.
from services.seller_identity import derive_seed_seller, ensure_observed_seller

TOOL = "external_brand_crawl"
# catalog_products.platform channel for external-seed supply (matches the mirror).
PLATFORM = "external_seed"


def _seed_id(epid: str) -> str:
    return f"{TOOL}::{epid}"


def _seed_domain(p: Dict[str, Any]) -> str:
    """Registrable host of the crawled brand site (its destination_url). This is
    the seller identity's domain half; it is also written to the seed's `domain`
    column so the mirror resolves the SAME observed seller for this product."""
    return normalize_host(p.get("destination_url"))


async def _resolve_seller_and_key(p: Dict[str, Any]) -> tuple[str, str]:
    """Resolve-or-mint this product's observed seller-of-record and its
    per-brand product_key. Deterministic on (brand, registrable-domain), so the
    key computed here MATCHES what the mirror writes for the same seed
    (ensure_observed_seller is idempotent). Raises on empty brand/domain — no
    fallback identity (ADR-009 D2)."""
    merchant_id = await ensure_observed_seller(
        brand=p.get("brand") or "",
        domain=_seed_domain(p),
        source_system=TOOL,
        primary_platform=PLATFORM,
    )
    return merchant_id, make_catalog_product_key(merchant_id, PLATFORM, p["external_product_id"])


async def _upsert_seed(
    p: Dict[str, Any],
    *,
    seller_ref: Optional[str],
    seed_kind: Optional[str],
) -> None:
    seed_data = {
        "snapshot": {
            "title": p["title"], "description": p.get("description"), "brand": p.get("brand"),
            "product_type": p.get("product_type"), "category": p.get("category_kind"),
        },
        "pdp_description_raw": p.get("description"),
        "pdp_ingredients_raw": p.get("raw_inci"),
        "inci_list": p.get("raw_inci"),
    }
    # ADR-009 D3: stamp the derived seller-of-record. A crawl-onboarded brand's
    # destination IS its own store, so this resolves to seed_kind='self',
    # seller_ref=<the observed seller>. NULL only if the brand/domain were
    # unmintable (derive_seed_seller logged loudly) — never assumed 'self'.
    await database.execute(
        """
        INSERT INTO external_product_seeds
          (id, external_product_id, market, tool, domain, destination_url, title, image_url,
           price_amount, price_currency, availability, seed_data, status, seller_ref, seed_kind)
        VALUES
          (:id, :epid, :market, :tool, :domain, :url, :title, :img,
           :price, :currency, 'in_stock', CAST(:data AS jsonb), 'active', :seller_ref, :seed_kind)
        ON CONFLICT (id) DO UPDATE SET
          title=EXCLUDED.title, image_url=EXCLUDED.image_url, price_amount=EXCLUDED.price_amount,
          price_currency=EXCLUDED.price_currency, market=EXCLUDED.market,
          domain=EXCLUDED.domain,
          seller_ref=COALESCE(EXCLUDED.seller_ref, external_product_seeds.seller_ref),
          seed_kind=COALESCE(EXCLUDED.seed_kind, external_product_seeds.seed_kind),
          seed_data=EXCLUDED.seed_data, status='active', updated_at=NOW()
        """,
        {"id": _seed_id(p["external_product_id"]), "epid": p["external_product_id"], "tool": TOOL,
         "domain": _seed_domain(p) or None,
         "url": p.get("destination_url"), "title": p["title"], "img": p.get("image_url"),
         "price": p.get("price_amount"), "data": json.dumps(seed_data, ensure_ascii=False),
         "market": (p.get("market") or "US").strip() or "US",
         "currency": (p.get("price_currency") or "USD").strip() or "USD",
         "seller_ref": seller_ref, "seed_kind": seed_kind},
    )


async def _set_category_and_offer(p: Dict[str, Any], product_key: str) -> None:
    pk = product_key
    if p.get("category_kind"):
        await database.execute(
            "UPDATE catalog_products SET category_kind=:ck, updated_at=NOW() WHERE product_key=:pk",
            {"ck": p["category_kind"], "pk": pk},
        )
    offer_type = (p.get("offer_type") or "brand_direct").strip()
    is_first_party = offer_type == "brand_direct"
    # Respect the product's real market (the mirror already set it from the seed);
    # don't force 'US' -- a KRW Korean offer must not be relabeled US-market.
    market = (p.get("market") or "US").strip() or "US"
    await database.execute(
        "UPDATE catalog_offers SET is_first_party=:fp, offer_type=:ot, market=:market, updated_at=NOW() "
        "WHERE product_key=:pk",
        {"fp": is_first_party, "ot": offer_type, "market": market, "pk": pk},
    )


async def _resolve_pdp_scope(p: Dict[str, Any], product_key: str, self_merchant_id: str) -> None:
    """Promote the mirrored row off the 'unverified' scope default so it clears
    the serving-eligibility `entity_unresolved` gate (which requires a resolved
    pdp_scope OR a product_group_id). Without this, a crawl-onboarded brand mirrors
    + scores fine but never serves. Single-seller brand-direct crawl → seller_count
    is 1 → classify() returns 'merchant_owned'; a product already carried by another
    seller correctly classifies 'multi_merchant_canonical'.

    seller_count counts distinct OTHER sellers of the product. Under ADR-009 D2
    the product's own seller is now a per-brand observed merchant (not the shared
    'external_seed' bucket), so the self-exclusion is on `self_merchant_id` — this
    preserves the exact prior semantics (count of OTHER sellers) without changing
    any serving-eligibility rule.
    """
    pk = product_key
    row = await database.fetch_one(
        """
        SELECT 1
          + COALESCE((SELECT COUNT(DISTINCT co.merchant_id) FROM catalog_offers co
                      WHERE co.product_key = :pk AND co.merchant_id IS NOT NULL
                        AND co.merchant_id <> :self_merchant), 0)
          + COALESCE((SELECT COUNT(DISTINCT eps.domain) FROM external_product_seeds eps
                      WHERE eps.attached_product_key = :pk AND eps.status = 'active'
                        AND eps.domain IS NOT NULL), 0) AS seller_count
        """,
        {"pk": pk, "self_merchant": self_merchant_id},
    )
    seller_count = int((dict(row) if row else {}).get("seller_count") or 1)
    scope = classify(ScopeSignals(category_label_source=None, seller_count=seller_count))
    await database.execute(
        "UPDATE catalog_products SET pdp_scope=:s, pdp_scope_source=:src, pdp_scope_set_at=NOW() "
        "WHERE product_key=:pk AND pdp_scope='unverified'",
        {"s": scope, "src": TOOL, "pk": pk},
    )


async def _onboard(cohort: List[Dict[str, Any]]) -> None:
    # ADR-009 D2/D3: resolve-or-mint each product's per-brand observed seller
    # BEFORE upserting the seed (so seller_ref/seed_kind land on the row) AND
    # before the mirror runs. The mirror derives the SAME identity (deterministic
    # on brand + registrable domain), so the product_key stashed here matches the
    # row the mirror writes — every downstream update targets the right PDP.
    keys: Dict[str, tuple[str, str]] = {}
    for p in cohort:
        keys[p["external_product_id"]] = await _resolve_seller_and_key(p)
    for p in cohort:
        merchant_id, _pk = keys[p["external_product_id"]]
        # A crawl-onboarded brand's destination is its own store → derive resolves
        # to ('self', <observed seller>). Using the shared derivation keeps a
        # single write-time rule and the honest NULL path (never assume 'self').
        seller_ref, seed_kind = await derive_seed_seller(
            anchor_merchant_id=merchant_id,
            brand=p.get("brand") or "",
            destination_domain=p.get("destination_url"),
            source_system=TOOL,
            primary_platform=PLATFORM,
        )
        await _upsert_seed(p, seller_ref=seller_ref, seed_kind=seed_kind)
    inserted = await mirror_apply(max(50, len(cohort) * 3))
    print(f"mirror inserted_catalog_products={inserted}")
    for p in cohort:
        merchant_id, product_key = keys[p["external_product_id"]]
        await _set_category_and_offer(p, product_key)
        # Resolve identity scope BEFORE make_external_seed_servable recomputes
        # eligibility, so the row can actually reach serving_eligible.
        await _resolve_pdp_scope(p, product_key, merchant_id)
    # INCI + enrichment via the shared crawled-INCI ingest (scripts.ingest_crawled_inci,
    # #855) -- one upsert-raw_inci + enrich_and_persist path for both crawl tools.
    # category_kind is set above first (enrichment reads it).
    inci_items = [
        {
            "product_key": keys[p["external_product_id"]][1],
            "sku_key": keys[p["external_product_id"]][1] + "::canonical",
            "raw_inci": p.get("raw_inci") or "",
        }
        for p in cohort
    ]
    report = await ingest_crawled_inci_items(inci_items, dry_run=False, db=database)
    print(f"inci ingest: written={report.get('inci_written')} actives={report.get('actives_filled')} skipped={report.get('skipped')}")
    for p in cohort:
        _merchant_id, product_key = keys[p["external_product_id"]]
        summary = await make_external_seed_servable(
            product_key=product_key,
            seed_id=_seed_id(p["external_product_id"]),
            source_product_id=p["external_product_id"],
            quality_payload=build_servable_quality_payload(
                title=p["title"], description=p.get("description"), price=p.get("price_amount"),
                image_url=p.get("image_url"), brand=p.get("brand"), product_type=p.get("product_type"),
            ),
            reason=TOOL,
        )
        print(f"  {p['external_product_id']}: {summary}")


def _load(args: argparse.Namespace) -> List[Dict[str, Any]]:
    raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit("input must be a JSON array of products")
    return data


async def _drive(args: argparse.Namespace) -> None:
    cohort = _load(args)
    print(f"{'APPLY' if args.apply else 'DRY'} :: {len(cohort)} products")
    if not args.apply:
        for p in cohort:
            print(
                f"  would onboard {p.get('external_product_id')} "
                f"({p.get('category_kind')}) "
                f"{p.get('price_amount')} {p.get('price_currency') or 'USD'} "
                f"market={p.get('market') or 'US'} :: {p.get('title')}"
            )
        return
    await database.connect()
    try:
        await _onboard(cohort)
    finally:
        await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="JSON array file (default: stdin)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    asyncio.run(_drive(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
