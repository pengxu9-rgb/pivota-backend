#!/usr/bin/env python3
"""Run the commerce-index graduation ladder over the observed corpus (P1.5).

The audit / external-seed doors stamp observed rows `referral_only` and never
re-assert tiers, so a row that has since cleared the index eligibility gate is
still labelled `referral_only`. The nightly index-health job advances these once
`INDEX_GRADUATION_LADDER_ENABLED` is on; this script is the on-demand corpus
runner (first enablement, or catch-up after a bulk import) built on the SAME
single transition writer (services.index_graduation_ladder).

For every distinct content_key that has an observed / `external_referral` row not
yet at the top of the ladder, it recomputes eligibility via the authoritative
oracle and advances the row's readiness_tier monotonically:

    referral_only → knowledge_ready (index_eligible) → commerce_ready (serving_eligible)

Conservative + idempotent: only the observed track is touched, tiers never move
down, first-party rows are excluded by the writer. Dry-run is the default and
reports the current distribution + candidate count WITHOUT writing:

  python scripts/backfill_graduation_ladder.py
Apply (staging first; production only with explicit user authorization):
  DATABASE_URL=... python scripts/backfill_graduation_ladder.py --apply --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

logger = logging.getLogger("backfill_graduation_ladder")

# Current readiness distribution on the observed track (the ladder's domain).
DISTRIBUTION_SQL = """
    SELECT readiness_tier, COUNT(*) AS n
    FROM catalog_products
    WHERE catalog_track = 'external_referral'
      AND truth_tier = 'observed'
    GROUP BY readiness_tier
    ORDER BY n DESC
"""

# Distinct content_keys with an observed row NOT yet at the top of the ladder —
# the graduation candidates. Ordered by recency so a bounded --limit run touches
# the freshest rows first.
CANDIDATE_KEYS_SQL = """
    SELECT content_key, MAX(updated_at) AS last_updated
    FROM catalog_products
    WHERE catalog_track = 'external_referral'
      AND truth_tier = 'observed'
      AND readiness_tier <> 'commerce_ready'
      AND content_key IS NOT NULL
      AND content_key <> ''
    GROUP BY content_key
    ORDER BY last_updated DESC
"""


async def run_backfill(*, apply: bool, limit: int) -> Dict[str, Any]:
    distribution = [dict(r) for r in await database.fetch_all(DISTRIBUTION_SQL)]
    candidate_rows = [dict(r) for r in await database.fetch_all(CANDIDATE_KEYS_SQL)]
    candidate_keys: List[str] = [r["content_key"] for r in candidate_rows]
    if limit and limit > 0:
        candidate_keys = candidate_keys[:limit]

    report: Dict[str, Any] = {
        "apply": apply,
        "observed_readiness_distribution": distribution,
        "candidate_content_keys": len(candidate_rows),
        "candidates_processed": 0,
        "advanced_to_knowledge_ready": 0,
        "advanced_to_commerce_ready": 0,
        "rows_advanced": 0,
    }
    if not apply:
        return report

    # An explicit, human-authorized --apply run opts the writer in for THIS
    # process regardless of the nightly kill-switch.
    os.environ["INDEX_GRADUATION_LADDER_ENABLED"] = "1"
    from services.index_graduation_ladder import graduate_content_key

    for content_key in candidate_keys:
        result = await graduate_content_key(content_key, reason="backfill_graduation")
        report["candidates_processed"] += 1
        advanced = int(result.get("advanced") or 0)
        if advanced:
            report["rows_advanced"] += advanced
            if result.get("target") == "commerce_ready":
                report["advanced_to_commerce_ready"] += advanced
            elif result.get("target") == "knowledge_ready":
                report["advanced_to_knowledge_ready"] += advanced

    report["observed_readiness_distribution_after"] = [
        dict(r) for r in await database.fetch_all(DISTRIBUTION_SQL)
    ]
    return report


async def _main(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        return await run_backfill(apply=args.apply, limit=args.limit)
    finally:
        await database.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="advance tiers (default: dry-run)")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="max candidate content_keys to process (0 = all)",
    )
    cli_args = parser.parse_args()

    result = asyncio.run(_main(cli_args))
    print(json.dumps(result, indent=2, default=str))
    if not cli_args.apply:
        print(
            "\nDRY-RUN — nothing written. Re-run with --apply to advance tiers.",
            file=sys.stderr,
        )
