#!/usr/bin/env python3
"""Promote enrichment-minted brand-official canonicals into the serving layer.

Path-C rows (source_system='catalog_enrichment_agent_v1') get catalog rows,
offers, seeds and singleton product groups at ingest — but NOTHING creates
their serving-layer artifacts: no quality snapshot is written (the quality
backfill queue is fed only by merchant catalog sync, and
make_external_seed_servable is seed-keyed + flag-gated), and no
index_pipeline_state row exists until the nightly index-health batch picks
them up. Result: catalog_row_trust.serving_decision stays 'blocked' on every
minted canonical.

This script runs the SAME machinery the servable path uses, keyed off the
catalog rows themselves:

  1. quality snapshot — build_servable_quality_payload + full_quality_eval
     (deterministic scorer; INCI attached when beauty_sku_ingredients has it);
  2. index_pipeline_state — recompute_serving_eligibility per content_key
     (the realtime creator; same classifier as the nightly batch);
  3. catalog_row_trust — upsert_catalog_row_trust_many for every touched row.

It NEVER fabricates content: rows whose description/image/price/quality don't
clear the classifier's gates simply classify as blocked, and the final report
shows the per-blocker census. Run scripts/backfill_brand_official_descriptions
first — the classifier reads agent_pdp_view.description, which that backfill
fills + refreshes.

Dry-run (default): score in memory, report the would-be pass/fail census.
    DATABASE_URL=... python3 scripts/promote_brand_official_canonicals.py

Apply:
    DATABASE_URL=... python3 scripts/promote_brand_official_canonicals.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many  # noqa: E402
from services.external_seed_servability import build_servable_quality_payload  # noqa: E402
from services.index_pipeline_state_service import recompute_serving_eligibility  # noqa: E402
from services.product_quality_service import full_quality_eval, preview_quality  # noqa: E402

PROMOTION_REASON = "brand_official_promotion_v1"
TRUST_CHUNK = 100

# Population: live enrichment-minted rows that do not yet hold a
# serving-eligible index_pipeline_state row. Includes rows with NO ips row at
# all (the common case — nothing creates one at ingest).
_SELECT_ROWS = """
    SELECT p.product_key, p.content_key, p.source_product_id, p.merchant_id,
           p.platform, p.title, p.description, p.brand, p.product_type,
           p.category, p.image_url, p.pdp_lifecycle_stage,
           (SELECT max(o.list_price) FROM catalog_offers o
             WHERE o.product_key = p.product_key AND o.list_price > 0
               AND o.suppressed_at IS NULL) AS price,
           bsi.raw_inci
    FROM catalog_products p
    LEFT JOIN beauty_sku_ingredients bsi
           ON bsi.sku_key = p.product_key || '::canonical'
    WHERE p.source_system = 'catalog_enrichment_agent_v1'
      AND p.sync_status = 'live'
      AND p.content_key IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM index_pipeline_state i
                       WHERE i.content_key = p.content_key
                         AND i.serving_eligible = TRUE)
"""

_BLOCKER_CENSUS = """
    SELECT i.serving_eligible, i.blocker_code, count(*) AS n
    FROM index_pipeline_state i
    WHERE i.content_key = ANY(:cks)
    GROUP BY 1, 2 ORDER BY n DESC
"""

_TRUST_CENSUS = """
    SELECT t.serving_decision, count(*) AS n
    FROM catalog_row_trust t
    WHERE t.product_key = ANY(:pks)
    GROUP BY 1 ORDER BY n DESC
"""


def _payload_for(row: Dict[str, Any]) -> Dict[str, Any]:
    price = row.get("price")
    return build_servable_quality_payload(
        title=row.get("title"),
        description=row.get("description"),
        price=float(price) if price is not None else None,
        image_url=row.get("image_url"),
        brand=row.get("brand"),
        product_type=row.get("product_type"),
        category=row.get("category"),
        raw_inci=row.get("raw_inci"),
    )


async def run(apply: bool, limit: int) -> int:
    await database.connect()
    rows = [dict(r) for r in await database.fetch_all(_SELECT_ROWS)]
    if limit:
        rows = rows[:limit]
    print(f"[population] {len(rows)} enrichment-minted rows without serving-eligible IPS "
          f"(apply={apply})")

    if not apply:
        dist: Counter = Counter()
        for row in rows:
            score = preview_quality(_payload_for(row), score_source_backed_components=True)[
                "content_quality_score"]
            dist["quality>=65" if (score or 0) >= 65 else "quality<65"] += 1
            dist[f"stage:{row.get('pdp_lifecycle_stage')}"] += 1
        print(f"[dry] {dict(dist)}")
        print("[dry] no writes. Re-run with --apply.")
        await database.disconnect()
        return 0

    # 1. quality snapshots (idempotent: latest snapshot wins on read).
    scored = 0
    for i, row in enumerate(rows, 1):
        try:
            await full_quality_eval(
                merchant_id=str(row["merchant_id"]),
                platform=str(row["platform"]),
                platform_product_id=str(row["source_product_id"]),
                geo_code="default",
                payload=_payload_for(row),
                score_source_backed_components=True,
            )
            scored += 1
        except Exception as exc:  # noqa: BLE001 — one row must not stop the batch
            print(f"  WARN quality eval failed pk={row['product_key']}: {str(exc)[:150]}")
        if i % 50 == 0:
            print(f"  [quality] {i}/{len(rows)}")

    # 2. index_pipeline_state — realtime recompute per distinct content_key.
    cks = sorted({str(r["content_key"]) for r in rows if r.get("content_key")})
    eligible = 0
    recompute_failed = 0
    for i, ck in enumerate(cks, 1):
        try:
            if await recompute_serving_eligibility(ck, reason=PROMOTION_REASON):
                eligible += 1
        except Exception as exc:  # noqa: BLE001 — isolate per key
            recompute_failed += 1
            print(f"  WARN ips recompute failed ck={ck}: {str(exc)[:150]}")
        if i % 50 == 0:
            print(f"  [ips] {i}/{len(cks)} (eligible so far: {eligible})")

    # 3. catalog_row_trust for every touched product_key.
    pks = [str(r["product_key"]) for r in rows]
    trusted = 0
    for start in range(0, len(pks), TRUST_CHUNK):
        chunk = pks[start:start + TRUST_CHUNK]
        try:
            trusted += await upsert_catalog_row_trust_many(db=database, product_keys=chunk)
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN trust chunk failed ({start}..): {str(exc)[:150]}")

    # Census — what actually happened, straight from the tables.
    print(f"[applied] quality={scored}/{len(rows)}  ips_recomputed={len(cks) - recompute_failed}"
          f"/{len(cks)} serving_eligible={eligible}  trust_writes={trusted}")
    for r in await database.fetch_all(_BLOCKER_CENSUS, {"cks": cks}):
        print(f"  ips  {dict(r)}")
    for r in await database.fetch_all(_TRUST_CENSUS, {"pks": pks}):
        print(f"  trust {dict(r)}")

    await database.disconnect()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    p.add_argument("--limit", type=int, default=0, help="cap rows (0 = all)")
    args = p.parse_args()
    return asyncio.run(run(args.apply, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
