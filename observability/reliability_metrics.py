from __future__ import annotations

from typing import Optional


def _metrics_enabled() -> bool:
    import os

    return os.getenv("PROMETHEUS_METRICS_ENABLED", "true").lower() == "true"


try:
    if not _metrics_enabled():  # pragma: no cover
        raise ImportError("metrics disabled")

    from prometheus_client import Counter, Gauge, Histogram  # type: ignore

    def _existing_collector(name: str):
        try:
            from prometheus_client import REGISTRY  # type: ignore

            return getattr(REGISTRY, "_names_to_collectors", {}).get(name)  # type: ignore[attr-defined]
        except Exception:
            return None

    try:
        payment_attempt_total = Counter(
            "payment_attempt_total",
            "Payment attempts by PSP and result.",
            ["psp", "result", "error_category"],
        )
    except ValueError:
        payment_attempt_total = _existing_collector("payment_attempt_total")

    try:
        payment_fallback_total = Counter(
            "payment_fallback_total",
            "Payment fallback transitions between PSPs.",
            ["from_psp", "to_psp", "reason"],
        )
    except ValueError:
        payment_fallback_total = _existing_collector("payment_fallback_total")

    try:
        payment_timeout_total = Counter(
            "payment_timeout_total",
            "Payment timeout count by PSP and stage.",
            ["psp", "stage"],
        )
    except ValueError:
        payment_timeout_total = _existing_collector("payment_timeout_total")

    try:
        payment_circuit_state = Gauge(
            "payment_circuit_state",
            "Payment circuit state (open=1, half_open=0.5, closed=0).",
            ["psp", "state"],
        )
    except ValueError:
        payment_circuit_state = _existing_collector("payment_circuit_state")

    try:
        payment_latency_seconds = Histogram(
            "payment_latency_seconds",
            "Payment latency by PSP and result.",
            ["psp", "result"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0),
        )
    except ValueError:
        payment_latency_seconds = _existing_collector("payment_latency_seconds")

    try:
        catalog_search_requests_total = Counter(
            "catalog_search_requests_total",
            "Catalog search requests by mode/path/result.",
            ["mode", "path", "result"],
        )
    except ValueError:
        catalog_search_requests_total = _existing_collector("catalog_search_requests_total")

    try:
        catalog_upstream_fallback_total = Counter(
            "catalog_upstream_fallback_total",
            "Catalog upstream fallback count by reason.",
            ["reason"],
        )
    except ValueError:
        catalog_upstream_fallback_total = _existing_collector("catalog_upstream_fallback_total")

    try:
        catalog_upstream_timeout_total = Counter(
            "catalog_upstream_timeout_total",
            "Catalog upstream timeout count by surface.",
            ["surface"],
        )
    except ValueError:
        catalog_upstream_timeout_total = _existing_collector("catalog_upstream_timeout_total")

    try:
        catalog_upstream_circuit_state = Gauge(
            "catalog_upstream_circuit_state",
            "Catalog upstream circuit state (open=1, half_open=0.5, closed=0).",
            ["surface", "state"],
        )
    except ValueError:
        catalog_upstream_circuit_state = _existing_collector("catalog_upstream_circuit_state")

    try:
        catalog_search_latency_seconds = Histogram(
            "catalog_search_latency_seconds",
            "Catalog search latency by path/result.",
            ["path", "result"],
            buckets=(0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
        )
    except ValueError:
        catalog_search_latency_seconds = _existing_collector("catalog_search_latency_seconds")

    try:
        retry_attempts_total = Counter(
            "retry_attempts_total",
            "Retry attempts by domain/category.",
            ["domain", "category"],
        )
    except ValueError:
        retry_attempts_total = _existing_collector("retry_attempts_total")

except Exception:  # pragma: no cover
    Counter = Gauge = Histogram = None  # type: ignore
    payment_attempt_total = None
    payment_fallback_total = None
    payment_timeout_total = None
    payment_circuit_state = None
    payment_latency_seconds = None
    catalog_search_requests_total = None
    catalog_upstream_fallback_total = None
    catalog_upstream_timeout_total = None
    catalog_upstream_circuit_state = None
    catalog_search_latency_seconds = None
    retry_attempts_total = None


def record_payment_attempt(
    *,
    psp: str,
    result: str,
    error_category: str = "none",
    duration_seconds: Optional[float] = None,
) -> None:
    if payment_attempt_total is not None:
        payment_attempt_total.labels(
            psp=str(psp or "unknown"),
            result=str(result or "unknown"),
            error_category=str(error_category or "none"),
        ).inc()
    if duration_seconds is not None and payment_latency_seconds is not None:
        payment_latency_seconds.labels(
            psp=str(psp or "unknown"),
            result=str(result or "unknown"),
        ).observe(max(0.0, float(duration_seconds)))


def record_payment_fallback(*, from_psp: str, to_psp: str, reason: str) -> None:
    if payment_fallback_total is not None:
        payment_fallback_total.labels(
            from_psp=str(from_psp or "unknown"),
            to_psp=str(to_psp or "unknown"),
            reason=str(reason or "unknown"),
        ).inc()


def record_payment_timeout(*, psp: str, stage: str) -> None:
    if payment_timeout_total is not None:
        payment_timeout_total.labels(
            psp=str(psp or "unknown"),
            stage=str(stage or "unknown"),
        ).inc()


def set_payment_circuit(*, psp: str, state: str) -> None:
    if payment_circuit_state is None:
        return
    state_norm = str(state or "closed").lower()
    value = 1.0 if state_norm == "open" else 0.5 if state_norm in {"half_open", "half-open"} else 0.0
    payment_circuit_state.labels(psp=str(psp or "unknown"), state=state_norm).set(value)


def record_catalog_search(*, mode: str, path: str, result: str, duration_seconds: float) -> None:
    if catalog_search_requests_total is not None:
        catalog_search_requests_total.labels(
            mode=str(mode or "unknown"),
            path=str(path or "unknown"),
            result=str(result or "unknown"),
        ).inc()
    if catalog_search_latency_seconds is not None:
        catalog_search_latency_seconds.labels(
            path=str(path or "unknown"),
            result=str(result or "unknown"),
        ).observe(max(0.0, float(duration_seconds)))


def record_catalog_upstream_fallback(*, reason: str) -> None:
    if catalog_upstream_fallback_total is not None:
        catalog_upstream_fallback_total.labels(reason=str(reason or "unknown")).inc()


def record_catalog_upstream_timeout(*, surface: str) -> None:
    if catalog_upstream_timeout_total is not None:
        catalog_upstream_timeout_total.labels(surface=str(surface or "unknown")).inc()


def set_catalog_upstream_circuit(*, surface: str, state: str) -> None:
    if catalog_upstream_circuit_state is None:
        return
    state_norm = str(state or "closed").lower()
    value = 1.0 if state_norm == "open" else 0.5 if state_norm in {"half_open", "half-open"} else 0.0
    catalog_upstream_circuit_state.labels(surface=str(surface or "unknown"), state=state_norm).set(value)


def record_retry_attempt(*, domain: str, category: str) -> None:
    if retry_attempts_total is not None:
        retry_attempts_total.labels(
            domain=str(domain or "unknown"),
            category=str(category or "unknown"),
        ).inc()
