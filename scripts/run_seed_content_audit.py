#!/usr/bin/env python3
"""Run services.seed_content_audit across external_product_seeds in
prod, write cleaned seed_data + review_summary back, and propagate
the cleaned content to catalog_products so chat shows it immediately.

Replaces codex's `seed-content-audit` + `seed-correction` skill cycle
with deterministic Python (no LLM calls). PR #409 (preserve review
fields on re-mirror) makes the writes stick across subsequent
backfills.

Usage:
  # Dry-run: count how many rows would be touched, show histograms.
  python3 scripts/run_seed_content_audit.py

  # Smoke test on a small slice.
  python3 scripts/run_seed_content_audit.py --apply --limit 50

  # Full audit (idempotent — already-audited rows are skipped unless
  # --force re-audits them).
  python3 scripts/run_seed_content_audit.py --apply --limit 0

  # Re-audit even rows already marked auto_corrected (e.g. after a
  # new auditor version landed).
  python3 scripts/run_seed_content_audit.py --apply --force --limit 0

The catalog_products write is intentional: external_product_seeds is
the source of truth, but chat reads from catalog_products. Updating
both in the same loop makes the cleanup user-visible without waiting
for the mirror script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.seed_content_audit import (  # noqa: E402
    AUDITOR_VERSION,
    audit_seed_data,
)


logger = logging.getLogger("run_seed_content_audit")


PROGRESS_LOG_EVERY = 250


def _select_sql(*, force: bool, limit: int, offset: int = 0) -> str:
    """Build the SELECT for candidate rows.

    Default (force=False): rows that have NOT been audited by the
    current auditor version. Re-runnable safely.
    Force=True: re-audit everything; useful when a new auditor
    version lands and old `auto_corrected` rows need a refresh.

    `offset` lets the operator paginate over a force-mode run.
    Important under --force: every applied chunk SETs `updated_at = NOW()`
    so subsequent chunks ordered by `updated_at DESC` would re-process
    the same rows. Order by `id` (deterministic, write-stable) instead
    so paginated runs cover the full table without duplication."""
    audited_filter = (
        ""
        if force
        else (
            "AND ("
            "  seed_data->'review_summary' IS NULL"
            "  OR seed_data->'review_summary'->>'auditor' != :auditor_version"
            ")"
        )
    )
    # Force mode uses a write-stable sort (id) so --offset is meaningful
    # across chunks. Default mode keeps updated_at DESC so freshest
    # unaudited rows come first.
    order_clause = "ORDER BY id ASC" if force else "ORDER BY updated_at DESC NULLS LAST"
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    offset_clause = "OFFSET :offset" if offset > 0 else ""
    return f"""
        SELECT id, external_product_id, market, seed_data, updated_at
        FROM external_product_seeds
        WHERE 1=1
          {audited_filter}
        {order_clause}
        {limit_clause}
        {offset_clause}
    """


# Update both tables in one transaction per row so chat sees the
# clean content immediately without waiting for the mirror script.
UPDATE_SEED_SQL = """
    UPDATE external_product_seeds
    SET seed_data = CAST(:seed_data AS jsonb),
        updated_at = NOW()
    WHERE id = :seed_id
"""

UPDATE_CATALOG_PRODUCTS_SQL = """
    UPDATE catalog_products
    SET description = COALESCE(:description, description),
        product_payload = jsonb_set(
            product_payload,
            '{seed_data}',
            CAST(:seed_data AS jsonb),
            true
        ),
        updated_at = NOW()
    WHERE source_product_id = :external_product_id
      AND merchant_id = 'external_seed'
"""


async def _fetch_candidates(args: argparse.Namespace) -> List[Dict[str, Any]]:
    offset = int(getattr(args, "offset", 0) or 0)
    sql = _select_sql(force=args.force, limit=args.limit, offset=offset)
    # Only bind params that actually appear in the SQL — sqlalchemy /
    # databases.text() rejects extra binds with `ArgumentError: This
    # text() construct doesn't define a bound parameter named ...`.
    # When force=True the SQL drops the :auditor_version bind, so we
    # must drop it from params too.
    params: Dict[str, Any] = {}
    if not args.force:
        params["auditor_version"] = AUDITOR_VERSION
    if args.limit > 0:
        params["limit"] = int(args.limit)
    if offset > 0:
        params["offset"] = offset
    rows = await database.fetch_all(sql, params)
    return [dict(r) for r in rows or []]


def _coerce_seed_data(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


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


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    candidates = await _fetch_candidates(args)
    logger.info(
        "fetched %d audit candidates (force=%s, limit=%d, apply=%s)",
        len(candidates), args.force, args.limit, args.apply,
    )

    issue_counter: Counter = Counter()
    fix_counter: Counter = Counter()
    status_counter: Counter = Counter()
    applied = 0
    catalog_products_updated = 0
    sample_rows: List[Dict[str, Any]] = []
    started_at = datetime.now(timezone.utc)

    for idx, row in enumerate(candidates, start=1):
        seed_data = _coerce_seed_data(row.get("seed_data"))
        if seed_data is None:
            status_counter["skipped_seed_data_unparseable"] += 1
            continue

        cleaned, summary = audit_seed_data(seed_data)
        status_counter[summary["review_status"]] += 1
        for issue in summary["issues_detected"]:
            issue_counter[issue] += 1
        for fix in summary["fixes_applied"]:
            fix_counter[fix] += 1

        if len(sample_rows) < 12 and summary["fixes_applied"]:
            sample_rows.append({
                "id": row.get("id"),
                "external_product_id": row.get("external_product_id"),
                "issues": summary["issues_detected"],
                "fixes": summary["fixes_applied"],
            })

        if not args.apply:
            continue

        # Merge the review_summary into the cleaned seed_data so the
        # full payload we write is self-describing.
        cleaned_with_summary = dict(cleaned)
        cleaned_with_summary["review_summary"] = summary
        cleaned_json = json.dumps(cleaned_with_summary, ensure_ascii=False, default=str)

        # External seeds: write back regardless (so future backfills
        # see the auditor stamp via review_summary).
        await database.execute(
            UPDATE_SEED_SQL,
            {"seed_id": row["id"], "seed_data": cleaned_json},
        )

        # Catalog products: only update if a fix was applied (avoids
        # touching catalog_products when nothing changed).
        if summary["fixes_applied"]:
            cat_result = await database.execute(
                UPDATE_CATALOG_PRODUCTS_SQL,
                {
                    "external_product_id": row["external_product_id"],
                    "description": cleaned.get("description"),
                    "seed_data": cleaned_json,
                },
            )
            # asyncpg returns the rowcount-equivalent — count any nonzero
            # as a successful catalog_products write.
            if cat_result is not None:
                catalog_products_updated += 1

        applied += 1

        if idx % PROGRESS_LOG_EVERY == 0:
            logger.info(
                "progress %d/%d (applied=%d, catalog_updated=%d)",
                idx, len(candidates), applied, catalog_products_updated,
            )

    finished_at = datetime.now(timezone.utc)
    return {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_s": round((finished_at - started_at).total_seconds(), 2),
        "force": args.force,
        "limit": args.limit,
        "apply": args.apply,
        "candidate_count": len(candidates),
        "applied_count": applied,
        "catalog_products_updated": catalog_products_updated,
        "status_counts": dict(status_counter),
        "issue_counts": dict(issue_counter.most_common(20)),
        "fix_counts": dict(fix_counter.most_common(20)),
        "sample_rows": sample_rows,
    }


def _print_summary(report: Dict[str, Any]) -> None:
    print()
    print("=== Seed content audit summary ===")
    print(f"  apply:                {report.get('apply')}")
    print(f"  force:                {report.get('force')}")
    print(f"  candidate count:      {report.get('candidate_count')}")
    if report.get("apply"):
        print(f"  applied count:        {report.get('applied_count')}")
        print(f"  catalog_products updated: {report.get('catalog_products_updated')}")
    print(f"  duration:             {report.get('duration_s')}s")

    statuses = report.get("status_counts") or {}
    if statuses:
        print("  review_status:")
        for s, n in sorted(statuses.items(), key=lambda x: -x[1]):
            print(f"    {s:30s} {n}")
    issues = report.get("issue_counts") or {}
    if issues:
        print("  top issues:")
        for i, n in issues.items():
            print(f"    {i:42s} {n}")
    fixes = report.get("fix_counts") or {}
    if fixes:
        print("  top fixes applied:")
        for f, n in fixes.items():
            print(f"    {f:42s} {n}")


def _write_report(report: Dict[str, Any]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path("reports/seed_content_audit") / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "run.json"
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return summary_path


async def _run(args: argparse.Namespace) -> int:
    await _connect_with_retry()
    try:
        report = await _drive(args)
    finally:
        try:
            await database.disconnect()
        except Exception:
            pass
    summary_path = _write_report(report)
    _print_summary(report)
    print(f"\n  full report: {summary_path}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually UPDATE rows (default dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="max rows to process (0 = all). Default 50 for safety; "
        "for the full prod cleanup pass --limit 0",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-audit rows already stamped by the current auditor version",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="skip the first N rows (paginate force-mode runs over a "
        "large table). Force mode orders by id ASC, so --offset N "
        "--limit M reliably covers a fresh slice on each call. Default 0.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
