#!/usr/bin/env python3
"""Backfill honest tier labels on audit-door catalog rows (convergence P1.1).

The audit intake door (services/audit_index_intake.py) wrote catalog_products
rows relying on DB server-defaults for the tier triple — which stamp
`internal_merchant/primary/commerce_ready`, the label of a FIRST-PARTY SYNCED
product. Audit seeds are OBSERVED, unclaimed records: the honest triple is
`external_referral/observed/referral_only` (same as the external-seed mirror
door). The intake now stamps this explicitly; this script fixes rows written
before the fix.

Provenance-keyed and conservative:
  - only rows with platform = 'url_audit' (the audit door's fingerprint);
  - only rows STILL carrying all three untouched server-defaults — a row any
    other writer or human has re-tiered is left alone.

Idempotent (the WHERE excludes already-fixed rows). Dry-run is the default:
  python scripts/backfill_audit_seed_tier_labels.py
Apply (staging first; production only with explicit user authorization):
  DATABASE_URL=... python scripts/backfill_audit_seed_tier_labels.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

logger = logging.getLogger("backfill_audit_seed_tier_labels")

# Fingerprint of the audit door + untouched server-defaults. Keep in sync with
# services/audit_index_intake.py PLATFORM_URL_AUDIT / _AUDIT_SEED_* constants.
#
# CRITICAL guard: claim + graduation NEVER write the tier triple (claim_state.py
# sets only claim_state; brand_verified_graduation.py sets only pdp_scope), so a
# CLAIMED or MERCHANT-OWNED audit row still carries the default triple and would
# otherwise match here — relabelling it to referral_only could pull it off the
# transact lane. Exclude any row that has advanced past the raw observed state.
WHERE_MISLABELED = """
    platform = 'url_audit'
      AND catalog_track = 'internal_merchant'
      AND truth_tier = 'primary'
      AND readiness_tier = 'commerce_ready'
      AND claim_state = 'unclaimed'
      AND pdp_scope = 'unverified'
"""

COUNT_SQL = f"SELECT COUNT(*) AS n FROM catalog_products WHERE {WHERE_MISLABELED}"

SAMPLE_SQL = f"""
    SELECT product_key, merchant_id, title, source_domain, created_at
    FROM catalog_products
    WHERE {WHERE_MISLABELED}
    ORDER BY created_at DESC
    LIMIT 10
"""

UPDATE_SQL = f"""
    UPDATE catalog_products
    SET catalog_track = 'external_referral',
        truth_tier = 'observed',
        readiness_tier = 'referral_only',
        updated_at = NOW()
    WHERE {WHERE_MISLABELED}
"""

# Sanity guard: audit-door rows that DON'T carry the full default triple —
# these were touched by someone else and are intentionally skipped; surfaced
# in the report so a human can review whether any need manual attention.
TOUCHED_SQL = """
    SELECT catalog_track, truth_tier, readiness_tier, COUNT(*) AS n
    FROM catalog_products
    WHERE platform = 'url_audit'
      AND NOT (catalog_track = 'internal_merchant'
               AND truth_tier = 'primary'
               AND readiness_tier = 'commerce_ready')
    GROUP BY catalog_track, truth_tier, readiness_tier
    ORDER BY n DESC
"""

# Audit rows carrying the raw default triple but which have ADVANCED
# (claimed / graduated) — deliberately skipped by WHERE_MISLABELED so we never
# downgrade a transact-eligible row. Surfaced so an operator can see them.
SKIPPED_ADVANCED_SQL = """
    SELECT claim_state, pdp_scope, COUNT(*) AS n
    FROM catalog_products
    WHERE platform = 'url_audit'
      AND catalog_track = 'internal_merchant'
      AND truth_tier = 'primary'
      AND readiness_tier = 'commerce_ready'
      AND (claim_state <> 'unclaimed' OR pdp_scope <> 'unverified')
    GROUP BY claim_state, pdp_scope
    ORDER BY n DESC
"""


async def run_backfill(*, apply: bool) -> Dict[str, Any]:
    row = await database.fetch_one(COUNT_SQL)
    mislabeled = int(dict(row).get("n") or 0) if row else 0
    samples = [dict(r) for r in await database.fetch_all(SAMPLE_SQL)]
    touched = [dict(r) for r in await database.fetch_all(TOUCHED_SQL)]
    skipped_advanced = [dict(r) for r in await database.fetch_all(SKIPPED_ADVANCED_SQL)]

    report: Dict[str, Any] = {
        "apply": apply,
        "mislabeled_rows": mislabeled,
        "sample": samples,
        "skipped_non_default_tiers": touched,
        "skipped_advanced_default_triple": skipped_advanced,
        "rows_updated": 0,
    }
    if apply and mislabeled:
        await database.execute(UPDATE_SQL)
        post = await database.fetch_one(COUNT_SQL)
        remaining = int(dict(post).get("n") or 0) if post else 0
        report["rows_updated"] = mislabeled - remaining
        report["remaining_mislabeled"] = remaining
    return report


async def _main(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        return await run_backfill(apply=args.apply)
    finally:
        await database.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write tier labels (default: dry-run)")
    cli_args = parser.parse_args()

    result = asyncio.run(_main(cli_args))
    print(json.dumps(result, indent=2, default=str))
    if not cli_args.apply:
        print("\nDRY-RUN — nothing written. Re-run with --apply to fix tier labels.", file=sys.stderr)
