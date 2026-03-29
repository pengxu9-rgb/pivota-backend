#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CORPUS = SCRIPT_DIR / "fixtures" / "generic_commerce_shadow_corpus.json"
BAD_PRICE_DELTA_RATIO_THRESHOLD = 0.20


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit generic commerce served-vs-pivot parity for default/fragrance queries."
    )
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--gateway-base-url", default=None)
    parser.add_argument("--pivot-base-url", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Repeatable raw header in 'Name: Value' form.",
    )
    parser.add_argument(
        "--gateway-header",
        action="append",
        default=[],
        help="Repeatable raw header for gateway in 'Name: Value' form.",
    )
    parser.add_argument(
        "--pivot-header",
        action="append",
        default=[],
        help="Repeatable raw header for pivot in 'Name: Value' form.",
    )
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _load_corpus(path_str: str) -> List[Dict[str, Any]]:
    payload = json.loads(Path(path_str).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Corpus must be a JSON list")
    return [item for item in payload if isinstance(item, dict)]


def _headers(raw_headers: Sequence[str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    for raw in raw_headers:
        if ":" not in str(raw):
            continue
        name, value = raw.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name:
            headers[name] = value
    return headers


def _write(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _post_json(
    *,
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout_seconds: float,
) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout_seconds)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        try:
            body = response.json()
        except Exception:
            body = {"raw_text": response.text[:2000]}
        return {
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "body": body,
            "request_failed": False,
            "request_timed_out": False,
        }
    except requests.RequestException as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        return {
            "status_code": None,
            "elapsed_ms": elapsed_ms,
            "body": {
                "error": exc.__class__.__name__,
                "message": str(exc)[:2000],
            },
            "request_failed": True,
            "request_timed_out": isinstance(exc, requests.Timeout),
        }


def _normalize_title(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _product_best_price(product: Any) -> Optional[float]:
    if not isinstance(product, dict):
        return None
    candidates: List[Any] = []
    best_deal = product.get("best_deal")
    if isinstance(best_deal, dict):
        candidates.append(best_deal.get("estimated_best_price"))
        candidates.append(best_deal.get("merchant_effective_price"))
    candidates.extend(
        [
            product.get("price"),
            product.get("estimated_best_price"),
            product.get("merchant_effective_price"),
            product.get("list_price"),
        ]
    )
    for variant in product.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        candidates.append(variant.get("price"))
        candidates.append(variant.get("compare_at_price"))
    for candidate in candidates:
        try:
            if candidate in (None, ""):
                continue
            return float(candidate)
        except Exception:
            continue
    return None


def _pivot_item_best_price(item: Any) -> Optional[float]:
    if not isinstance(item, dict):
        return None
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    offers = item.get("offers") if isinstance(item.get("offers"), list) else []
    primary_offer = offers[0] if offers and isinstance(offers[0], dict) else {}
    pricing = primary_offer.get("pricing") if isinstance(primary_offer.get("pricing"), dict) else {}
    product_like = {
        "price": product.get("price"),
        "estimated_best_price": pricing.get("estimated_best_price"),
        "merchant_effective_price": pricing.get("merchant_effective_price"),
        "list_price": pricing.get("list_price"),
        "best_deal": {
            "estimated_best_price": pricing.get("estimated_best_price"),
            "merchant_effective_price": pricing.get("merchant_effective_price"),
        },
    }
    return _product_best_price(product_like)


def _gateway_products(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    body = payload.get("body") if isinstance(payload, dict) else {}
    body = body if isinstance(body, dict) else {}
    products = body.get("products")
    return [item for item in products if isinstance(item, dict)] if isinstance(products, list) else []


def _pivot_products(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    body = payload.get("body") if isinstance(payload, dict) else {}
    body = body if isinstance(body, dict) else {}
    items = body.get("items")
    if not isinstance(items, list):
        return []
    products: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        sku = item.get("sku") if isinstance(item.get("sku"), dict) else {}
        products.append(
            {
                "merchant_id": product.get("merchant_id"),
                "product_id": product.get("source_product_id") or product.get("product_key") or sku.get("product_key"),
                "canonical_url": product.get("canonical_url"),
                "title": product.get("title"),
                "price": _pivot_item_best_price(item),
            }
        )
    return products


def _product_signatures(product: Any) -> set[str]:
    if not isinstance(product, dict):
        return set()
    merchant_id = str(product.get("merchant_id") or "").strip().lower()
    product_id = str(
        product.get("product_id")
        or product.get("id")
        or product.get("source_product_id")
        or product.get("product_key")
        or ""
    ).strip().lower()
    canonical_url = str(product.get("canonical_url") or "").strip().lower()
    title = _normalize_title(product.get("title"))
    signatures: set[str] = set()
    if merchant_id and product_id:
        signatures.add(f"{merchant_id}::{product_id}")
    if canonical_url:
        signatures.add(f"url::{canonical_url}")
    if title:
        signatures.add(f"title::{title}")
        if merchant_id:
            signatures.add(f"{merchant_id}::title::{title}")
    return signatures


def _signature_overlap_count(
    left_signatures: Sequence[set[str]],
    right_signatures: Sequence[set[str]],
) -> int:
    matched_right_indexes: set[int] = set()
    overlap_count = 0
    for left_signature_set in left_signatures:
        for right_idx, right_signature_set in enumerate(right_signatures):
            if right_idx in matched_right_indexes:
                continue
            if left_signature_set.intersection(right_signature_set):
                matched_right_indexes.add(right_idx)
                overlap_count += 1
                break
    return overlap_count


def _top5_overlap(
    gateway_products: Sequence[Dict[str, Any]],
    pivot_products: Sequence[Dict[str, Any]],
) -> int:
    gateway_signatures = [_product_signatures(product) for product in gateway_products[:5] if _product_signatures(product)]
    pivot_signatures = [_product_signatures(product) for product in pivot_products[:5] if _product_signatures(product)]
    return _signature_overlap_count(gateway_signatures, pivot_signatures)


def _top1_same(
    gateway_products: Sequence[Dict[str, Any]],
    pivot_products: Sequence[Dict[str, Any]],
) -> bool:
    if not gateway_products and not pivot_products:
        return True
    if not gateway_products or not pivot_products:
        return False
    return bool(_product_signatures(gateway_products[0]).intersection(_product_signatures(pivot_products[0])))


def _gateway_top_titles(products: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(product.get("title") or "").strip() for product in products[:5] if str(product.get("title") or "").strip()]


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


def _pivot_request(case: Dict[str, Any]) -> Dict[str, Any]:
    query = str(case.get("query") or "").strip()
    payload = {
        "query": query,
        "limit": int(case.get("limit") or 10),
        "include_external": True,
        "include_incentives": True,
    }
    market = str(case.get("market") or "").strip()
    if market:
        payload["market"] = market
    return payload


def _query_semantic_class(case: Dict[str, Any], gateway_payload: Optional[Dict[str, Any]]) -> str:
    body = (gateway_payload or {}).get("body") if isinstance(gateway_payload, dict) else {}
    metadata = body.get("metadata") if isinstance(body, dict) else {}
    route_health = metadata.get("route_health") if isinstance(metadata, dict) else {}
    resolved = (
        route_health.get("query_semantic_class")
        if isinstance(route_health, dict)
        else None
    ) or (
        metadata.get("query_semantic_class")
        if isinstance(metadata, dict)
        else None
    ) or case.get("semantic_class")
    return str(resolved or "unknown").strip() or "unknown"


def _source_summary(cases: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[str(case.get("source") or "unknown")].append(case)
    summary: Dict[str, Dict[str, Any]] = {}
    for source in sorted(grouped):
        items = grouped[source]
        top1_matches = sum(1 for case in items if bool(case.get("top1_same")) and int(case.get("gateway_returned_count") or 0) > 0 and int(case.get("pivot_returned_count") or 0) > 0)
        top1_evaluable = sum(1 for case in items if int(case.get("gateway_returned_count") or 0) > 0 and int(case.get("pivot_returned_count") or 0) > 0)
        summary[source] = {
            "sample_count": len(items),
            "top1_matches": top1_matches,
            "top1_evaluable": top1_evaluable,
            "top1_match_rate": round(top1_matches / top1_evaluable, 4) if top1_evaluable else None,
            "gateway_nonempty": sum(1 for case in items if int(case.get("gateway_returned_count") or 0) > 0),
            "pivot_nonempty": sum(1 for case in items if int(case.get("pivot_returned_count") or 0) > 0),
            "no_result_mismatch_cases": sum(1 for case in items if bool(case.get("no_result_mismatch"))),
            "bad_price_anomaly_cases": sum(1 for case in items if bool(case.get("bad_price_anomaly"))),
            "semantic_classes": dict(Counter(str(case.get("query_semantic_class") or "unknown") for case in items)),
        }
    return summary


def _semantic_class_summary(cases: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[str(case.get("query_semantic_class") or "unknown")].append(case)
    summary: Dict[str, Dict[str, Any]] = {}
    for semantic_class in sorted(grouped):
        items = grouped[semantic_class]
        top1_matches = sum(1 for case in items if bool(case.get("top1_same")) and int(case.get("gateway_returned_count") or 0) > 0 and int(case.get("pivot_returned_count") or 0) > 0)
        top1_evaluable = sum(1 for case in items if int(case.get("gateway_returned_count") or 0) > 0 and int(case.get("pivot_returned_count") or 0) > 0)
        summary[semantic_class] = {
            "sample_count": len(items),
            "top1_matches": top1_matches,
            "top1_evaluable": top1_evaluable,
            "top1_match_rate": round(top1_matches / top1_evaluable, 4) if top1_evaluable else None,
            "no_result_mismatch_cases": sum(1 for case in items if bool(case.get("no_result_mismatch"))),
            "sources": dict(Counter(str(case.get("source") or "unknown") for case in items)),
        }
    return summary


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Generic Commerce Shadow Audit",
        "",
        f"- corpus_path: `{report['corpus_path']}`",
        f"- gateway_base_url: `{report.get('gateway_base_url')}`",
        f"- pivot_base_url: `{report.get('pivot_base_url')}`",
        f"- case_count: `{report['summary']['case_count']}`",
        f"- top1_matches: `{report['summary']['top1_matches']}`",
        f"- top1_evaluable: `{report['summary']['top1_evaluable']}`",
        f"- gateway_nonempty: `{report['summary']['gateway_nonempty']}`",
        f"- pivot_nonempty: `{report['summary']['pivot_nonempty']}`",
        f"- no_result_mismatch_cases: `{report['summary']['no_result_mismatch_cases']}`",
        "",
        "## Source Summary",
        "",
    ]
    source_summary = report["summary"].get("source_summary") or {}
    if not source_summary:
        lines.append("- none")
    else:
        for source, details in source_summary.items():
            lines.append(
                f"- {source}: sample_count=`{details.get('sample_count')}` "
                f"top1=`{details.get('top1_matches')}`/`{details.get('top1_evaluable')}` "
                f"no_result_mismatch=`{details.get('no_result_mismatch_cases')}` "
                f"bad_price_anomaly=`{details.get('bad_price_anomaly_cases')}`"
            )
    lines.extend(["", "## Cases", ""])
    for case in report["cases"]:
        lines.extend(
            [
                f"### {case['query']} [{case['source']}]",
                "",
                f"- semantic_class: `{case['query_semantic_class']}`",
                f"- gateway_top1: `{case.get('gateway_top1')}`",
                f"- pivot_top1: `{case.get('pivot_top1')}`",
                f"- top1_same: `{case.get('top1_same')}`",
                f"- top5_overlap: `{case.get('top5_overlap')}`",
                f"- no_result_mismatch: `{case.get('no_result_mismatch')}`",
                f"- price_delta_ratio: `{case.get('price_delta_ratio')}`",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    args = _parse_args()
    corpus = _load_corpus(args.corpus)
    base_url = str(args.base_url or "").strip() or None
    gateway_base_url = str(args.gateway_base_url or base_url or "").strip() or None
    pivot_base_url = str(args.pivot_base_url or base_url or "").strip() or None
    shared_headers = _headers(args.header)
    gateway_headers = {**shared_headers, **_headers(args.gateway_header)}
    pivot_headers = {**shared_headers, **_headers(args.pivot_header)}

    cases: List[Dict[str, Any]] = []
    top1_matches = 0
    top1_evaluable = 0
    gateway_nonempty = 0
    pivot_nonempty = 0
    no_result_mismatch_cases = 0
    gateway_timed_out_cases = 0
    pivot_timed_out_cases = 0
    price_delta_evaluable_cases = 0
    bad_price_anomaly_cases = 0

    for case in corpus:
        request_payload = _build_request(case)
        gateway_payload = _post_json(
            url=f"{gateway_base_url.rstrip('/')}/agent/shop/v1/invoke",
            payload=request_payload,
            headers=gateway_headers,
            timeout_seconds=float(args.timeout_seconds),
        )
        pivot_payload = _post_json(
            url=f"{pivot_base_url.rstrip('/')}/v1/pivot/query",
            payload=_pivot_request(case),
            headers=pivot_headers,
            timeout_seconds=float(args.timeout_seconds),
        )
        gateway_products = _gateway_products(gateway_payload)
        pivot_products = _pivot_products(pivot_payload)
        query_semantic_class = _query_semantic_class(case, gateway_payload)
        gateway_top1 = str(gateway_products[0].get("title") or "").strip() if gateway_products else None
        pivot_top1 = str(pivot_products[0].get("title") or "").strip() if pivot_products else None
        top1_same = _top1_same(gateway_products, pivot_products)
        top5_overlap = _top5_overlap(gateway_products, pivot_products)
        gateway_returned_count = len(gateway_products)
        pivot_returned_count = len(pivot_products)
        no_result_mismatch = bool((gateway_returned_count == 0) != (pivot_returned_count == 0))
        gateway_top_price = _product_best_price(gateway_products[0]) if gateway_products else None
        pivot_top_price = _product_best_price(pivot_products[0]) if pivot_products else None
        price_delta: Optional[float] = None
        price_delta_ratio: Optional[float] = None
        if gateway_top_price is not None and pivot_top_price is not None:
            price_delta_evaluable_cases += 1
            price_delta = round(pivot_top_price - gateway_top_price, 4)
            if gateway_top_price > 0:
                price_delta_ratio = round(price_delta / gateway_top_price, 4)
        bad_price_anomaly = bool(
            price_delta_ratio is not None
            and abs(price_delta_ratio) >= BAD_PRICE_DELTA_RATIO_THRESHOLD
        )

        if gateway_returned_count > 0:
            gateway_nonempty += 1
        if pivot_returned_count > 0:
            pivot_nonempty += 1
        if gateway_returned_count > 0 and pivot_returned_count > 0:
            top1_evaluable += 1
            if top1_same:
                top1_matches += 1
        if no_result_mismatch:
            no_result_mismatch_cases += 1
        if gateway_payload.get("request_timed_out"):
            gateway_timed_out_cases += 1
        if pivot_payload.get("request_timed_out"):
            pivot_timed_out_cases += 1
        if bad_price_anomaly:
            bad_price_anomaly_cases += 1

        cases.append(
            {
                "case_id": str(case.get("case_id") or f"{case.get('source') or 'unknown'}::{case.get('query') or ''}"),
                "query": str(case.get("query") or ""),
                "source": str(case.get("source") or "shopping_agent"),
                "page": int(case.get("page") or 1),
                "limit": int(case.get("limit") or 10),
                "query_semantic_class": query_semantic_class,
                "expected_rollout_mode": case.get("expected_rollout_mode"),
                "gateway_status_code": gateway_payload.get("status_code"),
                "pivot_status_code": pivot_payload.get("status_code"),
                "gateway_request_failed": bool(gateway_payload.get("request_failed")),
                "pivot_request_failed": bool(pivot_payload.get("request_failed")),
                "gateway_request_timed_out": bool(gateway_payload.get("request_timed_out")),
                "pivot_request_timed_out": bool(pivot_payload.get("request_timed_out")),
                "gateway_elapsed_ms": gateway_payload.get("elapsed_ms"),
                "pivot_elapsed_ms": pivot_payload.get("elapsed_ms"),
                "gateway_rollout_mode": (
                    ((gateway_payload.get("body") or {}).get("metadata") or {}).get("pivot_rollout_mode")
                    if isinstance((gateway_payload.get("body") or {}).get("metadata"), dict)
                    else None
                ),
                "gateway_rollout_guard_passed": (
                    ((gateway_payload.get("body") or {}).get("metadata") or {}).get("pivot_rollout_guard_passed")
                    if isinstance((gateway_payload.get("body") or {}).get("metadata"), dict)
                    else None
                ),
                "gateway_query_source": (
                    ((gateway_payload.get("body") or {}).get("metadata") or {}).get("query_source")
                    if isinstance((gateway_payload.get("body") or {}).get("metadata"), dict)
                    else None
                ),
                "pivot_query_source": (
                    ((pivot_payload.get("body") or {}).get("metadata") or {}).get("query_source")
                    if isinstance((pivot_payload.get("body") or {}).get("metadata"), dict)
                    else None
                ),
                "gateway_top1": gateway_top1,
                "pivot_top1": pivot_top1,
                "gateway_top5": _gateway_top_titles(gateway_products),
                "pivot_top5": _gateway_top_titles(pivot_products),
                "gateway_returned_count": gateway_returned_count,
                "pivot_returned_count": pivot_returned_count,
                "top1_same": top1_same,
                "top5_overlap": top5_overlap,
                "returned_count_delta": pivot_returned_count - gateway_returned_count,
                "no_result_mismatch": no_result_mismatch,
                "price_delta": price_delta,
                "price_delta_ratio": price_delta_ratio,
                "bad_price_anomaly": bad_price_anomaly,
            }
        )

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus_path": str(Path(args.corpus).resolve()),
        "gateway_base_url": gateway_base_url,
        "pivot_base_url": pivot_base_url,
        "summary": {
            "case_count": len(cases),
            "top1_matches": top1_matches,
            "top1_evaluable": top1_evaluable,
            "top1_match_rate": round(top1_matches / top1_evaluable, 4) if top1_evaluable else None,
            "gateway_nonempty": gateway_nonempty,
            "pivot_nonempty": pivot_nonempty,
            "no_result_mismatch_cases": no_result_mismatch_cases,
            "gateway_timed_out_cases": gateway_timed_out_cases,
            "pivot_timed_out_cases": pivot_timed_out_cases,
            "price_delta_evaluable_cases": price_delta_evaluable_cases,
            "bad_price_anomaly_cases": bad_price_anomaly_cases,
            "source_summary": _source_summary(cases),
            "semantic_class_summary": _semantic_class_summary(cases),
        },
        "cases": cases,
    }

    json_text = json.dumps(report, indent=2, ensure_ascii=False)
    md_text = _render_markdown(report)
    _write(args.output_json, json_text)
    _write(args.output_md, md_text)
    print(json_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
