"""Re-run the seed refresh on rows whose served VARIANTS the old refresh never re-read.

Until the fix beside this script, `_refresh_external_seed_by_id` corrected the price and
availability COLUMNS and stamped `snapshot.extracted_at`, but left `seed_data.variants[]`
alone -- and the serving builders price and stock every offer from those variants. Two cohorts
carry that unearned stamp today:

  drift   ONE stored variant whose price or stock disagrees with the column the refresh
          re-read (the eyurs Round Lab sunscreen: column 17.0/in_stock, served 16.0/out).
          The fixed refresh writes the re-read values into the variant.
  multi   gate-fresh rows with SEVERAL stored variants. Their stamp vouched for sibling prices
          nobody re-read. The fixed refresh either re-reads every sibling (by variant id / SKU /
          barcode) or removes the stamp, so the gate serves them `live_quote_required`.

The nightly `external-referral-refresh` heals both as it rotates; this only gets there sooner.

DRY RUN BY DEFAULT: lists the cohorts and exits. `--apply` FETCHES EACH MERCHANT PAGE and
WRITES the seed rows, so it needs the crawl subnet and an explicit go:

    SUBNET=pivota-crawl scripts/ops/run_oneoff_job.sh \
        scripts/ops/refresh_seeds_with_unverified_variants.py --cohort drift --apply

Must run on an image that CONTAINS the fix; on an older image `--apply` re-stamps the same
unearned freshness. The script refuses to apply when the refresh it imports has no
`_reconcile_seed_variants_with_read`.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
from typing import Any, Dict, List

from db.database import database

_FRESH = """
  status = 'active'
  AND jsonb_typeof(seed_data->'variants') = 'array'
  AND (seed_data->'snapshot'->>'extracted_at') IS NOT NULL
  AND (seed_data->'snapshot'->>'extracted_at')::timestamptz >= NOW() - INTERVAL '7 days'
"""

# ONE variant, and the column (what the refresh re-read) disagrees with it on a KNOWN value.
_DRIFT_SQL = f"""
SELECT id, domain FROM external_product_seeds
WHERE {_FRESH}
  AND jsonb_array_length(seed_data->'variants') = 1
  AND (
    (
      price_amount IS NOT NULL
      AND COALESCE(seed_data->'variants'->0->>'price_amount', seed_data->'variants'->0->>'price') ~ '^[0-9]+(\\.[0-9]+)?$'
      AND ABS(price_amount - COALESCE(seed_data->'variants'->0->>'price_amount', seed_data->'variants'->0->>'price')::numeric) >= 0.005
    )
    OR (
      lower(availability) IN ('in_stock', 'out_of_stock')
      AND lower(seed_data->'variants'->0->>'availability') IN ('in_stock', 'out_of_stock')
      AND lower(availability) <> lower(seed_data->'variants'->0->>'availability')
    )
  )
ORDER BY domain, id
"""

_MULTI_SQL = f"""
SELECT id, domain FROM external_product_seeds
WHERE {_FRESH}
  AND jsonb_array_length(seed_data->'variants') > 1
ORDER BY domain, id
"""


async def _select(cohort: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if cohort in ("drift", "all"):
        rows += [dict(r) | {"cohort": "drift"} for r in await database.fetch_all(_DRIFT_SQL)]
    if cohort in ("multi", "all"):
        rows += [dict(r) | {"cohort": "multi"} for r in await database.fetch_all(_MULTI_SQL)]
    return rows


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    await database.connect()
    try:
        rows = await _select(args.cohort)
        if args.limit:
            rows = rows[: args.limit]
        summary: Dict[str, Any] = {
            "mode": "apply" if args.apply else "dry_run",
            "selected": len(rows),
            "by_cohort": dict(collections.Counter(r["cohort"] for r in rows)),
            "top_domains": collections.Counter(r["domain"] for r in rows).most_common(15),
        }
        if not args.apply:
            summary["sample_ids"] = [r["id"] for r in rows[:20]]
            return summary

        import routes.employee_products as ep

        if not hasattr(ep, "_reconcile_seed_variants_with_read"):
            raise SystemExit("this image predates the variant fix; --apply would re-stamp stale rows")

        outcomes: collections.Counter = collections.Counter()
        for row in rows:
            try:
                result = await ep._refresh_external_seed_by_id(row["id"], max_wait=0)
            except Exception as exc:  # noqa: BLE001 - one bad row must not end the repair
                outcomes[f"{row['cohort']}:error:{type(exc).__name__}"] += 1
                continue
            variant_status = (result.get("variant_refresh") or {}).get("status") or "none"
            outcomes[f"{row['cohort']}:{result.get('status')}:{variant_status}"] += 1
        summary["outcomes"] = dict(outcomes)
        return summary
    finally:
        await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cohort", choices=("drift", "multi", "all"), default="drift")
    parser.add_argument("--limit", type=int, default=0, help="0 = every selected row")
    parser.add_argument("--apply", action="store_true", help="FETCH and WRITE; default is a dry run")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
