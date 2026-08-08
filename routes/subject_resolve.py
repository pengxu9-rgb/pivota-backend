from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from contextlib import asynccontextmanager
from collections import deque
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from db.database import database
from utils.logger import logger

try:
    from prometheus_client import Counter, Gauge  # type: ignore
except Exception:  # pragma: no cover
    Counter = None  # type: ignore
    Gauge = None  # type: ignore


@asynccontextmanager
async def _subject_resolve_lifespan(_app: Any):
    if _SUBJECT_RESOLVE_UPSTREAM_WARMUP_ENABLED:
        # Keep startup non-blocking for deploy health checks.
        asyncio.create_task(_warm_subject_resolve_upstream_http_client())
    try:
        yield
    finally:
        await _close_subject_resolve_upstream_http_client()


router = APIRouter(tags=["subject-resolve"], lifespan=_subject_resolve_lifespan)

_REASON_MAPPED_HIT = "mapped_hit"
_REASON_NO_CANDIDATES = "no_candidates"
_REASON_DB_TIMEOUT = "db_timeout"
_REASON_UPSTREAM_TIMEOUT = "upstream_timeout"
_REASON_INVALID_ID = "invalid_id"

_VALID_REASON_CODES = {
    _REASON_MAPPED_HIT,
    _REASON_NO_CANDIDATES,
    _REASON_DB_TIMEOUT,
    _REASON_UPSTREAM_TIMEOUT,
    _REASON_INVALID_ID,
}

_metrics_lock = Lock()
_bridge_total_requests = 0
_bridge_hit_requests = 0
_latency_window_ms: deque[int] = deque(maxlen=400)

_bridge_hit_rate_gauge = Gauge("bridge_hit_rate", "Bridge hit ratio for subject resolve") if Gauge else None
_resolve_subject_p95_gauge = (
    Gauge("resolve_subject_p95_ms", "Rolling p95 latency for /v1/subject/resolve")
    if Gauge
    else None
)
_resolve_reason_counter = (
    Counter(
        "resolve_reason_count",
        "Count of resolve subject outcomes by reason code",
        ["reason_code"],
    )
    if Counter
    else None
)


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


_SUBJECT_RESOLVE_UPSTREAM_HTTP_MAX_CONNECTIONS = _env_int(
    "SUBJECT_RESOLVE_UPSTREAM_HTTP_MAX_CONNECTIONS",
    64,
    min_value=4,
    max_value=512,
)
_SUBJECT_RESOLVE_UPSTREAM_HTTP_MAX_KEEPALIVE_CONNECTIONS = _env_int(
    "SUBJECT_RESOLVE_UPSTREAM_HTTP_MAX_KEEPALIVE_CONNECTIONS",
    32,
    min_value=4,
    max_value=512,
)
_SUBJECT_RESOLVE_UPSTREAM_HTTP_KEEPALIVE_EXPIRY_SECONDS = _env_float(
    "SUBJECT_RESOLVE_UPSTREAM_HTTP_KEEPALIVE_EXPIRY_SECONDS",
    300.0,
    min_value=5.0,
    max_value=3600.0,
)
_SUBJECT_RESOLVE_UPSTREAM_HTTP2 = _env_bool(
    "SUBJECT_RESOLVE_UPSTREAM_HTTP2",
    True,
)
_SUBJECT_RESOLVE_UPSTREAM_WARMUP_ENABLED = _env_bool(
    "SUBJECT_RESOLVE_UPSTREAM_WARMUP_ENABLED",
    True,
)
_SUBJECT_RESOLVE_UPSTREAM_WARMUP_TIMEOUT_SECONDS = _env_float(
    "SUBJECT_RESOLVE_UPSTREAM_WARMUP_TIMEOUT_SECONDS",
    1.2,
    min_value=0.2,
    max_value=10.0,
)

_SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT_LOCK = asyncio.Lock()
_SUBJECT_RESOLVE_UPSTREAM_HTTP_LIMITS = httpx.Limits(
    max_connections=_SUBJECT_RESOLVE_UPSTREAM_HTTP_MAX_CONNECTIONS,
    max_keepalive_connections=_SUBJECT_RESOLVE_UPSTREAM_HTTP_MAX_KEEPALIVE_CONNECTIONS,
    keepalive_expiry=_SUBJECT_RESOLVE_UPSTREAM_HTTP_KEEPALIVE_EXPIRY_SECONDS,
)


class CanonicalProductRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    merchant_id: str
    product_id: str


class PdpTargetV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_version: str = Field("pdp_target.v1", alias="schema")
    kind: str
    product_group_id: Optional[str] = None
    product_ref: Optional[CanonicalProductRef] = None


class ResolveSubjectRequest(BaseModel):
    aurora_sku_uuid: Optional[str] = Field(None, description="Aurora SKU UUID")
    aurora_product_uuid: Optional[str] = Field(None, description="Aurora Product UUID")
    alias: Optional[str] = Field(None, description="Human/URL alias for product lookup")
    brand: Optional[str] = Field(None, description="Optional brand hint for alias disambiguation")


class ResolveSubjectResponseV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_version: str = Field("resolve_subject_response.v1", alias="schema")
    resolved: bool
    reason: str
    reason_code: str
    subject: Optional[PdpTargetV1] = None
    bridge_hit: bool
    latency_ms: int
    metadata: Dict[str, Any] = Field(default_factory=dict)


def _request_id(req: Request) -> str:
    rid = (
        getattr(req.state, "request_id", None)
        or req.headers.get("x-request-id")
        or req.headers.get("X-Request-Id")
    )
    rid_clean = str(rid or "").strip()
    return rid_clean if rid_clean else str(uuid.uuid4())


def _db_timeout_seconds() -> float:
    raw = str(os.getenv("SUBJECT_RESOLVE_DB_TIMEOUT_S", "1.8") or "1.8").strip()
    try:
        value = float(raw)
    except Exception:
        value = 1.8
    return max(0.05, min(value, 15.0))


def _upstream_timeout_seconds() -> float:
    raw = str(os.getenv("SUBJECT_RESOLVE_UPSTREAM_TIMEOUT_S", "2.0") or "2.0").strip()
    try:
        value = float(raw)
    except Exception:
        value = 2.0
    return max(0.05, min(value, 15.0))


def _build_upstream_http_timeout(total_seconds: float) -> httpx.Timeout:
    total = max(0.1, float(total_seconds or 0.1))
    connect_timeout = min(5.0, total)
    pool_timeout = min(2.0, total)
    return httpx.Timeout(connect=connect_timeout, read=total, write=total, pool=pool_timeout)


async def _get_subject_resolve_upstream_http_client() -> httpx.AsyncClient:
    global _SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT
    client = _SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT
    if client is not None:
        return client
    async with _SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT_LOCK:
        client = _SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT
        if client is None:
            client = httpx.AsyncClient(
                http2=_SUBJECT_RESOLVE_UPSTREAM_HTTP2,
                limits=_SUBJECT_RESOLVE_UPSTREAM_HTTP_LIMITS,
                timeout=_build_upstream_http_timeout(15.0),
            )
            _SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT = client
    return client


async def _close_subject_resolve_upstream_http_client() -> None:
    global _SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT
    client = _SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT
    if client is None:
        return
    _SUBJECT_RESOLVE_UPSTREAM_HTTP_CLIENT = None
    try:
        await client.aclose()
    except Exception:
        logger.debug("subject_resolve upstream client close failed", exc_info=True)


async def _warm_subject_resolve_upstream_http_client() -> None:
    if not _SUBJECT_RESOLVE_UPSTREAM_WARMUP_ENABLED:
        return
    url = str(os.getenv("SUBJECT_RESOLVE_UPSTREAM_URL", "") or "").strip()
    if not url:
        return
    try:
        client = await _get_subject_resolve_upstream_http_client()
        await client.get(
            url,
            timeout=_build_upstream_http_timeout(_SUBJECT_RESOLVE_UPSTREAM_WARMUP_TIMEOUT_SECONDS),
            headers={"Cache-Control": "no-cache"},
        )
    except Exception:
        logger.debug("subject_resolve upstream warmup probe failed", exc_info=True)


def _bridge_enabled() -> bool:
    return str(os.getenv("SUBJECT_RESOLVE_BRIDGE_ENABLED", "true") or "true").strip().lower() == "true"


def _normalize_uuid(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except Exception:
        return None


def _expand_aliases(raw_alias: Optional[str]) -> List[str]:
    raw = str(raw_alias or "").strip()
    if not raw:
        return []

    aliases: List[str] = []

    def _add(value: Optional[str]) -> None:
        normalized = str(value or "").strip()
        if normalized and normalized not in aliases:
            aliases.append(normalized)

    _add(raw)
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlparse(raw)
            segments = [p for p in (parsed.path or "").split("/") if p]
            for idx, seg in enumerate(segments):
                if seg == "products" and idx + 1 < len(segments):
                    _add(segments[idx + 1])
            if segments:
                _add(segments[-1])
        except Exception:
            pass

    if ":" in raw:
        _add(raw.split(":")[-1])
    if "/" in raw:
        _add(raw.rstrip("/").split("/")[-1])
    return aliases[:24]


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        return {}


def _build_subject(
    *,
    subject_kind: Optional[str],
    product_group_id: Optional[str],
    merchant_id: Optional[str],
    product_id: Optional[str],
) -> Optional[PdpTargetV1]:
    kind = str(subject_kind or "").strip().lower()
    if kind == "product_group":
        gid = str(product_group_id or "").strip()
        if not gid:
            return None
        return PdpTargetV1(kind="product_group", product_group_id=gid)
    if kind == "canonical_product":
        mid = str(merchant_id or "").strip()
        pid = str(product_id or "").strip()
        if not mid or not pid:
            return None
        return PdpTargetV1(
            kind="canonical_product",
            product_ref=CanonicalProductRef(merchant_id=mid, product_id=pid),
        )
    return None


def _source_row(
    *,
    source: str,
    started_at: float,
    ok: bool,
    reason_code: str,
    detail: Optional[str] = None,
    row_count: Optional[int] = None,
    error: Optional[str] = None,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    safe_reason = reason_code if reason_code in _VALID_REASON_CODES else _REASON_NO_CANDIDATES
    data: Dict[str, Any] = {
        "source": source,
        "ok": bool(ok),
        "reason_code": safe_reason,
        "reason": safe_reason,
        "latency_ms": int((time.perf_counter() - started_at) * 1000),
    }
    if detail:
        data["detail"] = detail
    if row_count is not None:
        data["row_count"] = int(row_count)
    if error:
        data["error"] = str(error)[:300]
    if query:
        data["query"] = query
    return data


def _compute_p95_ms(values: List[int]) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = max(0, math.ceil(len(sorted_values) * 0.95) - 1)
    return float(sorted_values[idx])


def _observe_metrics(*, reason_code: str, bridge_hit: bool, latency_ms: int) -> None:
    global _bridge_total_requests, _bridge_hit_requests

    if _resolve_reason_counter is not None:
        _resolve_reason_counter.labels(reason_code=reason_code).inc()

    with _metrics_lock:
        _bridge_total_requests += 1
        if bridge_hit:
            _bridge_hit_requests += 1
        _latency_window_ms.append(int(latency_ms))
        bridge_hit_rate = (
            float(_bridge_hit_requests) / float(_bridge_total_requests)
            if _bridge_total_requests
            else 0.0
        )
        p95_ms = _compute_p95_ms(list(_latency_window_ms))

    if _bridge_hit_rate_gauge is not None:
        _bridge_hit_rate_gauge.set(bridge_hit_rate)
    if _resolve_subject_p95_gauge is not None:
        _resolve_subject_p95_gauge.set(p95_ms)


async def _resolve_from_bridge_key(
    *,
    key_type: str,
    key_value: str,
    db_timeout_s: float,
) -> Tuple[Optional[PdpTargetV1], Dict[str, Any]]:
    started = time.perf_counter()
    try:
        row = await asyncio.wait_for(
            database.fetch_one(
                """
                SELECT subject_kind, product_group_id, merchant_id, product_id
                FROM id_bridge
                WHERE bridge_key_type = :key_type
                  AND bridge_key = :key_value
                LIMIT 1
                """,
                {"key_type": key_type, "key_value": key_value},
            ),
            timeout=db_timeout_s,
        )
        loaded = _row_to_dict(row)
        subject = _build_subject(
            subject_kind=loaded.get("subject_kind"),
            product_group_id=loaded.get("product_group_id"),
            merchant_id=loaded.get("merchant_id"),
            product_id=loaded.get("product_id"),
        )
        if subject is None:
            return None, _source_row(
                source="id_bridge",
                started_at=started,
                ok=False,
                reason_code=_REASON_NO_CANDIDATES,
                row_count=0 if not loaded else 1,
                detail=f"lookup={key_type}",
                query="id_bridge_by_key",
            )
        return subject, _source_row(
            source="id_bridge",
            started_at=started,
            ok=True,
            reason_code=_REASON_MAPPED_HIT,
            row_count=1,
            detail=f"lookup={key_type}",
            query="id_bridge_by_key",
        )
    except asyncio.TimeoutError:
        return None, _source_row(
            source="id_bridge",
            started_at=started,
            ok=False,
            reason_code=_REASON_DB_TIMEOUT,
            detail=f"lookup={key_type}",
            error="TimeoutError",
            query="id_bridge_by_key",
        )
    except Exception as exc:
        return None, _source_row(
            source="id_bridge",
            started_at=started,
            ok=False,
            reason_code=_REASON_DB_TIMEOUT,
            detail=f"lookup={key_type};db_error",
            error=type(exc).__name__,
            query="id_bridge_by_key",
        )


async def _resolve_from_alias(
    *,
    alias: str,
    brand: Optional[str],
    db_timeout_s: float,
) -> Tuple[Optional[PdpTargetV1], List[Dict[str, Any]]]:
    traces: List[Dict[str, Any]] = []
    aliases = _expand_aliases(alias)
    if not aliases:
        traces.append(
            _source_row(
                source="products_cache_alias",
                started_at=time.perf_counter(),
                ok=False,
                reason_code=_REASON_NO_CANDIDATES,
                detail="alias_empty",
                row_count=0,
                query="products_cache_by_alias",
            )
        )
        return None, traces

    brand_norm = str(brand or "").strip().lower() or None
    cache_started = time.perf_counter()
    try:
        rows = await asyncio.wait_for(
            database.fetch_all(
                """
                SELECT merchant_id, platform_product_id, product_data
                FROM products_cache
                WHERE (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                  AND (
                    platform_product_id = ANY(CAST(:aliases AS text[]))
                    OR COALESCE(product_data->>'id', '') = ANY(CAST(:aliases AS text[]))
                    OR COALESCE(product_data->>'product_id', '') = ANY(CAST(:aliases AS text[]))
                    OR LOWER(CAST(product_data AS TEXT)) LIKE :alias_like
                  )
                  AND (CAST(:brand_like AS text) IS NULL
                       OR LOWER(CAST(product_data AS TEXT)) LIKE CAST(:brand_like AS text))
                ORDER BY cached_at DESC
                LIMIT 40
                """,
                {
                    "aliases": aliases,
                    "alias_like": f"%{aliases[0].lower()}%",
                    "brand_like": f"%{brand_norm}%" if brand_norm else None,
                },
            ),
            timeout=db_timeout_s,
        )
    except asyncio.TimeoutError:
        traces.append(
            _source_row(
                source="products_cache_alias",
                started_at=cache_started,
                ok=False,
                reason_code=_REASON_DB_TIMEOUT,
                error="TimeoutError",
                query="products_cache_by_alias",
            )
        )
        return None, traces
    except Exception as exc:
        traces.append(
            _source_row(
                source="products_cache_alias",
                started_at=cache_started,
                ok=False,
                reason_code=_REASON_DB_TIMEOUT,
                detail="db_error",
                error=type(exc).__name__,
                query="products_cache_by_alias",
            )
        )
        return None, traces

    loaded_rows = [_row_to_dict(r) for r in (rows or [])]
    traces.append(
        _source_row(
            source="products_cache_alias",
            started_at=cache_started,
            ok=bool(loaded_rows),
            reason_code=_REASON_MAPPED_HIT if loaded_rows else _REASON_NO_CANDIDATES,
            row_count=len(loaded_rows),
            query="products_cache_by_alias",
        )
    )
    if not loaded_rows:
        return None, traces

    top = loaded_rows[0]
    product_data = top.get("product_data")
    if isinstance(product_data, str):
        try:
            product_data = json.loads(product_data)
        except Exception:
            product_data = {}
    if not isinstance(product_data, dict):
        product_data = {}

    merchant_id = str(top.get("merchant_id") or "").strip()
    platform_product_id = str(
        top.get("platform_product_id")
        or product_data.get("id")
        or product_data.get("product_id")
        or ""
    ).strip()

    if not platform_product_id:
        return None, traces

    group_started = time.perf_counter()
    try:
        group_row = await asyncio.wait_for(
            database.fetch_one(
                """
                SELECT product_group_id
                FROM product_group_members
                WHERE platform_product_id = :platform_product_id
                ORDER BY is_primary DESC, merchant_id ASC
                LIMIT 1
                """,
                {"platform_product_id": platform_product_id},
            ),
            timeout=db_timeout_s,
        )
        group = _row_to_dict(group_row)
        product_group_id = str(group.get("product_group_id") or "").strip()
        if product_group_id:
            traces.append(
                _source_row(
                    source="product_group_members_lookup",
                    started_at=group_started,
                    ok=True,
                    reason_code=_REASON_MAPPED_HIT,
                    row_count=1,
                    query="product_group_members_by_platform_product_id",
                )
            )
            return (
                PdpTargetV1(kind="product_group", product_group_id=product_group_id),
                traces,
            )
        traces.append(
            _source_row(
                source="product_group_members_lookup",
                started_at=group_started,
                ok=False,
                reason_code=_REASON_NO_CANDIDATES,
                row_count=0,
                query="product_group_members_by_platform_product_id",
            )
        )
    except asyncio.TimeoutError:
        traces.append(
            _source_row(
                source="product_group_members_lookup",
                started_at=group_started,
                ok=False,
                reason_code=_REASON_DB_TIMEOUT,
                error="TimeoutError",
                query="product_group_members_by_platform_product_id",
            )
        )
        return None, traces
    except Exception as exc:
        traces.append(
            _source_row(
                source="product_group_members_lookup",
                started_at=group_started,
                ok=False,
                reason_code=_REASON_DB_TIMEOUT,
                detail="db_error",
                error=type(exc).__name__,
                query="product_group_members_by_platform_product_id",
            )
        )
        return None, traces

    if merchant_id and platform_product_id:
        return (
            PdpTargetV1(
                kind="canonical_product",
                product_ref=CanonicalProductRef(
                    merchant_id=merchant_id,
                    product_id=platform_product_id,
                ),
            ),
            traces,
        )
    return None, traces


def _subject_from_upstream_payload(raw_subject: Any) -> Optional[PdpTargetV1]:
    if not isinstance(raw_subject, dict):
        return None
    kind = str(raw_subject.get("kind") or "").strip().lower()
    if kind == "product_group":
        gid = str(raw_subject.get("product_group_id") or "").strip()
        if gid:
            return PdpTargetV1(kind="product_group", product_group_id=gid)
        return None
    if kind == "canonical_product":
        pref = raw_subject.get("product_ref")
        if isinstance(pref, dict):
            merchant_id = str(pref.get("merchant_id") or "").strip()
            product_id = str(pref.get("product_id") or "").strip()
            if merchant_id and product_id:
                return PdpTargetV1(
                    kind="canonical_product",
                    product_ref=CanonicalProductRef(merchant_id=merchant_id, product_id=product_id),
                )
    return None


async def _resolve_via_upstream(
    *,
    payload: ResolveSubjectRequest,
) -> Tuple[Optional[PdpTargetV1], Optional[Dict[str, Any]]]:
    url = str(os.getenv("SUBJECT_RESOLVE_UPSTREAM_URL", "") or "").strip()
    if not url:
        return None, None

    timeout_s = _upstream_timeout_seconds()
    started = time.perf_counter()
    req_payload = (
        payload.model_dump(exclude_none=True)
        if hasattr(payload, "model_dump")
        else payload.dict(exclude_none=True)
    )
    try:
        client = await _get_subject_resolve_upstream_http_client()
        resp = await asyncio.wait_for(
            client.post(
                url,
                json=req_payload,
                timeout=_build_upstream_http_timeout(timeout_s),
            ),
            timeout=timeout_s,
        )
        if resp.status_code >= 400:
            return None, _source_row(
                source="subject_resolve_upstream",
                started_at=started,
                ok=False,
                reason_code=_REASON_NO_CANDIDATES,
                detail=f"status={resp.status_code}",
            )
        body = resp.json() if resp.content else {}
        subject = _subject_from_upstream_payload(body.get("subject") if isinstance(body, dict) else None)
        if subject is None:
            return None, _source_row(
                source="subject_resolve_upstream",
                started_at=started,
                ok=False,
                reason_code=_REASON_NO_CANDIDATES,
                detail="invalid_subject_shape",
            )
        return subject, _source_row(
            source="subject_resolve_upstream",
            started_at=started,
            ok=True,
            reason_code=_REASON_MAPPED_HIT,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return None, _source_row(
            source="subject_resolve_upstream",
            started_at=started,
            ok=False,
            reason_code=_REASON_UPSTREAM_TIMEOUT,
            error="TimeoutError",
        )
    except Exception as exc:
        reason = _REASON_UPSTREAM_TIMEOUT if "timeout" in str(exc).lower() else _REASON_NO_CANDIDATES
        return None, _source_row(
            source="subject_resolve_upstream",
            started_at=started,
            ok=False,
            reason_code=reason,
            error=type(exc).__name__,
        )


@router.post("/v1/subject/resolve", response_model=ResolveSubjectResponseV1)
async def resolve_subject(
    req: Request,
    payload: ResolveSubjectRequest,
) -> ResolveSubjectResponseV1:
    started = time.perf_counter()
    rid = _request_id(req)
    db_timeout_s = _db_timeout_seconds()

    sources: List[Dict[str, Any]] = []
    subject: Optional[PdpTargetV1] = None
    bridge_hit = False
    reason_code = _REASON_NO_CANDIDATES
    reason = _REASON_NO_CANDIDATES

    normalized_sku_uuid = _normalize_uuid(payload.aurora_sku_uuid)
    normalized_product_uuid = _normalize_uuid(payload.aurora_product_uuid)
    invalid_keys: List[str] = []
    if payload.aurora_sku_uuid and not normalized_sku_uuid:
        invalid_keys.append("aurora_sku_uuid")
    if payload.aurora_product_uuid and not normalized_product_uuid:
        invalid_keys.append("aurora_product_uuid")

    alias_text = str(payload.alias or "").strip()

    if invalid_keys:
        reason_code = _REASON_INVALID_ID
        reason = _REASON_INVALID_ID
    elif not normalized_sku_uuid and not normalized_product_uuid and not alias_text:
        reason_code = _REASON_INVALID_ID
        reason = "missing_identifiers"
    else:
        if _bridge_enabled():
            if normalized_sku_uuid:
                subject, src = await _resolve_from_bridge_key(
                    key_type="aurora_sku_uuid",
                    key_value=normalized_sku_uuid,
                    db_timeout_s=db_timeout_s,
                )
                sources.append(src)
                bridge_hit = subject is not None
            if subject is None and normalized_product_uuid:
                subject, src = await _resolve_from_bridge_key(
                    key_type="aurora_product_uuid",
                    key_value=normalized_product_uuid,
                    db_timeout_s=db_timeout_s,
                )
                sources.append(src)
                bridge_hit = subject is not None
        elif normalized_sku_uuid or normalized_product_uuid:
            sources.append(
                _source_row(
                    source="id_bridge",
                    started_at=time.perf_counter(),
                    ok=False,
                    reason_code=_REASON_NO_CANDIDATES,
                    detail="bridge_disabled",
                )
            )

        if subject is None and alias_text:
            alias_subject, alias_sources = await _resolve_from_alias(
                alias=alias_text,
                brand=payload.brand,
                db_timeout_s=db_timeout_s,
            )
            sources.extend(alias_sources)
            subject = alias_subject

        if subject is None:
            upstream_subject, upstream_trace = await _resolve_via_upstream(payload=payload)
            if upstream_trace is not None:
                sources.append(upstream_trace)
            subject = upstream_subject

        if subject is not None:
            reason_code = _REASON_MAPPED_HIT
            reason = _REASON_MAPPED_HIT
        elif any(str(s.get("reason_code")) == _REASON_DB_TIMEOUT for s in sources):
            reason_code = _REASON_DB_TIMEOUT
            reason = _REASON_DB_TIMEOUT
        elif any(str(s.get("reason_code")) == _REASON_UPSTREAM_TIMEOUT for s in sources):
            reason_code = _REASON_UPSTREAM_TIMEOUT
            reason = _REASON_UPSTREAM_TIMEOUT
        else:
            reason_code = _REASON_NO_CANDIDATES
            reason = _REASON_NO_CANDIDATES

    latency_ms = int((time.perf_counter() - started) * 1000)
    resolved = subject is not None
    _observe_metrics(reason_code=reason_code, bridge_hit=bridge_hit, latency_ms=latency_ms)

    logger.info(
        "subject_resolve request_id=%s reason_code=%s bridge_hit=%s latency_ms=%s alias=%s",
        rid,
        reason_code,
        bridge_hit,
        latency_ms,
        alias_text[:120] if alias_text else "",
    )

    return ResolveSubjectResponseV1(
        resolved=resolved,
        reason=reason,
        reason_code=reason_code,
        subject=subject,
        bridge_hit=bridge_hit,
        latency_ms=latency_ms,
        metadata={
            "request_id": rid,
            "bridge_hit": bridge_hit,
            "bridge_enabled": _bridge_enabled(),
            "db_timeout_s": db_timeout_s,
            "sources": sources,
            **({"invalid_fields": invalid_keys} if invalid_keys else {}),
        },
    )
