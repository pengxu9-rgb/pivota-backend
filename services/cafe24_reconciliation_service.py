from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from adapters.cafe24_adapter import (
    build_cafe24_api_base,
    build_cafe24_headers,
    normalize_cafe24_mall_id,
)
from services.cafe24_event_adapter import (
    UnsupportedCafe24Event,
    extract_cafe24_mall_id,
    map_cafe24_webhook,
)
from services.cafe24_integration_service import (
    find_cafe24_store_by_id,
    merge_cafe24_store_credentials,
    resolve_cafe24_access_token,
)
from services.merchant_event_ingest_service import ingest_merchant_event_batch


LOG_STREAMS = {
    "webhooks": "/admin/webhooks/logs",
    "databridge": "/admin/databridge/logs",
}

_LOG_LIST_KEYS = {
    "webhooks": ("webhooklogs", "webhook_logs", "webhooks", "logs"),
    "databridge": ("databridgelogs", "databridge_logs", "databridge", "logs"),
}


def _parse_request_body(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _extract_logs(payload: Any, stream: str) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in _LOG_LIST_KEYS[stream]:
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    # Be tolerant of future Cafe24 response-envelope naming while accepting
    # only an unambiguous list of object records.
    candidates = [
        value
        for value in payload.values()
        if isinstance(value, list) and all(isinstance(item, dict) for item in value)
    ]
    if len(candidates) == 1:
        return [dict(item) for item in candidates[0]]
    return []


def _log_sort_key(log: Dict[str, Any]) -> Tuple[int, str]:
    raw = str(log.get("log_id") or "").strip()
    try:
        return int(raw), raw
    except ValueError:
        return 0, raw


def _later_cursor(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    if not candidate:
        return current
    if not current:
        return candidate
    try:
        return str(max(int(current), int(candidate)))
    except ValueError:
        return max(current, candidate)


async def _fetch_log_page(
    *,
    client: httpx.AsyncClient,
    stream: str,
    mall_id: str,
    access_token: str,
    api_version: str,
    cursor: Optional[str],
    start_date: str,
    end_date: str,
    limit: int,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": limit}
    if cursor:
        params["since_log_id"] = cursor
    else:
        params.update(
            {
                "requested_start_date": start_date,
                "requested_end_date": end_date,
            }
        )
    response = await client.get(
        f"{build_cafe24_api_base(mall_id)}{LOG_STREAMS[stream]}",
        headers=build_cafe24_headers(access_token, api_version),
        params=params,
    )
    if response.status_code != 200:
        raise ValueError(
            f"Cafe24 {stream} log lookup failed (HTTP {response.status_code})"
        )
    return sorted(_extract_logs(response.json() or {}, stream), key=_log_sort_key)


async def _replay_stream(
    *,
    stream: str,
    logs: List[Dict[str, Any]],
    merchant_id: str,
    store_id: str,
    mall_id: str,
    starting_cursor: Optional[str],
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "stream": stream,
        "seen": len(logs),
        "accepted": 0,
        "duplicates": 0,
        "ignored": 0,
        "invalid": 0,
        "cursor": starting_cursor,
    }
    for log in logs:
        log_id = str(log.get("log_id") or "").strip() or None
        payload = _parse_request_body(log.get("request_body"))
        if payload is None:
            stats["invalid"] += 1
            stats["cursor"] = _later_cursor(stats["cursor"], log_id)
            continue
        payload_mall_id = extract_cafe24_mall_id(payload)
        if payload_mall_id and payload_mall_id != mall_id:
            stats["invalid"] += 1
            stats["cursor"] = _later_cursor(stats["cursor"], log_id)
            continue
        try:
            batch = map_cafe24_webhook(
                payload,
                trace_id=str(log.get("trace_id") or "").strip() or None,
                store_id=store_id,
            )
        except UnsupportedCafe24Event:
            stats["ignored"] += 1
            stats["cursor"] = _later_cursor(stats["cursor"], log_id)
            continue
        except ValueError:
            stats["invalid"] += 1
            stats["cursor"] = _later_cursor(stats["cursor"], log_id)
            continue

        result = await ingest_merchant_event_batch(
            merchant_id=merchant_id,
            batch=batch,
            agent_identity_confidence="platform_asserted",
        )
        stats["accepted"] += int(result.get("accepted") or 0)
        stats["duplicates"] += int(result.get("duplicates") or 0)
        stats["cursor"] = _later_cursor(stats["cursor"], log_id)
    return stats


async def reconcile_cafe24_store(
    *,
    store_id: str,
    lookback_days: int = 7,
    limit_per_stream: int = 500,
) -> Dict[str, Any]:
    store = await find_cafe24_store_by_id(store_id)
    if not store:
        raise ValueError("Cafe24 store was not found")
    credentials = dict(store.get("credentials") or {})
    credentials["store_id"] = store_id
    mall_id = normalize_cafe24_mall_id(
        credentials.get("mall_id") or str(store.get("domain") or "").split(".", 1)[0]
    )
    access_token = await resolve_cafe24_access_token(credentials)
    if not access_token or not mall_id:
        raise ValueError("Cafe24 credentials are incomplete")

    safe_days = max(1, min(int(lookback_days or 7), 90))
    safe_limit = max(1, min(int(limit_per_stream or 500), 10_000))
    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=safe_days)).date().isoformat()
    end_date = now.date().isoformat()
    previous = credentials.get("reconciliation")
    previous = dict(previous) if isinstance(previous, dict) else {}
    api_version = str(credentials.get("api_version") or "2026-03-01")

    stream_results: Dict[str, Dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for stream in LOG_STREAMS:
            cursor_key = f"{stream}_cursor"
            cursor = str(previous.get(cursor_key) or "").strip() or None
            logs = await _fetch_log_page(
                client=client,
                stream=stream,
                mall_id=mall_id,
                access_token=access_token,
                api_version=api_version,
                cursor=cursor,
                start_date=start_date,
                end_date=end_date,
                limit=safe_limit,
            )
            stream_results[stream] = await _replay_stream(
                stream=stream,
                logs=logs,
                merchant_id=str(store["merchant_id"]),
                store_id=store_id,
                mall_id=mall_id,
                starting_cursor=cursor,
            )

    state = {
        **previous,
        "webhooks_cursor": stream_results["webhooks"]["cursor"],
        "databridge_cursor": stream_results["databridge"]["cursor"],
        "last_run_at": now.isoformat(),
        "lookback_days": safe_days,
    }
    await merge_cafe24_store_credentials(
        store_id=store_id,
        updates={"reconciliation": state},
    )
    return {
        "status": "success",
        "platform": "cafe24",
        "store_id": store_id,
        "mall_id": mall_id,
        "streams": stream_results,
        "accepted": sum(item["accepted"] for item in stream_results.values()),
        "duplicates": sum(item["duplicates"] for item in stream_results.values()),
        "ignored": sum(item["ignored"] for item in stream_results.values()),
        "invalid": sum(item["invalid"] for item in stream_results.values()),
    }
