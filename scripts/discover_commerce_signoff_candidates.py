#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


LIVE_PSP_STATUSES = {"active", "connected", "validated"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and summarize production merchant candidates for Phase A commerce signoff expansion."
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--cohort", default=None, help="Optional cohort JSON manifest to compare against.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _write_if_requested(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_cohort(path_str: Optional[str]) -> Dict[str, Any]:
    if not path_str:
        return {}
    path = Path(path_str)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {"cohort_name": path.stem, "cases": payload}
    if isinstance(payload, dict):
        return payload
    raise ValueError(f"Unsupported cohort payload in {path}")


def _connect_postgres(database_url: str):
    try:
        import psycopg  # type: ignore

        return psycopg.connect(database_url)
    except Exception:
        import psycopg2  # type: ignore

        return psycopg2.connect(database_url)


def _normalize_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]
    normalized = []
    for item in items:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _fetch_merchant_rows(database_url: str, limit: int) -> List[Dict[str, Any]]:
    sql = """
    WITH merchant_universe AS (
      SELECT merchant_id FROM products_cache
      UNION
      SELECT merchant_id FROM merchant_psps
      UNION
      SELECT merchant_id FROM catalog_offers
    ),
    pc AS (
      SELECT
        merchant_id,
        COUNT(*) AS products_cache_rows,
        COUNT(*) FILTER (WHERE COALESCE(product_data->>'title', '') <> '') AS titled_rows,
        MAX(NULLIF(product_data->>'title', '')) AS sample_title,
        MAX(cached_at) AS latest_cached_at
      FROM products_cache
      GROUP BY merchant_id
    ),
    psp AS (
      SELECT
        merchant_id,
        COUNT(*) FILTER (WHERE LOWER(COALESCE(status, '')) IN ('active', 'connected', 'validated')) AS active_psp_rows,
        ARRAY_AGG(DISTINCT provider) FILTER (WHERE LOWER(COALESCE(status, '')) IN ('active', 'connected', 'validated')) AS active_psp_providers,
        ARRAY_AGG(DISTINCT environment) FILTER (WHERE LOWER(COALESCE(status, '')) IN ('active', 'connected', 'validated')) AS active_psp_environments,
        ARRAY_AGG(DISTINCT status) FILTER (WHERE status IS NOT NULL) AS psp_statuses
      FROM merchant_psps
      GROUP BY merchant_id
    ),
    cat AS (
      SELECT
        merchant_id,
        COUNT(*) AS catalog_offer_rows,
        ARRAY_AGG(DISTINCT currency) FILTER (WHERE currency IS NOT NULL) AS currencies
      FROM catalog_offers
      GROUP BY merchant_id
    )
    SELECT
      u.merchant_id,
      COALESCE(pc.products_cache_rows, 0) AS products_cache_rows,
      COALESCE(pc.titled_rows, 0) AS titled_rows,
      pc.sample_title,
      pc.latest_cached_at,
      COALESCE(psp.active_psp_rows, 0) AS active_psp_rows,
      COALESCE(psp.active_psp_providers, ARRAY[]::text[]) AS active_psp_providers,
      COALESCE(psp.active_psp_environments, ARRAY[]::text[]) AS active_psp_environments,
      COALESCE(psp.psp_statuses, ARRAY[]::text[]) AS psp_statuses,
      COALESCE(cat.catalog_offer_rows, 0) AS catalog_offer_rows,
      COALESCE(cat.currencies, ARRAY[]::text[]) AS currencies
    FROM merchant_universe u
    LEFT JOIN pc ON pc.merchant_id = u.merchant_id
    LEFT JOIN psp ON psp.merchant_id = u.merchant_id
    LEFT JOIN cat ON cat.merchant_id = u.merchant_id
    ORDER BY
      CASE
        WHEN COALESCE(pc.products_cache_rows, 0) > 0
         AND COALESCE(cat.catalog_offer_rows, 0) > 0
         AND COALESCE(psp.active_psp_rows, 0) > 0
        THEN 0
        ELSE 1
      END,
      COALESCE(psp.active_psp_rows, 0) DESC,
      COALESCE(cat.catalog_offer_rows, 0) DESC,
      COALESCE(pc.products_cache_rows, 0) DESC,
      u.merchant_id
    LIMIT %s
    """
    with _connect_postgres(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (limit,))
            cols = [desc[0] for desc in cursor.description]
            rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
    return rows


def _cohort_index(cohort: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for raw_case in cohort.get("cases") or []:
        merchant_id = str((raw_case or {}).get("merchant_id") or "").strip()
        if merchant_id and merchant_id not in index:
            index[merchant_id] = dict(raw_case)
    return index


def _build_candidate(row: Dict[str, Any], cohort_case: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    products_cache_rows = int(row.get("products_cache_rows") or 0)
    titled_rows = int(row.get("titled_rows") or 0)
    active_psp_rows = int(row.get("active_psp_rows") or 0)
    catalog_offer_rows = int(row.get("catalog_offer_rows") or 0)
    has_products_cache = products_cache_rows > 0
    has_live_payload = titled_rows > 0
    has_active_psp = active_psp_rows > 0
    has_catalog_offers = catalog_offer_rows > 0
    live_eligible = has_products_cache and has_live_payload and has_active_psp and has_catalog_offers

    gap_reasons: List[str] = []
    if not has_products_cache:
        gap_reasons.append("missing_products_cache")
    elif not has_live_payload:
        gap_reasons.append("missing_products_cache_live_payload")
    if not has_catalog_offers:
        gap_reasons.append("missing_catalog_offers")
    if not has_active_psp:
        gap_reasons.append("missing_active_psp")

    case = cohort_case or {}
    return {
        "merchant_id": str(row.get("merchant_id") or ""),
        "products_cache_rows": products_cache_rows,
        "titled_rows": titled_rows,
        "sample_title": row.get("sample_title"),
        "latest_cached_at": row.get("latest_cached_at"),
        "active_psp_rows": active_psp_rows,
        "active_psp_providers": _normalize_list(row.get("active_psp_providers")),
        "active_psp_environments": _normalize_list(row.get("active_psp_environments")),
        "psp_statuses": _normalize_list(row.get("psp_statuses")),
        "catalog_offer_rows": catalog_offer_rows,
        "currencies": _normalize_list(row.get("currencies")),
        "candidate_query": str(row.get("sample_title") or "").strip() or None,
        "has_products_cache": has_products_cache,
        "has_products_cache_live_payload": has_live_payload,
        "has_active_psp": has_active_psp,
        "has_catalog_offers": has_catalog_offers,
        "live_eligible": live_eligible,
        "gap_reasons": gap_reasons,
        "cohort_case_id": case.get("case_id"),
        "cohort_label": case.get("label"),
        "cohort_semantic_class": case.get("semantic_class"),
        "cohort_enabled": case.get("enabled"),
        "cohort_skip_reason": case.get("skip_reason"),
    }


def _count_if(candidates: Iterable[Dict[str, Any]], key: str) -> int:
    return sum(1 for item in candidates if bool(item.get(key)))


def _build_report(rows: List[Dict[str, Any]], cohort: Dict[str, Any]) -> Dict[str, Any]:
    cohort_by_merchant = _cohort_index(cohort)
    candidates = [_build_candidate(row, cohort_by_merchant.get(str(row.get("merchant_id") or ""))) for row in rows]
    live_eligible = [item for item in candidates if item.get("live_eligible")]
    in_cohort = [item for item in candidates if item.get("cohort_case_id")]
    live_eligible_in_cohort = [item for item in in_cohort if item.get("live_eligible")]
    live_eligible_outside_cohort = [item for item in live_eligible if not item.get("cohort_case_id")]
    gap_reason_counts = Counter(reason for item in candidates for reason in (item.get("gap_reasons") or []))

    min_enabled_cases = int(cohort.get("min_enabled_cases") or 0)
    target_enabled_cases = int(cohort.get("target_enabled_cases") or 0)
    summary = {
        "total_merchants_seen": len(candidates),
        "merchants_with_products_cache": _count_if(candidates, "has_products_cache"),
        "merchants_with_products_cache_live_payload": _count_if(candidates, "has_products_cache_live_payload"),
        "merchants_with_catalog_offers": _count_if(candidates, "has_catalog_offers"),
        "merchants_with_active_psp": _count_if(candidates, "has_active_psp"),
        "live_eligible_merchants": len(live_eligible),
        "live_eligible_merchants_in_cohort": len(live_eligible_in_cohort),
        "live_eligible_merchants_outside_cohort": len(live_eligible_outside_cohort),
        "min_enabled_cases": min_enabled_cases,
        "target_enabled_cases": target_enabled_cases,
        "enough_capacity_for_min_gate": len(live_eligible) >= min_enabled_cases if min_enabled_cases else None,
        "enough_capacity_for_target_gate": len(live_eligible) >= target_enabled_cases if target_enabled_cases else None,
        "missing_capacity_to_target": max(0, target_enabled_cases - len(live_eligible)),
        "gap_reason_counts": dict(sorted(gap_reason_counts.items())),
    }
    if cohort:
        summary.update(
            {
                "cohort_name": cohort.get("cohort_name"),
                "cohort_case_count": len(cohort.get("cases") or []),
                "cohort_enabled_case_count": sum(1 for case in (cohort.get("cases") or []) if bool(case.get("enabled", True))),
                "required_semantic_classes": [str(item) for item in (cohort.get("required_semantic_classes") or []) if str(item).strip()],
                "target_semantic_classes": [str(item) for item in (cohort.get("target_semantic_classes") or []) if str(item).strip()],
            }
        )

    return {
        "overall_ok": True,
        "summary": summary,
        "eligible_merchants": live_eligible,
        "candidates": candidates,
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Commerce Signoff Candidate Discovery",
        "",
        f"- total_merchants_seen: `{summary.get('total_merchants_seen')}`",
        f"- merchants_with_products_cache: `{summary.get('merchants_with_products_cache')}`",
        f"- merchants_with_catalog_offers: `{summary.get('merchants_with_catalog_offers')}`",
        f"- merchants_with_active_psp: `{summary.get('merchants_with_active_psp')}`",
        f"- live_eligible_merchants: `{summary.get('live_eligible_merchants')}`",
    ]
    if summary.get("cohort_name"):
        lines.extend(
            [
                f"- cohort_name: `{summary.get('cohort_name')}`",
                f"- min_enabled_cases: `{summary.get('min_enabled_cases')}`",
                f"- target_enabled_cases: `{summary.get('target_enabled_cases')}`",
                f"- enough_capacity_for_min_gate: `{summary.get('enough_capacity_for_min_gate')}`",
                f"- enough_capacity_for_target_gate: `{summary.get('enough_capacity_for_target_gate')}`",
                f"- missing_capacity_to_target: `{summary.get('missing_capacity_to_target')}`",
            ]
        )
    lines.extend(["", "## Eligible Merchants", ""])
    eligible = report.get("eligible_merchants") or []
    if not eligible:
        lines.append("- none")
    for item in eligible:
        providers = ", ".join(item.get("active_psp_providers") or [])
        currencies = ", ".join(item.get("currencies") or [])
        lines.append(
            f"- `{item['merchant_id']}` providers=`{providers or 'n/a'}` currencies=`{currencies or 'n/a'}` catalog_offers=`{item.get('catalog_offer_rows')}` query=`{item.get('candidate_query') or 'n/a'}`"
        )

    gap_reason_counts = summary.get("gap_reason_counts") or {}
    if gap_reason_counts:
        lines.extend(["", "## Gap Reasons", ""])
        for reason, count in gap_reason_counts.items():
            lines.append(f"- `{reason}`: `{count}`")

    blocked = [item for item in (report.get("candidates") or []) if not item.get("live_eligible")]
    if blocked:
        lines.extend(["", "## Blocked Merchants", ""])
        for item in blocked[:20]:
            lines.append(
                f"- `{item['merchant_id']}` gaps=`{', '.join(item.get('gap_reasons') or [])}` psp_statuses=`{', '.join(item.get('psp_statuses') or []) or 'n/a'}`"
            )

    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    cohort = _load_cohort(args.cohort)
    report = _build_report(_fetch_merchant_rows(args.database_url, args.limit), cohort)
    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    markdown = _render_markdown(report)
    _write_if_requested(args.output_json, payload + "\n")
    _write_if_requested(args.output_md, markdown)
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
