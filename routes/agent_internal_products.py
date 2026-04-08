from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import routes.agent_api as agent_api
from routes.agent_auth import AgentContext, get_agent_context
from utils.agent_search_intent import infer_query_overrides

router = APIRouter(prefix="/agent/internal", tags=["agent-internal-products"])

INTERNAL_PRODUCTS_SEARCH_ALLOWED_FIELDS = {
    "query",
    "limit",
    "offset",
    "merchant_id",
    "merchant_ids",
    "search_all_merchants",
    "catalog_surface",
    "in_stock_only",
    "allow_external_seed",
    "external_seed_strategy",
    "fast_mode",
    "target_step_family",
    "semantic_family",
    "query_step_strength",
    "product_only",
    "trace_id",
}

INTERNAL_PRODUCTS_SEARCH_FORBIDDEN_FIELDS = {
    "semantic_contract",
    "semanticContract",
    "search_request_contract",
    "searchRequestContract",
    "primary_lane",
    "primary_retrieval_contract",
    "primaryRetrievalContract",
    "supplement_lanes",
    "supplementLanes",
    "local_mainline_child",
    "localMainlineChild",
    "ui_surface",
    "uiSurface",
    "decision_mode",
    "decisionMode",
    "payload",
    "metadata",
    "search",
    "request_context",
    "requestContext",
}


def _first_non_empty_string(*values: Any) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _parse_bool_like(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _normalize_non_negative_int(
    value: Any,
    *,
    minimum: int = 0,
    maximum: int = 2**31 - 1,
) -> Optional[int]:
    try:
        numeric = int(value)
    except Exception:
        return None
    return max(minimum, min(maximum, numeric))


def _normalize_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in raw_items:
        normalized = str(item or "").strip()
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def _sanitize_internal_products_search_request(
    raw_input: Any,
    *,
    reject_unknown: bool = True,
    reject_forbidden: bool = True,
    default_search_all_merchants: bool = True,
) -> Dict[str, Any]:
    payload = raw_input if isinstance(raw_input, dict) else {}
    unknown_fields: List[str] = []
    forbidden_fields: List[str] = []

    for key in payload.keys():
        if key in INTERNAL_PRODUCTS_SEARCH_FORBIDDEN_FIELDS:
            forbidden_fields.append(key)
            continue
        if key not in INTERNAL_PRODUCTS_SEARCH_ALLOWED_FIELDS:
            unknown_fields.append(key)

    if (reject_forbidden and forbidden_fields) or (reject_unknown and unknown_fields):
        return {
            "ok": False,
            "search": None,
            "forbidden_fields": forbidden_fields,
            "unknown_fields": unknown_fields,
            "invalid_fields": list(dict.fromkeys([*forbidden_fields, *unknown_fields])),
        }

    merchant_id = _first_non_empty_string(payload.get("merchant_id"))
    merchant_ids = [
        mid
        for mid in _normalize_string_list(payload.get("merchant_ids"))
        if mid != merchant_id
    ]
    explicit_search_all = _parse_bool_like(payload.get("search_all_merchants"))
    limit = _normalize_non_negative_int(payload.get("limit"), minimum=1, maximum=50)
    offset = _normalize_non_negative_int(payload.get("offset"), minimum=0)
    query = _first_non_empty_string(payload.get("query"))
    in_stock_only = _parse_bool_like(payload.get("in_stock_only"))
    allow_external_seed = _parse_bool_like(payload.get("allow_external_seed"))
    fast_mode = _parse_bool_like(payload.get("fast_mode"))
    product_only = _parse_bool_like(payload.get("product_only"))

    search: Dict[str, Any] = {}
    if query:
        search["query"] = query
    if limit is not None:
        search["limit"] = limit
    if offset is not None:
        search["offset"] = offset
    if merchant_id:
        search["merchant_id"] = merchant_id
    elif merchant_ids:
        search["merchant_ids"] = merchant_ids
    if explicit_search_all is not None:
        search["search_all_merchants"] = explicit_search_all
    elif not merchant_id and not merchant_ids and default_search_all_merchants:
        search["search_all_merchants"] = True
    catalog_surface = _first_non_empty_string(payload.get("catalog_surface")).lower()
    if catalog_surface:
        search["catalog_surface"] = catalog_surface
    if in_stock_only is not None:
        search["in_stock_only"] = in_stock_only
    if allow_external_seed is not None:
        search["allow_external_seed"] = allow_external_seed
    external_seed_strategy = _first_non_empty_string(payload.get("external_seed_strategy")).lower()
    if external_seed_strategy:
        search["external_seed_strategy"] = external_seed_strategy
    if fast_mode is not None:
        search["fast_mode"] = fast_mode
    target_step_family = _first_non_empty_string(payload.get("target_step_family")).lower()
    if target_step_family:
        search["target_step_family"] = target_step_family
    semantic_family = _first_non_empty_string(payload.get("semantic_family")).lower()
    if semantic_family:
        search["semantic_family"] = semantic_family
    query_step_strength = _first_non_empty_string(payload.get("query_step_strength")).lower()
    if query_step_strength:
        search["query_step_strength"] = query_step_strength
    if product_only is not None:
        search["product_only"] = product_only
    trace_id = _first_non_empty_string(payload.get("trace_id"))
    if trace_id:
        search["trace_id"] = trace_id

    return {
        "ok": bool(search.get("query")),
        "search": search,
        "forbidden_fields": [],
        "unknown_fields": [],
        "invalid_fields": [] if search.get("query") else ["query"],
    }


def _resolve_timeout_seconds(header_value: Optional[str], fallback_ms: int = 5000) -> float:
    requested_ms = _normalize_non_negative_int(header_value, minimum=200, maximum=20000)
    timeout_ms = requested_ms if requested_ms is not None else fallback_ms
    return max(0.2, float(timeout_ms) / 1000.0)


def _derive_beauty_query_bucket(normalized_query: str, query_semantic_class: str) -> Optional[str]:
    if str(query_semantic_class or "").strip().lower() != "beauty":
        return None
    if agent_api._beauty_query_prefers_sunscreen(normalized_query):
        return "sunscreen"
    if agent_api._beauty_query_prefers_treatment(normalized_query):
        return "skincare"
    return "beauty"


@router.post("/products/search")
async def agent_internal_products_search(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    context: AgentContext = Depends(get_agent_context),
    x_internal_search_timeout_ms: Optional[str] = Header(None, alias="X-Internal-Search-Timeout-Ms"),
    x_trace_id: Optional[str] = Header(None, alias="X-Trace-ID"),
    x_internal_caller_lane: Optional[str] = Header(None, alias="X-Internal-Caller-Lane"),
):
    validation = _sanitize_internal_products_search_request(
        payload,
        reject_unknown=True,
        reject_forbidden=True,
        default_search_all_merchants=True,
    )
    if not validation["ok"]:
        return JSONResponse(
            status_code=400,
            content={
                "error": "INVALID_INTERNAL_PRODUCTS_SEARCH_REQUEST",
                "message": "request body must contain only thin search fields",
                "invalid_fields": validation["invalid_fields"],
                "forbidden_fields": validation["forbidden_fields"],
                "unknown_fields": validation["unknown_fields"],
            },
        )

    search = dict(validation["search"] or {})
    merchant_id = _first_non_empty_string(search.get("merchant_id"))
    merchant_ids = _normalize_string_list(search.get("merchant_ids"))
    if merchant_id and not context.can_access_merchant(merchant_id):
        raise HTTPException(status_code=403, detail="Not authorized for this merchant")
    for mid in merchant_ids:
        if not context.can_access_merchant(mid):
            raise HTTPException(status_code=403, detail=f"Not authorized for merchant {mid}")

    merchant_scope = list(dict.fromkeys([*([merchant_id] if merchant_id else []), *merchant_ids])) or None
    search_all_merchants = bool(search.get("search_all_merchants", True))
    if merchant_scope is None and not search_all_merchants:
        allowed = getattr(context, "allowed_merchants", None)
        if isinstance(allowed, list):
            normalized_allowed = [str(mid).strip() for mid in allowed if str(mid).strip()]
            merchant_scope = normalized_allowed or None

    overrides = infer_query_overrides(query=search.get("query"), category=None)
    normalized_query = str(overrides.get("query") or search.get("query") or "").strip()
    normalized_category = str(overrides.get("category") or "").strip() or None
    normalized_catalog_surface = agent_api._normalize_catalog_surface(search.get("catalog_surface"))
    retrieval_profile = agent_api._resolve_retrieval_profile(
        query_text=normalized_query,
        category_text=normalized_category,
        profile_hint=normalized_catalog_surface if normalized_catalog_surface == agent_api.CATALOG_SURFACE_BEAUTY else None,
    )
    query_semantic_class = agent_api._normalize_semantic_class_from_profile(
        str(retrieval_profile.get("id") or "default").strip().lower()
    )
    normalized_seed_strategy = agent_api._normalize_external_seed_strategy(
        search.get("external_seed_strategy"),
        fallback="legacy",
    )
    allow_external_seed = bool(search.get("allow_external_seed", True))
    limit = _normalize_non_negative_int(search.get("limit"), minimum=1, maximum=50) or 20
    offset = _normalize_non_negative_int(search.get("offset"), minimum=0) or 0
    trace_id = _first_non_empty_string(search.get("trace_id"), x_trace_id)
    timeout_seconds = _resolve_timeout_seconds(x_internal_search_timeout_ms, fallback_ms=5000)
    query_terms = agent_api._build_fast_mode_cache_query_terms(
        normalized_query=normalized_query.lower(),
        query_semantic_class=query_semantic_class,
    )

    try:
        result = await asyncio.wait_for(
            agent_api._search_products_fast_mode(
                merchant_scope=merchant_scope,
                query=normalized_query or None,
                category=normalized_category,
                catalog_surface=normalized_catalog_surface,
                min_price=None,
                max_price=None,
                in_stock_only=bool(search.get("in_stock_only", True)),
                limit=limit,
                offset=offset,
                normalized_seed_strategy=normalized_seed_strategy,
                allow_external_seed=allow_external_seed,
                allow_stale_cache=False,
                query_semantic_class=query_semantic_class,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=504,
            content={
                "error": "INTERNAL_PRODUCTS_SEARCH_TIMEOUT",
                "message": f"internal products search timed out after {int(timeout_seconds * 1000)}ms",
                "failure_stage": "local_cache_retrieval",
                "internal_error_code": "ECONNABORTED",
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": "INTERNAL_PRODUCTS_SEARCH_UPSTREAM_ERROR",
                "message": str(exc),
                "failure_stage": "local_cache_retrieval",
                "internal_error_code": _first_non_empty_string(getattr(exc, "code", None)) or None,
            },
        )

    products = list(result.get("products") or [])
    source_breakdown = dict(result.get("source_breakdown") or {})
    retrieval_sources = [
        {
            "source": "fast_mode_cache",
            "used": True,
            **source_breakdown,
        }
    ]
    metadata: Dict[str, Any] = {
        "query_source": "internal_products_search_primitive_cache",
        "transport_owner": "internal_products_search_primitive",
        "endpoint_kind": "internal_primitive",
        "thin_search_primitive": True,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "query_semantic_class": query_semantic_class,
        "retrieval_profile_id": str(retrieval_profile.get("id") or "default"),
        "retrieval_sources": retrieval_sources,
        "query_terms": query_terms,
    }
    if trace_id:
        metadata["trace_id"] = trace_id
    if merchant_id:
        metadata["merchant_id"] = merchant_id
    if merchant_ids:
        metadata["merchant_ids"] = merchant_ids
    if x_internal_caller_lane:
        metadata["caller_lane"] = str(x_internal_caller_lane).strip()
    if search.get("target_step_family"):
        metadata["query_target_step_family"] = search["target_step_family"]
    if search.get("semantic_family"):
        metadata["semantic_family"] = search["semantic_family"]
    if search.get("query_step_strength"):
        metadata["query_step_strength"] = search["query_step_strength"]
    beauty_query_bucket = _derive_beauty_query_bucket(normalized_query.lower(), query_semantic_class)
    if beauty_query_bucket:
        metadata["beauty_query_bucket"] = beauty_query_bucket

    page = (offset // max(1, limit)) + 1
    return {
        "status": "success",
        "success": True,
        "products": products,
        "total": int(result.get("total") or len(products)),
        "page": page,
        "page_size": len(products),
        "reply": None,
        "metadata": metadata,
    }
