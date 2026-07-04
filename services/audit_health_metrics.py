"""W7 audit-health metrics — the alarm the no-fallback main line assumes.

W4 deleted the deterministic-brief fallback in favour of honest failure (a brief
that can't ground returns nothing and the merchant is refunded, rather than a
fabricated template). That is only *safe* if honest-failures are rare AND observed.
This module is the "observed" half. Over a rolling window it computes:

  - run_failure_rate    — worker-level failures (status='failed') / total runs.
                          Refunded by the W6 billing invariant; a spike = a
                          pipeline/provider outage.
  - honest_failure_rate — briefs that returned an "unavailable_*" outcome
                          (next_best_action.brief_debug.outcome) / brief attempts,
                          scanned from recent COMPLETED runs' reports. An
                          honest-failure sits INSIDE a completed run, invisible to
                          run status — so this is the W4 signal proper.

A breach fires a create_alert (agent_alerts, deduped) + a Sentry message and logs
at ERROR. Purely observational — this module changes NO merchant-facing behaviour.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Rolling window + how many recent reports to scan for brief outcomes.
WINDOW_SECONDS = int(os.getenv("AUDIT_HEALTH_WINDOW_SECONDS", str(24 * 3600)))
REPORT_SAMPLE = int(os.getenv("AUDIT_HEALTH_REPORT_SAMPLE", "100"))
# A rate only alarms above this many observations, so one failure in a quiet
# window can't page.
MIN_OBSERVATIONS = int(os.getenv("AUDIT_HEALTH_MIN_OBSERVATIONS", "10"))
RUN_FAILURE_RATE_ALERT = float(os.getenv("AUDIT_FAILURE_RATE_ALERT", "0.30"))
HONEST_FAILURE_RATE_ALERT = float(os.getenv("AUDIT_HONEST_FAILURE_RATE_ALERT", "0.20"))


def _walk_brief_outcomes(node: Any, out: List[str]) -> None:
    """Collect every next_best_action.brief_debug.outcome in a report tree,
    tolerant of the brand (per_sku_reports[]) and single-product report shapes."""
    if isinstance(node, dict):
        bd = node.get("brief_debug")
        if isinstance(bd, dict) and bd.get("outcome"):
            out.append(str(bd["outcome"]))
        for v in node.values():
            _walk_brief_outcomes(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_brief_outcomes(v, out)


def _counter(items: List[str]) -> Dict[str, int]:
    d: Dict[str, int] = {}
    for i in items:
        d[i] = d.get(i, 0) + 1
    return d


def brief_outcome_rates(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure: fold recent reports' brief outcomes into the honest-failure rate.

    Denominator = briefs that were ATTEMPTED. The not-even-tried states
    (none_disabled = feature off, none_no_key = no provider key) are excluded —
    they aren't failures, they're "never ran". Numerator = unavailable_* (LLM
    error / rejected-after-retries / unexpected) + attach_exception.
    """
    outcomes: List[str] = []
    for r in reports or []:
        _walk_brief_outcomes((r or {}).get("report_jsonb"), outcomes)
    attempted = [o for o in outcomes if not o.startswith("none_")]
    unavailable = [
        o for o in attempted
        if o.startswith("unavailable_") or o == "attach_exception"
    ]
    total = len(attempted)
    rate = (len(unavailable) / total) if total else None
    return {
        "brief_attempts": total,
        "honest_failures": len(unavailable),
        "honest_failure_rate": round(rate, 4) if rate is not None else None,
        "outcome_breakdown": _counter(outcomes),
    }


def evaluate_breaches(health: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pure: which metrics crossed their alert threshold (rate-floored)."""
    breaches: List[Dict[str, Any]] = []
    total_runs = int(health.get("total_runs") or 0)
    rfr = health.get("run_failure_rate")
    if total_runs >= MIN_OBSERVATIONS and rfr is not None and rfr > RUN_FAILURE_RATE_ALERT:
        breaches.append({"metric": "run_failure_rate", "value": rfr, "threshold": RUN_FAILURE_RATE_ALERT})
    attempts = int(health.get("brief_attempts") or 0)
    hfr = health.get("honest_failure_rate")
    if attempts >= MIN_OBSERVATIONS and hfr is not None and hfr > HONEST_FAILURE_RATE_ALERT:
        breaches.append({"metric": "honest_failure_rate", "value": hfr, "threshold": HONEST_FAILURE_RATE_ALERT})
    return breaches


async def compute_audit_health(
    *, window_seconds: int = WINDOW_SECONDS, sample: int = REPORT_SAMPLE
) -> Dict[str, Any]:
    """Read the two sources and shape the health payload (+ breaches)."""
    from db.merchant_audit_runs import (
        audit_status_counts_in_window,
        recent_completed_reports,
    )

    status_counts = await audit_status_counts_in_window(window_seconds=window_seconds)
    total_runs = sum(status_counts.values())
    failed = int(status_counts.get("failed", 0))
    run_failure_rate = round(failed / total_runs, 4) if total_runs else None

    reports = await recent_completed_reports(limit=sample)
    briefs = brief_outcome_rates(reports)

    health = {
        "window_seconds": window_seconds,
        "total_runs": total_runs,
        "status_counts": status_counts,
        "run_failure_rate": run_failure_rate,
        "report_sample": len(reports),
        **briefs,
    }
    health["breaches"] = evaluate_breaches(health)
    return health


async def run_audit_health_tick() -> Dict[str, Any]:
    """Scheduler entry: compute health, alert on each breach. Never raises."""
    try:
        health = await compute_audit_health()
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit health compute failed: %s", str(exc)[:200])
        return {"error": str(exc)[:200]}

    for b in health.get("breaches", []):
        msg = (
            f"audit health: {b['metric']}={b['value']} exceeds {b['threshold']} "
            f"(window {health['window_seconds']}s, {health['total_runs']} runs, "
            f"{health.get('brief_attempts', 0)} brief attempts)"
        )
        logger.error("W7_AUDIT_HEALTH_ALERT %s", msg)
        try:
            from services.agent_anomaly_detector import create_alert

            await create_alert(
                agent_id="audit_pipeline",
                alert_type=f"audit_health.{b['metric']}",
                severity="critical",
                message=msg,
                metadata=b,
            )
        except Exception:  # noqa: BLE001
            logger.warning("audit health create_alert failed", exc_info=True)
        try:
            import sentry_sdk

            sentry_sdk.capture_message(msg, level="error")
        except Exception:  # noqa: BLE001
            pass
    return health
