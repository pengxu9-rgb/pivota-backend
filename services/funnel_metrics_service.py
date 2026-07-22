"""Audit-growth-funnel metrics (WS-4, plan §7).

Server-derivable stages of the registration-first funnel, windowed:

  2. URL submitted -> registration completed   (signup_source attribution)
  3. registration  -> first audit run          (activation)
  4. free          -> paid                     (subscriptions created)
  5. time-to-first-value                       (registration -> first
                                                succeeded report, minutes)

Stage 1 (marketing visitor -> URL submitted) is client-side analytics on
pivota.cc and is not tracked here.

All SQL is deliberately portable (plain WHERE/GROUP BY/HAVING — no FILTER,
no percentile_cont) because CI runs SQLite; medians and joins across the
three domains happen in Python on the windowed cohort, which is small.
"""

from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional

from db.database import database

AUDIT_FUNNEL_SOURCE = "ai-readiness-audit"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _as_utc(value: Any) -> Optional[datetime]:
    """Rows come back tz-aware datetimes from Postgres but ISO STRINGS (and
    naive) from SQLite/aiosqlite; normalize both to tz-aware UTC so the
    arithmetic never mixes types."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


async def compute_funnel_metrics(
    *,
    since: datetime,
    until: datetime,
    free_audit_cap: Optional[int] = None,
    free_count_since: Optional[datetime] = None,
) -> Dict[str, Any]:
    """`free_audit_cap` / `free_count_since` mirror the REAL allowance gate
    (FREE_URL_AUDITS_PER_MERCHANT / FREE_AUDIT_COUNT_SINCE) — the route
    passes the live values so quota.exhausted tracks the gate that actually
    402s merchants, not a hardcoded approximation. cap None/<=0 = allowance
    disabled -> exhausted reports 0."""
    params = {"since": since, "until": until}

    # -- Stage 2: registrations in window, attributed --------------------------
    reg_rows = await database.fetch_all(
        "SELECT merchant_id, signup_source, created_at "
        "FROM merchant_onboarding "
        "WHERE created_at >= :since AND created_at < :until "
        "AND status != 'deleted'",
        params,
    )
    registrations = [dict(r) for r in reg_rows]
    by_source: Dict[str, int] = {}
    for r in registrations:
        key = r.get("signup_source") or "(none)"
        by_source[key] = by_source.get(key, 0) + 1
    cohort_ids = [r["merchant_id"] for r in registrations if r.get("merchant_id")]
    funnel_cohort_ids = {
        r["merchant_id"]
        for r in registrations
        if r.get("signup_source") == AUDIT_FUNNEL_SOURCE and r.get("merchant_id")
    }

    # -- Stage 3 + 5: first URL-audit run per cohort merchant ------------------
    # One bounded fetch for the cohort's runs; first-run/first-success reduce
    # in Python (portable, and the windowed cohort is small).
    first_started: Dict[str, datetime] = {}
    first_succeeded: Dict[str, datetime] = {}
    if cohort_ids:
        placeholders = ", ".join(f":c{i}" for i in range(len(cohort_ids)))
        run_rows = await database.fetch_all(
            "SELECT merchant_id, status, requested_at, completed_at "
            "FROM merchant_audit_runs "
            "WHERE subject_type = 'merchant_url' "
            "AND requested_at >= :since AND requested_at < :until "
            f"AND merchant_id IN ({placeholders})",
            {"since": since, "until": until,
             **{f"c{i}": mid for i, mid in enumerate(cohort_ids)}},
        )
        for row in run_rows:
            d = dict(row)
            mid = d.get("merchant_id")
            if not mid:
                continue
            started = _as_utc(d.get("requested_at"))
            if started and (mid not in first_started or started < first_started[mid]):
                first_started[mid] = started
            if d.get("status") == "succeeded":
                done = _as_utc(d.get("completed_at")) or started
                if done and (mid not in first_succeeded or done < first_succeeded[mid]):
                    first_succeeded[mid] = done

    reg_created: Dict[str, datetime] = {
        r["merchant_id"]: _as_utc(r.get("created_at"))
        for r in registrations
        if r.get("merchant_id")
    }
    ttfv_minutes: List[float] = []
    for mid, done in first_succeeded.items():
        created = reg_created.get(mid)
        if created and done >= created:
            ttfv_minutes.append(round((done - created).total_seconds() / 60, 1))

    # -- Free-allowance exhaustion (candidates for the 402 -> upgrade path) ----
    # Mirrors count_runs_for_merchant_by_subject: counts since the gate's
    # FREE_AUDIT_COUNT_SINCE cutoff (not the report window), bounded at
    # `until` so historical windows stay reproducible.
    exhausted_ids: set = set()
    if free_audit_cap and free_audit_cap > 0:
        gate_since = free_count_since or datetime(1970, 1, 1, tzinfo=timezone.utc)
        exhausted_rows = await database.fetch_all(
            "SELECT merchant_id, COUNT(*) AS runs "
            "FROM merchant_audit_runs "
            "WHERE subject_type = 'merchant_url' AND status != 'failed' "
            "AND requested_at >= :gate_since AND requested_at < :until "
            "GROUP BY merchant_id HAVING COUNT(*) >= :cap",
            {"gate_since": gate_since, "until": until, "cap": free_audit_cap},
        )
        exhausted_ids = {dict(r)["merchant_id"] for r in exhausted_rows}

    # -- Stage 4: subscriptions created in window ------------------------------
    sub_rows = await database.fetch_all(
        "SELECT merchant_id, status, created_at "
        "FROM user_subscriptions "
        "WHERE created_at >= :since AND created_at < :until "
        "AND status IN ('active', 'trialing')",
        params,
    )
    subscriptions = [dict(r) for r in sub_rows]
    upgraded_ids = {s["merchant_id"] for s in subscriptions if s.get("merchant_id")}
    upgraded_from_funnel = len(upgraded_ids & funnel_cohort_ids)

    total = len(registrations)
    funnel_total = len(funnel_cohort_ids)
    started_total = len(first_started)
    succeeded_total = len(first_succeeded)
    started_funnel = len(funnel_cohort_ids & set(first_started))

    return {
        "window": {"since": _iso(since), "until": _iso(until)},
        "registrations": {
            "total": total,
            "audit_funnel": funnel_total,
            "by_source": by_source,
        },
        "activation": {
            "cohort_first_audit_started": started_total,
            "cohort_first_audit_succeeded": succeeded_total,
            "funnel_cohort_first_audit_started": started_funnel,
        },
        "quota": {
            "allowance_enabled": bool(free_audit_cap and free_audit_cap > 0),
            "free_audit_cap": free_audit_cap,
            "merchants_free_allowance_exhausted": len(exhausted_ids),
        },
        "upgrades": {
            "subscriptions_created": len(upgraded_ids),
            "from_audit_funnel_cohort": upgraded_from_funnel,
        },
        "time_to_first_value_minutes": {
            "n": len(ttfv_minutes),
            "avg": round(sum(ttfv_minutes) / len(ttfv_minutes), 1)
            if ttfv_minutes
            else None,
            "p50": round(median(ttfv_minutes), 1) if ttfv_minutes else None,
        },
        "conversion": {
            "registration_to_first_audit": _rate(started_total, total),
            "first_audit_to_succeeded_report": _rate(succeeded_total, started_total),
            "registration_to_paid": _rate(len(upgraded_ids & set(cohort_ids)), total),
        },
        "notes": [
            "Stage 1 (marketing visitor -> URL submitted) is client-side "
            "analytics on pivota.cc and is not tracked server-side.",
            "quota.exhausted mirrors the live allowance gate (cap + "
            "FREE_AUDIT_COUNT_SINCE cutoff), counted up to `until` — the "
            "candidates for the 402 upgrade path. 0 when the allowance is "
            "disabled.",
        ],
    }
