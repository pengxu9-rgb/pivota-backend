#!/usr/bin/env python3
"""LabelAgent batch worker (Phase O-3b).

Reads rows from catalog_products where deterministic taxonomy
extraction (Phase O-2) left fields NULL, runs them through the
LabelAgent (services.pdp_label_agent.classify_pdp), and writes
back per the preserve-merchant semantics. Per Decision 4 (tiered
eagerness), supports scope filters so canonical-track rows can be
labelled eagerly while merchant_owned long tail runs lazily / less
often.

Default is dry-run. Pass --apply to write to the DB.

Usage:
  # See what would be classified, no DB writes, no Gemini calls:
  python scripts/run_pdp_label_agent.py --scope canonical --limit 50 --no-gemini

  # Dry run with real Gemini calls (counts cost, no DB writes):
  GEMINI_API_KEY=... python scripts/run_pdp_label_agent.py --scope canonical --limit 50

  # Apply for real (canonical PDPs eager track):
  GEMINI_API_KEY=... DATABASE_URL=... python scripts/run_pdp_label_agent.py \\
    --scope canonical --limit 100 --apply

Scope choices (Decision 4 from PDP_ONBOARDING_PLAYBOOK.md):
  canonical      — pdp_scope='multi_merchant_canonical' OR
                   source_system='catalog_enrichment_agent_v1'.
                   These get eager labelling — small population,
                   high recall surface area, ROI per Gemini call is
                   highest.
  merchant_owned — pdp_scope='merchant_owned'. Lazy tier — only run
                   when peng explicitly invokes (e.g. monthly).
  all            — both. Defensive: don't use without --limit.

Confidence gate:
  --min-confidence N (default 0.5). Skip writing fields when the
  agent returned a confidence below the threshold. Per-write log
  captures both the row and the agent's reasoning.
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
from services.pdp_label_agent import (  # noqa: E402
    classify_pdp,
    merge_classification_into_row,
    should_classify,
)
from services.pdp_lifecycle import compute_lifecycle_stage  # noqa: E402


logger = logging.getLogger("run_pdp_label_agent")


SCOPE_QUERIES = {
    "canonical": """
        SELECT product_key, merchant_id, platform, source_system,
               title, description, brand, product_type, category_path,
               image_url, tags, demographic, use_case_tags, lifestyle_tags,
               pdp_scope, pdp_lifecycle_stage
        FROM catalog_products
        WHERE (
              pdp_scope = 'multi_merchant_canonical'
           OR source_system = 'catalog_enrichment_agent_v1'
        )
          AND (
              demographic IS NULL
           OR category_path IS NULL
           OR use_case_tags IS NULL
           OR lifestyle_tags IS NULL
          )
        ORDER BY updated_at DESC NULLS LAST
        LIMIT :limit
    """,
    "merchant_owned": """
        SELECT product_key, merchant_id, platform, source_system,
               title, description, brand, product_type, category_path,
               image_url, tags, demographic, use_case_tags, lifestyle_tags,
               pdp_scope, pdp_lifecycle_stage
        FROM catalog_products
        WHERE pdp_scope = 'merchant_owned'
          AND (
              demographic IS NULL
           OR category_path IS NULL
           OR use_case_tags IS NULL
           OR lifestyle_tags IS NULL
          )
        ORDER BY updated_at DESC NULLS LAST
        LIMIT :limit
    """,
    "all": """
        SELECT product_key, merchant_id, platform, source_system,
               title, description, brand, product_type, category_path,
               image_url, tags, demographic, use_case_tags, lifestyle_tags,
               pdp_scope, pdp_lifecycle_stage
        FROM catalog_products
        WHERE (
              demographic IS NULL
           OR category_path IS NULL
           OR use_case_tags IS NULL
           OR lifestyle_tags IS NULL
          )
        ORDER BY updated_at DESC NULLS LAST
        LIMIT :limit
    """,
}


UPDATE_SQL = """
    UPDATE catalog_products
    SET demographic     = COALESCE(demographic, :demographic),
        category_path   = COALESCE(category_path, :category_path),
        use_case_tags   = COALESCE(use_case_tags,  CAST(:use_case_tags AS jsonb)),
        lifestyle_tags  = COALESCE(lifestyle_tags, CAST(:lifestyle_tags AS jsonb)),
        category_label_source = CASE
            WHEN category_path IS NULL AND :category_path IS NOT NULL
                THEN 'label_agent_v1'
            ELSE category_label_source
        END,
        -- Phase O-4b: recompute the lifecycle stage from the merged
        -- row state. Without this, a row promoted from candidate →
        -- validated by the agent fill stays at candidate forever and
        -- never surfaces in the O-5 recall live-stage filter. The
        -- caller computes :new_stage off the merged dict (same gate
        -- function the 3 ingest paths use), so the column tracks
        -- monotonically with content fills. mig 077.
        pdp_lifecycle_stage = :new_stage,
        updated_at      = NOW()
    WHERE product_key = :product_key
"""


async def _fetch_candidates(scope: str, limit: int) -> List[Dict[str, Any]]:
    sql = SCOPE_QUERIES[scope]
    rows = await database.fetch_all(sql, {"limit": int(limit)})
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        d = dict(row)
        # Database driver returns JSONB columns as strings sometimes —
        # decode before passing to should_classify (which checks
        # field types, not just None).
        for col in ("tags", "use_case_tags", "lifestyle_tags"):
            v = d.get(col)
            if isinstance(v, str):
                try:
                    d[col] = json.loads(v)
                except Exception:
                    d[col] = None
        out.append(d)
    return out


async def _process_one(
    row: Dict[str, Any],
    *,
    apply: bool,
    no_gemini: bool,
    min_confidence: float,
    api_key: Optional[str],
) -> Dict[str, Any]:
    """Classify one row and (optionally) UPDATE the DB. Returns a
    summary dict for the per-row report."""
    base = {
        "product_key": row["product_key"],
        "scope": row.get("pdp_scope"),
        "needed_fields": [
            f
            for f in ("demographic", "category_path", "use_case_tags", "lifestyle_tags")
            if row.get(f) is None
        ],
    }

    if no_gemini:
        return {**base, "drop_reason": "no_gemini_flag", "applied": False}

    result = await classify_pdp(row, api_key=api_key)
    base["drop_reason"] = result.get("drop_reason")
    base["confidence"] = result.get("confidence")
    base["model"] = result.get("model")

    if result.get("drop_reason"):
        base["applied"] = False
        return base

    if (result.get("confidence") or 0.0) < min_confidence:
        base["applied"] = False
        base["skip_reason"] = f"confidence_below_threshold_{min_confidence}"
        return base

    merged = merge_classification_into_row(row, result)
    # What did the agent actually fill?
    actually_filled = []
    for field in ("demographic", "category_path", "use_case_tags", "lifestyle_tags"):
        if row.get(field) is None and merged.get(field) is not None:
            # For lists, only count "filled" if the merged list is non-empty
            v = merged.get(field)
            if isinstance(v, list) and len(v) == 0:
                continue
            actually_filled.append(field)
    base["fields_filled"] = actually_filled

    if not actually_filled:
        base["applied"] = False
        base["skip_reason"] = "agent_filled_nothing"
        return base

    # Capture the actual classified values so dry-run reports are
    # auditable. Only include fields the agent ACTUALLY filled (not
    # ones the merge step kept from merchant data) — otherwise the
    # report would imply the agent had an opinion when it didn't.
    base["classified_values"] = {f: merged.get(f) for f in actually_filled}
    base["title"] = row.get("title")
    base["brand"] = row.get("brand")
    if result.get("reasoning"):
        base["reasoning"] = result["reasoning"]

    # Phase O-4b: compute the new lifecycle stage off the merged row
    # state — same pure gate function the 3 ingest paths use. Capture
    # before/after so the run report shows promotions explicitly. The
    # stage is monotonic with content fills, so this never demotes a
    # row in practice (LabelAgent only fills NULLs).
    old_stage = row.get("pdp_lifecycle_stage")
    new_stage = compute_lifecycle_stage(merged)
    base["lifecycle_stage_before"] = old_stage
    base["lifecycle_stage_after"] = new_stage
    base["lifecycle_promoted"] = old_stage != new_stage

    if not apply:
        base["applied"] = False
        return base

    await database.execute(
        UPDATE_SQL,
        {
            "product_key": row["product_key"],
            "demographic": merged.get("demographic"),
            "category_path": merged.get("category_path"),
            "use_case_tags": json.dumps(merged.get("use_case_tags") or []),
            "lifestyle_tags": json.dumps(merged.get("lifestyle_tags") or []),
            "new_stage": new_stage,
        },
    )
    base["applied"] = True
    return base


async def _connect_with_retry(*, attempts: int = 3, backoff_s: tuple = (5.0, 15.0, 30.0)) -> None:
    """Railway's pgbouncer proxy occasionally TCP-stalls connect attempts
    for 60s+ and then surfaces an asyncio.TimeoutError. Retry with
    exponential-ish backoff so a transient proxy hiccup doesn't kill an
    already-in-progress run. Bounded to keep it from waiting forever."""
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
    await _connect_with_retry()

    candidates = await _fetch_candidates(args.scope, args.limit)
    logger.info(
        "fetched %d candidate rows (scope=%s, limit=%d)",
        len(candidates),
        args.scope,
        args.limit,
    )

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("PIVOTA_GEMINI_API_KEY")
    if not args.no_gemini and not api_key:
        logger.error(
            "no GEMINI_API_KEY in environment; pass --no-gemini for dry-shape audit"
        )
        return {"error": "missing_gemini_api_key"}

    per_row: List[Dict[str, Any]] = []
    drop_counter: Counter = Counter()
    fields_filled_counter: Counter = Counter()
    promotion_counter: Counter = Counter()
    applied = 0

    for row in candidates:
        result = await _process_one(
            row,
            apply=args.apply,
            no_gemini=args.no_gemini,
            min_confidence=args.min_confidence,
            api_key=api_key,
        )
        per_row.append(result)
        if result.get("drop_reason"):
            drop_counter[result["drop_reason"]] += 1
        for field in result.get("fields_filled") or []:
            fields_filled_counter[field] += 1
        if result.get("applied"):
            applied += 1
        if result.get("lifecycle_promoted"):
            promotion_key = (
                f"{result.get('lifecycle_stage_before') or 'null'}"
                f"->{result.get('lifecycle_stage_after')}"
            )
            promotion_counter[promotion_key] += 1

    return {
        "scope": args.scope,
        "limit": args.limit,
        "apply": args.apply,
        "no_gemini": args.no_gemini,
        "min_confidence": args.min_confidence,
        "candidate_count": len(candidates),
        "applied_count": applied,
        "drop_reason_counts": dict(drop_counter),
        "fields_filled_counts": dict(fields_filled_counter),
        "lifecycle_promotion_counts": dict(promotion_counter),
        "per_row": per_row,
    }


def _write_report(report: Dict[str, Any]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path("reports/o3_label_agent") / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "run.json"
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return summary_path


def _print_summary(report: Dict[str, Any]) -> None:
    print()
    print(f"=== LabelAgent run summary ===")
    print(f"  scope:           {report.get('scope')}")
    print(f"  limit:           {report.get('limit')}")
    print(f"  apply:           {report.get('apply')}")
    print(f"  no_gemini:       {report.get('no_gemini')}")
    print(f"  min_confidence:  {report.get('min_confidence')}")
    print(f"  candidate count: {report.get('candidate_count')}")
    print(f"  applied count:   {report.get('applied_count')}")
    drops = report.get("drop_reason_counts") or {}
    if drops:
        print(f"  drop reasons:")
        for reason, count in sorted(drops.items(), key=lambda x: -x[1]):
            print(f"    {reason:38s} {count}")
    fields = report.get("fields_filled_counts") or {}
    if fields:
        print(f"  fields filled:")
        for field, count in sorted(fields.items(), key=lambda x: -x[1]):
            print(f"    {field:24s} {count}")
    promotions = report.get("lifecycle_promotion_counts") or {}
    if promotions:
        print(f"  lifecycle promotions:")
        for transition, count in sorted(promotions.items(), key=lambda x: -x[1]):
            print(f"    {transition:30s} {count}")


async def _run(args: argparse.Namespace) -> int:
    report = await _drive(args)
    if "error" in report:
        return 1
    summary_path = _write_report(report)
    _print_summary(report)
    print(f"\n  full report: {summary_path}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=("canonical", "merchant_owned", "all"),
        default="canonical",
        help="which rows to consider — see docstring",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="max rows to fetch (default 25 — keep small while iterating)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually UPDATE rows (default dry-run)",
    )
    parser.add_argument(
        "--no-gemini",
        action="store_true",
        help="skip the Gemini call entirely; just count which rows would be candidates",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.5,
        help="minimum agent confidence (0.0-1.0) before writing back; default 0.5",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
