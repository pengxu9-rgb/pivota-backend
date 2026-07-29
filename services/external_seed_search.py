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


def _build_text_match_clause(
    *, param_key: str, include_seed_data_text_match: bool, columns_only: bool = False
) -> str:
    parts = [
        f"LOWER(destination_url) LIKE :{param_key}",
        f"LOWER(canonical_url) LIKE :{param_key}",
        f"LOWER(domain) LIKE :{param_key}",
        f"LOWER(title) LIKE :{param_key}",
    ]
    if columns_only:
        # Rank-only path: match the cheap indexed columns and DO NOT touch the
        # seed_data JSON recall paths. Extracting those (#>>) detoasts the large
        # TOASTed seed_data per candidate for the ORDER BY — the dominant cost of
        # a broad multi-term seed query. Ranking is a soft signal (the caller
        # re-ranks), so the JSON paths are omitted for speed. Recall (the WHERE)
        # is unaffected — it still uses build_external_seed_text_clause with the
        # full path set.
        return "(" + " OR ".join(parts) + ")"
    parts_extra = [
        # Match the recall enrichment so a product's verified actives / search
        # aliases (e.g. "soy isoflavones", "adenosine", "korean firming cream")
        # count toward recall — these live in derived.recall, NOT the title, so
        # without this a product is only findable by its title words and its
        # differentiators are unsearchable. Each path has a dedicated trigram GIN
        # index (idx_external_product_seeds_recall_{retrieval_title,retrieval_summary,
        # ingredient_tokens,alias_tokens}_trgm + the attached_* variants), so these
        # LIKEs are index-backed, not a full seed_data scan.
        f"LOWER(COALESCE(seed_data->'derived'->'recall'->>'retrieval_title', '')) LIKE :{param_key}",
        f"LOWER(COALESCE(seed_data->'derived'->'recall'->>'retrieval_summary', '')) LIKE :{param_key}",
        f"LOWER(COALESCE(seed_data#>>'{{derived,recall,ingredient_tokens}}', '')) LIKE :{param_key}",
        f"LOWER(COALESCE(seed_data#>>'{{derived,recall,alias_tokens}}', '')) LIKE :{param_key}",
    ]
    parts.extend(parts_extra)
    if include_seed_data_text_match:
        parts.append(f"LOWER(CAST(seed_data AS TEXT)) LIKE :{param_key}")
    return "(" + " OR ".join(parts) + ")"


def _normalize_text_terms(raw_terms: Optional[List[str]], *, max_terms: int = 8) -> List[str]:
    terms: List[str] = []
    seen = set()
    for item in raw_terms or []:
        token = str(item or "").strip().lower()
        if not token:
            continue
        normalized = " ".join(re.findall(r"[a-z0-9]+", token))
        if not normalized:
            continue
        if normalized in _EXTERNAL_SEED_QUERY_STOPWORDS:
            continue
        if len(normalized) <= 1:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
        if len(terms) >= max_terms:
            break
    return terms


def build_external_seed_text_clause(
    *,
    raw_query: Optional[str],
    include_seed_data_text_match: bool = False,
    param_prefix: str = "q",
    skip_phrase_arms: bool = False,
    lean_columns: bool = False,
) -> tuple[str, Dict[str, Any]]:
    query = str(raw_query or "").strip().lower()
    if not query:
        return "", {}

    values: Dict[str, Any] = {}
    clauses: List[str] = []
    tokens = seed_search_terms(query)

    # lean_columns restricts EVERY per-arm match to the four cheap inline columns
    # (destination_url/canonical_url/domain/title) and drops the seed_data->derived
    # ->recall JSON paths from the WHERE. Those paths are trgm-indexed, but GIN
    # trgm is lossy: every candidate the bitmap flags is heap-rechecked, and the
    # recheck detoasts the whole TOASTed seed_data blob. For a many-token query a
    # common token (e.g. "skin", "cream") matches thousands of rows via the long
    # retrieval_summary arm → thousands of detoasts → the query blows past the
    # stage-A timeout and returns nothing ("gentle foaming cleanser for oily skin"
    # ~2.4s → timeout → 0). Dropping the seed_data arms keeps the match on the
    # inline columns only (no detoast): worst-case ~0.06s, and title/url matching
    # is HIGHER precision for long natural-language queries. The caller only opts
    # into this above a token-count threshold, so short ingredient queries keep
    # the recall-rich full path.

    # The whole-phrase (%a b c%) and compact (%abc%) arms are a strict SUBSET of
    # the per-token OR (%a% OR %b% OR %c%) — anything matching the phrase matches
    # each token. When there ARE per-token arms they're redundant, and they bloat
    # the OR enough that the planner abandons the trgm GIN indexes for a full
    # parallel seq scan that detoasts seed_data per row (the multi-term perf
    # cliff: "brightening vitamin c serum" ~4s → ~1s once dropped). So skip them
    # when skip_phrase_arms and tokens exist — recall is unchanged (per-token is a
    # superset). If there are no usable tokens, keep the whole-phrase arm.
    if not (skip_phrase_arms and tokens):
        key = f"{param_prefix}_like"
        values[key] = f"%{query}%"
        clauses.append(
            _build_text_match_clause(
                param_key=key,
                include_seed_data_text_match=include_seed_data_text_match,
                columns_only=lean_columns,
            )
        )

        compact = "".join(query.split())
        if compact and compact != query:
            compact_key = f"{param_prefix}_compact_like"
            values[compact_key] = f"%{compact}%"
            clauses.append(
                _build_text_match_clause(
                    param_key=compact_key,
                    include_seed_data_text_match=include_seed_data_text_match,
                    columns_only=lean_columns,
                )
            )

    for idx, term in enumerate(tokens):
        term_key = f"{param_prefix}_term_{idx}"
        values[term_key] = f"%{term}%"
        clauses.append(
            _build_text_match_clause(
                param_key=term_key,
                include_seed_data_text_match=include_seed_data_text_match,
                columns_only=lean_columns,
            )
        )

    return "(" + " OR ".join(clauses) + ")", values


def _normalize_seed_terms(
    terms: Optional[List[str]],
    *,
    max_items: int = 8,
) -> List[str]:
    normalized: List[str] = []
    for term in terms or []:
        token = re.sub(r"\s+", " ", str(term or "").strip().lower())
        if not token:
            continue
        if token in normalized:
            continue
        normalized.append(token)
        if len(normalized) >= max_items:
            break
    return normalized


def build_external_seed_required_terms_clause(
    *,
    required_terms: Optional[List[str]],
    include_seed_data_text_match: bool = True,
    param_prefix: str = "required",
) -> tuple[str, Dict[str, Any]]:
    terms = _normalize_seed_terms(required_terms, max_items=6)
    if not terms:
        return "", {}
    values: Dict[str, Any] = {}
    clauses: List[str] = []
    for idx, term in enumerate(terms):
        key = f"{param_prefix}_{idx}"
        values[key] = f"%{term}%"
        clauses.append(
            _build_text_match_clause(
                param_key=key,
                include_seed_data_text_match=include_seed_data_text_match,
            )
        )
    return "(" + " AND ".join(clauses) + ")", values


def build_external_seed_prefer_terms_rank_sql(
    *,
    prefer_terms: Optional[List[str]],
    include_seed_data_text_match: bool = True,
    param_prefix: str = "prefer",
    columns_only: bool = False,
) -> tuple[str, Dict[str, Any]]:
    terms = _normalize_seed_terms(prefer_terms, max_items=8)
    if not terms:
        return "0", {}
    values: Dict[str, Any] = {}
    rank_terms: List[str] = []
    for idx, term in enumerate(terms):
        key = f"{param_prefix}_{idx}"
        values[key] = f"%{term}%"
        rank_terms.append(
            "(CASE WHEN "
            + _build_text_match_clause(
                param_key=key,
                include_seed_data_text_match=include_seed_data_text_match,
                columns_only=columns_only,
            )
            + " THEN 1 ELSE 0 END)"
        )
    return "(" + " + ".join(rank_terms) + ")", values


def _is_missing_external_seed_table(exc: Exception) -> bool:
    """Missing-table classifier for this query's tables — now BOTH of them.

    The quarantine anti-join makes `catalog_source_quarantine` a hard dependency
    of every seed-search route. Matching only on `external_product_seeds` meant a
    database without migration 134 raised `relation "catalog_source_quarantine"
    does not exist`, which this classifier did not recognise and the timeout
    classifier did not either — so the exception escaped and every seed-search
    route 500'd instead of degrading to empty.

    Prod is unaffected (134 is applied, 15 quarantines live), but prod runs with
    SKIP_HEAVY_STARTUP_INIT=true and manual psql migrations, so a fresh staging
    DB is exactly the case that would have hit this.
    """
    msg = str(exc or "")
    names = ("external_product_seeds", "catalog_source_quarantine")
    return any(name in msg for name in names) and (
        "does not exist" in msg
        or "UndefinedTable" in msg
        or "relation" in msg
        or "no such table" in msg.lower()
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


# The seed row's storefront column, passed RAW.
#
# Normalisation (lowercase, `www.` strip, empty-as-NULL) lives in
# `source_quarantine._sql_bare_domain` and is applied to BOTH sides of the
# comparison there. An earlier version normalised only here — which under-blocked
# a `www.`-prefixed quarantine value and split this gate from the Python matcher,
# each blocking a pair the other let through. One normaliser, both sides, one
# place: a second copy is how the first three drifted.
#
# A `destination_url` fallback for the 1,779 domain-less seeds was measured on
# prod and rescues exactly 0 additional rows, so it is deliberately NOT added —
# an unused branch is an untested branch.
_SEED_QUARANTINE_DOMAIN_EXPR = "domain"


def build_seed_quarantine_anti_join() -> str:
    """The shared quarantine anti-join, scoped to what a seed row can supply.

    `external_product_seeds` has no merchant_id / platform / source_system /
    source_ref columns, so those three match types are passed NULL: `NULL || ':'
    || NULL` is NULL on both engines, and `match_value = NULL` is never TRUE, so
    they cannot match rather than matching wrongly.

    That means only `domain` quarantines bite at this seam. Stated rather than
    implied: an operator adding a `merchant_platform` quarantine expecting it to
    stop external seeds would otherwise get a quarantine that reports success and
    does nothing. The per-consumer Python gate in `services/offer_currency_policy`
    still covers merchant_platform on the lanes it runs on.
    """
    from services.source_quarantine import build_quarantine_anti_join_sql

    return build_quarantine_anti_join_sql(
        row_domain_expr=_SEED_QUARANTINE_DOMAIN_EXPR,
        row_merchant_expr="NULL",
        row_platform_expr="NULL",
        row_source_system_expr="NULL",
        row_source_ref_expr="NULL",
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
    market: Optional[str],
    query: Optional[str],
    limit: int,
    offset: int = 0,
    include_seed_data_text_match: bool = False,
    only_unattached: bool = True,
    query_timeout_seconds: float = 0.8,
    required_terms: Optional[List[str]] = None,
    prefer_terms: Optional[List[str]] = None,
    scope: str = "default",
    use_required_terms_filter: bool = False,
    include_total_count: bool = True,
    fast_multiterm: bool = False,
    lean_where_min_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    # For queries at/above lean_where_min_tokens tokens, restrict the WHERE to the
    # cheap inline columns (no seed_data->recall JSON arms) so a broad many-token
    # OR can't detoast thousands of rows and blow the stage-A timeout. Below the
    # threshold the recall-rich full path is unchanged. None/0 disables entirely.
    lean_where_applied = bool(
        lean_where_min_tokens
        and lean_where_min_tokens > 0
        and len(seed_search_terms(query)) >= lean_where_min_tokens
    )
    where = ["status = :status"]
    values: Dict[str, Any] = {
        "status": "active",
        "limit": max(1, int(limit or 1)),
        "offset": max(0, int(offset or 0)),
    }
    if only_unattached:
        where.append("attached_product_key IS NULL")
    normalized_market = str(market or "").strip().upper()
    if normalized_market:
        where.append("market = :market")
        values["market"] = normalized_market
    text_clause, text_values = build_external_seed_text_clause(
        raw_query=query,
        include_seed_data_text_match=include_seed_data_text_match,
        param_prefix="q",
        skip_phrase_arms=fast_multiterm,
        lean_columns=lean_where_applied,
    )
    if text_clause:
        where.append(text_clause)
        values.update(text_values)
    if use_required_terms_filter:
        required_clause, required_values = build_external_seed_required_terms_clause(
            required_terms=required_terms,
            include_seed_data_text_match=include_seed_data_text_match,
            param_prefix="required",
        )
        if required_clause:
            where.append(required_clause)
            values.update(required_values)
    # Recall-first policy: required_terms are ranking hints only (via prefer_terms),
    # never a hard SQL filter.
    rank_sql, rank_values = build_external_seed_prefer_terms_rank_sql(
        prefer_terms=prefer_terms or required_terms,
        include_seed_data_text_match=include_seed_data_text_match,
        param_prefix="prefer",
        columns_only=fast_multiterm,
    )
    scope_token = str(scope or "default").strip().lower() or "default"
    rank_enabled = scope_token in {"brand_strict", "brand_broad", "brand", "default"}
    rank_expr = rank_sql if rank_enabled else "0"
    query_values: Dict[str, Any] = {**values}
    if rank_enabled:
        query_values.update(rank_values)

    # Applied to BOTH statements. A quarantine clause on the page query but not
    # the count would make total_count advertise rows the page cannot contain —
    # the caller then pages into a tail that is permanently empty.
    quarantine_clause = build_seed_quarantine_anti_join()

    query_sql = f"""
                SELECT
                  id, external_product_id, market, tool, utm_template, partner_type, disclosure_text,
                  destination_url, canonical_url, domain, title, image_url,
                  price_amount, price_currency, availability,
                  seed_data,
                  status, notes, created_by_employee_id,
                  attached_product_key, attached_variant_id,
                  seller_ref, seed_kind,
                  created_at, updated_at,
                  {rank_expr} AS brand_term_hit
                FROM external_product_seeds
                WHERE {" AND ".join(where)}
                {quarantine_clause}
                ORDER BY brand_term_hit DESC, created_at DESC, id DESC
                LIMIT :limit OFFSET :offset
                """
    count_sql = f"""
                SELECT COUNT(*) AS total_count
                FROM external_product_seeds
                WHERE {" AND ".join(where)}
                {quarantine_clause}
                """
    count_values: Dict[str, Any] = {
        k: v for k, v in values.items() if k not in {"limit", "offset"}
    }
    timeout_seconds = max(0.05, float(query_timeout_seconds or 0.8))
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
                    rows = await database.fetch_all(query_sql, query_values)
                    if include_total_count:
                        try:
                            count_row = await database.fetch_one(count_sql, count_values)
                            total_count = int(
                                (count_row.get("total_count") if isinstance(count_row, dict) else dict(count_row).get("total_count"))
                                or 0
                            )
                        except Exception:
                            total_count = len(rows or [])
                    else:
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
                    database.fetch_all(query_sql, query_values),
                    timeout=timeout_seconds,
                )
                if include_total_count:
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
                    total_count = len(rows or [])
        else:
            rows = await asyncio.wait_for(
                database.fetch_all(query_sql, query_values),
                timeout=timeout_seconds,
            )
            if include_total_count:
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
                total_count = len(rows or [])
        if total_count <= 0:
            total_count = len(rows or [])
        return {
            "rows": [dict(row) for row in (rows or [])],
            "total_count": total_count,
            "query_ms": int((time.perf_counter() - started) * 1000),
            "query_timeout": False,
            "table_missing": False,
            "scope": str(scope or "default"),
            "lean_where_applied": lean_where_applied,
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
        snapshot = ensure_json_obj(seed_data.get("snapshot"))
        canonical_url = (
            str(seed_row.get("canonical_url") or "").strip()
            or str(snapshot.get("canonical_url") or "").strip()
            or str(snapshot.get("destination_url") or "").strip()
            or str(seed_data.get("canonical_url") or "").strip()
            or str(seed_data.get("destination_url") or "").strip()
        )
        destination_url = (
            str(seed_row.get("destination_url") or "").strip()
            or str(snapshot.get("destination_url") or "").strip()
            or str(snapshot.get("canonical_url") or "").strip()
            or str(seed_data.get("destination_url") or "").strip()
            or str(seed_data.get("canonical_url") or "").strip()
        )
        external_product_id = (
            str(seed_row.get("external_product_id") or "").strip()
            or str(seed_data.get("external_product_id") or "").strip()
            or str(snapshot.get("external_product_id") or "").strip()
            or stable_external_product_id(canonical_url or destination_url or str(seed_row.get("id") or ""))
        )
        if not external_product_id or external_product_id in seen:
            continue
        seen.add(external_product_id)
        deduped.append(seed_row)
        if len(deduped) >= max_items:
            break
    return deduped
