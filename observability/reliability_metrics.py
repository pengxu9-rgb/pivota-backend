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
        catalog_pivot_shadow_compare_total = Counter(
            "catalog_pivot_shadow_compare_total",
            "Catalog pivot shadow compare count by served path, shadow path, and top1 agreement.",
            ["source", "page_bucket", "query_semantic_class", "served_path", "shadow_path", "top1_same"],
        )
    except ValueError:
        catalog_pivot_shadow_compare_total = _existing_collector("catalog_pivot_shadow_compare_total")

    try:
        catalog_pivot_shadow_overlap_ratio = Histogram(
            "catalog_pivot_shadow_overlap_ratio",
            "Catalog pivot shadow overlap ratio by served path and shadow path.",
            ["source", "page_bucket", "query_semantic_class", "served_path", "shadow_path", "top1_same"],
            buckets=(0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0),
        )
    except ValueError:
        catalog_pivot_shadow_overlap_ratio = _existing_collector("catalog_pivot_shadow_overlap_ratio")

    try:
        catalog_pivot_shadow_returned_count_delta = Histogram(
            "catalog_pivot_shadow_returned_count_delta",
            "Signed delta between pivot shadow returned count and served returned count.",
            ["source", "page_bucket", "query_semantic_class", "served_path", "shadow_path", "top1_same"],
            buckets=(-20, -10, -5, -2, -1, 0, 1, 2, 5, 10, 20),
        )
    except ValueError:
        catalog_pivot_shadow_returned_count_delta = _existing_collector("catalog_pivot_shadow_returned_count_delta")

    try:
        catalog_pivot_shadow_internal_share_delta = Histogram(
            "catalog_pivot_shadow_internal_share_delta",
            "Signed delta between pivot and served internal-result share.",
            ["source", "page_bucket", "query_semantic_class", "served_path", "shadow_path", "top1_same"],
            buckets=(-1.0, -0.75, -0.5, -0.25, -0.1, 0.0, 0.1, 0.25, 0.5, 0.75, 1.0),
        )
    except ValueError:
        catalog_pivot_shadow_internal_share_delta = _existing_collector("catalog_pivot_shadow_internal_share_delta")

    try:
        catalog_pivot_shadow_external_share_delta = Histogram(
            "catalog_pivot_shadow_external_share_delta",
            "Signed delta between pivot and served external-result share.",
            ["source", "page_bucket", "query_semantic_class", "served_path", "shadow_path", "top1_same"],
            buckets=(-1.0, -0.75, -0.5, -0.25, -0.1, 0.0, 0.1, 0.25, 0.5, 0.75, 1.0),
        )
    except ValueError:
        catalog_pivot_shadow_external_share_delta = _existing_collector("catalog_pivot_shadow_external_share_delta")

    try:
        catalog_pivot_shadow_no_result_mismatch_total = Counter(
            "catalog_pivot_shadow_no_result_mismatch_total",
            "Count of shadow compare cases where served and pivot disagree on empty vs non-empty results.",
            ["source", "page_bucket", "query_semantic_class", "served_path", "shadow_path"],
        )
    except ValueError:
        catalog_pivot_shadow_no_result_mismatch_total = _existing_collector("catalog_pivot_shadow_no_result_mismatch_total")

    try:
        catalog_pivot_shadow_estimated_price_delta_ratio = Histogram(
            "catalog_pivot_shadow_estimated_price_delta_ratio",
            "Relative delta between pivot and served top-result estimated-best-price.",
            ["source", "page_bucket", "query_semantic_class", "served_path", "shadow_path", "top1_same"],
            buckets=(-1.0, -0.5, -0.25, -0.1, -0.05, 0.0, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0),
        )
    except ValueError:
        catalog_pivot_shadow_estimated_price_delta_ratio = _existing_collector("catalog_pivot_shadow_estimated_price_delta_ratio")

    try:
        catalog_pivot_shadow_bad_price_anomaly_total = Counter(
            "catalog_pivot_shadow_bad_price_anomaly_total",
            "Count of shadow compare cases where pivot estimated-best-price delta exceeds anomaly threshold.",
            ["source", "page_bucket", "query_semantic_class", "served_path", "shadow_path"],
        )
    except ValueError:
        catalog_pivot_shadow_bad_price_anomaly_total = _existing_collector("catalog_pivot_shadow_bad_price_anomaly_total")

    try:
        retry_attempts_total = Counter(
            "retry_attempts_total",
            "Retry attempts by domain/category.",
            ["domain", "category"],
        )
    except ValueError:
        retry_attempts_total = _existing_collector("retry_attempts_total")

    try:
        traffic_taxonomy_records_total = Counter(
            "traffic_taxonomy_records_total",
            "Traffic taxonomy records by stage.",
            ["stage"],
        )
    except ValueError:
        traffic_taxonomy_records_total = _existing_collector("traffic_taxonomy_records_total")

    try:
        traffic_taxonomy_missing_total = Counter(
            "traffic_taxonomy_missing_total",
            "Traffic taxonomy missing values by stage and field.",
            ["stage", "field"],
        )
    except ValueError:
        traffic_taxonomy_missing_total = _existing_collector("traffic_taxonomy_missing_total")

    try:
        traffic_taxonomy_diagnostics_warning_total = Counter(
            "traffic_taxonomy_diagnostics_warning_total",
            "Traffic taxonomy diagnostics warnings by stage and reason.",
            ["stage", "reason"],
        )
    except ValueError:
        traffic_taxonomy_diagnostics_warning_total = _existing_collector("traffic_taxonomy_diagnostics_warning_total")

    try:
        commerce_attribution_silent_reject_total = Counter(
            "commerce_attribution_silent_reject_total",
            "Order attribution edges skipped before insert (gate rejected metadata).",
            ["merchant_id", "reason"],
        )
    except ValueError:
        commerce_attribution_silent_reject_total = _existing_collector("commerce_attribution_silent_reject_total")

    try:
        commerce_attribution_inferred_recovered_total = Counter(
            "commerce_attribution_inferred_recovered_total",
            "Order attribution edges RECOVERED via the token-less fallback join "
            "(agent+merchant+window). Recorded + flagged inferred, EXCLUDED from billing (#1481).",
            ["merchant_id"],
        )
    except ValueError:
        commerce_attribution_inferred_recovered_total = _existing_collector("commerce_attribution_inferred_recovered_total")

    try:
        catalog_import_task_total = Counter(
            "catalog_import_task_total",
            "Platform catalog import ATTEMPT outcomes by connector, status and error category. "
            "A retrying task emits one sample per attempt, not one per task.",
            ["connector", "status", "error_category"],
        )
    except ValueError:
        catalog_import_task_total = _existing_collector("catalog_import_task_total")

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
    catalog_pivot_shadow_compare_total = None
    catalog_pivot_shadow_overlap_ratio = None
    catalog_pivot_shadow_returned_count_delta = None
    catalog_pivot_shadow_internal_share_delta = None
    catalog_pivot_shadow_external_share_delta = None
    catalog_pivot_shadow_no_result_mismatch_total = None
    catalog_pivot_shadow_estimated_price_delta_ratio = None
    catalog_pivot_shadow_bad_price_anomaly_total = None
    retry_attempts_total = None
    traffic_taxonomy_records_total = None
    traffic_taxonomy_missing_total = None
    traffic_taxonomy_diagnostics_warning_total = None
    commerce_attribution_silent_reject_total = None
    commerce_attribution_inferred_recovered_total = None
    catalog_import_task_total = None


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


def record_catalog_pivot_shadow_compare(
    *,
    source: str,
    page_bucket: str,
    query_semantic_class: str,
    served_path: str,
    shadow_path: str,
    top1_same: bool,
    overlap_ratio: float,
    returned_count_delta: int = 0,
    internal_share_delta: float = 0.0,
    external_share_delta: float = 0.0,
    no_result_mismatch: bool = False,
    estimated_price_delta_ratio: Optional[float] = None,
    bad_price_anomaly: bool = False,
) -> None:
    labels = {
        "source": str(source or "unknown"),
        "page_bucket": str(page_bucket or "unknown"),
        "query_semantic_class": str(query_semantic_class or "default"),
        "served_path": str(served_path or "unknown"),
        "shadow_path": str(shadow_path or "unknown"),
        "top1_same": "true" if bool(top1_same) else "false",
    }
    if catalog_pivot_shadow_compare_total is not None:
        catalog_pivot_shadow_compare_total.labels(**labels).inc()
    if catalog_pivot_shadow_overlap_ratio is not None:
        try:
            normalized_ratio = max(0.0, min(1.0, float(overlap_ratio)))
        except Exception:
            normalized_ratio = 0.0
        catalog_pivot_shadow_overlap_ratio.labels(**labels).observe(normalized_ratio)
    if catalog_pivot_shadow_returned_count_delta is not None:
        try:
            normalized_delta = int(returned_count_delta)
        except Exception:
            normalized_delta = 0
        catalog_pivot_shadow_returned_count_delta.labels(**labels).observe(normalized_delta)
    if catalog_pivot_shadow_internal_share_delta is not None:
        try:
            normalized_delta = max(-1.0, min(1.0, float(internal_share_delta)))
        except Exception:
            normalized_delta = 0.0
        catalog_pivot_shadow_internal_share_delta.labels(**labels).observe(normalized_delta)
    if catalog_pivot_shadow_external_share_delta is not None:
        try:
            normalized_delta = max(-1.0, min(1.0, float(external_share_delta)))
        except Exception:
            normalized_delta = 0.0
        catalog_pivot_shadow_external_share_delta.labels(**labels).observe(normalized_delta)
    if no_result_mismatch and catalog_pivot_shadow_no_result_mismatch_total is not None:
        catalog_pivot_shadow_no_result_mismatch_total.labels(
            source=labels["source"],
            page_bucket=labels["page_bucket"],
            query_semantic_class=labels["query_semantic_class"],
            served_path=labels["served_path"],
            shadow_path=labels["shadow_path"],
        ).inc()
    if estimated_price_delta_ratio is not None and catalog_pivot_shadow_estimated_price_delta_ratio is not None:
        try:
            normalized_delta = max(-5.0, min(5.0, float(estimated_price_delta_ratio)))
        except Exception:
            normalized_delta = 0.0
        catalog_pivot_shadow_estimated_price_delta_ratio.labels(**labels).observe(normalized_delta)
    if bad_price_anomaly and catalog_pivot_shadow_bad_price_anomaly_total is not None:
        catalog_pivot_shadow_bad_price_anomaly_total.labels(
            source=labels["source"],
            page_bucket=labels["page_bucket"],
            query_semantic_class=labels["query_semantic_class"],
            served_path=labels["served_path"],
            shadow_path=labels["shadow_path"],
        ).inc()


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


def record_traffic_taxonomy(
    *,
    stage: str,
    taxonomy: dict,
    diagnostics_warning: bool = False,
    warning_reason: str = "unknown_identity",
) -> None:
    if traffic_taxonomy_records_total is not None:
        traffic_taxonomy_records_total.labels(stage=str(stage or "unknown")).inc()
    if traffic_taxonomy_missing_total is not None:
        for field in (
            "source_channel",
            "query_source",
            "agent_id",
            "protocol_name",
        ):
            value = str((taxonomy or {}).get(field) or "unknown").strip().lower()
            if value in {"", "unknown"}:
                traffic_taxonomy_missing_total.labels(
                    stage=str(stage or "unknown"),
                    field=field,
                ).inc()
    if diagnostics_warning and traffic_taxonomy_diagnostics_warning_total is not None:
        traffic_taxonomy_diagnostics_warning_total.labels(
            stage=str(stage or "unknown"),
            reason=str(warning_reason or "unknown"),
        ).inc()


def record_commerce_attribution_silent_reject(
    *,
    merchant_id: Optional[str],
    reason: str = "no_attribution_signal",
) -> None:
    if commerce_attribution_silent_reject_total is None:
        return
    commerce_attribution_silent_reject_total.labels(
        merchant_id=str(merchant_id or "unknown"),
        reason=str(reason or "unknown"),
    ).inc()


def record_commerce_attribution_inferred_recovered(*, merchant_id: Optional[str]) -> None:
    """A token-less order was recovered via the fallback join (#1481) — recorded as
    an inferred edge (NOT billed). Together with the silent-reject counter this makes
    attribution coverage a known number: matched vs inferred-recovered vs dropped."""
    if commerce_attribution_inferred_recovered_total is None:
        return
    commerce_attribution_inferred_recovered_total.labels(
        merchant_id=str(merchant_id or "unknown"),
    ).inc()


# Label allowlist for `connector`. NOT decoration: platform_import_tasks.connector
# is a plain String(100) with no CHECK and no enum, `schedule_import_task` takes a
# bare Optional[str], and services/platform_onboarding_service.py CATCHES
# InvalidConnectorError and logs it without resetting the value — so a merchant
# calling POST /platform/onboarding/register can put an arbitrary string in that
# column and loop to seed unlimited distinct values.
#
# The drain lane is scoped to connector='shopify' today, so those rows are never
# processed and never labelled. But db/platform_import_tasks.py explicitly invites
# widening the lane, and the day it widens every string already sitting in that
# column becomes a live label value. Clamping HERE survives that, because it does
# not depend on the lane staying narrow.
_KNOWN_IMPORT_CONNECTORS = frozenset(
    {
        "shopify",
        "manual",
        "amazon_sp_api",
        "amazon_report",
        "temu_report",
        "amazon_orders",
        "temu_orders",
    }
)


def record_catalog_import_task(
    *,
    connector: Optional[str],
    status: str,
    error_category: Optional[str] = None,
) -> None:
    """Record the outcome of one platform catalog import ATTEMPT.

    ATTEMPT, not task — the distinction matters for anyone writing an alert.

    jobs/catalog_import_worker emitted NO metrics at all, which is why this
    exists. `catalog_import_drain_tick` (#1964) is dormant behind
    CATALOG_IMPORT_DRAIN_ENABLED, and the first thing it will do when armed is
    walk a backlog nothing has ever drained. Without a counter, the two
    outcomes that most need a human are indistinguishable from a quiet queue:

      * a spike in error_category="credentials_unavailable" — either a
        credential-resolution outage or a cohort whose stores no longer
        resolve (#1989);
      * a spike in status="failed" generally, which on this path means the
        backlog is burning down into dead rows rather than importing.

    A task that retries emits one sample per attempt. Specifically, a
    ShopifyCredentialsUnavailableError is deliberately retryable, so ONE
    merchant hitting ONE credential outage emits up to
    SHOPIFY_MAX_RETRY_ATTEMPTS (5) samples of
    status="retry_scheduled" plus one of status="failed" — six, not one. An
    alert that treats each sample as a distinct affected merchant will
    over-report by that multiplier. Filter on status="failed" to count tasks
    that actually gave up, or leave status unfiltered to see retry storms; both
    are legitimate, but they answer different questions.

    `status` and `error_category` are bounded by enumeration — three terminal
    states, and eight category literals the retry handlers assign.
    `connector` is bounded by the allowlist above, NOT by construction, because
    the column it comes from accepts arbitrary merchant-supplied strings.
    """
    if catalog_import_task_total is None:
        return
    raw_connector = str(connector or "unknown")
    catalog_import_task_total.labels(
        connector=raw_connector if raw_connector in _KNOWN_IMPORT_CONNECTORS else "other",
        status=str(status or "unknown"),
        error_category=str(error_category or "none"),
    ).inc()
