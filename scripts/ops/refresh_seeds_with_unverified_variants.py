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

THREE MODES, in the order to run them:
  (default)   lists the cohorts and exits. Reads the DB, fetches nothing, writes nothing.
  --simulate  FETCHES each page with the fixed extractor and runs the fixed reconcile IN
              MEMORY: what `--apply` would write, per variant, before and after. Writes
              nothing (not even the offer-snapshot cache). Needs the crawl subnet.
  --apply     runs the real refresh: fetches, and WRITES the seed rows. Needs an explicit go.

    SUBNET=pivota-crawl scripts/ops/run_oneoff_job.sh \
        scripts/ops/refresh_seeds_with_unverified_variants.py --cohort drift --simulate
    SUBNET=pivota-crawl scripts/ops/run_oneoff_job.sh \
        scripts/ops/refresh_seeds_with_unverified_variants.py --cohort drift --apply

`--simulate` fetches the same URL the refresh does and applies the refresh's own "did we read
the served product" rule (`_read_the_served_product`); a row it cannot read is `not_read`,
exactly as `--apply` would leave it. It approximates only the column decision (a positive
price in the row's own currency); the full rules live in
`routes/employee_products._refresh_external_seed_by_id`.

Must run on an image that CONTAINS the fix; on an older image `--apply` re-stamps the same
unearned freshness. The script refuses to apply when the refresh it imports has no
`_reconcile_seed_variants_with_read`.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
from typing import Any, Dict, List

# `python scripts/ops/<this>.py` puts scripts/ops/ on sys.path, not the repo root, so the
# first run through run_oneoff_job.sh died on `No module named 'db'` before touching anything.
# Same line as the other scripts/ops entry points.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from db.database import database  # noqa: E402

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


async def _simulate(row_id: str) -> Dict[str, Any]:
    """What the fixed refresh would write to one row's variants. Fetches; writes nothing."""
    import routes.employee_products as ep
    from services.external_offers_service import (
        _extract_from_html,
        _fetch_html,
        _normalize_url,
        evidence_variant_fields,
    )

    row = await database.fetch_one("SELECT * FROM external_product_seeds WHERE id = :id", {"id": row_id})
    row = dict(row)
    seed_data = ep._ensure_json_obj(row.get("seed_data"))
    # The SAME url the refresh fetches (`destination_url`), and the SAME rule for whether that
    # fetch read the product we serve. Fetching `canonical_url` instead made this preview
    # report writes the refresh then refused (eyurs, fenty; see `_read_the_served_product`).
    dest = row.get("destination_url")
    url = _normalize_url(str(dest))
    observed: Dict[str, Any] = {}
    html, _ = await _fetch_html(url, observed=observed, max_wait=0)
    _, read = ep._read_the_served_product(row, dest, observed)
    if not read:
        return {"status": "not_read", "replaced": [], "not_re_read_count": 0}
    extracted = _extract_from_html(url, html)
    fields = evidence_variant_fields(extracted)
    market = str(row.get("market") or "US").upper()
    fresh_cur = (extracted.get("price_currency") or ("JPY" if market == "JP" else "USD")).upper()
    col_cur = str(row.get("price_currency") or "").strip().upper() or None
    amount = ep._as_price(extracted.get("price_amount"))
    re_read = amount is not None and amount > 0 and (col_cur is None or col_cur == fresh_cur)
    availability = str(extracted.get("availability") or "").strip()
    snapshot = seed_data.get("snapshot") if isinstance(seed_data.get("snapshot"), dict) else {}
    served = seed_data.get("variants") if isinstance(seed_data.get("variants"), list) else snapshot.get("variants")
    _, report = ep._reconcile_seed_variants_with_read(
        [v for v in (served or []) if isinstance(v, dict)],
        fields.get("variants") or [],
        product_amount=amount if re_read else None,
        product_currency=col_cur or fresh_cur,
        product_availability=availability if availability.lower() not in ("", "unknown") else None,
        census=fields.get("variant_census"),
    )
    return report


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    await database.connect()
    try:
        rows = await _select(args.cohort)
        if args.limit:
            rows = rows[: args.limit]
        summary: Dict[str, Any] = {
            "mode": "apply" if args.apply else "simulate" if args.simulate else "dry_run",
            "selected": len(rows),
            "by_cohort": dict(collections.Counter(r["cohort"] for r in rows)),
            "top_domains": collections.Counter(r["domain"] for r in rows).most_common(15),
        }
        if not (args.apply or args.simulate):
            summary["sample_ids"] = [r["id"] for r in rows[:20]]
            return summary

        import routes.employee_products as ep

        if not hasattr(ep, "_reconcile_seed_variants_with_read"):
            raise SystemExit("this image predates the variant fix; --apply would re-stamp stale rows")

        if args.simulate:
            outcomes_sim: collections.Counter = collections.Counter()
            samples: List[Dict[str, Any]] = []
            for row in rows:
                try:
                    report = await _simulate(row["id"])
                except Exception as exc:  # noqa: BLE001 - one unreachable page must not end the preview
                    outcomes_sim[f"{row['cohort']}:error:{type(exc).__name__}"] += 1
                    continue
                verdict = (
                    report.get("status")
                    or ("all_re_read" if not report["not_re_read_count"] else "not_all_re_read")
                )
                outcomes_sim[f"{row['cohort']}:{verdict}:{'writes' if report['replaced'] else 'no_write'}"] += 1
                if report["replaced"] and len(samples) < 40:
                    samples.append({"id": row["id"], "replaced": report["replaced"][:3]})
            summary["outcomes"] = dict(outcomes_sim)
            summary["replaced_samples"] = samples
            return summary

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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--simulate", action="store_true", help="FETCH, reconcile in memory, write nothing")
    mode.add_argument("--apply", action="store_true", help="FETCH and WRITE; default is a dry run")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
