#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


REQUIRED_ROUTE_HEALTH_FIELDS = [
    "pivot_shadow_scheduled",
    "pivot_shadow_mode",
    "pivot_rollout_mode",
    "pivot_rollout_guard_passed",
]
SOURCE_STAGE_ORDER = ["shopping_agent", "shopping-agent-ui", "shopping-agent-web"]
SOURCE_STAGE_LABELS = {
    "shopping_agent": "stage_1",
    "shopping-agent-ui": "stage_2",
    "shopping-agent-web": "stage_3",
}
DEFAULT_REQUEST_RETRIES = 2
DEFAULT_RETRY_SLEEP_SECONDS = 0.5


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live/staging find_products_multi release-gate corpus against /agent/shop/v1/invoke."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--request-retries", type=int, default=DEFAULT_REQUEST_RETRIES)
    parser.add_argument("--retry-sleep-seconds", type=float, default=DEFAULT_RETRY_SLEEP_SECONDS)
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
    parser.add_argument(
        "--source-filter",
        action="append",
        default=[],
        help="Optional repeatable source filter. When set, only matching corpus sources are executed.",
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


def _filter_corpus_by_source(
    corpus: List[Dict[str, Any]],
    source_filters: List[str],
) -> List[Dict[str, Any]]:
    normalized_filters = {
        str(source or "").strip()
        for source in source_filters
        if str(source or "").strip()
    }
    if not normalized_filters:
        return list(corpus)
    return [
        case
        for case in corpus
        if str(case.get("source") or "shopping_agent").strip() in normalized_filters
    ]


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
    request_attempts: int = 1,
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
    query_semantic_class = str(
        route_health.get("query_semantic_class")
        or metadata.get("query_semantic_class")
        or "unknown"
    ).strip() or "unknown"
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
        "request_failed": False,
        "request_timed_out": False,
        "request_attempts": max(int(request_attempts), 1),
        "request_error": None,
        "product_count": len(products),
        "query_source": str(metadata.get("query_source") or ""),
        "query_semantic_class": query_semantic_class,
        "service_commit": str(response.headers.get("X-Service-Commit") or "").strip() or None,
        "route_health_missing": missing_route_health,
        "pivot_shadow_scheduled": bool(route_health.get("pivot_shadow_scheduled")),
        "pivot_shadow_mode": route_health.get("pivot_shadow_mode"),
        "pivot_rollout_mode": actual_rollout_mode,
        "pivot_rollout_guard_passed": bool(route_health.get("pivot_rollout_guard_passed")),
        "pass": all(pass_checks),
        "check_notes": check_notes,
    }


def _build_request_failure_result(
    *,
    case: Dict[str, Any],
    elapsed_ms: float,
    error: requests.RequestException,
    request_attempts: int,
) -> Dict[str, Any]:
    error_name = type(error).__name__
    error_message = str(error).strip() or error_name
    check_notes = [f"request_failed={error_name}"]
    if error_message and error_message != error_name:
        check_notes.append(f"request_error={error_message}")
    return {
        "case_id": str(case.get("case_id") or ""),
        "category": str(case.get("category") or "uncategorized"),
        "query": str(case.get("query") or ""),
        "source": str(case.get("source") or "shopping_agent"),
        "page": int(case.get("page") or 1),
        "limit": int(case.get("limit") or 10),
        "elapsed_ms": round(elapsed_ms, 1),
        "http_status": None,
        "request_failed": True,
        "request_timed_out": isinstance(error, requests.Timeout),
        "request_attempts": max(int(request_attempts), 1),
        "request_error": f"{error_name}: {error_message}" if error_message != error_name else error_name,
        "product_count": 0,
        "query_source": "",
        "query_semantic_class": "unknown",
        "service_commit": None,
        "route_health_missing": list(REQUIRED_ROUTE_HEALTH_FIELDS),
        "pivot_shadow_scheduled": False,
        "pivot_shadow_mode": None,
        "pivot_rollout_mode": None,
        "pivot_rollout_guard_passed": False,
        "pass": False,
        "check_notes": check_notes,
    }


def _perform_request(
    *,
    session: requests.Session,
    base_url: str,
    headers: Dict[str, str],
    request_payload: Dict[str, Any],
    timeout_seconds: float,
    request_retries: int,
    retry_sleep_seconds: float,
) -> Tuple[requests.Response, int]:
    attempts = max(int(request_retries), 0) + 1
    last_error: Optional[requests.RequestException] = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.post(
                f"{base_url}/agent/shop/v1/invoke",
                headers=headers,
                json=request_payload,
                timeout=timeout_seconds,
            )
            return response, attempt
        except requests.RequestException as error:
            last_error = error
            if attempt >= attempts:
                break
            if retry_sleep_seconds > 0:
                time.sleep(retry_sleep_seconds)
    assert last_error is not None
    raise last_error


def _summarize_by_source(cases: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[str(case.get("source") or "unknown")].append(case)

    def _sort_key(source: str) -> tuple[int, str]:
        if source in SOURCE_STAGE_ORDER:
            return (SOURCE_STAGE_ORDER.index(source), source)
        return (len(SOURCE_STAGE_ORDER) + 1, source)

    summary: Dict[str, Dict[str, Any]] = {}
    for source in sorted(grouped, key=_sort_key):
        items = grouped[source]
        passed_cases = sum(1 for case in items if bool(case.get("pass")))
        failed_cases = len(items) - passed_cases
        summary[source] = {
            "source_stage": SOURCE_STAGE_LABELS.get(source),
            "sample_count": len(items),
            "passed_cases": passed_cases,
            "failed_cases": failed_cases,
            "rollout_modes": dict(Counter(str(case.get("pivot_rollout_mode") or "unknown") for case in items)),
            "semantic_classes": dict(
                Counter(str(case.get("query_semantic_class") or "unknown") for case in items)
            ),
            "ready_for_canary": len(items) > 0 and failed_cases == 0,
        }
    return summary


def _summarize_by_semantic_class(cases: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[str(case.get("query_semantic_class") or "unknown")].append(case)

    summary: Dict[str, Dict[str, Any]] = {}
    for semantic_class in sorted(grouped):
        items = grouped[semantic_class]
        passed_cases = sum(1 for case in items if bool(case.get("pass")))
        failed_cases = len(items) - passed_cases
        summary[semantic_class] = {
            "sample_count": len(items),
            "passed_cases": passed_cases,
            "failed_cases": failed_cases,
            "sources": dict(Counter(str(case.get("source") or "unknown") for case in items)),
            "rollout_modes": dict(Counter(str(case.get("pivot_rollout_mode") or "unknown") for case in items)),
        }
    return summary


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
    lines.extend(["", "## Source Summary", ""])
    source_summary = report["summary"].get("source_summary") or {}
    if not source_summary:
        lines.append("- none")
    else:
        for source, details in source_summary.items():
            lines.append(
                f"- {source}: sample_count=`{details.get('sample_count')}` "
                f"passed=`{details.get('passed_cases')}` failed=`{details.get('failed_cases')}` "
                f"source_stage=`{details.get('source_stage') or 'n/a'}` "
                f"ready_for_canary=`{details.get('ready_for_canary')}` "
                f"rollout_modes=`{details.get('rollout_modes')}`"
            )
    lines.extend(["", "## Semantic Classes", ""])
    semantic_summary = report["summary"].get("semantic_class_summary") or {}
    if not semantic_summary:
        lines.append("- none")
    else:
        for semantic_class, details in semantic_summary.items():
            lines.append(
                f"- {semantic_class}: sample_count=`{details.get('sample_count')}` "
                f"passed=`{details.get('passed_cases')}` failed=`{details.get('failed_cases')}` "
                f"sources=`{details.get('sources')}`"
            )
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
    corpus = _filter_corpus_by_source(_load_corpus(args.corpus), list(args.source_filter or []))
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
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        try:
            response, request_attempts = _perform_request(
                session=session,
                base_url=base_url,
                headers=headers,
                request_payload=request_payload,
                timeout_seconds=args.timeout_seconds,
                request_retries=args.request_retries,
                retry_sleep_seconds=args.retry_sleep_seconds,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            cases.append(
                _extract_case_result(
                    case=case,
                    response=response,
                    elapsed_ms=elapsed_ms,
                    request_attempts=request_attempts,
                )
            )
        except requests.RequestException as error:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            cases.append(
                _build_request_failure_result(
                    case=case,
                    elapsed_ms=elapsed_ms,
                    error=error,
                    request_attempts=max(int(args.request_retries), 0) + 1,
                )
            )

    summary = {
        "total_cases": len(cases),
        "passed_cases": sum(1 for case in cases if case.get("pass")),
        "failed_cases": sum(1 for case in cases if not case.get("pass")),
        "avg_elapsed_ms": round(statistics.mean(case["elapsed_ms"] for case in cases), 1) if cases else 0.0,
        "rollout_modes": dict(Counter(str(case.get("pivot_rollout_mode") or "unknown") for case in cases)),
        "categories": dict(Counter(str(case.get("category") or "uncategorized") for case in cases)),
        "sources": dict(Counter(str(case.get("source") or "unknown") for case in cases)),
        "source_summary": _summarize_by_source(cases),
        "semantic_class_summary": _summarize_by_semantic_class(cases),
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
