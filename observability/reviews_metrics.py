from __future__ import annotations

from typing import Optional


def _metrics_enabled() -> bool:
    # Keep metrics on by default in dev; operators can disable via env.
    import os

    return os.getenv("PROMETHEUS_METRICS_ENABLED", "true").lower() == "true"


try:
    if not _metrics_enabled():  # pragma: no cover
        raise ImportError("metrics disabled")

    from prometheus_client import Counter, Histogram  # type: ignore

    reviews_invoke_requests_total = Counter(
        "reviews_invoke_requests_total",
        "Reviews Center invoke requests (by operation + status).",
        ["operation", "status_code"],
    )
    reviews_invoke_errors_total = Counter(
        "reviews_invoke_errors_total",
        "Reviews Center invoke errors (by operation + error type).",
        ["operation", "error_type"],
    )
    reviews_invoke_duration_seconds = Histogram(
        "reviews_invoke_duration_seconds",
        "Reviews Center invoke duration (seconds).",
        ["operation"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
    )

    reviews_media_requests_total = Counter(
        "reviews_media_requests_total",
        "Review media requests (by result code).",
        ["result"],
    )
    reviews_media_duration_seconds = Histogram(
        "reviews_media_duration_seconds",
        "Review media duration (seconds).",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
    )
    reviews_media_sig_verify_failed_total = Counter(
        "reviews_media_sig_verify_failed_total",
        "Review media signature verification failures (by reason).",
        ["reason"],
    )
    reviews_media_rate_limited_total = Counter(
        "reviews_media_rate_limited_total",
        "Review media requests rate limited.",
    )
    reviews_media_bytes_served_total = Counter(
        "reviews_media_bytes_served_total",
        "Review media bytes served.",
    )

    reviews_pdp_default_view_total = Counter(
        "reviews_pdp_default_view_total",
        "PDP default reviews view selection.",
        ["view"],
    )

    reviews_employee_actions_total = Counter(
        "reviews_employee_actions_total",
        "Employee Reviews Center actions.",
        ["action", "result"],
    )
    reviews_employee_authz_denied_total = Counter(
        "reviews_employee_authz_denied_total",
        "Employee Reviews Center authorization denied events.",
        ["endpoint", "required_permission"],
    )

    reviews_import_validate_total = Counter(
        "reviews_import_validate_total",
        "Import validate attempts.",
        ["result", "reason"],
    )
    reviews_import_validate_duration_seconds = Histogram(
        "reviews_import_validate_duration_seconds",
        "Import validate duration (seconds).",
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    )
    reviews_import_commit_total = Counter(
        "reviews_import_commit_total",
        "Import commit attempts.",
        ["result", "reason", "succeeded"],
    )
    reviews_import_commit_duration_seconds = Histogram(
        "reviews_import_commit_duration_seconds",
        "Import commit duration (seconds).",
        buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
    )

    reviews_buyer_exchange_total = Counter(
        "reviews_buyer_exchange_total",
        "Buyer proof exchange attempts (by result + reason).",
        ["result", "reason"],
    )
    reviews_buyer_exchange_duration_seconds = Histogram(
        "reviews_buyer_exchange_duration_seconds",
        "Buyer proof exchange duration (seconds).",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
    )

    reviews_buyer_create_total = Counter(
        "reviews_buyer_create_total",
        "Buyer review create attempts (by result + reason).",
        ["result", "reason"],
    )
    reviews_buyer_create_duration_seconds = Histogram(
        "reviews_buyer_create_duration_seconds",
        "Buyer review create duration (seconds).",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
    )

    reviews_buyer_media_upload_total = Counter(
        "reviews_buyer_media_upload_total",
        "Buyer review media upload attempts (by result + reason).",
        ["result", "reason"],
    )
    reviews_buyer_media_upload_duration_seconds = Histogram(
        "reviews_buyer_media_upload_duration_seconds",
        "Buyer review media upload duration (seconds).",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    )

    reviews_invitation_issue_total = Counter(
        "reviews_invitation_issue_total",
        "Invitation issue-from-order attempts (by result + reason).",
        ["result", "reason"],
    )
    reviews_invitation_issue_duration_seconds = Histogram(
        "reviews_invitation_issue_duration_seconds",
        "Invitation issue-from-order duration (seconds).",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
    )

    reviews_invitation_send_total = Counter(
        "reviews_invitation_send_total",
        "Invitation send-email-from-order attempts (by result + reason + sent).",
        ["result", "reason", "sent"],
    )
    reviews_invitation_send_duration_seconds = Histogram(
        "reviews_invitation_send_duration_seconds",
        "Invitation send-email-from-order duration (seconds).",
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    )

except Exception:  # pragma: no cover
    # Prometheus client not installed or metrics disabled; provide no-ops.
    Counter = Histogram = None  # type: ignore
    reviews_invoke_requests_total = None
    reviews_invoke_errors_total = None
    reviews_invoke_duration_seconds = None
    reviews_media_requests_total = None
    reviews_media_duration_seconds = None
    reviews_media_sig_verify_failed_total = None
    reviews_media_rate_limited_total = None
    reviews_media_bytes_served_total = None
    reviews_pdp_default_view_total = None
    reviews_employee_actions_total = None
    reviews_employee_authz_denied_total = None
    reviews_import_validate_total = None
    reviews_import_validate_duration_seconds = None
    reviews_import_commit_total = None
    reviews_import_commit_duration_seconds = None
    reviews_buyer_exchange_total = None
    reviews_buyer_exchange_duration_seconds = None
    reviews_buyer_create_total = None
    reviews_buyer_create_duration_seconds = None
    reviews_buyer_media_upload_total = None
    reviews_buyer_media_upload_duration_seconds = None
    reviews_invitation_issue_total = None
    reviews_invitation_issue_duration_seconds = None
    reviews_invitation_send_total = None
    reviews_invitation_send_duration_seconds = None


def record_invoke_request(*, operation: str, status_code: int, duration_seconds: float, error_type: Optional[str] = None) -> None:
    if reviews_invoke_requests_total is not None:
        reviews_invoke_requests_total.labels(operation=str(operation), status_code=str(int(status_code))).inc()
    if reviews_invoke_duration_seconds is not None:
        reviews_invoke_duration_seconds.labels(operation=str(operation)).observe(max(0.0, float(duration_seconds)))
    if error_type and reviews_invoke_errors_total is not None:
        reviews_invoke_errors_total.labels(operation=str(operation), error_type=str(error_type)).inc()


def record_media_request(
    *,
    status_code: int,
    duration_seconds: float,
    bytes_served: int = 0,
    sig_fail_reason: Optional[str] = None,
    rate_limited: bool = False,
) -> None:
    if reviews_media_requests_total is not None:
        reviews_media_requests_total.labels(result=str(int(status_code))).inc()
    if reviews_media_duration_seconds is not None:
        reviews_media_duration_seconds.observe(max(0.0, float(duration_seconds)))
    if bytes_served and reviews_media_bytes_served_total is not None:
        reviews_media_bytes_served_total.inc(int(bytes_served))
    if sig_fail_reason and reviews_media_sig_verify_failed_total is not None:
        reviews_media_sig_verify_failed_total.labels(reason=str(sig_fail_reason)).inc()
    if rate_limited and reviews_media_rate_limited_total is not None:
        reviews_media_rate_limited_total.inc()


def record_pdp_default_view(view: str) -> None:
    if reviews_pdp_default_view_total is not None:
        reviews_pdp_default_view_total.labels(view=str(view or "unknown")).inc()


def record_employee_action(*, action: str, result: str) -> None:
    if reviews_employee_actions_total is not None:
        reviews_employee_actions_total.labels(action=str(action), result=str(result)).inc()


def record_employee_authz_denied(*, endpoint: str, required_permission: str) -> None:
    if reviews_employee_authz_denied_total is not None:
        reviews_employee_authz_denied_total.labels(endpoint=str(endpoint), required_permission=str(required_permission)).inc()


def record_import_validate(*, result: str, reason: str, duration_seconds: float) -> None:
    if reviews_import_validate_total is not None:
        reviews_import_validate_total.labels(result=str(result), reason=str(reason)).inc()
    if reviews_import_validate_duration_seconds is not None:
        reviews_import_validate_duration_seconds.observe(max(0.0, float(duration_seconds)))


def record_import_commit(*, result: str, reason: str, duration_seconds: float, succeeded: bool) -> None:
    if reviews_import_commit_total is not None:
        reviews_import_commit_total.labels(result=str(result), reason=str(reason), succeeded=str(bool(succeeded)).lower()).inc()
    if reviews_import_commit_duration_seconds is not None:
        reviews_import_commit_duration_seconds.observe(max(0.0, float(duration_seconds)))


def record_buyer_exchange(*, result: str, reason: str, duration_seconds: float) -> None:
    if reviews_buyer_exchange_total is not None:
        reviews_buyer_exchange_total.labels(result=str(result), reason=str(reason)).inc()
    if reviews_buyer_exchange_duration_seconds is not None:
        reviews_buyer_exchange_duration_seconds.observe(max(0.0, float(duration_seconds)))


def record_buyer_create(*, result: str, reason: str, duration_seconds: float) -> None:
    if reviews_buyer_create_total is not None:
        reviews_buyer_create_total.labels(result=str(result), reason=str(reason)).inc()
    if reviews_buyer_create_duration_seconds is not None:
        reviews_buyer_create_duration_seconds.observe(max(0.0, float(duration_seconds)))


def record_buyer_media_upload(*, result: str, reason: str, duration_seconds: float) -> None:
    if reviews_buyer_media_upload_total is not None:
        reviews_buyer_media_upload_total.labels(result=str(result), reason=str(reason)).inc()
    if reviews_buyer_media_upload_duration_seconds is not None:
        reviews_buyer_media_upload_duration_seconds.observe(max(0.0, float(duration_seconds)))


def record_invitation_issue(*, result: str, reason: str, duration_seconds: float) -> None:
    if reviews_invitation_issue_total is not None:
        reviews_invitation_issue_total.labels(result=str(result), reason=str(reason)).inc()
    if reviews_invitation_issue_duration_seconds is not None:
        reviews_invitation_issue_duration_seconds.observe(max(0.0, float(duration_seconds)))


def record_invitation_send(*, result: str, reason: str, sent: bool, duration_seconds: float) -> None:
    if reviews_invitation_send_total is not None:
        reviews_invitation_send_total.labels(result=str(result), reason=str(reason), sent=str(bool(sent)).lower()).inc()
    if reviews_invitation_send_duration_seconds is not None:
        reviews_invitation_send_duration_seconds.observe(max(0.0, float(duration_seconds)))
