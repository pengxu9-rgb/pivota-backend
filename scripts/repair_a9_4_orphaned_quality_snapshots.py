#!/usr/bin/env python3
"""Re-point the ``product_quality_snapshot`` rows the ADR-009 A9-4 flip orphaned.

WHAT BROKE (2026-08-14, diagnosed 2026-08-15). The A9-4 re-key moved 11,099
``catalog_products`` rows onto their observed seller-of-record. Every reflected
dependent followed them — except ``product_quality_snapshot``, which
``SellerBackfill.discover_cascade_tables`` never selected: that reflection
requires a product-scope column in (product_key, content_key, sku_key), and this
table scopes by ``(platform, platform_product_id)`` instead, so it fell through
the ``continue`` and was silently skipped.

The classifier reads the score with a correlated lookup keyed on the CURRENT
owner::

    WHERE pqs.merchant_id       = cp.merchant_id
      AND pqs.platform          = cp.platform
      AND pqs.platform_product_id = cp.source_product_id

so the moment ``cp.merchant_id`` moved, the lookup missed and
``content_quality_score`` read NULL. NULL then failed the quality gate, and the
sitemap-eligible set halved: 8,222 -> 3,884 content_keys, held off the public
sitemap only because the agent-ui publish cron was independently wedged.

**The scores were never lost — they were orphaned.** This is a re-pointing, not
a re-score: no scorer runs, no content is re-read, no score CHANGES. Measured on
prod 2026-08-15: 6,424 of 6,466 unscored rows have snapshots under exactly one
other merchant and NONE under their current merchant; zero are ambiguous.

PROVENANCE IS THE WHOLE SAFETY ARGUMENT. The cohort is not "rows that look
unscored" — it is bounded by the flip's OWN checkpoint table, and every row must
satisfy all three of:
  * ``a9_4_backfill_checkpoint`` has it done in the catalog phase;
  * the donor holding its snapshots IS that checkpoint's ``previous_value``;
  * its current ``catalog_products.merchant_id`` IS that checkpoint's
    ``observed_id`` (so the flip's result is still standing and we are not
    fighting a later, deliberate re-key).
All three held for 6,424/6,424 when measured. A row that fails any of them is
left alone — this tool repairs the flip's cascade miss and nothing else, and it
can never merge two genuinely different merchants' quality history.

Safe to re-run: a repaired product no longer has an empty destination, so it
leaves the cohort. There is no unique index on
(merchant_id, platform, platform_product_id) — only the ``id`` PK and a
non-unique btree — so the UPDATE cannot collide.

DRY-RUN by default; nothing is written without BOTH ``--apply`` and
``--confirm REPAIR_A9_4_QUALITY_SNAPSHOTS``.

Dry-run:
  python -m scripts.repair_a9_4_orphaned_quality_snapshots

Apply:
  python -m scripts.repair_a9_4_orphaned_quality_snapshots \
      --apply --confirm REPAIR_A9_4_QUALITY_SNAPSHOTS
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.database import database  # noqa: E402
from services.catalog_row_trust_upserter import (  # noqa: E402
    upsert_catalog_row_trust_many,
)
from services.index_pipeline_state_service import (  # noqa: E402
    recompute_serving_eligibility,
)

CONFIRM_TOKEN = "REPAIR_A9_4_QUALITY_SNAPSHOTS"

# The cohort, stated as the three provenance conjuncts in the module docstring.
# `previous_value <> observed_id` keeps a no-op checkpoint row from selecting a
# product whose "donor" and "target" are the same merchant.
COHORT_SQL = """
WITH moved AS (
    SELECT k.ref_id AS product_key,
           k.observed_id,
           k.previous_value
      FROM a9_4_backfill_checkpoint k
     WHERE k.phase = 'catalog'
       AND k.status = 'done'
       AND k.observed_id IS NOT NULL
       AND k.previous_value IS NOT NULL
       AND k.previous_value <> k.observed_id
)
SELECT cp.product_key,
       cp.content_key,
       cp.platform,
       cp.source_product_id,
       m.observed_id,
       m.previous_value
  FROM moved m
  JOIN catalog_products cp ON cp.product_key = m.product_key
 WHERE cp.merchant_id = m.observed_id
   AND cp.platform IS NOT NULL
   AND cp.source_product_id IS NOT NULL
   -- destination is empty: nothing to clobber, and this is what makes re-runs safe
   AND NOT EXISTS (
       SELECT 1 FROM product_quality_snapshot p
        WHERE p.platform = cp.platform
          AND p.platform_product_id = cp.source_product_id
          AND p.merchant_id = m.observed_id)
   -- donor is exactly the merchant the flip moved this row OFF of
   AND EXISTS (
       SELECT 1 FROM product_quality_snapshot p
        WHERE p.platform = cp.platform
          AND p.platform_product_id = cp.source_product_id
          AND p.merchant_id = m.previous_value)
 ORDER BY cp.product_key
"""

REPOINT_SQL = """
UPDATE product_quality_snapshot
   SET merchant_id = :new_merchant
 WHERE platform = :platform
   AND platform_product_id = :spid
   AND merchant_id = :old_merchant
"""

# Counted separately rather than from the UPDATE's return: `databases.execute`
# does not surface a rowcount for asyncpg, so summing it would silently report
# 0 repointed on a run that moved thousands — a report that lies in the
# reassuring direction.
DONOR_ROWS_SQL = """
SELECT count(*) FROM product_quality_snapshot
 WHERE platform = :platform
   AND platform_product_id = :spid
   AND merchant_id = :old_merchant
"""


async def _connect_if_needed() -> bool:
    if getattr(database, "is_connected", False):
        return True
    await database.connect()
    return False


async def _disconnect_if_needed(was_connected: bool) -> None:
    if not was_connected:
        await database.disconnect()


async def _load_cohort() -> List[Dict[str, Any]]:
    rows = await database.fetch_all(COHORT_SQL)
    return [dict(r) for r in (rows or [])]


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    cohort = await _load_cohort()
    report: Dict[str, Any] = {
        "cohort": len(cohort),
        "applied": False,
        "snapshots_repointed": 0,
        "products_repointed": 0,
        "content_keys_recomputed": 0,
        "became_serving_eligible": 0,
        "trust_rows_written": 0,
        "failures": [],
    }
    if not cohort:
        return report

    report["sample"] = [
        {
            "product_key": r["product_key"],
            "from": r["previous_value"],
            "to": r["observed_id"],
        }
        for r in cohort[:5]
    ]

    if not args.apply:
        return report

    report["applied"] = True

    # Step 1 — re-point the snapshots so the classifier's lookup resolves again.
    for row in cohort:
        try:
            binds = {
                "new_merchant": row["observed_id"],
                "old_merchant": row["previous_value"],
                "platform": row["platform"],
                "spid": row["source_product_id"],
            }
            moving = await database.fetch_val(
                DONOR_ROWS_SQL,
                {k: binds[k] for k in ("platform", "spid", "old_merchant")},
            )
            await database.execute(REPOINT_SQL, binds)
            report["products_repointed"] += 1
            report["snapshots_repointed"] += int(moving or 0)
        except Exception as exc:  # one product must never abort the run
            report["failures"].append(
                {"product_key": row["product_key"], "stage": "repoint", "error": str(exc)}
            )

    # Step 2 — reclassify. The score is only an INPUT to index_pipeline_state;
    # without this the restored score sits unread and nothing re-serves.
    content_keys = sorted({r["content_key"] for r in cohort if r.get("content_key")})
    for ck in content_keys:
        try:
            became_eligible = await recompute_serving_eligibility(ck, reason="a9_4_quality_repair")
            report["content_keys_recomputed"] += 1
            if became_eligible:
                report["became_serving_eligible"] += 1
        except Exception as exc:
            report["failures"].append(
                {"content_key": ck, "stage": "recompute", "error": str(exc)}
            )

    # Step 3 — the trust flip. serving_eligible alone leaves the row
    # `blocked` for public READERS until the phase-2d drift cron notices; the
    # sibling external-seed rescore learned this the hard way (proven live
    # 2026-07-23), so promote durably here instead of waiting for a cron.
    if not args.skip_trust:
        product_keys = [r["product_key"] for r in cohort]
        for i in range(0, len(product_keys), args.trust_chunk):
            chunk = product_keys[i : i + args.trust_chunk]
            try:
                written = await upsert_catalog_row_trust_many(db=database, product_keys=chunk)
                if isinstance(written, int):
                    report["trust_rows_written"] += written
            except Exception as exc:
                report["failures"].append(
                    {"stage": "trust", "chunk_start": i, "error": str(exc)}
                )

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write. Without this the tool only reports the cohort.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Must be {CONFIRM_TOKEN!r} when --apply is set.",
    )
    parser.add_argument(
        "--skip-trust",
        action="store_true",
        help="Do step 1+2 only, leaving the catalog_row_trust flip to the drift cron.",
    )
    parser.add_argument(
        "--trust-chunk",
        type=int,
        default=200,
        help="Product keys per catalog_row_trust batch (default 200).",
    )
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    was_connected = await _connect_if_needed()
    try:
        report = await _drive(args)
    finally:
        await _disconnect_if_needed(was_connected)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 1 if report["failures"] else 0


def main() -> int:
    args = build_parser().parse_args()
    if args.apply and args.confirm != CONFIRM_TOKEN:
        print(
            f"refusing to apply: --confirm must be {CONFIRM_TOKEN!r}",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
