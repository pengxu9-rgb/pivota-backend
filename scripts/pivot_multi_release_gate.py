#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


REQUIRED_ROUTE_HEALTH_FIELDS = [
    "pivot_shadow_scheduled",
    "pivot_shadow_mode",
    "pivot_rollout_mode",
    "pivot_rollout_guard_passed",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live/staging find_products_multi release-gate corpus against /agent/shop/v1/invoke."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Repeatable raw header in 'Name: Value' form.",
    )
    parser.add_argument(
        "--default-rollout-mode",
        choices=("shadow", "serve", "legacy"),
        default=None,
        help="Optional fallback expected rollout mode when a case omits expected_rollout_mode.",
    )
    return parser.parse_args()


def _load_headers(header_args: List[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    for raw in header_args:
        if ":" not in str(raw):
            continue
        name, value = raw.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name:
            headers[name] = value
    return headers


def _load_corpus(path: str) -> List[Dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Corpus must be a JSON list")
    return [item for item in payload if isinstance(item, dict)]


def _build_request(case: Dict[str, Any]) -> Dict[str, Any]:
    query = str(case.get("query") or "").strip()
    if not query:
        raise ValueError(f"Case missing query: {case}")
    source = str(case.get("source") or "shopping_agent").strip() or "shopping_agent"
    payload = {
        "operation": "find_products_multi",
        "payload": {
            "search": {
                "query": query,
                "page": int(case.get("page") or 1),
                "limit": int(case.get("limit") or 10),
                "in_stock_only": bool(case.get("in_stock_only") or False),
            }
        },
        "metadata": {
            "source": source,
        },
    }
    market = str(case.get("market") or "").strip()
    if market:
        payload["metadata"]["market"] = market
    return payload


def _extract_case_result(
    *,
    case: Dict[str, Any],
    response: requests.Response,
    elapsed_ms: float,
) -> Dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        body = {}
    metadata = body.get("metadata") if isinstance(body, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    route_health = metadata.get("route_health") if isinstance(metadata, dict) else {}
    route_health = route_health if isinstance(route_health, dict) else {}
    products = body.get("products") if isinstance(body, dict) else []
    products = products if isinstance(products, list) else []
    missing_route_health = [field for field in REQUIRED_ROUTE_HEALTH_FIELDS if field not in route_health]
    expected_rollout = str(
        case.get("expected_rollout_mode")
        or ""
    ).strip() or None

    pass_checks: List[bool] = [response.status_code == 200, not missing_route_health]
    check_notes: List[str] = []
    if response.status_code != 200:
        check_notes.append(f"http_status={response.status_code}")
    if missing_route_health:
        check_notes.append(f"missing_route_health={','.join(missing_route_health)}")

    actual_rollout_mode = str(route_health.get("pivot_rollout_mode") or metadata.get("pivot_rollout_mode") or "").strip() or None
    if expected_rollout:
        rollout_ok = actual_rollout_mode == expected_rollout
        pass_checks.append(rollout_ok)
        if not rollout_ok:
            check_notes.append(f"rollout_mode={actual_rollout_mode or 'null'} expected={expected_rollout}")

    expected_nonempty = case.get("expected_nonempty")
    if expected_nonempty is not None:
        nonempty_ok = bool(products) is bool(expected_nonempty)
        pass_checks.append(nonempty_ok)
        if not nonempty_ok:
            check_notes.append(f"nonempty={bool(products)} expected={bool(expected_nonempty)}")

    return {
        "case_id": str(case.get("case_id") or ""),
        "category": str(case.get("category") or "uncategorized"),
        "query": str(case.get("query") or ""),
        "source": str(case.get("source") or "shopping_agent"),
        "page": int(case.get("page") or 1),
        "limit": int(case.get("limit") or 10),
        "elapsed_ms": round(elapsed_ms, 1),
        "http_status": response.status_code,
        "product_count": len(products),
        "query_source": str(metadata.get("query_source") or ""),
        "service_commit": str(response.headers.get("X-Service-Commit") or "").strip() or None,
        "route_health_missing": missing_route_health,
        "pivot_shadow_scheduled": bool(route_health.get("pivot_shadow_scheduled")),
        "pivot_shadow_mode": route_health.get("pivot_shadow_mode"),
        "pivot_rollout_mode": actual_rollout_mode,
        "pivot_rollout_guard_passed": bool(route_health.get("pivot_rollout_guard_passed")),
        "pass": all(pass_checks),
        "check_notes": check_notes,
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Pivot Multi Release Gate Report",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- base_url: `{report['base_url']}`",
        f"- corpus: `{report['corpus_path']}`",
        f"- total_cases: `{report['summary']['total_cases']}`",
        f"- passed_cases: `{report['summary']['passed_cases']}`",
        f"- failed_cases: `{report['summary']['failed_cases']}`",
        f"- avg_elapsed_ms: `{report['summary']['avg_elapsed_ms']}`",
        "",
        "## Rollout Modes",
        "",
    ]
    for key, value in sorted((report["summary"].get("rollout_modes") or {}).items()):
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Failed Cases", ""])
    failed_cases = [case for case in report["cases"] if not case.get("pass")]
    if not failed_cases:
        lines.append("- none")
    else:
        for case in failed_cases:
            lines.append(
                f"- `{case['case_id']}` `{case['category']}` `{case['source']}` "
                f"status=`{case['http_status']}` notes=`{' | '.join(case.get('check_notes') or [])}`"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    corpus = _load_corpus(args.corpus)
    headers = _load_headers(args.header)
    base_url = args.base_url.rstrip("/")
    session = requests.Session()
    cases: List[Dict[str, Any]] = []

    for raw_case in corpus:
        case = dict(raw_case)
        if not case.get("expected_rollout_mode") and args.default_rollout_mode:
            case["expected_rollout_mode"] = args.default_rollout_mode
        request_payload = _build_request(case)
        started = time.perf_counter()
        response = session.post(
            f"{base_url}/agent/shop/v1/invoke",
            headers=headers,
            json=request_payload,
            timeout=args.timeout_seconds,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        cases.append(_extract_case_result(case=case, response=response, elapsed_ms=elapsed_ms))

    summary = {
        "total_cases": len(cases),
        "passed_cases": sum(1 for case in cases if case.get("pass")),
        "failed_cases": sum(1 for case in cases if not case.get("pass")),
        "avg_elapsed_ms": round(statistics.mean(case["elapsed_ms"] for case in cases), 1) if cases else 0.0,
        "rollout_modes": dict(Counter(str(case.get("pivot_rollout_mode") or "unknown") for case in cases)),
        "categories": dict(Counter(str(case.get("category") or "uncategorized") for case in cases)),
        "sources": dict(Counter(str(case.get("source") or "unknown") for case in cases)),
        "service_commits": dict(Counter(str(case.get("service_commit") or "unknown") for case in cases)),
    }
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base_url,
        "corpus_path": str(Path(args.corpus)),
        "summary": summary,
        "cases": cases,
    }

    if args.output_json:
        path = Path(args.output_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.output_md:
        path = Path(args.output_md)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_markdown(report), encoding="utf-8")

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if summary["failed_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
