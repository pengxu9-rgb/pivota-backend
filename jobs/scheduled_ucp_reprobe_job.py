"""Domain/TTL-based Store Audit UCP re-probe scheduler.

This is intentionally separate from ``scheduled_audit_job``: it neither
requires Agent Presence Monitoring nor creates a full merchant audit. It
looks only at known execution routes, then queues a remote ``ucp_probe`` job
against the canonical audit run that last observed that route.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_

logger = logging.getLogger(__name__)

_DEFAULT_TTL_HOURS = 24 * 7
_MAX_TTL_HOURS = 24 * 30
_DEFAULT_BATCH_SIZE = 25
_MAX_BATCH_SIZE = 100


def _enabled() -> bool:
    scheduler_enabled = (
        os.getenv("STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED", "false")
        .strip().lower() == "true"
    )
    receipt_enabled = (
        os.getenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "false")
        .strip().lower() == "true"
    )
    receipt_key = str(os.getenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY") or "").strip()
    return scheduler_enabled and receipt_enabled and bool(receipt_key)


def _bounded_int(value: Optional[str], default: int, maximum: int) -> int:
    try:
        parsed = int(str(value or default))
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def reprobe_ttl_hours() -> int:
    return _bounded_int(
        os.getenv("STORE_AUDIT_UCP_REPROBE_TTL_HOURS"),
        _DEFAULT_TTL_HOURS,
        _MAX_TTL_HOURS,
    )


def reprobe_batch_size() -> int:
    return _bounded_int(
        os.getenv("STORE_AUDIT_UCP_REPROBE_BATCH_SIZE"),
        _DEFAULT_BATCH_SIZE,
        _MAX_BATCH_SIZE,
    )


def route_reprobe_idempotency_key(
    *, execution_route_id: str, scheduled_at: datetime,
) -> str:
    """One deterministic enqueue key per route per UTC day."""
    bucket = scheduled_at.astimezone(timezone.utc).date().isoformat()
    raw = f"ucp_route_reprobe|{execution_route_id}|{bucket}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def list_due_ucp_routes(
    *,
    now: Optional[datetime] = None,
    ttl_hours: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return active UCP routes due by expiry or last-verification TTL."""
    from db.audit_evidence import ensure_audit_evidence_tables, execution_routes
    from db.database import database

    now_utc = now or datetime.now(timezone.utc)
    ttl = ttl_hours if ttl_hours is not None else reprobe_ttl_hours()
    capped = limit if limit is not None else reprobe_batch_size()
    cutoff = now_utc - timedelta(hours=max(1, ttl))
    try:
        await ensure_audit_evidence_tables()
        rows = await database.fetch_all(
            execution_routes.select()
            .where(
                execution_routes.c.route_kind == "ucp",
                execution_routes.c.is_active.is_(True),
                execution_routes.c.last_audit_run_id.isnot(None),
                or_(
                    execution_routes.c.expires_at <= now_utc,
                    execution_routes.c.last_verified_at.is_(None),
                    execution_routes.c.last_verified_at <= cutoff,
                ),
            )
            .order_by(
                execution_routes.c.expires_at.asc().nullsfirst(),
                execution_routes.c.last_verified_at.asc().nullsfirst(),
            )
            .limit(max(1, min(int(capped), _MAX_BATCH_SIZE)))
        )
        return [dict(row) for row in rows or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduled_ucp_reprobe: due-route query failed: %s", exc)
        return []


async def run_scheduled_ucp_reprobes() -> Dict[str, Any]:
    """Queue a bounded batch of due routes; default-off until rollout review."""
    if not _enabled():
        return {"enabled": False, "due": 0, "enqueued": 0, "deduped": 0, "failed": 0}

    from db.audit_evidence import (
        VERIFIER_UCP_PROBE,
        enqueue_verification_run,
        has_in_flight_verification_for_route,
    )

    now = datetime.now(timezone.utc)
    due = await list_due_ucp_routes(now=now)
    summary = {"enabled": True, "due": len(due), "enqueued": 0, "deduped": 0, "failed": 0}
    for route in due:
        route_id = str(route.get("execution_route_id") or "")
        audit_run_id = str(route.get("last_audit_run_id") or "")
        if not route_id or not audit_run_id:
            summary["failed"] += 1
            continue
        if await has_in_flight_verification_for_route(
            execution_route_id=route_id,
            verifier_id=VERIFIER_UCP_PROBE,
        ):
            summary["deduped"] += 1
            continue
        merchant_id = route.get("merchant_id")
        if merchant_id and str(merchant_id).startswith("prospect_"):
            merchant_id = None
        try:
            verify_id = await enqueue_verification_run(
                audit_run_id=audit_run_id,
                verifier_id=VERIFIER_UCP_PROBE,
                merchant_id=merchant_id,
                execution_route_id=route_id,
                # A one-attempt UCP discovery is intentionally cheap and
                # terminal state handles WAF/rate limit as blocked.
                max_retries=1,
                idempotency_key=route_reprobe_idempotency_key(
                    execution_route_id=route_id,
                    scheduled_at=now,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scheduled_ucp_reprobe: enqueue failed route=%s: %s",
                route_id, str(exc)[:200],
            )
            verify_id = None
        if verify_id:
            summary["enqueued"] += 1
        else:
            summary["failed"] += 1
    logger.info("scheduled_ucp_reprobe: %s", summary)
    return summary
