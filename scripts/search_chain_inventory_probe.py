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
import hashlib
import json
import os
import statistics
import subprocess
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
SCHEMA_VERSION = "v2"
ROUTE_HEALTH_CONTRACT_FIELDS = [
    "orchestrator_path",
    "decision_node",
    "domain_filter_dropped_external",
    "external_fill_gate_reason",
    "semantic_retry_applied",
    "semantic_retry_query",
    "semantic_retry_hits",
    "external_seed_brand_strict_rows",
    "external_seed_brand_relevant_rows",
    "external_seed_broad_fallback_used",
    "external_seed_broad_scope_rows",
    "internal_raw_count",
    "external_raw_count",
    "merged_pre_limit_count",
    "primary_quality_gate_passed",
    "primary_quality_score",
    "low_quality_nonempty_detected",
    "supplement_attempted",
    "supplement_skip_reason",
    "retry_attempt_count",
    "final_returned_count",
    "fallback_reason",
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
    search_decision = metadata.get("search_decision")
    if not isinstance(search_decision, dict):
        search_decision = {}
    missing_route_health_fields = [
        field for field in ROUTE_HEALTH_CONTRACT_FIELDS if field not in route_health
    ]

    source_breakdown = metadata.get("source_breakdown")
    if not isinstance(source_breakdown, dict):
        source_breakdown = {}
    fallback_strategy = metadata.get("fallback_strategy")
    if not isinstance(fallback_strategy, dict):
        fallback_strategy = {}
    proxy_search_fallback = metadata.get("proxy_search_fallback")
    if not isinstance(proxy_search_fallback, dict):
        proxy_search_fallback = {}

    products = body.get("products") if isinstance(body, dict) else []
    product_count = len(products) if isinstance(products, list) else 0
    clarification = body.get("clarification") if isinstance(body, dict) else None
    has_clarification = bool(
        isinstance(clarification, dict) and str(clarification.get("question") or "").strip()
    )

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
    top_domain_drop = _non_negative_int(metadata.get("domain_filter_dropped_external") or 0)
    route_domain_drop = _non_negative_int(
        route_health.get("domain_filter_dropped_external")
        if route_health.get("domain_filter_dropped_external") is not None
        else top_domain_drop
    )
    decision_domain_drop = _non_negative_int(
        search_decision.get("domain_filter_dropped_external")
        if search_decision.get("domain_filter_dropped_external") is not None
        else route_domain_drop
    )
    top_semantic_class = str(metadata.get("query_semantic_class") or "").strip().lower()
    route_semantic_class = str(
        route_health.get("query_semantic_class")
        if route_health.get("query_semantic_class") is not None
        else top_semantic_class
    ).strip().lower()
    decision_semantic_class = str(
        search_decision.get("query_semantic_class")
        if search_decision.get("query_semantic_class") is not None
        else route_semantic_class
    ).strip().lower()
    semantic_class_layer_diff = (
        ""
        if top_semantic_class == route_semantic_class == decision_semantic_class
        else f"top={top_semantic_class or 'null'}|route={route_semantic_class or 'null'}|decision={decision_semantic_class or 'null'}"
    )
    fallback_attempt_count = _non_negative_int(
        fallback_strategy.get("secondary_attempt_count")
        if fallback_strategy.get("secondary_attempt_count") is not None
        else len(fallback_strategy.get("secondary_attempts") or [])
    )
    base_query = str(
        (metadata.get("search_trace") or {}).get("raw_query")
        if isinstance(metadata.get("search_trace"), dict)
        else ""
    ).strip()
    base_query_norm = " ".join(base_query.lower().split())
    retry_query = str(
        route_health.get("semantic_retry_query")
        if route_health.get("semantic_retry_query") is not None
        else metadata.get("semantic_retry_query")
        or fallback_strategy.get("secondary_selected_query")
        or ""
    ).strip()
    retry_query_norm = " ".join(retry_query.lower().split())
    actual_retry_attempted = bool(
        fallback_attempt_count > 1
        or proxy_search_fallback.get("query_variant") == "semantic_retry"
        or (
            retry_query_norm
            and base_query_norm
            and retry_query_norm != base_query_norm
        )
    )
    search_decision_final = str(search_decision.get("final_decision") or "").strip()
    decision_final_vs_products_diff = False
    if search_decision_final:
        if product_count == 0 and has_clarification:
            decision_final_vs_products_diff = search_decision_final not in {
                "clarify",
                "products_returned_with_clarification",
            }
        elif product_count == 0 and not has_clarification:
            decision_final_vs_products_diff = search_decision_final in {
                "products_returned",
                "products_returned_with_clarification",
                "upstream_returned",
                "cache_returned",
                "resolver_returned",
            }

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
        "query_semantic_class": route_semantic_class,
        "metadata_query_semantic_class": top_semantic_class,
        "search_decision_query_semantic_class": decision_semantic_class,
        "query_semantic_class_sync_ok": bool(
            top_semantic_class == route_semantic_class == decision_semantic_class
        ),
        "semantic_class_layer_diff": semantic_class_layer_diff,
        "domain_filter_dropped_external": route_domain_drop,
        "metadata_domain_filter_dropped_external": top_domain_drop,
        "search_decision_domain_filter_dropped_external": decision_domain_drop,
        "domain_filter_shadow_dropped_external": decision_domain_drop,
        "domain_filter_sync_ok": bool(
            top_domain_drop == route_domain_drop == decision_domain_drop
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
        "actual_retry_attempted": actual_retry_attempted,
        "fallback_attempt_count": fallback_attempt_count,
        "retry_attempt_count": _non_negative_int(
            route_health.get("retry_attempt_count")
            if route_health.get("retry_attempt_count") is not None
            else metadata.get("retry_attempt_count")
            or fallback_attempt_count
        ),
        "internal_raw_count": _non_negative_int(
            route_health.get("internal_raw_count")
            if route_health.get("internal_raw_count") is not None
            else metadata.get("internal_raw_count")
            or source_breakdown.get("internal_count")
            or 0
        ),
        "external_raw_count": _non_negative_int(
            route_health.get("external_raw_count")
            if route_health.get("external_raw_count") is not None
            else metadata.get("external_raw_count")
            or source_breakdown.get("external_seed_count")
            or 0
        ),
        "merged_pre_limit_count": _non_negative_int(
            route_health.get("merged_pre_limit_count")
            if route_health.get("merged_pre_limit_count") is not None
            else metadata.get("merged_pre_limit_count")
            or body.get("total")
            or 0
        ),
        "primary_quality_gate_passed": bool(
            route_health.get("primary_quality_gate_passed")
            if route_health.get("primary_quality_gate_passed") is not None
            else metadata.get("primary_quality_gate_passed")
            if metadata.get("primary_quality_gate_passed") is not None
            else True
        ),
        "primary_quality_score": _safe_float(
            route_health.get("primary_quality_score")
            if route_health.get("primary_quality_score") is not None
            else metadata.get("primary_quality_score")
        ),
        "low_quality_nonempty_detected": bool(
            route_health.get("low_quality_nonempty_detected")
            if route_health.get("low_quality_nonempty_detected") is not None
            else metadata.get("low_quality_nonempty_detected")
            or False
        ),
        "supplement_attempted": bool(
            route_health.get("supplement_attempted")
            if route_health.get("supplement_attempted") is not None
            else metadata.get("supplement_attempted")
            or False
        ),
        "supplement_skip_reason": (
            str(
                route_health.get("supplement_skip_reason")
                if route_health.get("supplement_skip_reason") is not None
                else metadata.get("supplement_skip_reason")
                or ""
            ).strip()
            or None
        ),
        "final_returned_count": _non_negative_int(
            route_health.get("final_returned_count")
            if route_health.get("final_returned_count") is not None
            else metadata.get("final_returned_count")
            or product_count
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
        "search_decision_final_decision": search_decision_final,
        "decision_final_vs_products_diff": bool(decision_final_vs_products_diff),
        "route_health_contract_missing_count": len(missing_route_health_fields),
        "route_health_contract_missing_fields": missing_route_health_fields,
        "fallback_reason_sync_ok": metadata.get("fallback_reason") == route_health.get("fallback_reason"),
    }


def _sha12_for_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except Exception:
        return ""
    return hashlib.sha1(data).hexdigest()[:12]


def _git_short_sha(repo_dir: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except Exception:
        return ""


def _probe_agent_search(
    *,
    base_url: str,
    query: str,
    limit: int,
    source: str,
    timeout_seconds: float,
    headers: Dict[str, str],
) -> Tuple[int, Dict[str, Any]]:
    params = {
        "query": query,
        "search_all_merchants": "true",
        "limit": limit,
        "offset": 0,
        "source": source,
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
    source: str,
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
            "source": source,
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
        internal_raw_counts = [int(r.get("metrics", {}).get("internal_raw_count") or 0) for r in ok_rows]
        external_raw_counts = [int(r.get("metrics", {}).get("external_raw_count") or 0) for r in ok_rows]
        merged_pre_limit_counts = [int(r.get("metrics", {}).get("merged_pre_limit_count") or 0) for r in ok_rows]
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
            "avg_internal_raw_count": round(statistics.mean(internal_raw_counts), 2) if internal_raw_counts else 0.0,
            "avg_external_raw_count": round(statistics.mean(external_raw_counts), 2) if external_raw_counts else 0.0,
            "avg_merged_pre_limit_count": round(statistics.mean(merged_pre_limit_counts), 2) if merged_pre_limit_counts else 0.0,
            "avg_primary_latency_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
            "path_distribution": dict(path_counter),
            "decision_distribution": dict(decision_counter),
            "fallback_distribution": dict(fallback_counter),
            "semantic_retry_applied_rounds": sum(
                1 for r in ok_rows if bool(r.get("metrics", {}).get("semantic_retry_applied"))
            ),
            "actual_retry_attempted_rounds": sum(
                1 for r in ok_rows if bool(r.get("metrics", {}).get("actual_retry_attempted"))
            ),
            "low_quality_nonempty_rounds": sum(
                1 for r in ok_rows if bool(r.get("metrics", {}).get("low_quality_nonempty_detected"))
            ),
            "supplement_attempted_rounds": sum(
                1 for r in ok_rows if bool(r.get("metrics", {}).get("supplement_attempted"))
            ),
            "avg_fallback_attempt_count": round(
                statistics.mean(
                    [
                        _non_negative_int(r.get("metrics", {}).get("fallback_attempt_count") or 0)
                        for r in ok_rows
                    ]
                ),
                2,
            )
            if ok_rows
            else 0.0,
            "brand_broad_fallback_rounds": sum(
                1 for r in ok_rows if bool(r.get("metrics", {}).get("external_seed_broad_fallback_used"))
            ),
            "domain_filter_dropped_external_total": sum(
                _non_negative_int(r.get("metrics", {}).get("domain_filter_dropped_external") or 0)
                for r in ok_rows
            ),
            "domain_filter_shadow_dropped_external_total": sum(
                _non_negative_int(
                    r.get("metrics", {}).get("domain_filter_shadow_dropped_external") or 0
                )
                for r in ok_rows
            ),
            "domain_filter_sync_fail_rounds": sum(
                1
                for r in ok_rows
                if not bool(r.get("metrics", {}).get("domain_filter_sync_ok"))
            ),
            "query_semantic_class_sync_fail_rounds": sum(
                1
                for r in ok_rows
                if not bool(r.get("metrics", {}).get("query_semantic_class_sync_ok"))
            ),
            "semantic_class_layer_diff_rounds": sum(
                1
                for r in ok_rows
                if bool(str(r.get("metrics", {}).get("semantic_class_layer_diff") or "").strip())
            ),
            "decision_final_vs_products_diff_rounds": sum(
                1
                for r in ok_rows
                if bool(r.get("metrics", {}).get("decision_final_vs_products_diff"))
            ),
            "route_health_contract_missing_rounds": sum(
                1
                for r in ok_rows
                if _non_negative_int(
                    r.get("metrics", {}).get("route_health_contract_missing_count") or 0
                )
                > 0
            ),
            "fallback_reason_sync_fail_rounds": sum(
                1
                for r in ok_rows
                if not bool(r.get("metrics", {}).get("fallback_reason_sync_ok"))
            ),
        }
    return out


def _render_markdown(
    *,
    generated_at: str,
    schema_version: str,
    probe_script_sha: str,
    release_sha_agent: str,
    release_sha_backend: str,
    source_used: str,
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
    lines.append(f"- Schema version: `{schema_version}`")
    lines.append(f"- Probe script SHA: `{probe_script_sha or 'unknown'}`")
    lines.append(f"- Release SHA (agent): `{release_sha_agent or 'unknown'}`")
    lines.append(f"- Release SHA (backend): `{release_sha_backend or 'unknown'}`")
    lines.append(f"- Source used (both entries): `{source_used}`")
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
    lines.append("## Field Consistency")
    lines.append("")
    lines.append(
        "| Entry | Query | Domain Drop Avg (route/shadow) | Domain Sync Fail | Semantic Sync Fail |"
    )
    lines.append("|---|---|---|---:|---:|")
    for key in sorted(aggregation.keys()):
        row = aggregation[key]
        rounds_ok = max(1, int(row.get("rounds_ok") or 0))
        route_avg = round(
            _safe_float(row.get("domain_filter_dropped_external_total")) / rounds_ok,
            2,
        )
        shadow_avg = round(
            _safe_float(row.get("domain_filter_shadow_dropped_external_total")) / rounds_ok,
            2,
        )
        lines.append(
            f"| {row['entry']} | {row['query']} | {route_avg}/{shadow_avg} | "
            f"{row.get('domain_filter_sync_fail_rounds', 0)} | "
            f"{row.get('query_semantic_class_sync_fail_rounds', 0)} |"
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
        if row["query"] == "perfume" and row.get("actual_retry_attempted_rounds", 0) == 0:
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` had no actual retry attempts "
                f"(`actual_retry_attempted_rounds=0/{row['rounds_ok']}`)."
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
        if row["route_health_contract_missing_rounds"] > 0:
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` route_health contract missing "
                f"`{row['route_health_contract_missing_rounds']}/{row['rounds_ok']}` rounds."
            )
        if row["fallback_reason_sync_fail_rounds"] > 0:
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` fallback reason top-level/route_health mismatch in "
                f"`{row['fallback_reason_sync_fail_rounds']}/{row['rounds_ok']}` rounds."
            )
        if row.get("domain_filter_sync_fail_rounds", 0) > 0:
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` domain_filter_dropped_external split detected in "
                f"`{row['domain_filter_sync_fail_rounds']}/{row['rounds_ok']}` rounds "
                f"(route/top vs search_decision)."
            )
        if row.get("query_semantic_class_sync_fail_rounds", 0) > 0:
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` query_semantic_class split detected in "
                f"`{row['query_semantic_class_sync_fail_rounds']}/{row['rounds_ok']}` rounds."
            )
        if row.get("semantic_class_layer_diff_rounds", 0) > 0:
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` semantic class layer diff detected in "
                f"`{row['semantic_class_layer_diff_rounds']}/{row['rounds_ok']}` rounds."
            )
        if row.get("decision_final_vs_products_diff_rounds", 0) > 0:
            missing_contract_rows.append(
                f"- `{row['entry']}::{row['query']}` search_decision.final_decision mismatched products in "
                f"`{row['decision_final_vs_products_diff_rounds']}/{row['rounds_ok']}` rounds."
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
    parser.add_argument("--source", default="shopping_agent")
    parser.add_argument(
        "--agent-api-key",
        default=os.getenv("AGENT_API_KEY", "").strip(),
        help="Optional API key for /agent/v1/products/search (sent as X-Agent-API-Key and X-API-Key).",
    )
    parser.add_argument(
        "--gateway-api-key",
        default=(
            os.getenv("GATEWAY_API_KEY", "").strip()
            or os.getenv("X_API_KEY", "").strip()
            or os.getenv("API_KEY", "").strip()
        ),
        help="Optional API key for /api/gateway (sent as X-API-Key).",
    )
    parser.add_argument("--release-sha-agent", default=os.getenv("RELEASE_SHA_AGENT", "").strip())
    parser.add_argument("--release-sha-backend", default=os.getenv("RELEASE_SHA_BACKEND", "").strip())
    args = parser.parse_args()

    rounds = max(1, int(args.rounds))
    limit = max(1, int(args.limit))
    sleep_seconds = max(0.0, int(args.sleep_ms) / 1000.0)

    timestamp = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    source_used = str(args.source or "").strip() or "shopping_agent"
    script_path = Path(__file__).resolve()
    probe_script_sha = _sha12_for_file(script_path)
    repo_root = script_path.parents[1]
    release_sha_backend = str(args.release_sha_backend or "").strip() or _git_short_sha(repo_root)
    release_sha_agent = str(args.release_sha_agent or "").strip()

    agent_headers: Dict[str, str] = {}
    agent_api_key = str(args.agent_api_key or "").strip()
    if agent_api_key:
        agent_headers["X-Agent-API-Key"] = agent_api_key
        # Some deployments only inspect X-API-Key.
        agent_headers["X-API-Key"] = agent_api_key

    gateway_headers: Dict[str, str] = {}
    gateway_api_key = str(args.gateway_api_key or "").strip()
    if gateway_api_key:
        gateway_headers["X-API-Key"] = gateway_api_key

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
                            source=source_used,
                            timeout_seconds=float(args.timeout_seconds),
                            headers=agent_headers,
                        )
                    else:
                        status_code, body = _probe_gateway(
                            gateway_url=args.gateway_url,
                            query=query,
                            limit=limit,
                            source=source_used,
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
                        "source_used": source_used,
                    }
                )

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

    aggregation = _aggregate(records)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "probe_script_sha": probe_script_sha,
        "release_sha_agent": release_sha_agent,
        "release_sha_backend": release_sha_backend,
        "source_used": source_used,
        "generated_at": timestamp,
        "config": {
            "rounds": rounds,
            "limit": limit,
            "sleep_ms": int(args.sleep_ms),
            "queries": list(args.queries),
            "agent_base_url": args.agent_base_url,
            "gateway_url": args.gateway_url,
            "source": source_used,
            "agent_auth_header_used": bool(agent_api_key),
            "gateway_auth_header_used": bool(gateway_api_key),
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
            schema_version=SCHEMA_VERSION,
            probe_script_sha=probe_script_sha,
            release_sha_agent=release_sha_agent,
            release_sha_backend=release_sha_backend,
            source_used=source_used,
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
