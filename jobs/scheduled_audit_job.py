"""PR-1b: scheduled re-audit job.

Fires once a day (03:00 UTC, see services/audit_scheduler.py). Picks
up merchants opted into Agent Presence Monitoring and runs the full
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
import os
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


# Legacy schedule → minimum days between auto-re-audits.
_INTERVAL_DAYS = {
    "weekly": 7,
    "monthly": 30,
}

_APM_CADENCE_DAYS = {7, 14, 30}


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
    now_utc = _as_utc(now if now is not None else datetime.now(timezone.utc))
    interval = timedelta(days=_INTERVAL_DAYS[schedule])
    return (now_utc - _as_utc(last_audit_at)) >= interval


def is_apm_audit_due(
    *,
    apm_enabled: bool,
    cadence_days: Optional[int],
    apm_last_run_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> bool:
    """Return whether a merchant's APM config is due for cron pickup."""
    if not apm_enabled:
        return False
    if cadence_days not in _APM_CADENCE_DAYS:
        return False
    if apm_last_run_at is None:
        return True
    now_utc = now if now is not None else datetime.now(timezone.utc)
    return (_as_utc(now_utc) - _as_utc(apm_last_run_at)) >= timedelta(
        days=cadence_days,
    )


async def _list_due_merchants() -> List[Dict[str, Any]]:
    """Query merchant_onboarding for APM-enabled merchants due for audit.

    Returns list of `{merchant_id, cadence_days, last_audit_at}`.
    """
    from db.database import database
    from db.merchant_audit_runs import merchant_audit_runs
    from db.merchant_onboarding import merchant_onboarding

    try:
        apm_rows = await database.fetch_all(
            select(
                merchant_onboarding.c.merchant_id,
                merchant_onboarding.c.apm_enabled,
                merchant_onboarding.c.apm_cadence_days,
                merchant_onboarding.c.apm_last_run_at,
            ).where(
                merchant_onboarding.c.apm_configured_at.isnot(None),
                merchant_onboarding.c.apm_enabled.is_(True),
                merchant_onboarding.c.status != "deleted",
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scheduled_audit_job: APM config query failed: %s", exc,
        )
        return []

    if not apm_rows:
        return []

    out: List[Dict[str, Any]] = []
    now_utc = datetime.now(timezone.utc)
    min_gap = timedelta(hours=_MIN_HOURS_BETWEEN_AUDITS)

    for raw_row in apm_rows:
        row = dict(raw_row)
        merchant_id = row["merchant_id"]
        cadence_days = row.get("apm_cadence_days")
        apm_last_run_at = row.get("apm_last_run_at")
        if not is_apm_audit_due(
            apm_enabled=bool(row.get("apm_enabled")),
            cadence_days=cadence_days,
            apm_last_run_at=apm_last_run_at,
            now=now_utc,
        ):
            continue
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

        most_recent_data = dict(most_recent) if most_recent else None
        last_at = (
            most_recent_data["requested_at"]
            if most_recent_data and most_recent_data.get("requested_at")
            else None
        )
        # Idempotency belt: even if schedule says due, skip if audit
        # happened within the last 23h. Avoids stacked misfire double-
        # runs.
        if last_at is not None and (now_utc - _as_utc(last_at)) < min_gap:
            continue
        out.append({
            "merchant_id": merchant_id,
            "cadence_days": cadence_days,
            "last_audit_at": apm_last_run_at,
            "last_audit_run_id": (
                str(most_recent_data["run_id"]) if most_recent_data else None
            ),
        })
    return out


def _advisory_lock_id_for_merchant(merchant_id: str) -> int:
    """Stable signed-64-bit advisory lock id derived from merchant_id.

    Postgres advisory locks take a single int8 (or two int4s). Hash
    the merchant_id with SHA-256, take the first 8 bytes as a
    signed int — collisions are practically impossible at our
    merchant count.
    """
    import hashlib
    digest = hashlib.sha256(merchant_id.encode("utf-8")).digest()
    raw = int.from_bytes(digest[:8], byteorder="big", signed=True)
    return raw


async def _try_acquire_scheduler_lock(merchant_id: str) -> bool:
    """P1-5: leader-election for per-merchant scheduled audits.

    pg_try_advisory_lock is non-blocking: returns true if THIS
    session acquired the lock, false if another session holds it.
    Two pods racing at 03:00 UTC on the same due merchant: one wins,
    the other returns False and skips.

    Returns False on non-Postgres backends (SQLite tests) — those
    are single-pod by definition, no leader-election needed.
    """
    from db.database import database
    db_url = str(getattr(database, "url", "") or "")
    if not db_url.startswith(("postgres://", "postgresql://")):
        # Non-Postgres backend — no advisory-lock semantics.
        # Assume single-pod (test / sqlite) and proceed.
        return True
    try:
        row = await database.fetch_one(
            "SELECT pg_try_advisory_lock(:lock_id) AS got",
            {"lock_id": _advisory_lock_id_for_merchant(merchant_id)},
        )
        return bool(row and row["got"])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scheduled_audit_job: advisory-lock attempt failed for "
            "merchant_id=%s (treating as unlocked to fail open): %s",
            merchant_id, str(exc)[:200],
        )
        # Fail open — we'd rather double-audit than block all
        # scheduled audits on a transient lock-system failure.
        return True


async def _release_scheduler_lock(merchant_id: str) -> None:
    """Release the per-merchant advisory lock. Idempotent — Postgres
    silently ignores releases for locks not held."""
    from db.database import database
    db_url = str(getattr(database, "url", "") or "")
    if not db_url.startswith(("postgres://", "postgresql://")):
        return
    try:
        await database.execute(
            "SELECT pg_advisory_unlock(:lock_id)",
            {"lock_id": _advisory_lock_id_for_merchant(merchant_id)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scheduled_audit_job: advisory-unlock failed for "
            "merchant_id=%s: %s", merchant_id, str(exc)[:200],
        )


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
        "cadence_days": due.get("cadence_days"),
        "status": "skipped",
        "reason": None,
    }

    # P1-5: per-merchant advisory lock keeps two pods from running
    # the same scheduled audit at the same time. Codex P1 #6
    # ("Multiple API pods can all pick the same due merchant at
    # 03:00 UTC and run duplicate audits"). Lock is released in the
    # finally below regardless of audit outcome.
    locked = await _try_acquire_scheduler_lock(merchant_id)
    if not locked:
        summary["reason"] = (
            "another pod holds the scheduler advisory lock; skipped"
        )
        return summary

    try:
        return await _re_audit_merchant_locked(
            due=due, summary=summary,
            database=database,
            merchant_audit_runs=merchant_audit_runs,
            record_audit_run_started=record_audit_run_started,
            record_audit_run_completed=record_audit_run_completed,
            recent_runs_for_merchant=recent_runs_for_merchant,
            run_brand_report=run_brand_report,
        )
    finally:
        await _release_scheduler_lock(merchant_id)


async def _re_audit_merchant_locked(
    *,
    due: Dict[str, Any],
    summary: Dict[str, Any],
    database,
    merchant_audit_runs,
    record_audit_run_started,
    record_audit_run_completed,
    recent_runs_for_merchant,
    run_brand_report,
) -> Dict[str, Any]:
    """Body of _re_audit_merchant, called inside the advisory-lock
    guard. Extracted so the lock/unlock pair sits in a single
    try/finally regardless of which early-return path the body
    takes (no-prior-audit, prior-fetch-failed, no-products, etc.)."""
    merchant_id = due["merchant_id"]
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

    prior_data = dict(prior) if prior else None
    if not prior_data or not prior_data.get("report_jsonb"):
        summary["reason"] = "prior report_jsonb missing"
        return summary

    products = _extract_products_from_prior_report(prior_data["report_jsonb"])
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

    # P1-3: scope every probe call inside this scheduled audit with
    # audit_run_id + merchant_id so probe telemetry rows attribute
    # to the right run / merchant. Pre-fix, scheduled audits ran
    # outside any audit_telemetry context — daily cron traffic was
    # invisible to per-merchant cost accounting and per-run cost
    # rollups. The async worker already wraps via audit_run_worker;
    # this brings the cron path up to parity.
    from services.audit_telemetry_context import audit_telemetry
    try:
        async with audit_telemetry(
            run_id=run_id, merchant_id=merchant_id,
        ):
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
    try:
        from db.apm_config import mark_apm_audit_run_completed

        await mark_apm_audit_run_completed(merchant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scheduled_audit_job: apm_last_run_at update failed for %s: %s",
            merchant_id,
            exc,
        )
    summary["status"] = "succeeded"
    # Wave-3 B1: change-digest email (env-flagged, best-effort — a mail
    # failure must never mark the audit errored). Content = the same
    # reaudit_delta the report attaches: honest movements, no re-derivation.
    try:
        await _send_apm_digest_email(merchant_id=merchant_id, report=out)
    except Exception:  # noqa: BLE001
        logger.warning("apm digest email failed for %s", merchant_id, exc_info=True)
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


_APM_DIGEST_EMAIL_ENABLED = (
    os.getenv("APM_DIGEST_EMAIL_ENABLED", "false").strip().lower() == "true"
)


async def _send_apm_digest_email(*, merchant_id: str, report: Any) -> None:
    """Wave-3 B1: after a SCHEDULED re-audit, mail the merchant what changed.

    Copy is built verbatim from the report's reaudit_delta (headline +
    material movements) — the same honest layer the portal renders; nothing
    is re-derived or embellished for the email. Env-flagged off by default;
    silently skipped when the merchant has no contact email."""
    if not _APM_DIGEST_EMAIL_ENABLED or not isinstance(report, dict):
        return
    from db.database import database
    from utils.email_sender import send_email

    row = await database.fetch_one(
        "SELECT contact_email FROM merchant_onboarding WHERE merchant_id = :m",
        {"m": merchant_id},
    )
    to_email = (row and row["contact_email"] or "").strip()
    if not to_email:
        return
    # Scheduled runs produce the legacy shape (delta under merchant_view);
    # per-SKU runs attach it top-level. Read both, never re-derive.
    delta = (
        report.get("reaudit_delta")
        or (report.get("merchant_view") or {}).get("reaudit_delta")
        or {}
    )
    headline = str(delta.get("headline") or "Your scheduled AI-visibility re-audit finished.")
    lines = [headline, ""]
    for move in (delta.get("movements") or []):
        if not isinstance(move, dict) or not move.get("is_material"):
            continue
        arrow = "up" if move.get("direction") == "improved" else "down"
        lines.append(
            f"- {move.get('label')}: {move.get('from')} -> {move.get('to')} ({arrow})"
        )
    if len(lines) == 2:
        lines.append("- No material movement since your last audit.")
    lines += [
        "",
        "Open the full report: https://merchant.pivota.cc/dashboard/agent-center/url-audit",
        "",
        "You get this because weekly re-audits are enabled for your account. "
        "Turn them off any time from the AI visibility page.",
    ]
    send_email(
        to_email=to_email,
        subject="Your weekly AI-visibility check: " + headline[:80],
        text_body="\n".join(lines),
        tags={"kind": "apm_digest", "merchant_id": merchant_id},
    )


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
