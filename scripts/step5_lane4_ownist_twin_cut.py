#!/usr/bin/env python3
"""Step-5 Lane 4 — ownist seed↔first-party twin cut (4 groups, signed off).

The lane-4 review (reports/step5/lane4_review_2026-07-10.md) adjudicated four
ownist.com content_keys as SUPPRESS_seed_twin_pending_signoff: each has a
claimed first-party shopify row (merch_test_ownist_001) AND an external_seed
mirror of the SAME canonical_url. Founder signed off 2026-07-10.

This applies migration 139's predicate (tombstone the redundant external_seed
mirror when a live first-party sibling shares the content_key — reason
`cross_merchant_redundant_external_seed`, seeds deliberately untouched, same
as the 50-row precedent) narrowed to exactly these four keys, plus run-id
metadata for revert. Migration 139 itself never re-ran for these (Railway
skips db/migrations/ — only schema_guard-encoded steps execute on deploy).

The 5th twin group from the review (anuko, url_audit sibling) is expressly
EXCLUDED: an audit row never wins serving, so the seed row there is the real
card (verdict KEEP_audit_observation) — and 139's raw predicate would have
wrongly suppressed it.

  Dry-run (default):  python3 scripts/step5_lane4_ownist_twin_cut.py
  Apply:              python3 scripts/step5_lane4_ownist_twin_cut.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SUPPRESSION_REASON = "cross_merchant_redundant_external_seed"
FIRST_PARTY_MERCHANT = "merch_test_ownist_001"

# The four signed-off groups (full keys from reports/step5/lane4_review_2026-07-10.json).
CONTENT_KEYS_PREFIXES = [
    "ck_07993a56a",
    "ck_207b48883",
    "ck_6ade2fac1",
    "ck_b6c694cf4",
]

SELECT_SQL = """
SELECT cp.product_key, cp.content_key, cp.sync_status, cp.canonical_url,
       sib.product_key AS sibling_product_key, sib.sync_status AS sibling_sync_status
FROM catalog_products cp
JOIN catalog_products sib
  ON sib.content_key = cp.content_key
 AND sib.merchant_id = $2
 AND sib.platform = 'shopify'
 AND sib.suppression_reason IS NULL
WHERE cp.merchant_id = 'external_seed'
  AND cp.suppression_reason IS NULL
  AND cp.content_key LIKE ANY($1::text[])
"""

SUPPRESS_SQL = """
UPDATE catalog_products cp
SET suppression_reason = $1,
    suppressed_at = COALESCE(suppressed_at, NOW()),
    suppression_metadata = $2::jsonb,
    updated_at = NOW()
WHERE cp.merchant_id = 'external_seed'
  AND cp.suppression_reason IS NULL
  AND cp.content_key LIKE ANY($3::text[])
  AND EXISTS (
    SELECT 1 FROM catalog_products sib
    WHERE sib.content_key = cp.content_key
      AND sib.merchant_id = $4
      AND sib.platform = 'shopify'
      AND sib.suppression_reason IS NULL
  )
"""


async def _connect_with_retry(dsn: str, attempts: int = 6):
    import asyncpg

    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await asyncpg.connect(dsn, timeout=30, command_timeout=120)
        except Exception as e:
            last = e
            await asyncio.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


async def _run(apply: bool) -> int:
    patterns = [p + "%" for p in CONTENT_KEYS_PREFIXES]
    conn = await _connect_with_retry(os.environ["DATABASE_URL"])
    try:
        rows = [dict(r) for r in await conn.fetch(
            SELECT_SQL, patterns, FIRST_PARTY_MERCHANT
        )]
        print(json.dumps({"candidates": rows}, indent=2, default=str))
        if len(rows) != 4:
            print(f"ABORT: expected exactly 4 seed twin rows, found {len(rows)}.")
            return 1
        if not apply:
            print("DRY-RUN — no changes written. Re-run with --apply to execute.")
            return 0

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        metadata = json.dumps(
            {
                "script": "step5_lane4_ownist_twin_cut",
                "run_id": run_id,
                "review": "reports/step5/lane4_review_2026-07-10.md",
                "decision": "founder sign-off 2026-07-10: suppress seed mirror, keep first-party",
            }
        )
        result = await conn.execute(
            SUPPRESS_SQL, SUPPRESSION_REASON, metadata, patterns, FIRST_PARTY_MERCHANT
        )
        remaining = await conn.fetchval(
            f"SELECT COUNT(*) FROM ({SELECT_SQL}) t", patterns, FIRST_PARTY_MERCHANT
        )
        print(json.dumps({"applied": result, "run_id": run_id,
                          "twin_rows_remaining": remaining}, indent=2))
        return 0 if remaining == 0 else 1
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the suppressions (default is dry-run)")
    args = parser.parse_args()
    return asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
