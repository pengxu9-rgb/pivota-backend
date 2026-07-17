#!/usr/bin/env python3
"""Re-derive catalog_row_trust for the enrichment-minted cohort.

Run AFTER the identity coverage lift enables live_read on the cohort's
pdp_identity_listing rows: the patched trust upserter join (minted-lane pil
routing via attached seed) now sees identity_status/confidence/live_read, so
re-derivation flips IDENTITY_CONFIDENCE_NULL shadow rows to public where the
policy allows. Chunked + reconnect-retry for the flaky public proxy.

    DATABASE_URL=... python3 scripts/refresh_trust_minted_cohort.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many  # noqa: E402

CHUNK = 50

_KEYS_SQL = """
    SELECT product_key FROM catalog_products
    WHERE source_system = 'catalog_enrichment_agent_v1' AND sync_status = 'live'
    ORDER BY product_key
"""

_CENSUS_SQL = """
    SELECT t.serving_decision, count(*) AS n
    FROM catalog_products p
    JOIN catalog_row_trust t ON t.product_key = p.product_key
    WHERE p.source_system = 'catalog_enrichment_agent_v1' AND p.sync_status = 'live'
    GROUP BY 1 ORDER BY 2 DESC
"""


async def _connect_with_retry(tries: int = 6) -> None:
    for i in range(tries):
        try:
            await database.connect()
            return
        except Exception as e:  # noqa: BLE001
            print(f"  (connect attempt {i+1}: {type(e).__name__} — retry 20s)", flush=True)
            await asyncio.sleep(20)
    raise SystemExit("could not connect")


async def main() -> int:
    await _connect_with_retry()
    try:
        keys = [r["product_key"] for r in await database.fetch_all(_KEYS_SQL)]
        print(f"[population] {len(keys)} minted product_keys", flush=True)
        written = 0
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i:i + CHUNK]
            for attempt in range(3):
                try:
                    written += await upsert_catalog_row_trust_many(db=database, product_keys=chunk)
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  [chunk {i}] attempt {attempt+1}: {type(e).__name__} — reconnect", flush=True)
                    try:
                        await database.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.sleep(15)
                    await _connect_with_retry()
            if (i // CHUNK) % 4 == 0:
                print(f"  [trust] {min(i+CHUNK, len(keys))}/{len(keys)} written={written}", flush=True)
        print(f"[done] trust rows written: {written}", flush=True)
        print("[census]", flush=True)
        for r in await database.fetch_all(_CENSUS_SQL):
            print(f"  {dict(r)}", flush=True)
        return 0
    finally:
        if bool(getattr(database, "is_connected", False)):
            await database.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
