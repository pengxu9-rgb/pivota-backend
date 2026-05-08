#!/usr/bin/env python3
"""One-shot backfill for catalog_products.pdp_lifecycle_stage (Phase O-6b).

After O-4 ships, every NEW write through the 3 onboarding paths sets
pdp_lifecycle_stage at write time. Existing rows are NULL — and the
O-5 recall filter (pdp_lifecycle_stage IN ('validated', 'published'))
would drop them all. This script computes the stage for each existing
row from its current content + taxonomy + scope state, then UPDATEs
in batches.

Default is dry-run. Pass --apply to write to the DB.

Usage:
  # See histogram of stages without touching the DB:
  python scripts/backfill_pdp_lifecycle_stage.py --limit 1000

  # Apply against a small slice first (smoke test):
  DATABASE_URL=... python scripts/backfill_pdp_lifecycle_stage.py \\
    --limit 50 --apply

  # Full backfill:
  DATABASE_URL=... python scripts/backfill_pdp_lifecycle_stage.py \\
    --limit 100000 --apply

Idempotency: only processes rows where pdp_lifecycle_stage IS NULL.
Re-runs are safe — already-staged rows are skipped at SELECT time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from services.pdp_lifecycle import compute_lifecycle_stage  # noqa: E402


logger = logging.getLogger("backfill_pdp_lifecycle_stage")


SELECT_SQL = """
    SELECT product_key,
           title, description, image_url, category_path,
           tags, demographic, use_case_tags, lifestyle_tags,
           pdp_scope, source_system
    FROM catalog_products
    WHERE pdp_lifecycle_stage IS NULL
    ORDER BY updated_at DESC NULLS LAST
    LIMIT :limit
"""


UPDATE_SQL = """
    UPDATE catalog_products
    SET pdp_lifecycle_stage = :stage,
        updated_at = NOW()
    WHERE product_key = :product_key
      AND pdp_lifecycle_stage IS NULL
"""


def _normalize_jsonb_field(value: Any) -> Any:
    """Driver returns JSONB columns as either list or JSON-encoded
    string depending on the codec path. compute_lifecycle_stage
    handles both, but normalizing here makes the per-row report
    cleaner."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


async def _connect_with_retry(*, attempts: int = 3, backoff_s: tuple = (5.0, 15.0, 30.0)) -> None:
    if getattr(database, "is_connected", False):
        return
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            await database.connect()
            return
        except (asyncio.TimeoutError, OSError, ConnectionError) as exc:
            last_exc = exc
            if i + 1 < attempts:
                wait = backoff_s[i] if i < len(backoff_s) else backoff_s[-1]
                logger.warning(
                    "DB connect attempt %d/%d failed (%s); retrying in %.0fs",
                    i + 1, attempts, type(exc).__name__, wait,
                )
                await asyncio.sleep(wait)
            else:
                raise
    if last_exc is not None:
        raise last_exc


async def _fetch_candidates(limit: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(SELECT_SQL, {"limit": int(limit)})
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        d = dict(row)
        for col in ("tags", "use_case_tags", "lifestyle_tags"):
            d[col] = _normalize_jsonb_field(d.get(col))
        out.append(d)
    return out


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    await _connect_with_retry()
    candidates = await _fetch_candidates(args.limit)
    logger.info("fetched %d NULL-stage rows (limit=%d)", len(candidates), args.limit)

    stage_counter: Counter = Counter()
    per_scope_stage: Dict[str, Counter] = {}
    applied = 0
    sample_rows: List[Dict[str, Any]] = []

    for row in candidates:
        stage = compute_lifecycle_stage(row)
        stage_counter[stage] += 1
        scope_key = row.get("pdp_scope") or row.get("source_system") or "unknown"
        per_scope_stage.setdefault(scope_key, Counter())[stage] += 1

        if len(sample_rows) < 12:
            sample_rows.append({
                "product_key": row.get("product_key"),
                "title": row.get("title"),
                "pdp_scope": row.get("pdp_scope"),
                "source_system": row.get("source_system"),
                "category_path": row.get("category_path"),
                "computed_stage": stage,
            })

        if args.apply:
            await database.execute(
                UPDATE_SQL,
                {"product_key": row["product_key"], "stage": stage},
            )
            applied += 1

    return {
        "limit": args.limit,
        "apply": args.apply,
        "candidate_count": len(candidates),
        "applied_count": applied,
        "stage_counts": dict(stage_counter),
        "per_scope_stage": {k: dict(v) for k, v in per_scope_stage.items()},
        "sample_rows": sample_rows,
    }


def _write_report(report: Dict[str, Any]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path("reports/o6b_lifecycle_backfill") / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "run.json"
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return summary_path


def _print_summary(report: Dict[str, Any]) -> None:
    print()
    print("=== Lifecycle backfill summary ===")
    print(f"  limit:           {report.get('limit')}")
    print(f"  apply:           {report.get('apply')}")
    print(f"  candidate count: {report.get('candidate_count')}")
    print(f"  applied count:   {report.get('applied_count')}")
    stages = report.get("stage_counts") or {}
    if stages:
        print("  computed stages:")
        for stage, count in sorted(stages.items(), key=lambda x: -x[1]):
            print(f"    {stage:24s} {count}")
    per_scope = report.get("per_scope_stage") or {}
    if per_scope:
        print("  per-scope breakdown:")
        for scope, counts in per_scope.items():
            counts_str = ", ".join(
                f"{s}={n}" for s, n in sorted(counts.items(), key=lambda x: -x[1])
            )
            print(f"    {scope:42s} {counts_str}")


async def _run(args: argparse.Namespace) -> int:
    report = await _drive(args)
    summary_path = _write_report(report)
    _print_summary(report)
    print(f"\n  full report: {summary_path}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="max rows to process per run (default 1000; full table is ~5k)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually UPDATE rows (default dry-run)",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
