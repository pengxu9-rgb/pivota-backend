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
from services.agent_pdp_view_assembler import (  # noqa: E402
    refresh_agent_pdp_view_for_content_key,
)
from services.catalog_enrichment_agent.bulk_writer import is_transport_error  # noqa: E402
from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many  # noqa: E402
from services.external_seed_servability import build_servable_quality_payload  # noqa: E402
from services.index_pipeline_state_service import recompute_serving_eligibility  # noqa: E402
from services.product_quality_service import full_quality_eval, preview_quality  # noqa: E402

PROMOTION_REASON = "brand_official_promotion_v1"
TRUST_CHUNK = 100
PAGE_SIZE = 150

# Population: live enrichment-minted rows that do not yet hold a
# serving-eligible index_pipeline_state row. Includes rows with NO ips row at
# all (the common case — nothing creates one at ingest). Keyset-paginated:
# one 969-row fetch of description-wide rows over the ~530ms proxy died
# mid-stream repeatedly (2026-07-17); small pages + reconnect survive weather.
_SELECT_ROWS_PAGE = """
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
      AND p.product_key > :after
      AND NOT EXISTS (SELECT 1 FROM index_pipeline_state i
                       WHERE i.content_key = p.content_key
                         AND i.serving_eligible = TRUE)
    ORDER BY p.product_key
    LIMIT :page
"""

# Step 0 (APV heal): the serving classifier reads agent_pdp_view.description,
# so a cohort row whose catalog description is real but whose view row is
# missing/stale would classify short_description forever. Refresh exactly those.
_SELECT_STALE_APV = """
    SELECT DISTINCT p.content_key
    FROM catalog_products p
    LEFT JOIN agent_pdp_view a ON a.content_key = p.content_key
    WHERE p.source_system = 'catalog_enrichment_agent_v1'
      AND p.sync_status = 'live'
      AND p.content_key IS NOT NULL
      AND length(coalesce(p.description, '')) >= 50
      AND (a.content_key IS NULL OR length(coalesce(a.description, '')) < 50)
"""


def _should_heal(exc: BaseException) -> bool:
    """Transport failure OR the databases-lib poisoned-connection state (a bare
    AssertionError 'Connection is already acquired') — both mean the shared
    connection is unusable and a reconnect can save the rest of the batch."""
    return is_transport_error(exc) or "already acquired" in str(exc)


async def _reconnect() -> None:
    try:
        await database.disconnect()
    except Exception:  # noqa: BLE001
        pass
    for attempt in range(1, 6):
        try:
            await database.connect()
            return
        except Exception as exc:  # noqa: BLE001
            print(f"  (reconnect attempt {attempt} failed: {str(exc)[:100]})")
            await asyncio.sleep(5 * attempt)
    raise RuntimeError("could not re-establish DB connection")


async def _fetch_all_hardened(sql: str, values: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    for attempt in range(1, 5):
        try:
            return [dict(r) for r in await database.fetch_all(sql, values)]
        except Exception as exc:  # noqa: BLE001
            if not _should_heal(exc) or attempt == 4:
                raise
            print(f"  (fetch transport error, attempt {attempt}: {str(exc)[:100]} — reconnecting)")
            await _reconnect()
    return []


async def _fetch_population(limit: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    after = ""
    while True:
        page = await _fetch_all_hardened(_SELECT_ROWS_PAGE, {"after": after, "page": PAGE_SIZE})
        rows.extend(page)
        if len(page) < PAGE_SIZE or (limit and len(rows) >= limit):
            break
        after = str(page[-1]["product_key"])
    return rows[:limit] if limit else rows

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
    # initial connect through the same healer as mid-run reconnects —
    # a proxy TLS bad-window at process start must not burn a whole
    # stage attempt (observed 2026-07-17).
    await _reconnect()

    # 0. APV heal — refresh views whose description lags the catalog row.
    if apply:
        stale_cks = [r["content_key"] for r in await _fetch_all_hardened(_SELECT_STALE_APV)]
        print(f"[apv-heal] {len(stale_cks)} content_key view(s) missing/stale vs catalog description")
        healed = 0
        for i, ck in enumerate(stale_cks, 1):
            for attempt in (1, 2):
                try:
                    await refresh_agent_pdp_view_for_content_key(ck, refresh_source=PROMOTION_REASON)
                    healed += 1
                    break
                except Exception as exc:  # noqa: BLE001 — heal the connection, then
                    # one retry; a still-failing ck stays stale (honest-blocked later).
                    if attempt == 1 and _should_heal(exc):
                        await _reconnect()
                        continue
                    print(f"  WARN apv heal failed for {ck}: {str(exc)[:120]}")
                    break
            if i % 50 == 0:
                print(f"  [apv-heal] {i}/{len(stale_cks)}")
        print(f"[apv-heal] refreshed {healed}/{len(stale_cks)}")

    rows = await _fetch_population(limit)
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
        for attempt in (1, 2):
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
                break
            except Exception as exc:  # noqa: BLE001 — one row must not stop the
                # batch; a transport error heals the connection and retries once.
                if attempt == 1 and _should_heal(exc):
                    await _reconnect()
                    continue
                print(f"  WARN quality eval failed pk={row['product_key']}: {str(exc)[:150]}")
                break
        if i % 50 == 0:
            print(f"  [quality] {i}/{len(rows)}")

    # 2. index_pipeline_state — realtime recompute per distinct content_key.
    cks = sorted({str(r["content_key"]) for r in rows if r.get("content_key")})
    eligible = 0
    recompute_failed = 0
    for i, ck in enumerate(cks, 1):
        for attempt in (1, 2):
            try:
                if await recompute_serving_eligibility(ck, reason=PROMOTION_REASON):
                    eligible += 1
                break
            except Exception as exc:  # noqa: BLE001 — isolate per key
                if attempt == 1 and _should_heal(exc):
                    await _reconnect()
                    continue
                recompute_failed += 1
                print(f"  WARN ips recompute failed ck={ck}: {str(exc)[:150]}")
                break
        if i % 50 == 0:
            print(f"  [ips] {i}/{len(cks)} (eligible so far: {eligible})")

    # 3. catalog_row_trust for every touched product_key.
    pks = [str(r["product_key"]) for r in rows]
    trusted = 0
    for start in range(0, len(pks), TRUST_CHUNK):
        chunk = pks[start:start + TRUST_CHUNK]
        for attempt in (1, 2):
            try:
                trusted += await upsert_catalog_row_trust_many(db=database, product_keys=chunk)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 1 and _should_heal(exc):
                    await _reconnect()
                    continue
                print(f"  WARN trust chunk failed ({start}..): {str(exc)[:150]}")
                break

    # Census — what actually happened, straight from the tables.
    print(f"[applied] quality={scored}/{len(rows)}  ips_recomputed={len(cks) - recompute_failed}"
          f"/{len(cks)} serving_eligible={eligible}  trust_writes={trusted}")
    for r in await _fetch_all_hardened(_BLOCKER_CENSUS, {"cks": cks}):
        print(f"  ips  {dict(r)}")
    for r in await _fetch_all_hardened(_TRUST_CENSUS, {"pks": pks}):
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
