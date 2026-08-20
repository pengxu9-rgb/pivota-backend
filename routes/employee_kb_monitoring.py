from __future__ import annotations

import asyncio
import copy
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from statistics import median
from typing import Any, Deque, Dict, Iterable, Literal, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from utils.auth import get_current_employee


router = APIRouter(
    prefix="/employee/monitoring/aurora-kb-v0",
    tags=["employee-kb-monitoring"],
)

SCHEMA_VERSION = "2026-02-22.v1"
POLL_INTERVAL_SEC = 30
DEFAULT_BASE_URL = "https://gateway.pivota.cc"
DEFAULT_CACHE_TTL_SEC = 15
DEFAULT_TIMEOUT_MS = 2500
DEFAULT_HISTORY_SIZE = 120
DEFAULT_CLIMATE_SPIKE_THRESHOLD = 10

WINDOW_SECONDS: Dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600}
METRIC_KEYS: Dict[str, str] = {
    "loader_error_total": "aurora_kb_v0_loader_error_total",
    "rule_match_total": "aurora_kb_v0_rule_match_total",
    "legacy_fallback_total": "aurora_kb_v0_legacy_fallback_total",
    "climate_fallback_total": "aurora_kb_v0_climate_fallback_total",
}

PROM_LINE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)$"
)

STATE_LOCK = asyncio.Lock()
CACHE_PAYLOAD: Optional[Dict[str, Any]] = None
CACHE_TS: float = 0.0
LAST_SUCCESS_PAYLOAD: Optional[Dict[str, Any]] = None
LAST_SUCCESS_TS: float = 0.0
HISTORY: Deque[Dict[str, Any]] = deque(maxlen=DEFAULT_HISTORY_SIZE)


def _now_iso(ts: Optional[float] = None) -> str:
    actual = ts if ts is not None else time.time()
    return (
        datetime.fromtimestamp(actual, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def _extract_from_candidates(
    candidates: Iterable[Dict[str, Any]],
    keys: Iterable[str],
) -> Any:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in keys:
            if key in candidate:
                return candidate.get(key)
    return None


def _safe_metric_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_prometheus_metrics(text: str) -> Tuple[Dict[str, int], list[str]]:
    values = {key: 0 for key in METRIC_KEYS}
    seen = {key: False for key in METRIC_KEYS}
    metric_name_to_key = {name: key for key, name in METRIC_KEYS.items()}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROM_LINE_RE.match(line)
        if not match:
            continue
        metric_name, value_token = match.group(1), match.group(2)
        response_key = metric_name_to_key.get(metric_name)
        if not response_key:
            continue
        values[response_key] += _safe_metric_int(value_token)
        seen[response_key] = True

    errors: list[str] = []
    for response_key, was_seen in seen.items():
        if not was_seen:
            errors.append(f"metric_missing:{METRIC_KEYS[response_key]}")
    return values, errors


def _extract_runtime_from_health(health_json: Dict[str, Any]) -> Dict[str, Any]:
    runtime = {
        "kb_v0_enabled": None,
        "kb_fail_mode": "unknown",
        "kill_switch_enabled": None,
        "source": "unknown",
    }

    candidates: list[Dict[str, Any]] = []
    if isinstance(health_json, dict):
        candidates.append(health_json)
        nested_runtime = health_json.get("runtime")
        if isinstance(nested_runtime, dict):
            candidates.append(nested_runtime)
        raw = health_json.get("raw")
        if isinstance(raw, dict):
            candidates.append(raw)
            raw_runtime = raw.get("runtime")
            if isinstance(raw_runtime, dict):
                candidates.append(raw_runtime)

    enabled_val = _extract_from_candidates(
        candidates,
        ["kb_v0_enabled", "aurora_kb_v0_enabled"],
    )
    disable_val = _extract_from_candidates(
        candidates,
        ["kb_v0_disable", "aurora_kb_v0_disable", "AURORA_KB_V0_DISABLE"],
    )
    kill_switch_val = _extract_from_candidates(
        candidates,
        ["kill_switch_enabled"],
    )
    fail_mode_val = _extract_from_candidates(
        candidates,
        ["kb_fail_mode", "aurora_kb_fail_mode", "AURORA_KB_FAIL_MODE"],
    )

    enabled_bool = _parse_bool(enabled_val)
    disable_bool = _parse_bool(disable_val)
    kill_switch_bool = _parse_bool(kill_switch_val)

    if enabled_bool is not None:
        runtime["kb_v0_enabled"] = enabled_bool
        runtime["source"] = "health"
    if disable_bool is not None:
        runtime["kill_switch_enabled"] = disable_bool
        runtime["kb_v0_enabled"] = not disable_bool
        runtime["source"] = "health"
    if kill_switch_bool is not None:
        runtime["kill_switch_enabled"] = kill_switch_bool
        if runtime["kb_v0_enabled"] is None:
            runtime["kb_v0_enabled"] = not kill_switch_bool
        runtime["source"] = "health"

    if isinstance(fail_mode_val, str):
        normalized = fail_mode_val.strip().lower()
        if normalized in {"open", "closed"}:
            runtime["kb_fail_mode"] = normalized
            runtime["source"] = "health"

    if runtime["source"] != "health":
        expected_enabled = _env_bool("AURORA_EXPECT_KB_V0_ENABLED", True)
        expected_fail_mode = (os.getenv("AURORA_EXPECT_FAIL_MODE") or "closed").strip().lower()
        runtime["kb_v0_enabled"] = expected_enabled
        runtime["kill_switch_enabled"] = not expected_enabled
        runtime["kb_fail_mode"] = expected_fail_mode if expected_fail_mode in {"open", "closed"} else "unknown"
        runtime["source"] = "expected"

    return runtime


def _pick_first_non_empty(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_service_identity(
    base_url: str,
    health_status: str,
    health_http_status: Optional[int],
    health_json: Dict[str, Any],
    version_json: Dict[str, Any],
    metrics_response: Optional[httpx.Response],
    health_response: Optional[httpx.Response],
    version_response: Optional[httpx.Response],
) -> Dict[str, Any]:
    commit_header = _pick_first_non_empty(
        metrics_response.headers.get("x-service-commit") if metrics_response else None,
        health_response.headers.get("x-service-commit") if health_response else None,
        version_response.headers.get("x-service-commit") if version_response else None,
    )
    deployment_header = _pick_first_non_empty(
        metrics_response.headers.get("x-service-deployment-id") if metrics_response else None,
        health_response.headers.get("x-service-deployment-id") if health_response else None,
        version_response.headers.get("x-service-deployment-id") if version_response else None,
    )

    health_git = health_json.get("git") if isinstance(health_json.get("git"), dict) else {}
    version_git = version_json.get("git") if isinstance(version_json.get("git"), dict) else {}
    health_version = health_json.get("version") if isinstance(health_json.get("version"), dict) else {}
    backend_version = version_json.get("version") if isinstance(version_json.get("version"), dict) else {}
    version_railway = (
        version_json.get("railway")
        if isinstance(version_json.get("railway"), dict)
        else {}
    )

    commit_sha = _pick_first_non_empty(
        commit_header,
        backend_version.get("full_sha"),
        health_version.get("full_sha"),
        version_json.get("full_sha"),
        version_json.get("commit_sha"),
        health_json.get("commit_sha"),
        health_git.get("commit_sha"),
        version_git.get("commit_sha"),
        backend_version.get("commit"),
        health_version.get("commit"),
        version_json.get("version"),
    )
    deployment_id = _pick_first_non_empty(
        deployment_header,
        backend_version.get("deployment_id"),
        health_version.get("deployment_id"),
        version_json.get("deployment_id"),
        health_json.get("deployment_id"),
        version_railway.get("deployment_id"),
    )
    version_value = _pick_first_non_empty(
        backend_version.get("build_id"),
        health_version.get("build_id"),
        version_json.get("version"),
        health_json.get("version"),
        commit_sha[:8] if commit_sha else None,
    )

    return {
        "name": "aurora-bff",
        "base_url": base_url,
        "health_status": health_status,
        "health_http_status": health_http_status,
        "commit_sha": commit_sha,
        "deployment_id": deployment_id,
        "version": version_value,
    }


def _resize_history_if_needed() -> None:
    global HISTORY
    target_size = max(30, _env_int("AURORA_MONITOR_HISTORY_SIZE", DEFAULT_HISTORY_SIZE))
    if HISTORY.maxlen == target_size:
        return
    HISTORY = deque(list(HISTORY), maxlen=target_size)


def _append_history(ts: float, metrics: Dict[str, int]) -> None:
    HISTORY.append({"ts": ts, **metrics})


def _compute_window_increase(
    history_points: list[Dict[str, Any]],
    metric_key: str,
    current_total: int,
    now_ts: float,
    window_seconds: int,
) -> int:
    cutoff = now_ts - window_seconds
    baseline = current_total
    for point in reversed(history_points):
        if float(point.get("ts", 0)) <= cutoff:
            baseline = _safe_metric_int(point.get(metric_key))
            break
    return max(current_total - baseline, 0)


def _compute_rule_spike(
    history_points: list[Dict[str, Any]],
    now_ts: float,
    window_seconds: int,
    current_window_increase: int,
) -> Tuple[bool, float]:
    recent_points = [point for point in history_points if float(point.get("ts", 0)) >= now_ts - 1800]
    if len(recent_points) < 3:
        return False, 0.0

    increments: list[int] = []
    prev_total: Optional[int] = None
    for point in recent_points:
        total = _safe_metric_int(point.get("rule_match_total"))
        if prev_total is not None:
            increments.append(max(total - prev_total, 0))
        prev_total = total

    if not increments:
        return False, 0.0

    per_scrape_median = float(median(increments))
    scaled_baseline = per_scrape_median * max(window_seconds / max(POLL_INTERVAL_SEC, 1), 1.0)
    spike = (
        scaled_baseline > 0
        and current_window_increase >= 20
        and current_window_increase > (3.0 * scaled_baseline)
    )
    return spike, scaled_baseline


def _build_disabled_payload(window: str, errors: list[str]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "disabled",
        "timestamp": _now_iso(),
        "window": window,
        "data": {
            "service": {
                "name": "aurora-bff",
                "base_url": "",
                "health_status": "disabled",
                "health_http_status": None,
                "commit_sha": None,
                "deployment_id": None,
                "version": None,
            },
            "runtime": {
                "kb_v0_enabled": None,
                "kb_fail_mode": "unknown",
                "kill_switch_enabled": None,
                "source": "unknown",
            },
            "metrics": {
                "loader_error_total": 0,
                "rule_match_total": 0,
                "legacy_fallback_total": 0,
                "climate_fallback_total": 0,
            },
            "derived": {
                "rule_match_plus_fallback_total": 0,
                "legacy_fallback_ratio": 0.0,
                "loader_error_increase": 0,
                "rule_match_increase": 0,
                "legacy_fallback_increase": 0,
                "climate_fallback_increase": 0,
            },
            "guardrails": {
                "loader_error_alert": False,
                "legacy_fallback_ratio_alert": False,
                "rule_match_spike_alert": False,
                "climate_spike_alert": False,
            },
        },
        "meta": {
            "poll_interval_sec": POLL_INTERVAL_SEC,
            "cache_ttl_sec": _env_int("AURORA_MONITOR_CACHE_TTL_SEC", DEFAULT_CACHE_TTL_SEC),
            "sample_age_sec": 0,
            "source": "aurora_metrics+health+version",
            "stale": False,
            "cache_hit": False,
        },
        "errors": errors,
    }


async def _collect_live_snapshot(
    base_url: str,
    timeout_ms: int,
    metrics_bearer_token: str,
) -> Dict[str, Any]:
    timeout_s = max(float(timeout_ms) / 1000.0, 0.5)
    headers = {}
    if metrics_bearer_token:
        headers["Authorization"] = f"Bearer {metrics_bearer_token}"

    metrics_response: Optional[httpx.Response] = None
    health_response: Optional[httpx.Response] = None
    version_response: Optional[httpx.Response] = None
    errors: list[str] = []

    metrics_url = f"{base_url}/metrics"
    health_url = f"{base_url}/health"
    version_url = f"{base_url}/version"

    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        metrics_result, health_result, version_result = await asyncio.gather(
            client.get(metrics_url, headers=headers),
            client.get(health_url),
            client.get(version_url),
            return_exceptions=True,
        )

    if isinstance(metrics_result, Exception):
        raise RuntimeError(f"metrics_fetch_failed:{type(metrics_result).__name__}")
    metrics_response = metrics_result
    if metrics_response.status_code != 200:
        raise RuntimeError(f"metrics_http_{metrics_response.status_code}")

    metrics_values, parse_errors = _parse_prometheus_metrics(metrics_response.text)
    errors.extend(parse_errors)

    health_status = "unknown"
    health_http_status: Optional[int] = None
    health_json: Dict[str, Any] = {}
    if isinstance(health_result, Exception):
        errors.append(f"health_fetch_failed:{type(health_result).__name__}")
        health_status = "unreachable"
    else:
        health_response = health_result
        health_http_status = health_response.status_code
        health_status = "healthy" if health_response.status_code == 200 else "unhealthy"
        try:
            health_json = health_response.json() if health_response.text else {}
            if not isinstance(health_json, dict):
                health_json = {}
        except ValueError:
            errors.append("health_json_parse_failed")
            health_json = {}

    version_json: Dict[str, Any] = {}
    if isinstance(version_result, Exception):
        errors.append(f"version_fetch_failed:{type(version_result).__name__}")
    else:
        version_response = version_result
        if version_response.status_code != 200:
            errors.append(f"version_http_{version_response.status_code}")
        else:
            try:
                version_json = version_response.json() if version_response.text else {}
                if not isinstance(version_json, dict):
                    version_json = {}
            except ValueError:
                errors.append("version_json_parse_failed")
                version_json = {}

    runtime = _extract_runtime_from_health(health_json)
    service = _extract_service_identity(
        base_url=base_url,
        health_status=health_status,
        health_http_status=health_http_status,
        health_json=health_json,
        version_json=version_json,
        metrics_response=metrics_response,
        health_response=health_response,
        version_response=version_response,
    )

    return {
        "service": service,
        "runtime": runtime,
        "metrics": metrics_values,
        "_errors": errors,
        "_source": "aurora_metrics+health+version",
        "_debug": {
            "metrics_url": metrics_url,
            "health_url": health_url,
            "version_url": version_url,
            "metrics_http_status": metrics_response.status_code if metrics_response else None,
            "health_http_status": health_http_status,
            "version_http_status": version_response.status_code if version_response else None,
        },
    }


def _reset_state_for_tests() -> None:
    global CACHE_PAYLOAD, CACHE_TS, LAST_SUCCESS_PAYLOAD, LAST_SUCCESS_TS, HISTORY
    CACHE_PAYLOAD = None
    CACHE_TS = 0.0
    LAST_SUCCESS_PAYLOAD = None
    LAST_SUCCESS_TS = 0.0
    HISTORY = deque(maxlen=DEFAULT_HISTORY_SIZE)


@router.get("/summary")
async def get_aurora_kb_v0_summary(
    window: Literal["5m", "15m", "1h"] = Query("5m"),
    include_debug: bool = Query(False),
    _: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    base_url = _normalize_base_url((os.getenv("AURORA_BFF_BASE_URL") or DEFAULT_BASE_URL).strip())
    if not base_url:
        return _build_disabled_payload(window, ["AURORA_BFF_BASE_URL not configured"])

    timeout_ms = _env_int("AURORA_MONITOR_TIMEOUT_MS", DEFAULT_TIMEOUT_MS)
    cache_ttl_sec = max(0, _env_int("AURORA_MONITOR_CACHE_TTL_SEC", DEFAULT_CACHE_TTL_SEC))
    climate_spike_threshold = max(1, _env_int("AURORA_KB_CLIMATE_SPIKE_THRESHOLD", DEFAULT_CLIMATE_SPIKE_THRESHOLD))
    metrics_bearer_token = (os.getenv("AURORA_METRICS_BEARER_TOKEN") or "").strip()

    stale = False
    cache_hit = False
    sample_ts = time.time()
    snapshot: Dict[str, Any]
    fetch_errors: list[str] = []
    history_points: list[Dict[str, Any]]

    try:
        async with STATE_LOCK:
            _resize_history_if_needed()
            now_ts = time.time()
            if CACHE_PAYLOAD and (now_ts - CACHE_TS) <= cache_ttl_sec:
                snapshot = copy.deepcopy(CACHE_PAYLOAD)
                sample_ts = CACHE_TS
                cache_hit = True
            else:
                try:
                    snapshot = await _collect_live_snapshot(base_url, timeout_ms, metrics_bearer_token)
                    sample_ts = now_ts
                    _append_history(now_ts, snapshot.get("metrics", {}))
                    globals()["CACHE_PAYLOAD"] = copy.deepcopy(snapshot)
                    globals()["CACHE_TS"] = now_ts
                    globals()["LAST_SUCCESS_PAYLOAD"] = copy.deepcopy(snapshot)
                    globals()["LAST_SUCCESS_TS"] = now_ts
                except Exception as exc:
                    fetch_errors.append(str(exc))
                    if LAST_SUCCESS_PAYLOAD is not None:
                        snapshot = copy.deepcopy(LAST_SUCCESS_PAYLOAD)
                        sample_ts = LAST_SUCCESS_TS
                        stale = True
                    else:
                        snapshot = {
                            "service": {
                                "name": "aurora-bff",
                                "base_url": base_url,
                                "health_status": "unreachable",
                                "health_http_status": None,
                                "commit_sha": None,
                                "deployment_id": None,
                                "version": None,
                            },
                            "runtime": {
                                "kb_v0_enabled": None,
                                "kb_fail_mode": "unknown",
                                "kill_switch_enabled": None,
                                "source": "unknown",
                            },
                            "metrics": {key: 0 for key in METRIC_KEYS},
                            "_errors": [],
                            "_source": "aurora_metrics+health+version",
                            "_debug": {},
                        }
                        sample_ts = now_ts
                        stale = True

            history_points = list(HISTORY)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"KB_MONITOR_INTERNAL_ERROR: {type(exc).__name__}")

    now_ts = time.time()
    metrics = snapshot.get("metrics", {})
    loader_total = _safe_metric_int(metrics.get("loader_error_total"))
    rule_total = _safe_metric_int(metrics.get("rule_match_total"))
    legacy_total = _safe_metric_int(metrics.get("legacy_fallback_total"))
    climate_total = _safe_metric_int(metrics.get("climate_fallback_total"))

    window_seconds = WINDOW_SECONDS[window]
    loader_inc = _compute_window_increase(history_points, "loader_error_total", loader_total, now_ts, window_seconds)
    rule_inc = _compute_window_increase(history_points, "rule_match_total", rule_total, now_ts, window_seconds)
    legacy_inc = _compute_window_increase(history_points, "legacy_fallback_total", legacy_total, now_ts, window_seconds)
    climate_inc = _compute_window_increase(history_points, "climate_fallback_total", climate_total, now_ts, window_seconds)

    denominator = max(rule_total + legacy_total, 1)
    legacy_ratio = float(legacy_total) / float(denominator)
    rule_spike_alert, baseline_increase_median = _compute_rule_spike(
        history_points,
        now_ts,
        window_seconds,
        rule_inc,
    )

    guardrails = {
        "loader_error_alert": loader_inc > 0,
        "legacy_fallback_ratio_alert": legacy_ratio > 0.05 and (rule_total + legacy_total) >= 20,
        "rule_match_spike_alert": rule_spike_alert,
        "climate_spike_alert": climate_inc >= climate_spike_threshold,
    }

    runtime = snapshot.get("runtime", {})
    runtime_enabled = runtime.get("kb_v0_enabled")
    runtime_kill = runtime.get("kill_switch_enabled")
    service = snapshot.get("service", {})

    combined_errors = list(snapshot.get("_errors", [])) + fetch_errors
    status = "healthy"
    if runtime_enabled is False or runtime_kill is True:
        status = "disabled"
    elif stale or combined_errors or service.get("health_status") not in {"healthy", "ok"}:
        status = "degraded"

    response: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "timestamp": _now_iso(sample_ts),
        "window": window,
        "data": {
            "service": {
                "name": service.get("name", "aurora-bff"),
                "base_url": service.get("base_url", base_url),
                "health_status": service.get("health_status", "unknown"),
                "health_http_status": service.get("health_http_status"),
                "commit_sha": service.get("commit_sha"),
                "deployment_id": service.get("deployment_id"),
                "version": service.get("version"),
            },
            "runtime": {
                "kb_v0_enabled": runtime.get("kb_v0_enabled"),
                "kb_fail_mode": runtime.get("kb_fail_mode", "unknown"),
                "kill_switch_enabled": runtime.get("kill_switch_enabled"),
                "source": runtime.get("source", "unknown"),
            },
            "metrics": {
                "loader_error_total": loader_total,
                "rule_match_total": rule_total,
                "legacy_fallback_total": legacy_total,
                "climate_fallback_total": climate_total,
            },
            "derived": {
                "rule_match_plus_fallback_total": rule_total + legacy_total,
                "legacy_fallback_ratio": legacy_ratio,
                "loader_error_increase": loader_inc,
                "rule_match_increase": rule_inc,
                "legacy_fallback_increase": legacy_inc,
                "climate_fallback_increase": climate_inc,
            },
            "guardrails": guardrails,
        },
        "meta": {
            "poll_interval_sec": POLL_INTERVAL_SEC,
            "cache_ttl_sec": cache_ttl_sec,
            "sample_age_sec": max(0, int(now_ts - sample_ts)),
            "source": snapshot.get("_source", "aurora_metrics+health+version"),
            "stale": stale,
            "cache_hit": cache_hit,
        },
        "errors": combined_errors,
    }

    if include_debug:
        response["debug"] = {
            "history_points": len(history_points),
            "window_seconds": window_seconds,
            "baseline_increase_median": baseline_increase_median,
            "thresholds": {
                "legacy_fallback_ratio_alert": 0.05,
                "legacy_fallback_ratio_min_denominator": 20,
                "rule_match_spike_multiplier": 3.0,
                "rule_match_spike_min_increase": 20,
                "climate_spike_threshold": climate_spike_threshold,
            },
            "raw": snapshot.get("_debug", {}),
        }

    return response
