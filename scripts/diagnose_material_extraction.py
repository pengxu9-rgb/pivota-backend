#!/usr/bin/env python3
"""Material-extraction outlier diagnostic.

The coverage report showed material at 0.7% (1/148) vs care 33% / size_guide 40%.
This script samples N fashion-categorized catalog_products rows where
material IS NULL, calls the Deepseek batched extractor directly on each,
and classifies the outcome bucket for material specifically:

  - llm_declined         — LLM returned value=null + reason ≈ "not_stated"
  - paraphrased          — LLM returned a value but substring grounding rejected
                           (the LLM didn't copy verbatim)
  - low_self_confidence  — LLM extracted, grounding passed, but final confidence
                           < 0.6 trust gate
  - would_populate       — LLM extracted, grounded, confidence ≥ 0.6 (= what
                           A1's live wiring will start writing once it ships)
  - llm_error            — Deepseek transport/parse fail
  - haystack_empty       — row has no description text (skipped before LLM)

The output tells us whether the material gap is fixed by:
  (a) wiring A1 alone (if many rows are "would_populate" — they just haven't
      been re-extracted since the data landed),
  (b) loosening substring grounding (if many rows are "paraphrased"),
  (c) the merchant actually not authoring material info (if "llm_declined"
      dominates — no fix beyond requesting metafield data).

Uses the *deployed* batch extractor + grounding so the classification
matches what the live ingest path would do today.

Usage:
  python scripts/diagnose_material_extraction.py --sample 20
  python scripts/diagnose_material_extraction.py --sample 50 --merchant-id merch_xxx

Read-only. Does NOT write to catalog_products. Does call Deepseek (cost ≈
$0.0001 × sample size; 50 samples = $0.005).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from services.fashion_field_extractor import (  # noqa: E402
    _build_batch_user_message,
    _call_deepseek_batch,
    _substring_grounded,
    _extract_enabled,
    _is_fashion_category,
)


_TRUST_GATE = 0.6


async def _sample_rows(
    *, merchant_id: Optional[str], sample: int
) -> List[Dict[str, Any]]:
    where_merchant = "AND cp.merchant_id = :merchant_id" if merchant_id else ""
    sql = f"""
        SELECT
          cp.product_key,
          cp.merchant_id,
          cp.platform,
          cp.title,
          cp.description,
          cp.category_path,
          cp.product_payload
        FROM catalog_products cp
        WHERE cp.material IS NULL
          AND (
            LOWER(cp.category_path) LIKE 'fashion/%'
            OR LOWER(cp.category_path) LIKE 'apparel/%'
            OR LOWER(cp.category_path) LIKE 'clothing/%'
            OR LOWER(cp.category_path) LIKE 'shoes/%'
            OR LOWER(cp.category_path) LIKE 'accessories/%'
          )
          AND COALESCE(NULLIF(TRIM(cp.description), ''), '') <> ''
          {where_merchant}
        ORDER BY RANDOM()
        LIMIT :sample
    """
    rows = await database.fetch_all(
        sql, {"sample": sample, "merchant_id": merchant_id} if merchant_id else {"sample": sample},
    )
    return [dict(r) for r in rows or []]


def _classify_material(*, raw_response: Any, haystack: str) -> Dict[str, Any]:
    """Bucket one row's outcome for the material field."""
    if raw_response is None:
        return {"bucket": "llm_error", "raw_value": None, "self_confidence": None,
                "substring_score": None, "final_confidence": None, "reason": None}
    if not isinstance(raw_response, dict):
        return {"bucket": "llm_error", "raw_value": None, "self_confidence": None,
                "substring_score": None, "final_confidence": None, "reason": "non_dict_response"}
    mat = raw_response.get("material")
    if not isinstance(mat, dict):
        return {"bucket": "llm_error", "raw_value": None, "self_confidence": None,
                "substring_score": None, "final_confidence": None,
                "reason": f"material_subobject_missing_or_non_dict: {type(mat).__name__}"}
    raw_value = mat.get("value")
    self_report = mat.get("confidence")
    reason = mat.get("reason")
    self_report_n = (
        float(self_report)
        if isinstance(self_report, (int, float)) and 0.0 <= float(self_report) <= 1.0
        else 0.5
    )
    if not isinstance(raw_value, str) or not raw_value.strip():
        return {"bucket": "llm_declined", "raw_value": None, "self_confidence": self_report_n,
                "substring_score": None, "final_confidence": None, "reason": reason}
    raw_value = raw_value.strip()
    substring_score = _substring_grounded(raw_value, haystack)
    final_confidence = round(self_report_n * substring_score, 4)
    if substring_score == 0.0:
        bucket = "paraphrased"
    elif final_confidence < _TRUST_GATE:
        bucket = "low_self_confidence"
    else:
        bucket = "would_populate"
    return {
        "bucket": bucket,
        "raw_value": raw_value,
        "self_confidence": self_report_n,
        "substring_score": substring_score,
        "final_confidence": final_confidence,
        "reason": reason,
    }


def _row_haystack(row: Dict[str, Any]) -> str:
    """Mirror what batch_extract_fashion_fields sees as haystack."""
    parts = []
    if row.get("title"):
        parts.append(str(row["title"]))
    if row.get("description"):
        parts.append(str(row["description"]))
    return "\n".join(parts)


async def diagnose(*, merchant_id: Optional[str], sample: int) -> Dict[str, Any]:
    if not _extract_enabled():
        return {
            "error": "FASHION_EXTRACT_ENABLED is off — set the env var to enable LLM calls.",
        }
    if not getattr(database, "is_connected", False):
        await database.connect()
    rows = await _sample_rows(merchant_id=merchant_id, sample=sample)
    results: List[Dict[str, Any]] = []
    bucket_counts: Dict[str, int] = {}
    for row in rows:
        cat = row.get("category_path")
        if not _is_fashion_category(cat):
            # Shouldn't happen given the SQL filter, but defend.
            res = {"product_key": row["product_key"], "bucket": "haystack_empty",
                   "category_path": cat, "raw_value": None}
            results.append(res)
            bucket_counts[res["bucket"]] = bucket_counts.get(res["bucket"], 0) + 1
            continue
        haystack = _row_haystack(row)
        if not haystack.strip():
            res = {"product_key": row["product_key"], "bucket": "haystack_empty",
                   "category_path": cat, "raw_value": None}
            results.append(res)
            bucket_counts[res["bucket"]] = bucket_counts.get(res["bucket"], 0) + 1
            continue
        user_message = _build_batch_user_message(
            title=row.get("title"), description=row.get("description"), html_blob=None,
        )
        raw = await _call_deepseek_batch(user_message=user_message)
        classification = _classify_material(raw_response=raw, haystack=haystack)
        classification.update({
            "product_key": row["product_key"],
            "merchant_id": row["merchant_id"],
            "platform": row["platform"],
            "title": row.get("title"),
            "category_path": cat,
        })
        results.append(classification)
        bucket_counts[classification["bucket"]] = bucket_counts.get(classification["bucket"], 0) + 1

    return {
        "scope": {"merchant_id": merchant_id, "sample": sample, "rows_sampled": len(rows)},
        "bucket_counts": bucket_counts,
        "buckets_pct": {
            b: round(100.0 * n / len(rows), 1) for b, n in bucket_counts.items()
        } if rows else {},
        "trust_gate": _TRUST_GATE,
        "rows": results,
    }


async def _run(args: argparse.Namespace) -> int:
    try:
        report = await diagnose(merchant_id=args.merchant_id, sample=args.sample)
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--merchant-id", default=None)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
