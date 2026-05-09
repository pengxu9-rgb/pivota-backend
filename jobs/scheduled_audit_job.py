"""PR-1b: scheduled re-audit job.

Fires once a day (03:00 UTC, see services/audit_scheduler.py). Picks
up merchants opted into weekly/monthly auto-re-audit and runs the full
audit pipeline for any that are due, persisting results via the
existing merchant_audit_runs lifecycle.

Design notes:
- Throttled: max N concurrent audits per tick (avoids slamming the
  Gemini quota when many merchants come due on the same day).
- Idempotent per-day: a merchant audited within the last 24h is
  skipped, regardless of nominal due time. Belt and suspenders against
  double-runs.
- Best-effort: per-merchant audit failures are caught + logged, do
  not abort the rest of the batch.
- Product list: re-audits the same products as the most recent
  succeeded run (extracted from report_jsonb). If no prior run exists
  (race condition: schedule set but never audited), skip.

Out of scope for PR-1b (defer to PR-1c if needed):
- Cold-start prospect re-audit (synthetic merchant_id) — would need
  separate registration flow + gating
- Per-merchant audit-time customization (per-merchant max_runs,
  category opt-out, etc.) — for now the cron uses defaults
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.sql import select

logger = logging.getLogger(__name__)


# Max concurrent audits per cron tick. Each audit takes ~30-100s and
# bursts ~13 grounded Gemini calls. 3 in parallel keeps quota pressure
# bounded; the dispatch loop processes the rest of the queue as slots
# free up.
_MAX_CONCURRENT_AUDITS = 3

# Per-merchant safety: even if schedule says re-run, skip if audited
# within this window. Catches double-runs from misfire_grace_time
# stacking + manual audit just before the cron.
_MIN_HOURS_BETWEEN_AUDITS = 23


# Schedule → minimum days between auto-re-audits.
_INTERVAL_DAYS = {
    "weekly": 7,
    "monthly": 30,
}


def is_audit_due(
    last_audit_at: Optional[datetime],
    schedule: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Returns True when a merchant on `schedule` is due for re-audit
    given their `last_audit_at` (or never audited).

    Pure function — no I/O. Tested with monkey-patched `now`.
    """
    if schedule not in _INTERVAL_DAYS:
        return False
    if last_audit_at is None:
        # Opted in but never audited — kick off the first run on the
        # next cron tick.
        return True
    now_utc = now if now is not None else datetime.now(timezone.utc)
    interval = timedelta(days=_INTERVAL_DAYS[schedule])
    return (now_utc - last_audit_at) >= interval


async def _list_due_merchants() -> List[Dict[str, Any]]:
    """Query catalog_merchants for opted-in merchants whose most recent
    audit run is past their schedule's interval.

    Returns list of `{merchant_id, schedule, last_audit_at}`.
    """
    from db.database import database
    from db.catalog import catalog_merchants
    from db.merchant_audit_runs import merchant_audit_runs

    try:
        # Subquery: most recent succeeded audit per merchant.
        # We use requested_at (not completed_at) so a merchant whose
        # audit is currently running but started ≤ interval ago is
        # NOT picked up again.
        opted_in_rows = await database.fetch_all(
            select(
                catalog_merchants.c.merchant_id,
                catalog_merchants.c.audit_schedule,
            ).where(
                catalog_merchants.c.audit_schedule.in_(["weekly", "monthly"]),
                catalog_merchants.c.status == "active",
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scheduled_audit_job: opted-in query failed: %s", exc,
        )
        return []

    if not opted_in_rows:
        return []

    out: List[Dict[str, Any]] = []
    now_utc = datetime.now(timezone.utc)
    min_gap = timedelta(hours=_MIN_HOURS_BETWEEN_AUDITS)

    for row in opted_in_rows:
        merchant_id = row["merchant_id"]
        schedule = row["audit_schedule"]
        try:
            most_recent = await database.fetch_one(
                merchant_audit_runs.select()
                .where(merchant_audit_runs.c.merchant_id == merchant_id)
                .order_by(merchant_audit_runs.c.requested_at.desc())
                .limit(1)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scheduled_audit_job: most-recent lookup failed for %s: %s",
                merchant_id, exc,
            )
            continue

        last_at = (
            most_recent["requested_at"]
            if most_recent and most_recent.get("requested_at")
            else None
        )
        # Idempotency belt: even if schedule says due, skip if audit
        # happened within the last 23h. Avoids stacked misfire double-
        # runs.
        if last_at is not None and (now_utc - last_at) < min_gap:
            continue
        if not is_audit_due(last_at, schedule, now=now_utc):
            continue
        out.append({
            "merchant_id": merchant_id,
            "schedule": schedule,
            "last_audit_at": last_at,
            "last_audit_run_id": (
                str(most_recent["run_id"]) if most_recent else None
            ),
        })
    return out


async def _re_audit_merchant(due: Dict[str, Any]) -> Dict[str, Any]:
    """Run a re-audit for one due merchant. Returns a summary dict
    for logging. Catches all exceptions internally — never re-raises
    (the batch loop must continue across per-merchant failures)."""
    from db.database import database
    from db.merchant_audit_runs import (
        merchant_audit_runs,
        record_audit_run_started,
        record_audit_run_completed,
        recent_runs_for_merchant,
    )
    from services.agent_center_bd_report_service import run_brand_report

    merchant_id = due["merchant_id"]
    summary: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "schedule": due["schedule"],
        "status": "skipped",
        "reason": None,
    }

    # Extract the product set from the most-recent succeeded audit's
    # report_jsonb. Re-audit the same set so trend deltas are
    # apples-to-apples.
    if not due.get("last_audit_run_id"):
        summary["reason"] = "no prior succeeded audit; skipped"
        return summary
    try:
        prior = await database.fetch_one(
            merchant_audit_runs.select().where(
                merchant_audit_runs.c.run_id == due["last_audit_run_id"]
            )
        )
    except Exception as exc:  # noqa: BLE001
        summary["status"] = "error"
        summary["reason"] = f"prior fetch failed: {exc!r}"
        return summary

    if not prior or not prior.get("report_jsonb"):
        summary["reason"] = "prior report_jsonb missing"
        return summary

    products = _extract_products_from_prior_report(prior["report_jsonb"])
    if not products:
        summary["reason"] = "no products extracted from prior report"
        return summary

    # Run the audit lifecycle the same way the HTTP handler does.
    product_keys = [
        p.get("product_key") or p.get("pdp_url") or p.get("title")
        for p in products
    ]
    run_id = await record_audit_run_started(
        merchant_id=merchant_id,
        product_keys=[k for k in product_keys if k],
    )
    prior_runs = await recent_runs_for_merchant(merchant_id=merchant_id, limit=5)
    if run_id:
        prior_runs = [r for r in prior_runs if r.get("run_id") != run_id]

    try:
        out = await run_brand_report(
            merchant_name=str(merchant_id),  # actual name lookup is caller-side; merchant_id is fine for cron
            merchant_domain=None,
            products=products,
            provider="gemini",
            max_runs=3,
            prior_runs=prior_runs,
            integration_state=None,  # cron has no integration context; engine handles None
        )
    except Exception as exc:  # noqa: BLE001
        await record_audit_run_completed(
            run_id=run_id, status="failed", error_message=str(exc)[:2000],
        )
        summary["status"] = "error"
        summary["reason"] = f"run_brand_report failed: {exc!r}"
        return summary

    agg = out.get("aggregate") or {}
    verdict_labels = [
        ((p.get("verdict") or {}).get("label") or "")
        for p in (out.get("per_product") or [])
    ]
    await record_audit_run_completed(
        run_id=run_id,
        status="succeeded",
        verdict_labels=[v for v in verdict_labels if v],
        visibility_score_avg=agg.get("avg_visibility"),
        attribution_score_avg=agg.get("avg_attribution"),
        category_visibility_score_avg=agg.get("avg_category_visibility"),
        report_jsonb=out,
    )
    summary["status"] = "succeeded"
    summary["run_id"] = run_id
    summary["visibility"] = agg.get("avg_visibility")
    summary["attribution"] = agg.get("avg_attribution")
    return summary


def _extract_products_from_prior_report(
    report_jsonb: Any,
) -> List[Dict[str, Any]]:
    """Pull the product list out of the cached report_jsonb so we can
    re-audit the same set. Tolerates the various shapes the audit
    pipeline has emitted across versions.
    """
    if not isinstance(report_jsonb, dict):
        return []
    per_product = report_jsonb.get("per_product")
    if not isinstance(per_product, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in per_product:
        if not isinstance(entry, dict):
            continue
        product = entry.get("product") or {}
        title = product.get("title")
        pdp_url = entry.get("merchant_pdp_url")
        if not (title and pdp_url):
            continue
        out.append({
            "title": title,
            "pdp_url": pdp_url,
            "vendor": product.get("vendor"),
            "product_type": product.get("product_type"),
        })
    return out


async def run_scheduled_audits() -> Dict[str, Any]:
    """Cron entry point. Returns a summary dict for the structured log
    line (also re-emitted at the top of the function for ops visibility).
    """
    started_at = datetime.now(timezone.utc)
    logger.info("scheduled_audit_job: tick started at %s", started_at.isoformat())
    due = await _list_due_merchants()
    if not due:
        logger.info("scheduled_audit_job: no merchants due")
        return {
            "started_at": started_at.isoformat(),
            "due_count": 0,
            "completed": [],
        }
    logger.info("scheduled_audit_job: %d merchants due", len(due))

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_AUDITS)

    async def _bounded(item: Dict[str, Any]) -> Dict[str, Any]:
        async with semaphore:
            return await _re_audit_merchant(item)

    results = await asyncio.gather(
        *[_bounded(d) for d in due], return_exceptions=True,
    )
    completed: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            completed.append({"status": "error", "reason": repr(r)})
        else:
            completed.append(r)
    succeeded = sum(1 for c in completed if c.get("status") == "succeeded")
    errored = sum(1 for c in completed if c.get("status") == "error")
    skipped = sum(1 for c in completed if c.get("status") == "skipped")
    logger.info(
        "scheduled_audit_job: tick complete due=%d succeeded=%d errored=%d skipped=%d",
        len(due), succeeded, errored, skipped,
    )
    return {
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "due_count": len(due),
        "succeeded": succeeded,
        "errored": errored,
        "skipped": skipped,
        "completed": completed,
    }
