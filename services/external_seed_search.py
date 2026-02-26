from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional


_EXTERNAL_SEED_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "buy",
    "cart",
    "checkout",
    "find",
    "for",
    "item",
    "items",
    "me",
    "of",
    "or",
    "please",
    "product",
    "products",
    "recommend",
    "recommendation",
    "show",
    "the",
    "to",
    "with",
}


def stable_external_product_id(url: str) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        return ""
    return "ext_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def ensure_json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def seed_search_terms(raw_query: str) -> List[str]:
    query = str(raw_query or "").strip()
    if not query:
        return []
    terms = re.findall(r"[a-z0-9]+", query.lower())
    if not terms:
        terms = [t for t in query.lower().split() if t]

    filtered: List[str] = []
    for term in terms:
        if term in _EXTERNAL_SEED_QUERY_STOPWORDS:
            continue
        if len(term) <= 1:
            continue
        if term not in filtered:
            filtered.append(term)
        if len(filtered) >= 8:
            break
    return filtered or terms[:4]


def _build_text_match_clause(*, param_key: str, include_seed_data_text_match: bool) -> str:
    parts = [
        f"LOWER(destination_url) LIKE :{param_key}",
        f"LOWER(canonical_url) LIKE :{param_key}",
        f"LOWER(domain) LIKE :{param_key}",
        f"LOWER(title) LIKE :{param_key}",
    ]
    if include_seed_data_text_match:
        parts.append(f"LOWER(CAST(seed_data AS TEXT)) LIKE :{param_key}")
    return "(" + " OR ".join(parts) + ")"


def build_external_seed_text_clause(
    *,
    raw_query: Optional[str],
    include_seed_data_text_match: bool = False,
    param_prefix: str = "q",
) -> tuple[str, Dict[str, Any]]:
    query = str(raw_query or "").strip().lower()
    if not query:
        return "", {}

    values: Dict[str, Any] = {}
    clauses: List[str] = []

    key = f"{param_prefix}_like"
    values[key] = f"%{query}%"
    clauses.append(_build_text_match_clause(param_key=key, include_seed_data_text_match=include_seed_data_text_match))

    compact = "".join(query.split())
    if compact and compact != query:
        compact_key = f"{param_prefix}_compact_like"
        values[compact_key] = f"%{compact}%"
        clauses.append(
            _build_text_match_clause(
                param_key=compact_key,
                include_seed_data_text_match=include_seed_data_text_match,
            )
        )

    for idx, term in enumerate(seed_search_terms(query)):
        term_key = f"{param_prefix}_term_{idx}"
        values[term_key] = f"%{term}%"
        clauses.append(
            _build_text_match_clause(
                param_key=term_key,
                include_seed_data_text_match=include_seed_data_text_match,
            )
        )

    return "(" + " OR ".join(clauses) + ")", values


def _is_missing_external_seed_table(exc: Exception) -> bool:
    msg = str(exc or "")
    return (
        "external_product_seeds" in msg
        and ("does not exist" in msg or "UndefinedTable" in msg or "relation" in msg)
    )


def _is_external_seed_query_timeout(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    return (
        "timeout" in msg
        or "querycancellederror" in msg
        or "query canceled" in msg
        or "statement timeout" in msg
        or "canceling statement due to statement timeout" in msg
    )


def _database_supports_statement_timeout(database: Any) -> bool:
    db_url = getattr(database, "url", None)
    if db_url is None:
        return False
    raw_url = str(db_url).strip().lower()
    return raw_url.startswith("postgres://") or raw_url.startswith("postgresql://")


async def fetch_external_seed_rows(
    *,
    database: Any,
    market: str,
    query: Optional[str],
    limit: int,
    offset: int = 0,
    include_seed_data_text_match: bool = False,
    query_timeout_seconds: float = 0.35,
) -> Dict[str, Any]:
    where = ["status = :status", "attached_product_key IS NULL", "market = :market"]
    values: Dict[str, Any] = {
        "status": "active",
        "market": str(market or "US").strip().upper() or "US",
        "limit": max(1, int(limit or 1)),
        "offset": max(0, int(offset or 0)),
    }
    text_clause, text_values = build_external_seed_text_clause(
        raw_query=query,
        include_seed_data_text_match=include_seed_data_text_match,
        param_prefix="q",
    )
    if text_clause:
        where.append(text_clause)
        values.update(text_values)

    query_sql = f"""
                SELECT
                  id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
                  destination_url, canonical_url, domain, title, image_url,
                  price_amount, price_currency, availability,
                  seed_data,
                  status, notes, created_by_employee_id,
                  attached_product_key, attached_variant_id,
                  created_at, updated_at
                FROM external_product_seeds
                WHERE {" AND ".join(where)}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT :limit OFFSET :offset
                """
    count_sql = f"""
                SELECT COUNT(*) AS total_count
                FROM external_product_seeds
                WHERE {" AND ".join(where)}
                """
    count_values: Dict[str, Any] = {
        k: v for k, v in values.items() if k not in {"limit", "offset"}
    }
    timeout_seconds = max(0.05, float(query_timeout_seconds or 0.35))
    timeout_ms = max(50, int(timeout_seconds * 1000))

    started = time.perf_counter()
    try:
        rows = None
        total_count = 0
        if (
            _database_supports_statement_timeout(database)
            and hasattr(database, "transaction")
            and hasattr(database, "execute")
        ):
            try:
                async with database.transaction():
                    await database.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                    rows = await database.fetch_all(query_sql, values)
                    try:
                        count_row = await database.fetch_one(count_sql, count_values)
                        total_count = int(
                            (count_row.get("total_count") if isinstance(count_row, dict) else dict(count_row).get("total_count"))
                            or 0
                        )
                    except Exception:
                        total_count = len(rows or [])
            except Exception as exc:
                if _is_missing_external_seed_table(exc):
                    return {
                        "rows": [],
                        "total_count": 0,
                        "query_ms": int((time.perf_counter() - started) * 1000),
                        "query_timeout": False,
                        "table_missing": True,
                    }
                if _is_external_seed_query_timeout(exc):
                    return {
                        "rows": [],
                        "total_count": 0,
                        "query_ms": int((time.perf_counter() - started) * 1000),
                        "query_timeout": True,
                        "table_missing": False,
                    }
                rows = await asyncio.wait_for(
                    database.fetch_all(query_sql, values),
                    timeout=timeout_seconds,
                )
                try:
                    count_row = await asyncio.wait_for(
                        database.fetch_one(count_sql, count_values),
                        timeout=timeout_seconds,
                    )
                    total_count = int(
                        (
                            count_row.get("total_count")
                            if isinstance(count_row, dict)
                            else dict(count_row).get("total_count")
                        )
                        or 0
                    )
                except Exception:
                    total_count = len(rows or [])
        else:
            rows = await asyncio.wait_for(
                database.fetch_all(query_sql, values),
                timeout=timeout_seconds,
            )
            try:
                count_row = await asyncio.wait_for(
                    database.fetch_one(count_sql, count_values),
                    timeout=timeout_seconds,
                )
                total_count = int(
                    (
                        count_row.get("total_count")
                        if isinstance(count_row, dict)
                        else dict(count_row).get("total_count")
                    )
                    or 0
                )
            except Exception:
                total_count = len(rows or [])
        if total_count <= 0:
            total_count = len(rows or [])
        return {
            "rows": [dict(row) for row in (rows or [])],
            "total_count": total_count,
            "query_ms": int((time.perf_counter() - started) * 1000),
            "query_timeout": False,
            "table_missing": False,
        }
    except asyncio.TimeoutError:
        return {
            "rows": [],
            "total_count": 0,
            "query_ms": int((time.perf_counter() - started) * 1000),
            "query_timeout": True,
            "table_missing": False,
        }
    except Exception as exc:
        if _is_missing_external_seed_table(exc):
            return {
                "rows": [],
                "total_count": 0,
                "query_ms": int((time.perf_counter() - started) * 1000),
                "query_timeout": False,
                "table_missing": True,
            }
        if _is_external_seed_query_timeout(exc):
            return {
                "rows": [],
                "total_count": 0,
                "query_ms": int((time.perf_counter() - started) * 1000),
                "query_timeout": True,
                "table_missing": False,
            }
        raise


def dedupe_external_seed_rows(rows: List[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    max_items = max(1, int(limit or 1))
    for raw in rows or []:
        seed_row = dict(raw or {})
        seed_data = ensure_json_obj(seed_row.get("seed_data"))
        external_product_id = (
            str(seed_row.get("external_product_id") or "").strip()
            or str(seed_data.get("external_product_id") or "").strip()
            or stable_external_product_id(seed_row.get("canonical_url") or seed_row.get("destination_url") or "")
        )
        if not external_product_id or external_product_id in seen:
            continue
        seen.add(external_product_id)
        deduped.append(seed_row)
        if len(deduped) >= max_items:
            break
    return deduped
