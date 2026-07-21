#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from sqlalchemy import create_engine, text

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db.database import database
from services.beauty_external_ranking import (
    BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION,
    build_ranking_audit_record,
    rank_external_seed_rows,
)
from services.external_seed_search import fetch_external_seed_rows, seed_search_terms
from services.external_seed_search import (
    build_external_seed_prefer_terms_rank_sql,
    build_external_seed_text_clause,
)


DEFAULT_CORPUS = (
    Path(__file__).resolve().parent / "fixtures" / "beauty_ranking_golden_corpus.json"
)

_SHOPPING_SURFACES = {"shopping_agent", "aurora", "aurora-bff"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit beauty cross-merchant ranking parity across SQL raw rows, gateway, and pivot."
    )
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--market", default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--gateway-base-url", default=None)
    parser.add_argument("--pivot-base-url", default=None)
    parser.add_argument("--header", action="append", default=[], help="Repeatable raw header in 'Name: Value' form.")
    parser.add_argument("--gateway-header", action="append", default=[], help="Repeatable raw header for gateway in 'Name: Value' form.")
    parser.add_argument("--pivot-header", action="append", default=[], help="Repeatable raw header for pivot in 'Name: Value' form.")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--db-mode", choices=("auto", "async", "sync"), default="auto")
    parser.add_argument("--seed-fetch-mode", choices=("fast", "deep"), default="fast")
    parser.add_argument("--seed-stage-a-timeout-seconds", type=float, default=0.9)
    parser.add_argument("--seed-stage-b-timeout-seconds", type=float, default=1.6)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    return parser.parse_args()


def _headers(raw_headers: List[str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    for raw in raw_headers:
        if ":" not in str(raw):
            continue
        name, value = raw.split(":", 1)
        if name.strip():
            headers[name.strip()] = value.strip()
    return headers


def _write(path_str: Optional[str], content: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_corpus(path_str: str) -> List[Dict[str, Any]]:
    payload = json.loads(Path(path_str).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Corpus must be a JSON list")
    return [item for item in payload if isinstance(item, dict)]


def _raw_seed_fetch_limit(case: Dict[str, Any], default_limit: int) -> int:
    display_limit = max(1, int(case.get("limit") or default_limit or 1))
    page = max(1, int(case.get("page") or 1))
    source = str(case.get("source") or "").strip().lower()
    if source in _SHOPPING_SURFACES:
        return min(max(display_limit * page * 2, 30), 200)
    return display_limit


def _resolve_database_url(args: argparse.Namespace) -> Optional[str]:
    explicit = str(getattr(args, "database_url", "") or "").strip()
    if explicit:
        return explicit
    env_value = str(os.getenv("DATABASE_URL") or "").strip()
    return env_value or None


def _use_sync_db(args: argparse.Namespace) -> bool:
    mode = str(getattr(args, "db_mode", "auto") or "auto").strip().lower()
    if mode == "sync":
        return True
    if mode == "async":
        return False
    return bool(_resolve_database_url(args))


def _fetch_external_seed_rows_sync(
    *,
    connection: Any,
    query: str,
    limit: int,
    market: Optional[str],
    include_seed_data_text_match: bool,
    query_timeout_seconds: float,
) -> Dict[str, Any]:
    where = ["status = :status"]
    values: Dict[str, Any] = {
        "status": "active",
        "limit": max(1, int(limit or 1)),
    }
    normalized_market = str(market or "").strip().upper()
    if normalized_market:
        where.append("market = :market")
        values["market"] = normalized_market

    # The seed_data recall arms use PostgreSQL JSON path operators (->, #>>)
    # that sqlite can't parse. The sync path accepts any --database-url, so on
    # a sqlite connection restrict matching to the inline columns
    # (url/domain/title) instead of failing the whole audit.
    columns_only = (
        getattr(getattr(connection, "dialect", None), "name", "") == "sqlite"
    )

    text_clause, text_values = build_external_seed_text_clause(
        raw_query=query,
        include_seed_data_text_match=include_seed_data_text_match and not columns_only,
        param_prefix="q",
        lean_columns=columns_only,
    )
    if text_clause:
        where.append(text_clause)
        values.update(text_values)

    rank_sql, rank_values = build_external_seed_prefer_terms_rank_sql(
        prefer_terms=seed_search_terms(query),
        include_seed_data_text_match=include_seed_data_text_match and not columns_only,
        param_prefix="prefer",
        columns_only=columns_only,
    )
    values.update(rank_values)
    query_sql = text(
        f"""
        SELECT
          id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
          destination_url, canonical_url, domain, title, image_url,
          price_amount, price_currency, availability,
          seed_data,
          status, notes, created_by_employee_id,
          attached_product_key, attached_variant_id,
          created_at, updated_at,
          {rank_sql} AS brand_term_hit
        FROM external_product_seeds
        WHERE {" AND ".join(where)}
        ORDER BY brand_term_hit DESC, created_at DESC, id DESC
        LIMIT :limit
        """
    )
    started = time.perf_counter()
    try:
        rows = [dict(row._mapping) for row in connection.execute(query_sql, values)]
        return {
            "rows": rows,
            "query_ms": int((time.perf_counter() - started) * 1000),
            "query_timeout": False,
            "table_missing": False,
        }
    except Exception as exc:
        message = str(exc or "").lower()
        table_missing = "external_product_seeds" in message and (
            "no such table" in message
            or "does not exist" in message
            or "undefinedtable" in message
            or "relation" in message
        )
        query_timeout = "timeout" in message or "statement timeout" in message
        if table_missing or query_timeout:
            return {
                "rows": [],
                "query_ms": int((time.perf_counter() - started) * 1000),
                "query_timeout": bool(query_timeout),
                "table_missing": bool(table_missing),
            }
        raise


async def _fetch_raw_external_rows(
    *,
    query: str,
    limit: int,
    market: Optional[str],
    stage_a_timeout_seconds: float,
    stage_b_timeout_seconds: float,
    seed_fetch_mode: str,
    sync_connection: Any = None,
) -> Dict[str, Any]:
    query_terms = seed_search_terms(query)
    if sync_connection is not None:
        stage_a = _fetch_external_seed_rows_sync(
            connection=sync_connection,
            market=market,
            query=query,
            limit=limit,
            include_seed_data_text_match=False,
            query_timeout_seconds=stage_a_timeout_seconds,
        )
    else:
        stage_a = await fetch_external_seed_rows(
            database=database,
            market=market,
            query=query,
            limit=limit,
            offset=0,
            include_seed_data_text_match=False,
            only_unattached=False,
            query_timeout_seconds=stage_a_timeout_seconds,
            required_terms=None,
            prefer_terms=query_terms or None,
            scope="default",
            use_required_terms_filter=False,
            include_total_count=False,
        )
    rows = stage_a.get("rows") or []
    stage_b = None
    if (
        str(seed_fetch_mode or "fast").strip().lower() == "deep"
        and not rows
        and query.strip()
        and not bool(stage_a.get("table_missing"))
    ):
        if sync_connection is not None:
            stage_b = _fetch_external_seed_rows_sync(
                connection=sync_connection,
                market=market,
                query=query,
                limit=limit,
                include_seed_data_text_match=True,
                query_timeout_seconds=stage_b_timeout_seconds,
            )
        else:
            stage_b = await fetch_external_seed_rows(
                database=database,
                market=market,
                query=query,
                limit=limit,
                offset=0,
                include_seed_data_text_match=True,
                only_unattached=False,
                query_timeout_seconds=stage_b_timeout_seconds,
                required_terms=None,
                prefer_terms=query_terms or None,
                scope="default",
                use_required_terms_filter=False,
                include_total_count=False,
            )
        rows = stage_b.get("rows") or []
    ranked = rank_external_seed_rows(rows, query=query, limit=limit)
    return {
        "stage_a": {
            "row_count": len(stage_a.get("rows") or []),
            "query_timeout": bool(stage_a.get("query_timeout") or False),
            "query_ms": stage_a.get("query_ms"),
            "table_missing": bool(stage_a.get("table_missing") or False),
        },
        "stage_b": {
            "executed": bool(stage_b is not None),
            "row_count": len((stage_b or {}).get("rows") or []),
            "query_timeout": bool((stage_b or {}).get("query_timeout") or False),
            "query_ms": (stage_b or {}).get("query_ms"),
            "table_missing": bool((stage_b or {}).get("table_missing") or False),
        },
        "raw_rows": rows,
        "ranked_candidates": ranked,
        "ranking_audit": build_ranking_audit_record(
            query=query,
            raw_rows=rows,
            ranked_candidates=ranked,
        ),
    }


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


def _extract_titles(payload: Dict[str, Any], *, payload_type: str) -> List[str]:
    body = payload.get("body") if isinstance(payload, dict) else {}
    body = body if isinstance(body, dict) else {}
    if payload_type == "gateway":
        products = body.get("products")
        if not isinstance(products, list):
            return []
        return [str((item or {}).get("title") or "").strip() for item in products if isinstance(item, dict)]
    items = body.get("items")
    if not isinstance(items, list):
        return []
    titles: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        titles.append(str(product.get("title") or "").strip())
    return [title for title in titles if title]


def _top5_overlap(a: Iterable[str], b: Iterable[str]) -> int:
    a_norm = [str(item or "").strip().lower() for item in a if str(item or "").strip()]
    b_norm = [str(item or "").strip().lower() for item in b if str(item or "").strip()]
    return len(set(a_norm[:5]).intersection(set(b_norm[:5])))


def _structured_field_loss_diff(
    ranked_candidates: List[Dict[str, Any]],
    pivot_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    ranked_top = ranked_candidates[0] if ranked_candidates else {}
    ranked_ingredients = ranked_top.get("normalized_ingredient_ids") if isinstance(ranked_top, dict) else []
    ranked_visible_attributes = ranked_top.get("normalized_visible_attributes") if isinstance(ranked_top, dict) else {}
    body = (pivot_payload or {}).get("body") if isinstance(pivot_payload, dict) else {}
    items = body.get("items") if isinstance(body, dict) else []
    items = items if isinstance(items, list) else []
    pivot_top = items[0] if items else {}
    pivot_sku = pivot_top.get("sku") if isinstance(pivot_top, dict) else {}
    pivot_ingredients = pivot_sku.get("ingredient_ids") if isinstance(pivot_sku, dict) else []
    pivot_visible_attributes = pivot_sku.get("visible_attributes") if isinstance(pivot_sku, dict) else {}
    return {
        "ranked_top_ingredient_ids": ranked_ingredients or [],
        "pivot_top_ingredient_ids": pivot_ingredients or [],
        "ranked_top_visible_attributes": ranked_visible_attributes or {},
        "pivot_top_visible_attributes": pivot_visible_attributes or {},
        "ingredient_loss": bool(ranked_ingredients and not pivot_ingredients),
        "visible_attribute_loss": bool(ranked_visible_attributes and not pivot_visible_attributes),
    }


def _candidate_omission_reason(raw_rows: List[Dict[str, Any]], ranked_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kept_ids = {
        str(item.get("external_product_id") or "").strip()
        for item in ranked_candidates
        if isinstance(item, dict)
    }
    omitted: List[Dict[str, Any]] = []
    for idx, row in enumerate(raw_rows):
        external_product_id = str(
            row.get("external_product_id")
            or (row.get("seed_data") or {}).get("external_product_id")
            or ""
        ).strip()
        if external_product_id and external_product_id in kept_ids:
            continue
        omitted.append(
            {
                "source_order": idx,
                "external_product_id": external_product_id,
                "title": str(row.get("title") or "").strip(),
                "reason": "deduped_or_trimmed_after_canonical_rerank",
            }
        )
    return omitted


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Beauty Ranking Audit",
        "",
        f"- ranking_audit_version: `{report['ranking_audit_version']}`",
        f"- corpus_path: `{report['corpus_path']}`",
        f"- db_mode: `{report.get('db_mode')}`",
        f"- seed_fetch_mode: `{report.get('seed_fetch_mode')}`",
        f"- cases: `{report['summary']['case_count']}`",
        f"- gateway_top1_matches: `{report['summary']['gateway_top1_matches']}`",
        f"- gateway_top1_evaluable: `{report['summary']['gateway_top1_evaluable']}`",
        f"- pivot_top1_matches: `{report['summary']['pivot_top1_matches']}`",
        f"- pivot_top1_evaluable: `{report['summary']['pivot_top1_evaluable']}`",
        f"- gateway_nonempty: `{report['summary']['gateway_nonempty']}`",
        f"- pivot_nonempty: `{report['summary']['pivot_nonempty']}`",
        f"- raw_seed_available_cases: `{report['summary']['raw_seed_available_cases']}`",
        f"- raw_seed_table_missing_cases: `{report['summary']['raw_seed_table_missing_cases']}`",
        "",
        "## Cases",
        "",
    ]
    for case in report["cases"]:
        lines.extend(
            [
                f"### {case['query']}",
                "",
                f"- gateway_top1_diff: `{case['top1_diff']['gateway_vs_ranked']}`",
                f"- pivot_top1_diff: `{case['top1_diff']['pivot_vs_ranked']}`",
                f"- gateway_top5_overlap: `{case['top5_overlap']['gateway_vs_ranked']}`",
                f"- pivot_top5_overlap: `{case['top5_overlap']['pivot_vs_ranked']}`",
                f"- candidate_omissions: `{len(case['candidate_omission_reason'])}`",
                f"- structured_field_loss: `{case['structured_field_loss_diff']}`",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


async def _build_report(args: argparse.Namespace) -> Dict[str, Any]:
    corpus = _load_corpus(args.corpus)
    use_sync_db = _use_sync_db(args)
    sync_engine = None
    sync_connection = None
    connected_here = False
    if use_sync_db:
        database_url = _resolve_database_url(args)
        if not database_url:
            raise ValueError("database_url is required when db_mode resolves to sync")
        sync_engine = create_engine(database_url, pool_pre_ping=True)
        sync_connection = sync_engine.connect()
    elif hasattr(database, "is_connected") and not database.is_connected:
        await database.connect()
        connected_here = True
    try:
        base_url = str(args.base_url or "").strip() or None
        gateway_base_url = str(args.gateway_base_url or base_url or "").strip() or None
        pivot_base_url = str(args.pivot_base_url or base_url or "").strip() or None
        shared_headers = _headers(args.header)
        gateway_headers = {**shared_headers, **_headers(args.gateway_header)}
        pivot_headers = {**shared_headers, **_headers(args.pivot_header)}
        cases: List[Dict[str, Any]] = []
        gateway_top1_matches = 0
        pivot_top1_matches = 0
        gateway_nonempty = 0
        pivot_nonempty = 0
        gateway_top1_evaluable = 0
        pivot_top1_evaluable = 0
        raw_seed_available_cases = 0
        raw_seed_table_missing_cases = 0

        for case in corpus:
            query = str(case.get("query") or "").strip()
            if not query:
                continue
            market = str(case.get("market") or args.market or "").strip() or None
            raw_limit = _raw_seed_fetch_limit(case, int(args.limit))
            raw_fetch = await _fetch_raw_external_rows(
                query=query,
                limit=raw_limit,
                market=market,
                stage_a_timeout_seconds=float(args.seed_stage_a_timeout_seconds),
                stage_b_timeout_seconds=float(args.seed_stage_b_timeout_seconds),
                seed_fetch_mode=str(args.seed_fetch_mode or "fast"),
                sync_connection=sync_connection,
            )
            ranked_candidates = raw_fetch["ranking_audit"]["ranked_candidates"]
            ranked_titles = [str(item.get("title") or "").strip() for item in ranked_candidates if isinstance(item, dict)]
            raw_seed_available = bool(raw_fetch["raw_rows"])
            raw_seed_table_missing = bool(raw_fetch["stage_a"].get("table_missing") or raw_fetch["stage_b"].get("table_missing"))
            if raw_seed_available:
                raw_seed_available_cases += 1
            if raw_seed_table_missing:
                raw_seed_table_missing_cases += 1

            gateway_payload = None
            pivot_payload = None
            if gateway_base_url:
                gateway_payload = _post_json(
                    url=f"{gateway_base_url.rstrip('/')}/agent/shop/v1/invoke",
                    payload={
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
                            "source": str(case.get("source") or "shopping_agent"),
                            **({"market": market} if market else {}),
                        },
                    },
                    headers=gateway_headers,
                    timeout_seconds=float(args.timeout_seconds),
                )
            if pivot_base_url:
                pivot_payload = _post_json(
                    url=f"{pivot_base_url.rstrip('/')}/v1/pivot/query",
                    payload={
                        "query": query,
                        "limit": int(case.get("limit") or 10),
                        "include_external": True,
                        "include_incentives": False,
                        **({"market": market} if market else {}),
                    },
                    headers=pivot_headers,
                    timeout_seconds=float(args.timeout_seconds),
                )

            gateway_titles = _extract_titles(gateway_payload or {}, payload_type="gateway")
            pivot_titles = _extract_titles(pivot_payload or {}, payload_type="pivot")
            if gateway_titles:
                gateway_nonempty += 1
            if pivot_titles:
                pivot_nonempty += 1
            if ranked_titles and gateway_titles:
                gateway_top1_evaluable += 1
                if ranked_titles[0].strip().lower() == gateway_titles[0].strip().lower():
                    gateway_top1_matches += 1
            if ranked_titles and pivot_titles:
                pivot_top1_evaluable += 1
                if ranked_titles[0].strip().lower() == pivot_titles[0].strip().lower():
                    pivot_top1_matches += 1

            cases.append(
                {
                    "query": query,
                    "market": market,
                    "raw_seed_fetch_limit": raw_limit,
                    "raw_seed_available": raw_seed_available,
                    "raw_seed_table_missing": raw_seed_table_missing,
                    "raw_seed_fetch": {
                        "stage_a": raw_fetch["stage_a"],
                        "stage_b": raw_fetch["stage_b"],
                    },
                    "sql_raw_seed_rows": raw_fetch["ranking_audit"]["raw_seed_rows"],
                    "gateway_external_candidate_pool": ranked_candidates,
                    "gateway_final_ranking": gateway_payload,
                    "pivot_final_ranking": pivot_payload,
                    "top1_diff": {
                        "gateway_vs_ranked": {
                            "ranked_top1": ranked_titles[0] if ranked_titles else None,
                            "gateway_top1": gateway_titles[0] if gateway_titles else None,
                            "same": bool(
                                ranked_titles
                                and gateway_titles
                                and ranked_titles[0].strip().lower() == gateway_titles[0].strip().lower()
                            ),
                        },
                        "pivot_vs_ranked": {
                            "ranked_top1": ranked_titles[0] if ranked_titles else None,
                            "pivot_top1": pivot_titles[0] if pivot_titles else None,
                            "same": bool(
                                ranked_titles
                                and pivot_titles
                                and ranked_titles[0].strip().lower() == pivot_titles[0].strip().lower()
                            ),
                        },
                    },
                    "top5_overlap": {
                        "gateway_vs_ranked": _top5_overlap(ranked_titles, gateway_titles),
                        "pivot_vs_ranked": _top5_overlap(ranked_titles, pivot_titles),
                    },
                    "candidate_omission_reason": _candidate_omission_reason(
                        raw_fetch["raw_rows"],
                        ranked_candidates,
                    ),
                    "structured_field_loss_diff": _structured_field_loss_diff(
                        ranked_candidates,
                        pivot_payload,
                    ),
                }
            )

        return {
            "ranking_audit_version": BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION,
            "corpus_path": str(Path(args.corpus).resolve()),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "gateway_base_url": gateway_base_url,
            "pivot_base_url": pivot_base_url,
            "db_mode": "sync" if use_sync_db else "async",
            "seed_fetch_mode": str(args.seed_fetch_mode or "fast"),
            "seed_stage_a_timeout_seconds": float(args.seed_stage_a_timeout_seconds),
            "seed_stage_b_timeout_seconds": float(args.seed_stage_b_timeout_seconds),
            "summary": {
                "case_count": len(cases),
                "gateway_top1_matches": gateway_top1_matches,
                "gateway_top1_evaluable": gateway_top1_evaluable,
                "pivot_top1_matches": pivot_top1_matches,
                "pivot_top1_evaluable": pivot_top1_evaluable,
                "gateway_nonempty": gateway_nonempty,
                "pivot_nonempty": pivot_nonempty,
                "raw_seed_available_cases": raw_seed_available_cases,
                "raw_seed_table_missing_cases": raw_seed_table_missing_cases,
            },
            "cases": cases,
        }
    finally:
        if sync_connection is not None:
            sync_connection.close()
        if sync_engine is not None:
            sync_engine.dispose()
        if connected_here:
            await database.disconnect()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_build_report(args))
    json_text = json.dumps(report, indent=2, ensure_ascii=False)
    md_text = _render_markdown(report)
    _write(args.output_json, json_text)
    _write(args.output_md, md_text)
    print(json_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
