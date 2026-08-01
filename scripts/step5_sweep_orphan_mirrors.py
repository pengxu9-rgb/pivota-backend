#!/usr/bin/env python3
"""Step-5 orphan-mirror sweep — suppress catalog mirrors of dead seeds.

First apply cut of docs/plans/adr011_step5_catalog_identity_reconciliation.md
(Lane 0's exclusion, made durable). The two-mirror gotcha: deactivating an
external_product_seeds row does NOT tombstone its catalog_products mirror
(the stale-catalog sweep excludes external_seed), so seeds retired by the
crawl-side dedup left phantom catalog rows behind. Those rows are already
serving-blocked at runtime (catalog_trust_policy joins the seed's status =
EXTERNAL_SEED_INACTIVE), but they still pollute the identity backlog: they
count in the D-1 duplication gauge and are valid Tier-0 ATTACH targets for
services/intake_identity.py. This script makes the block durable and visible
the same way scripts/onboard_external_brand_from_crawl.py does for the rows
it drops itself: a reversible suppression_reason tombstone. No hard deletes.

Selection is IMPORTED from scripts/step5_working_set.py (ORPHAN_MIRRORS_SQL)
— the sweep suppresses exactly the population the Lane-0 report calls
`orphan_mirrors`, nothing else. The UPDATE re-checks the orphan condition in
the same statement, so a seed reactivated between select and apply is left
alone, and the suppression_reason IS NULL guard makes re-runs no-ops.

Seed linkage is BIDIRECTIONAL (learned the hard way in the first dry-run,
which surfaced 482 false orphans): mirror-door rows carry the seed id in
catalog_products.source_ref, but enrichment-door rows
(source_system='catalog_enrichment_agent_v1') have NO source_ref and are
linked via external_product_seeds.attached_product_key instead. A row is an
orphan only when NEITHER direction finds an active seed.

Rows that carry a pivota_signature_id are reported separately in the dry-run
(signatures are write-once and may be cited; suppression does not retire
them, and the row was already serving-blocked — but the reviewer should see
the count before applying).

Revert (by run):
  UPDATE catalog_products
  SET suppression_reason = NULL, suppressed_at = NULL,
      suppression_metadata = NULL, updated_at = NOW()
  WHERE suppression_reason = 'step5_orphan_seed_mirror'
    AND suppression_metadata->>'run_id' = '<run_id printed at apply time>';

Usage (prod via railway, same access notes as step5_working_set.py):
  Dry-run (default):
    python3 scripts/step5_sweep_orphan_mirrors.py
  Apply:
    python3 scripts/step5_sweep_orphan_mirrors.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.step5_working_set import ORPHAN_MIRRORS_SQL  # noqa: E402

SUPPRESSION_REASON = "step5_orphan_seed_mirror"

# Re-checks the full orphan condition (not just membership in the selected
# keys) so a concurrent seed reactivation wins over the sweep. Seed linkage
# is bidirectional (source_ref for mirror-door rows, attached_product_key for
# enrichment-door rows) — mirroring step5_working_set.SEED_STATUS_SQL.
UPDATE_SQL = """
UPDATE catalog_products cp
SET suppression_reason = $2,
    suppressed_at = COALESCE(suppressed_at, NOW()),
    suppression_metadata = $3::jsonb,
    updated_at = NOW()
WHERE cp.product_key = ANY($1::text[])
  AND cp.platform = 'external_seed'
  AND cp.suppression_reason IS NULL
  AND NOT EXISTS (
        SELECT 1 FROM external_product_seeds eps
        WHERE (eps.id = cp.source_ref
               OR eps.attached_product_key = cp.product_key)
          AND lower(coalesce(eps.status, '')) = 'active'
  )
"""


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Dry-run summary of the orphan population (pure)."""
    by_status = Counter(str(r.get("seed_status")) for r in rows)
    signed = [r for r in rows if r.get("pivota_signature_id")]
    return {
        "rows": len(rows),
        "by_seed_status": dict(by_status),
        "with_signature": len(signed),
        "signature_product_keys": sorted(
            str(r.get("product_key")) for r in signed
        )[:50],
    }


def build_metadata(run_id: str) -> str:
    return json.dumps(
        {
            "script": "step5_sweep_orphan_mirrors",
            "run_id": run_id,
            "plan": "docs/plans/adr011_step5_catalog_identity_reconciliation.md",
        }
    )


async def _connect_with_retry(dsn: str, attempts: int = 6):
    import asyncpg

    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return await asyncpg.connect(dsn, timeout=30, command_timeout=180)
        except Exception as e:  # public proxy flakes intermittently
            last = e
            await asyncio.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


async def _run(apply: bool) -> int:
    conn = await _connect_with_retry(os.environ["DATABASE_URL"])
    try:
        rows = [dict(r) for r in await conn.fetch(ORPHAN_MIRRORS_SQL)]
        summary = summarize(rows)
        print(json.dumps({"orphan_mirrors": summary}, indent=2))

        if not rows:
            print("Nothing to sweep.")
            return 0
        if not apply:
            print("DRY-RUN — no changes written. Re-run with --apply to execute.")
            return 0

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        product_keys = [str(r["product_key"]) for r in rows]
        async with conn.transaction():
            result = await conn.execute(
                UPDATE_SQL, product_keys, SUPPRESSION_REASON, build_metadata(run_id)
            )
        remaining = await conn.fetchval(
            f"SELECT COUNT(*) FROM ({ORPHAN_MIRRORS_SQL}) t"
        )
        print(
            json.dumps(
                {
                    "applied": result,
                    "run_id": run_id,
                    "reason": SUPPRESSION_REASON,
                    "orphans_remaining_after": remaining,
                },
                indent=2,
            )
        )
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the suppressions (default is dry-run)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
