#!/usr/bin/env python3
"""Source-backed quality rescore for a specific set of catalog rows → serving.

Targeted enrichment for canonicals that carry real brand-official content
(description + image + INCI) but score < the serving quality floor because
their quality snapshot was computed WITHOUT the source-backed components
(summary + ingredient attributes). Re-scoring with those components on lifts
them the same way the brand-official promotion did for the enrichment cohort
— NOT by inventing content, only by scoring the content already present.

Reuses the reviewed serving machinery per row: build_servable_quality_payload
+ full_quality_eval (source-backed on) → recompute_serving_eligibility →
catalog_row_trust. Rows that still miss the floor stay honestly blocked.

Input is an explicit product_key set (never a broad scan) — this is a
follow-up tool for specific rows a human identified (e.g. merge winners):

    DATABASE_URL=... python3 scripts/enrich_quality_pks.py --pks pk1,pk2,...
    DATABASE_URL=... python3 scripts/enrich_quality_pks.py --pks-file keys.txt
    DATABASE_URL=... python3 scripts/enrich_quality_pks.py \
        --merge-manifest merge_run_1.json   # enriches its winner_pks

Dry-run (default) reports the rescore; --apply writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many  # noqa: E402
from services.external_seed_servability import build_servable_quality_payload  # noqa: E402
from services.index_pipeline_state_service import (  # noqa: E402
    recompute_serving_eligibility,
)
from services.product_quality_service import (  # noqa: E402
    full_quality_eval,
    preview_quality,
)

REASON = "quality_rescore_source_backed_v1"

_SELECT = """
    SELECT p.product_key, p.content_key, p.source_product_id, p.merchant_id,
           p.platform, p.title, p.description, p.brand, p.product_type,
           p.category, p.image_url, p.sync_status,
           (SELECT max(o.list_price) FROM catalog_offers o
             WHERE o.product_key = p.product_key AND o.list_price > 0
               AND o.suppressed_at IS NULL) AS price,
           bsi.raw_inci
    FROM catalog_products p
    LEFT JOIN beauty_sku_ingredients bsi
           ON bsi.sku_key = p.product_key || '::canonical'
    WHERE p.product_key = ANY(:pks)
"""


def _resolve_pks(args: argparse.Namespace) -> List[str]:
    if args.merge_manifest:
        m = json.load(open(args.merge_manifest))
        return [l["winner_pk"] for l in m.get("ledgers", [])]
    if args.pks_file:
        return [l.strip() for l in open(args.pks_file) if l.strip()]
    return [p.strip() for p in (args.pks or "").split(",") if p.strip()]


def _payload_for(row: Dict[str, Any]) -> Dict[str, Any]:
    price = row.get("price")
    return build_servable_quality_payload(
        title=row.get("title"), description=row.get("description"),
        price=float(price) if price is not None else None,
        image_url=row.get("image_url"), brand=row.get("brand"),
        product_type=row.get("product_type"), category=row.get("category"),
        raw_inci=row.get("raw_inci"),
    )


async def run(pks: List[str], apply: bool) -> int:
    await database.connect()
    rows = [dict(r) for r in await database.fetch_all(_SELECT, {"pks": pks})]
    live = [r for r in rows if r.get("sync_status") == "live"]
    print(f"[enrich] {len(pks)} requested; {len(live)} live rows (apply={apply})")

    would_pass = 0
    for r in live:
        q = preview_quality(_payload_for(r), score_source_backed_components=True)[
            "content_quality_score"]
        r["_rescore"] = q
        if q >= 65:
            would_pass += 1
    print(f"[enrich] {would_pass}/{len(live)} clear the 65 quality floor on rescore")

    if not apply:
        for r in live:
            print(f"  {r['title'][:38]:40s} -> {r['_rescore']:.1f} {'PASS' if r['_rescore']>=65 else '(stays blocked)'}")
        await database.disconnect()
        return 0

    scored = 0
    for r in live:
        try:
            await full_quality_eval(
                merchant_id=str(r["merchant_id"]), platform=str(r["platform"]),
                platform_product_id=str(r["source_product_id"]), geo_code="default",
                payload=_payload_for(r), score_source_backed_components=True,
            )
            scored += 1
        except Exception as exc:  # noqa: BLE001 — one row must not stop the batch
            print(f"  WARN quality eval failed pk={r['product_key']}: {str(exc)[:140]}")

    cks = sorted({str(r["content_key"]) for r in live if r.get("content_key")})
    eligible = 0
    for ck in cks:
        try:
            if await recompute_serving_eligibility(ck, reason=REASON):
                eligible += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN ips recompute failed ck={ck}: {str(exc)[:140]}")

    trusted = 0
    try:
        trusted = await upsert_catalog_row_trust_many(
            db=database, product_keys=[r["product_key"] for r in live])
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN trust upsert failed: {str(exc)[:140]}")

    print(f"[applied] rescored={scored}/{len(live)}  serving_eligible={eligible}/{len(cks)}  "
          f"trust_writes={trusted}")
    # census straight from the tables
    for r in await database.fetch_all(
        "SELECT i.serving_eligible, i.blocker_code, count(*) n FROM catalog_products p "
        "LEFT JOIN index_pipeline_state i ON i.content_key=p.content_key "
        "WHERE p.product_key = ANY(:pks) GROUP BY 1,2 ORDER BY n DESC", {"pks": pks}):
        print(f"  ips  {dict(r)}")
    for r in await database.fetch_all(
        "SELECT t.serving_decision, count(*) n FROM catalog_products p "
        "LEFT JOIN catalog_row_trust t ON t.product_key=p.product_key "
        "WHERE p.product_key = ANY(:pks) GROUP BY 1 ORDER BY n DESC", {"pks": pks}):
        print(f"  trust {dict(r)}")
    await database.disconnect()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pks", help="comma-separated product_keys")
    p.add_argument("--pks-file", help="file of product_keys, one per line")
    p.add_argument("--merge-manifest", help="merge run manifest (uses winner_pks)")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    pks = _resolve_pks(args)
    if not pks:
        p.error("no product_keys (use --pks / --pks-file / --merge-manifest)")
    return asyncio.run(run(pks, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
