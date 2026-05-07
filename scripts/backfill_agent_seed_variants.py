#!/usr/bin/env python3
"""Phase 7c backfill runner — executes scripts/backfill_agent_seed_variants.sql
against $DATABASE_URL via raw asyncpg.

Idempotent: only touches rows where seed_data.variants is missing or
empty. See the .sql file for the full rationale.

Usage:
  DATABASE_URL=postgresql://... python3 scripts/backfill_agent_seed_variants.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    sql_path = Path(__file__).resolve().with_name("backfill_agent_seed_variants.sql")
    raw = sql_path.read_text(encoding="utf-8")

    # Split off the trailing verification SELECT so we can fetch its rows
    # separately (asyncpg.execute on a multi-statement string returns no
    # row data even when the last statement is a SELECT).
    marker = "-- Verification"
    if marker in raw:
        update_sql, _, _verify_tail = raw.partition(marker)
    else:
        update_sql = raw

    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        print("--- running UPDATE on agent seeds ---")
        result = await conn.execute(update_sql)
        print(result)

        print("--- verification ---")
        row = await conn.fetchrow(
            """
            SELECT
              COUNT(*) AS total_agent_seeds,
              COUNT(*) FILTER (WHERE availability = 'in_stock') AS in_stock,
              COUNT(*) FILTER (WHERE jsonb_array_length(COALESCE(seed_data->'variants', '[]'::jsonb)) >= 1) AS has_variant,
              COUNT(*) FILTER (WHERE seed_data ? 'title') AS has_seed_title,
              COUNT(*) FILTER (WHERE jsonb_array_length(COALESCE(seed_data->'image_urls', '[]'::jsonb)) >= 1) AS has_image_urls
            FROM external_product_seeds
            WHERE tool = 'catalog_enrichment_agent_v1' AND status = 'active'
            """
        )
        print(dict(row) if row else "no rows")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
