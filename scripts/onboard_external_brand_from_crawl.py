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
     (scripts.ingest_crawled_inci._drive, #855) -- one upsert-raw_inci +
     enrich_and_persist implementation for both crawl tools, so they don't drift
  5. make_external_seed_servable: attached_product_key back-link + quality
     snapshot + agent_pdp_view refresh + recompute eligibility

Idempotent throughout. Input: a JSON array on --file or stdin; each item:
  {external_product_id, brand, title, category_kind, product_type,
   destination_url, image_url, price_amount, description, raw_inci,
   offer_type?("brand_direct"|"retailer", default brand_direct)}

Usage:
  python -m scripts.onboard_external_brand_from_crawl --file cohort.json --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from scripts.ingest_crawled_inci import _drive as ingest_crawled_inci_items
from scripts.mirror_external_seeds_to_catalog_products import _apply as mirror_apply
from services.external_seed_servability import (
    build_servable_quality_payload,
    make_external_seed_servable,
)

TOOL = "external_brand_crawl"


def _seed_id(epid: str) -> str:
    return f"{TOOL}::{epid}"


def _product_key(epid: str) -> str:
    return f"prod::external_seed::external_seed::{epid}"


async def _upsert_seed(p: Dict[str, Any]) -> None:
    seed_data = {
        "snapshot": {
            "title": p["title"], "description": p.get("description"), "brand": p.get("brand"),
            "product_type": p.get("product_type"), "category": p.get("category_kind"),
        },
        "pdp_description_raw": p.get("description"),
        "pdp_ingredients_raw": p.get("raw_inci"),
        "inci_list": p.get("raw_inci"),
    }
    await database.execute(
        """
        INSERT INTO external_product_seeds
          (id, external_product_id, market, tool, destination_url, title, image_url,
           price_amount, price_currency, availability, seed_data, status)
        VALUES
          (:id, :epid, 'US', :tool, :url, :title, :img,
           :price, 'USD', 'in_stock', CAST(:data AS jsonb), 'active')
        ON CONFLICT (id) DO UPDATE SET
          title=EXCLUDED.title, image_url=EXCLUDED.image_url, price_amount=EXCLUDED.price_amount,
          seed_data=EXCLUDED.seed_data, status='active', updated_at=NOW()
        """,
        {"id": _seed_id(p["external_product_id"]), "epid": p["external_product_id"], "tool": TOOL,
         "url": p.get("destination_url"), "title": p["title"], "img": p.get("image_url"),
         "price": p.get("price_amount"), "data": json.dumps(seed_data, ensure_ascii=False)},
    )


async def _set_category_and_offer(p: Dict[str, Any]) -> None:
    pk = _product_key(p["external_product_id"])
    if p.get("category_kind"):
        await database.execute(
            "UPDATE catalog_products SET category_kind=:ck, updated_at=NOW() WHERE product_key=:pk",
            {"ck": p["category_kind"], "pk": pk},
        )
    offer_type = (p.get("offer_type") or "brand_direct").strip()
    is_first_party = offer_type == "brand_direct"
    await database.execute(
        "UPDATE catalog_offers SET is_first_party=:fp, offer_type=:ot, market='US', updated_at=NOW() "
        "WHERE product_key=:pk",
        {"fp": is_first_party, "ot": offer_type, "pk": pk},
    )


async def _onboard(cohort: List[Dict[str, Any]]) -> None:
    for p in cohort:
        await _upsert_seed(p)
    inserted = await mirror_apply(max(50, len(cohort) * 3))
    print(f"mirror inserted_catalog_products={inserted}")
    for p in cohort:
        await _set_category_and_offer(p)
    # INCI + enrichment via the shared crawled-INCI ingest (scripts.ingest_crawled_inci,
    # #855) -- one upsert-raw_inci + enrich_and_persist path for both crawl tools.
    # category_kind is set above first (enrichment reads it).
    inci_items = [
        {
            "product_key": _product_key(p["external_product_id"]),
            "sku_key": _product_key(p["external_product_id"]) + "::canonical",
            "raw_inci": p.get("raw_inci") or "",
        }
        for p in cohort
    ]
    report = await ingest_crawled_inci_items(inci_items, dry_run=False, db=database)
    print(f"inci ingest: written={report.get('inci_written')} actives={report.get('actives_filled')} skipped={report.get('skipped')}")
    for p in cohort:
        summary = await make_external_seed_servable(
            product_key=_product_key(p["external_product_id"]),
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
            print(f"  would onboard {p.get('external_product_id')} ({p.get('category_kind')})")
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
