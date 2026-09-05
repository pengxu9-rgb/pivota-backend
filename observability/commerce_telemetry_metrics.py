"""Prometheus metrics for the commerce telemetry ingresses.

Every route that writes to the canonical commerce ledger reports through
services.telemetry_ingress, which calls these three recorders. Labels are
bounded: `write_path` is the ledger vocabulary, `result` and `reason` are
fixed buckets derived from the response status, never from caller input.
"""

from __future__ import annotations

from typing import Optional


def _metrics_enabled() -> bool:
    import os

    return os.getenv("PROMETHEUS_METRICS_ENABLED", "true").lower() == "true"


try:
    if not _metrics_enabled():  # pragma: no cover
        raise ImportError("metrics disabled")
    from prometheus_client import Counter, Histogram  # type: ignore

    def _existing_collector(name: str):
        try:
            from prometheus_client import REGISTRY  # type: ignore

            return getattr(REGISTRY, "_names_to_collectors", {}).get(name)  # type: ignore[attr-defined]
        except Exception:
            return None

    commerce_telemetry_requests_total = _existing_collector(
        "commerce_telemetry_requests_total"
    ) or Counter(
        "commerce_telemetry_requests_total",
        "Commerce telemetry ingress requests (by write path, result bucket, reason).",
        ["write_path", "result", "reason"],
    )
    commerce_telemetry_events_total = _existing_collector(
        "commerce_telemetry_events_total"
    ) or Counter(
        "commerce_telemetry_events_total",
        "Commerce telemetry events by ledger outcome (accepted, duplicate, ignored, rejected).",
        ["write_path", "outcome"],
    )
    commerce_telemetry_request_duration_seconds = _existing_collector(
        "commerce_telemetry_request_duration_seconds"
    ) or Histogram(
        "commerce_telemetry_request_duration_seconds",
        "Commerce telemetry ingress request duration (seconds).",
        ["write_path"],
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )
except Exception:  # pragma: no cover - prometheus_client absent or disabled
    commerce_telemetry_requests_total = None  # type: ignore[assignment]
    commerce_telemetry_events_total = None  # type: ignore[assignment]
    commerce_telemetry_request_duration_seconds = None  # type: ignore[assignment]


def record_request(
    *, write_path: str, result: str, reason: str, duration_seconds: float
) -> None:
    if commerce_telemetry_requests_total is not None:
        commerce_telemetry_requests_total.labels(
            write_path=str(write_path or "unknown"),
            result=str(result),
            reason=str(reason),
        ).inc()
    if commerce_telemetry_request_duration_seconds is not None:
        commerce_telemetry_request_duration_seconds.labels(
            write_path=str(write_path or "unknown")
        ).observe(max(0.0, float(duration_seconds)))


def record_events(*, write_path: str, outcome: str, count: int) -> None:
    if count <= 0 or commerce_telemetry_events_total is None:
        return
    commerce_telemetry_events_total.labels(
        write_path=str(write_path or "unknown"), outcome=str(outcome)
    ).inc(int(count))


def counter_value(name: str, **labels: str) -> Optional[float]:
    """Test helper: current value of a labelled counter, or None if disabled."""
    collector = {
        "requests": commerce_telemetry_requests_total,
        "events": commerce_telemetry_events_total,
    }.get(name)
    if collector is None:
        return None
    try:
        return float(collector.labels(**labels)._value.get())  # type: ignore[attr-defined]
    except Exception:
        return None
