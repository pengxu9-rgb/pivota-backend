#!/usr/bin/env python3
"""Step-5 Lane 1 — collapse the 92sfrj-bi duplicate store connection (bbd side).

The Shopify store 92sfrj-bi.myshopify.com was connected under TWO merchant
accounts, mirroring its whole catalog twice (360 cross-merchant shared
content_keys — see docs/plans/adr011_step5_catalog_identity_reconciliation.md
§Lane 1 and the Lane-0 report). Founder decision 2026-07-10: the store is
test data; the losing side is `merch_bbd34645bc1950cc` — its connection is
already `inactive` in merchant_stores (stopped syncing 2026-06-29) and
nothing tests against it. The surviving side, `merch_efbc46b4619cfbdf`, is
the LIVE checkout/ACP canary rig (real test orders, latest within the hour
of this decision) and is deliberately left untouched — its intra-store dup
groups stay until that rig is retired.

This script tombstones the losing side's catalog rows (reversible, run-id
tagged). It does NOT touch merchant_stores (the bbd connection is already
non-active, so every sync/store-lookup path — which filter
status IN ('active','connected') — already skips it), does not touch seeds
(first-party shopify rows have none), and cannot touch money (orders key on
merchant-scoped ids and are never suppressed).

Sync-resurrection safety: catalog_sync_service._preserve_non_stale_suppression
carries any non-STALE_AFTER_SYNC tombstone through a future re-sync, so even
reconnecting the store would not silently clear these.

  Dry-run (default):  python3 scripts/step5_lane1_dedup_92sfrj.py
  Apply:              python3 scripts/step5_lane1_dedup_92sfrj.py --apply

Revert (by run):
  UPDATE catalog_products
  SET suppression_reason = NULL, suppressed_at = NULL,
      suppression_metadata = NULL, updated_at = NOW()
  WHERE suppression_reason = 'step5_duplicate_store_connection'
    AND suppression_metadata->>'run_id' = '<run_id printed at apply time>';
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

SUPPRESSION_REASON = "step5_duplicate_store_connection"
LOSING_MERCHANT = "merch_bbd34645bc1950cc"
SURVIVING_MERCHANT = "merch_efbc46b4619cfbdf"
DOMAIN = "92sfrj-bi.myshopify.com"

SELECT_SQL = """
SELECT COUNT(*) AS n
FROM catalog_products
WHERE merchant_id = $1
  AND platform = 'shopify'
  AND source_domain = $2
  AND suppression_reason IS NULL
"""

SUPPRESS_SQL = """
UPDATE catalog_products
SET suppression_reason = $1,
    suppressed_at = COALESCE(suppressed_at, NOW()),
    suppression_metadata = $2::jsonb,
    updated_at = NOW()
WHERE merchant_id = $3
  AND platform = 'shopify'
  AND source_domain = $4
  AND suppression_reason IS NULL
"""

# The losing connection must not be syncable; abort if someone reactivated it.
CONNECTION_GUARD_SQL = """
SELECT COUNT(*) FROM merchant_stores
WHERE merchant_id = $1 AND lower(domain) = $2
  AND status IN ('active', 'connected')
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
    conn = await _connect_with_retry(os.environ["DATABASE_URL"])
    try:
        live_connections = await conn.fetchval(
            CONNECTION_GUARD_SQL, LOSING_MERCHANT, DOMAIN
        )
        if live_connections:
            print(
                f"ABORT: {LOSING_MERCHANT} still has an active/connected "
                f"{DOMAIN} connection — deactivate it first or it will re-sync."
            )
            return 1

        n = await conn.fetchval(SELECT_SQL, LOSING_MERCHANT, DOMAIN)
        print(f"losing-side active rows ({LOSING_MERCHANT}, {DOMAIN}): {n}")
        if not n:
            print("Nothing to suppress.")
            return 0
        if not apply:
            print("DRY-RUN — no changes written. Re-run with --apply to execute.")
            return 0

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        metadata = json.dumps(
            {
                "script": "step5_lane1_dedup_92sfrj",
                "run_id": run_id,
                "plan": "docs/plans/adr011_step5_catalog_identity_reconciliation.md",
                "surviving_merchant": SURVIVING_MERCHANT,
                "decision": "founder 2026-07-10: 92sfrj is test data; bbd side collapsed",
            }
        )
        result = await conn.execute(
            SUPPRESS_SQL, SUPPRESSION_REASON, metadata, LOSING_MERCHANT, DOMAIN
        )
        remaining = await conn.fetchval(SELECT_SQL, LOSING_MERCHANT, DOMAIN)
        print(
            json.dumps(
                {
                    "applied": result,
                    "run_id": run_id,
                    "reason": SUPPRESSION_REASON,
                    "losing_side_active_rows_after": remaining,
                },
                indent=2,
            )
        )
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
