#!/usr/bin/env python3
"""
Run dual-entry search chain probes and generate JSON/Markdown reports.

Default matrix:
- queries: tom ford, kylie, sigma, fenty, lingerie, perfume
- entries: /agent/v1/products/search and /api/gateway
- rounds: 5
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

DEFAULT_QUERIES = [
    "tom ford",
    "kylie",
    "sigma",
    "fenty",
    "lingerie",
    "perfume",
]


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _extract_common_metrics(body: Dict[str, Any]) -> Dict[str, Any]:
    metadata = body.get("metadata") if isinstance(body, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    route_health = metadata.get("route_health")
    if not isinstance(route_health, dict):
        route_health = {}

    source_breakdown = metadata.get("source_breakdown")
    if not isinstance(source_breakdown, dict):
        source_breakdown = {}

    products = body.get("products") if isinstance(body, dict) else []
    product_count = len(products) if isinstance(products, list) else 0

    pagination = body.get("pagination") if isinstance(body, dict) else {}
    if not isinstance(pagination, dict):
        pagination = {}

    primary_path_used = str(
        route_health.get("primary_path_used")
        or metadata.get("primary_path_used")
        or metadata.get("query_source")
        or "unknown"
    )
    decision_node = str(
        route_health.get("decision_node")
        or metadata.get("decision_node")
        or metadata.get("query_source")
        or primary_path_used
    )

    return {
        "status": str(body.get("status") if isinstance(body, dict) else ""),
        "product_count": product_count,
        "total": body.get("total") if isinstance(body, dict) else pagination.get("total_count"),
        "primary_path_used": primary_path_used,
        "decision_node": decision_node,
        "orchestrator_path": str(
            route_health.get("orchestrator_path")
            or metadata.get("orchestrator_path")
            or ""
        ),
        "fallback_reason": route_health.get("fallback_reason"),
        "fallback_triggered": bool(route_health.get("fallback_triggered") or False),
        "primary_latency_ms": _non_negative_int(route_health.get("primary_latency_ms") or metadata.get("latency_ms") or 0),
        "query_semantic_class": str(
            route_health.get("query_semantic_class")
            or metadata.get("query_semantic_class")
            or ""
        ),
        "domain_filter_dropped_external": _non_negative_int(
            route_health.get("domain_filter_dropped_external")
            or metadata.get("domain_filter_dropped_external")
            or 0
        ),
        "external_fill_gate_reason": (
            route_health.get("external_fill_gate_reason")
            if route_health.get("external_fill_gate_reason") is not None
            else metadata.get("external_fill_gate_reason")
        ),
        "semantic_retry_applied": bool(
            route_health.get("semantic_retry_applied")
            if route_health.get("semantic_retry_applied") is not None
            else metadata.get("semantic_retry_applied")
            or False
        ),
        "semantic_retry_query": (
            route_health.get("semantic_retry_query")
            if route_health.get("semantic_retry_query") is not None
            else metadata.get("semantic_retry_query")
        ),
        "semantic_retry_hits": _non_negative_int(
            route_health.get("semantic_retry_hits")
            if route_health.get("semantic_retry_hits") is not None
            else metadata.get("semantic_retry_hits")
            or 0
        ),
        "external_seed_brand_strict_rows": _non_negative_int(
            route_health.get("external_seed_brand_strict_rows")
            if route_health.get("external_seed_brand_strict_rows") is not None
            else metadata.get("external_seed_brand_strict_rows")
            or 0
        ),
        "external_seed_brand_relevant_rows": _non_negative_int(
            route_health.get("external_seed_brand_relevant_rows")
            if route_health.get("external_seed_brand_relevant_rows") is not None
            else metadata.get("external_seed_brand_relevant_rows")
            or 0
        ),
        "external_seed_broad_fallback_used": bool(
            route_health.get("external_seed_broad_fallback_used")
            if route_health.get("external_seed_broad_fallback_used") is not None
            else metadata.get("external_seed_broad_fallback_used")
            or False
        ),
        "external_seed_broad_scope_rows": _non_negative_int(
            route_health.get("external_seed_broad_scope_rows")
            if route_health.get("external_seed_broad_scope_rows") is not None
            else metadata.get("external_seed_broad_scope_rows")
            or 0
        ),
        "external_seed_returned_count": _non_negative_int(
            metadata.get("external_seed_returned_count")
            or source_breakdown.get("external_seed_count")
            or 0
        ),
        "internal_count": _non_negative_int(source_breakdown.get("internal_count") or 0),
        "source_breakdown_external_count": _non_negative_int(source_breakdown.get("external_seed_count") or 0),
    }


def _probe_agent_search(
    *,
    base_url: str,
    query: str,
    limit: int,
    timeout_seconds: float,
    headers: Dict[str, str],
) -> Tuple[int, Dict[str, Any]]:
    params = {
        "query": query,
        "search_all_merchants": "true",
        "limit": limit,
        "offset": 0,
    }
    res = requests.get(
        f"{base_url.rstrip('/')}/agent/v1/products/search",
        params=params,
        headers=headers,
        timeout=timeout_seconds,
    )
    try:
        body = res.json()
    except Exception:
        body = {"error": "invalid_json", "raw": res.text[:600]}
    return res.status_code, body


def _probe_gateway(
    *,
    gateway_url: str,
    query: str,
    limit: int,
    timeout_seconds: float,
    headers: Dict[str, str],
) -> Tuple[int, Dict[str, Any]]:
    body = {
        "operation": "find_products_multi",
        "payload": {
            "search": {
                "query": query,
                "page": 1,
                "limit": limit,
                "in_stock_only": False,
            }
        },
        "metadata": {
            "source": "shopping_agent",
        },
    }
    res = requests.post(
        gateway_url,
        json=body,
        headers=headers,
        timeout=timeout_seconds,
    )
    try:
        payload = res.json()
    except Exception:
        payload = {"error": "invalid_json", "raw": res.text[:600]}
    return res.status_code, payload


def _aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["entry"], record["query"])].append(record)

    for (entry, query), rows in grouped.items():
        ok_rows = [r for r in rows if r.get("http_status") == 200 and r.get("parse_ok")]
        product_counts = [int(r.get("metrics", {}).get("product_count") or 0) for r in ok_rows]
        external_counts = [int(r.get("metrics", {}).get("external_seed_returned_count") or 0) for r in ok_rows]
        latencies = [int(r.get("metrics", {}).get("primary_latency_ms") or 0) for r in ok_rows]
        path_counter = Counter(str(r.get("metrics", {}).get("primary_path_used") or "unknown") for r in ok_rows)
        decision_counter = Counter(str(r.get("metrics", {}).get("decision_node") or "unknown") for r in ok_rows)
        fallback_counter = Counter(
            str(r.get("metrics", {}).get("fallback_reason") or "none")
            for r in ok_rows
            if r.get("metrics", {}).get("fallback_triggered")
        )

        out[f"{entry}::{query}"] = {
            "entry": entry,
            "query": query,
            "rounds_total": len(rows),
            "rounds_ok": len(ok_rows),
            "rounds_nonempty": sum(1 for c in product_counts if c > 0),
            "avg_products": round(statistics.mean(product_counts), 2) if product_counts else 0.0,
            "min_products": min(product_counts) if product_counts else 0,
            "max_products": max(product_counts) if product_counts else 0,
            "avg_external_seed_count": round(statistics.mean(external_counts), 2) if external_counts else 0.0,
            "avg_primary_latency_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
            "path_distribution": dict(path_counter),
            "decision_distribution": dict(decision_counter),
            "fallback_distribution": dict(fallback_counter),
            "semantic_retry_applied_rounds": sum(
                1 for r in ok_rows if bool(r.get("metrics", {}).get("semantic_retry_applied"))
            ),
            "brand_broad_fallback_rounds": sum(
                1 for r in ok_rows if bool(r.get("metrics", {}).get("external_seed_broad_fallback_used"))
            ),
            "domain_filter_dropped_external_total": sum(
                _non_negative_int(r.get("metrics", {}).get("domain_filter_dropped_external") or 0)
                for r in ok_rows
            ),
        }
    return out


def _render_markdown(
    *,
    generated_at: str,
    rounds: int,
    limit: int,
    agent_base_url: str,
    gateway_url: str,
    aggregation: Dict[str, Any],
) -> str:
    lines: List[str] = []
    lines.append("# Search Chain Inventory Report")
    lines.append("")
    lines.append(f"- Generated at: `{generated_at}`")
    lines.append(f"- Rounds per query: `{rounds}`")
    lines.append(f"- Requested limit: `{limit}`")
    lines.append(f"- Agent entry: `{agent_base_url.rstrip('/')}/agent/v1/products/search`")
    lines.append(f"- Gateway entry: `{gateway_url}`")
    lines.append("")
    lines.append("## Summary Table")
    lines.append("")
    lines.append(
        "| Entry | Query | OK/Total | Nonempty | Avg Products | Avg External | Paths | Fallbacks |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---|---|")

    for key in sorted(aggregation.keys()):
        row = aggregation[key]
        entry = row["entry"]
        query = row["query"]
        ok_total = f"{row['rounds_ok']}/{row['rounds_total']}"
        nonempty = row["rounds_nonempty"]
        avg_products = row["avg_products"]
        avg_external = row["avg_external_seed_count"]
        paths = ", ".join(f"{k}:{v}" for k, v in sorted(row["path_distribution"].items())) or "-"
        fallbacks = ", ".join(f"{k}:{v}" for k, v in sorted(row["fallback_distribution"].items())) or "-"
        lines.append(
            f"| {entry} | {query} | {ok_total} | {nonempty} | {avg_products} | {avg_external} | {paths} | {fallbacks} |"
        )

    lines.append("")
    lines.append("## Structural Signals")
    lines.append("")

    missing_contract_rows: List[str] = []
    for key in sorted(aggregation.keys()):
        row = aggregation[key]
        if not row["rounds_ok"]:
            continue
        if row["query"] == "perfume" and row["semantic_retry_applied_rounds"] == 0:
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` semantic retry observed rounds: `0/{row['rounds_ok']}`"
            )
        if (
            row["query"] == "perfume"
            and row["semantic_retry_applied_rounds"] > 0
            and row["rounds_nonempty"] == 0
        ):
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` semantic retry was applied but final result stayed empty "
                f"(`retry_rounds={row['semantic_retry_applied_rounds']}`, `nonempty_rounds=0`)."
            )
        if row["fallback_distribution"].get("primary_irrelevant_no_fallback", 0) > 0:
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` fallback contains `primary_irrelevant_no_fallback` "
                f"({row['fallback_distribution']['primary_irrelevant_no_fallback']} rounds)."
            )

    if missing_contract_rows:
        lines.append("Potential follow-ups observed:")
        lines.extend(missing_contract_rows)
    else:
        lines.append("No obvious semantic retry or fallback anomalies in this snapshot.")

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe dual search chains and emit reports.")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--sleep-ms", type=int, default=250)
    parser.add_argument(
        "--queries",
        nargs="*",
        default=DEFAULT_QUERIES,
        help="Queries to probe. Default: tom ford kylie sigma fenty lingerie perfume",
    )
    parser.add_argument(
        "--agent-base-url",
        default="https://pivota-agent-production.up.railway.app",
    )
    parser.add_argument(
        "--gateway-url",
        default="https://agent.pivota.cc/api/gateway",
    )
    parser.add_argument(
        "--out-dir",
        default=str(
            Path(__file__).resolve().parents[2] / "reports"
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()

    rounds = max(1, int(args.rounds))
    limit = max(1, int(args.limit))
    sleep_seconds = max(0.0, int(args.sleep_ms) / 1000.0)

    timestamp = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    agent_headers: Dict[str, str] = {}
    agent_api_key = os.getenv("AGENT_API_KEY", "").strip()
    if agent_api_key:
        agent_headers["X-Agent-API-Key"] = agent_api_key

    gateway_headers: Dict[str, str] = {}

    records: List[Dict[str, Any]] = []

    for entry in ("agent_v1", "api_gateway"):
        for query in args.queries:
            for round_index in range(1, rounds + 1):
                started = time.perf_counter()
                try:
                    if entry == "agent_v1":
                        status_code, body = _probe_agent_search(
                            base_url=args.agent_base_url,
                            query=query,
                            limit=limit,
                            timeout_seconds=float(args.timeout_seconds),
                            headers=agent_headers,
                        )
                    else:
                        status_code, body = _probe_gateway(
                            gateway_url=args.gateway_url,
                            query=query,
                            limit=limit,
                            timeout_seconds=float(args.timeout_seconds),
                            headers=gateway_headers,
                        )
                    parse_ok = isinstance(body, dict)
                    metrics = _extract_common_metrics(body) if parse_ok else {}
                    error_message = None
                    if status_code != 200:
                        if isinstance(body, dict):
                            error_message = body.get("message") or body.get("error") or str(body)[:240]
                        else:
                            error_message = str(body)[:240]
                except Exception as exc:  # network/runtime guard
                    status_code = 0
                    body = {"error": "request_exception", "message": str(exc)}
                    parse_ok = True
                    metrics = {}
                    error_message = str(exc)

                elapsed_ms = int((time.perf_counter() - started) * 1000)
                records.append(
                    {
                        "entry": entry,
                        "query": query,
                        "round": round_index,
                        "http_status": status_code,
                        "elapsed_ms": elapsed_ms,
                        "parse_ok": parse_ok,
                        "error": error_message,
                        "metrics": metrics,
                        "raw": body,
                    }
                )

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

    aggregation = _aggregate(records)

    payload = {
        "generated_at": timestamp,
        "config": {
            "rounds": rounds,
            "limit": limit,
            "sleep_ms": int(args.sleep_ms),
            "queries": list(args.queries),
            "agent_base_url": args.agent_base_url,
            "gateway_url": args.gateway_url,
        },
        "aggregation": aggregation,
        "records": records,
    }

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"search_chain_inventory_{stamp}.json"
    md_path = out_dir / f"search_chain_inventory_{stamp}.md"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(
        _render_markdown(
            generated_at=timestamp,
            rounds=rounds,
            limit=limit,
            agent_base_url=args.agent_base_url,
            gateway_url=args.gateway_url,
            aggregation=aggregation,
        ),
        encoding="utf-8",
    )

    print(str(json_path))
    print(str(md_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
