#!/usr/bin/env python3
"""Retire the merch_efbc46b4619cfbdf checkout/ACP test rig (founder decision
2026-07-10, the deferred half of step-5 Lane 1).

This merchant was the live checkout/ACP canary: the surviving 92sfrj-bi
Shopify connection (the store's duplicate connection under merch_bbd was
collapsed in Lane 1), a wix test store whose brand-less dog-harness products
minted keyless `no_identity_inputs` rows on every sync tick, and a link to
the pivota-review-demo store. Retirement was deliberately deferred while
P-T2.3.x live-capture testing ran against it; the founder has now called it.

What --apply does (all reversible; NO hard deletes, money untouched):
  1. merchant_stores: every ACTIVE/CONNECTED connection of this merchant ->
     status='retired_test_rig' (every sync/store-lookup path filters
     status IN ('active','connected'), so all syncing stops — including the
     wix keyless-MINT source);
  2. catalog_products: every unsuppressed row of this merchant ->
     suppression_reason='step5_test_rig_retirement' + run-id metadata
     (survives any re-sync via _preserve_non_stale_suppression, and the
     disconnect makes re-sync impossible anyway);
  3. prints the order footprint (orders are merchant-scoped money rows;
     suppression never touches them — the 543 test orders stay queryable).

Expected gauge effect: the merchant's ~115 no-URL dup families leave the
same-merchant D-1 count; the weekly sweep will alert-check the new baseline.

Revert: flip the connections back to 'active' and clear the tombstones by
run_id (recipe in docs/plans/adr011_step5_catalog_identity_reconciliation.md).

  Dry-run (default):  python3 scripts/retire_test_rig_merch_efbc.py
  Apply:              python3 scripts/retire_test_rig_merch_efbc.py --apply
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

MERCHANT = "merch_efbc46b4619cfbdf"
SUPPRESSION_REASON = "step5_test_rig_retirement"
RETIRED_STATUS = "retired_test_rig"

RECON_SQL = """
SELECT
  (SELECT COUNT(*) FROM merchant_stores
    WHERE merchant_id = $1 AND status IN ('active', 'connected')) AS active_connections,
  (SELECT COUNT(*) FROM catalog_products
    WHERE merchant_id = $1 AND suppression_reason IS NULL) AS active_rows,
  (SELECT COUNT(*) FROM orders WHERE merchant_id = $1) AS orders,
  (SELECT MAX(created_at) FROM orders WHERE merchant_id = $1) AS last_order
"""

DISCONNECT_SQL = """
UPDATE merchant_stores
SET status = $2
WHERE merchant_id = $1 AND status IN ('active', 'connected')
RETURNING store_id, platform, domain
"""

SUPPRESS_SQL = """
UPDATE catalog_products
SET suppression_reason = $2,
    suppressed_at = COALESCE(suppressed_at, NOW()),
    suppression_metadata = $3::jsonb,
    updated_at = NOW()
WHERE merchant_id = $1 AND suppression_reason IS NULL
"""


async def _connect_with_retry(dsn: str, attempts: int = 6):
    import asyncpg

    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await asyncpg.connect(dsn, timeout=30, command_timeout=180)
        except Exception as e:
            last = e
            await asyncio.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


async def _run(apply: bool) -> int:
    conn = await _connect_with_retry(os.environ["DATABASE_URL"])
    try:
        recon = dict(await conn.fetchrow(RECON_SQL, MERCHANT))
        print(json.dumps({"merchant": MERCHANT, "recon": recon}, indent=2,
                         default=str))
        if not recon["active_connections"] and not recon["active_rows"]:
            print("Nothing to retire.")
            return 0
        if not apply:
            print("DRY-RUN — no changes written. Re-run with --apply to execute.")
            return 0

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        metadata = json.dumps({
            "script": "retire_test_rig_merch_efbc",
            "run_id": run_id,
            "decision": "founder 2026-07-10: retire the checkout/ACP test rig",
        })
        async with conn.transaction():
            stores = await conn.fetch(DISCONNECT_SQL, MERCHANT, RETIRED_STATUS)
            result = await conn.execute(
                SUPPRESS_SQL, MERCHANT, SUPPRESSION_REASON, metadata
            )
        remaining = dict(await conn.fetchrow(RECON_SQL, MERCHANT))
        print(json.dumps({
            "run_id": run_id,
            "connections_retired": [dict(s) for s in stores],
            "rows_suppressed": result,
            "post": {k: remaining[k] for k in ("active_connections", "active_rows")},
        }, indent=2, default=str))
        return 0 if (remaining["active_connections"] == 0
                     and remaining["active_rows"] == 0) else 1
    finally:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001 — proxy close flake, work already done
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Execute the retirement (default is dry-run)")
    args = parser.parse_args()
    return asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
