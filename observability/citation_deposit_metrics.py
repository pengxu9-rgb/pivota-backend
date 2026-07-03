"""Telemetry for the citation-deposit gate (ADR-008 #2).

When an audit's citations resolve to a non-depositable content_key (identity
`unresolved`, or no map entry), `extract_citation_observations` silently drops
them — which is exactly how brand fragmentation hides (the deposit never
accretes and nothing warns). These counters make that drop observable so a
coverage regression from fragmentation is visible instead of silent.

Follows the graceful-degradation pattern of observability.reviews_metrics: real
Prometheus counters when available, no-ops otherwise; the record function never
raises.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _metrics_enabled() -> bool:
    return os.getenv("PROMETHEUS_METRICS_ENABLED", "true").lower() == "true"


try:
    if not _metrics_enabled():  # pragma: no cover
        raise ImportError("metrics disabled")

    from prometheus_client import Counter  # type: ignore

    def _counter(name: str, doc: str, labels: list):
        try:
            return Counter(name, doc, labels)
        except ValueError:  # pragma: no cover — already registered (double import)
            from prometheus_client import REGISTRY  # type: ignore

            return getattr(REGISTRY, "_names_to_collectors", {}).get(name)

    citation_deposit_dropped_skus_total = _counter(
        "citation_deposit_dropped_skus_total",
        "SKUs whose audit citations were dropped from deposit because the "
        "content_key identity was not resolvable (silent brand fragmentation).",
        ["basis"],
    )
    citation_deposit_dropped_observations_total = _counter(
        "citation_deposit_dropped_observations_total",
        "Individual citation observations suppressed from deposit due to an "
        "unresolved content_key identity.",
        ["basis"],
    )
except Exception:  # pragma: no cover — prometheus absent or metrics disabled
    citation_deposit_dropped_skus_total = None
    citation_deposit_dropped_observations_total = None


def record_deposit_dropped(*, basis: str, observations: int) -> None:
    """Record that one SKU's citations were dropped from deposit (unresolved
    identity). `basis` is the deposit basis ('unresolved', 'missing', …);
    `observations` is how many citation rows were suppressed. No-op when metrics
    are disabled/unavailable. NEVER raises — telemetry must not break the caller."""
    b = str(basis or "missing")
    try:
        if citation_deposit_dropped_skus_total is not None:
            citation_deposit_dropped_skus_total.labels(basis=b).inc()
        if citation_deposit_dropped_observations_total is not None and observations > 0:
            citation_deposit_dropped_observations_total.labels(basis=b).inc(observations)
    except Exception:  # pragma: no cover — telemetry must never break the caller
        pass
