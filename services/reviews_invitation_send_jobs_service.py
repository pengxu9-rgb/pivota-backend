from __future__ import annotations

import os
from typing import Any, Dict, Optional

from db.database import database
from services.buyer_reviews_service import buyer_submit_enabled, buyer_submit_merchant_allowed


def invitation_send_delay_seconds() -> int:
    raw = (os.getenv("REVIEWS_INVITATION_SEND_DELAY_SECONDS") or "").strip()
    try:
        val = int(raw) if raw else 0
    except Exception:
        val = 0
    return max(0, val)


def invitation_worker_enabled() -> bool:
    raw = (os.getenv("REVIEWS_INVITATION_WORKER_ENABLED") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


async def ensure_invitation_send_jobs_table_best_effort() -> None:
    """
    Best-effort schema ensure for delayed invitation email jobs.
    Keeps prod usable even if migrations are not applied yet.
    """
    try:
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews_invitation_send_jobs (
              id BIGSERIAL PRIMARY KEY,
              merchant_id VARCHAR(64) NOT NULL,
              order_id VARCHAR(64) NOT NULL,
              send_at TIMESTAMPTZ NOT NULL,
              status VARCHAR(32) NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              last_attempt_at TIMESTAMPTZ NULL,
              sent_at TIMESTAMPTZ NULL,
              sendgrid_message_id TEXT NULL,
              last_error TEXT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    except Exception:
        return

    try:
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_reviews_invitation_send_jobs_due ON reviews_invitation_send_jobs (status, send_at)"
        )
    except Exception:
        pass

    try:
        await database.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_reviews_invitation_send_jobs_order_status ON reviews_invitation_send_jobs (order_id, status)"
        )
    except Exception:
        pass


async def enqueue_invitation_send_job_from_order(
    *,
    merchant_id: str,
    order_id: str,
    force_reschedule: bool = False,
) -> Dict[str, Any]:
    """
    Enqueue a delayed invitation email send job for an order (idempotent).

    Returns a small status dict for logging/debugging. Callers should treat this
    as best-effort and must not block order flows on failure.
    """
    mid = (merchant_id or "").strip()
    oid = (order_id or "").strip()
    if not mid or not oid:
        return {"ok": False, "reason": "MISSING_INPUT"}

    delay = invitation_send_delay_seconds()
    if delay <= 0 and not invitation_worker_enabled():
        return {"ok": False, "reason": "DISABLED"}

    if not buyer_submit_enabled():
        return {"ok": False, "reason": "BUYER_SUBMIT_DISABLED"}
    if not buyer_submit_merchant_allowed(mid):
        return {"ok": False, "reason": "BUYER_SUBMIT_NOT_ALLOWED"}

    await ensure_invitation_send_jobs_table_best_effort()

    try:
        existing = await database.fetch_one(
            """
            SELECT id, status
            FROM reviews_invitation_send_jobs
            WHERE order_id = :order_id AND status IN ('sent', 'processing', 'pending')
            ORDER BY
              CASE status
                WHEN 'sent' THEN 1
                WHEN 'processing' THEN 2
                WHEN 'pending' THEN 3
                ELSE 99
              END ASC
            LIMIT 1
            """,
            {"order_id": oid},
        )
        if existing:
            st = str(existing.get("status") or "").strip()
            if st == "sent":
                return {"ok": False, "reason": "ALREADY_SENT"}
            if st == "processing":
                return {"ok": True, "reason": "ALREADY_PROCESSING", "job_id": int(existing.get("id") or 0)}
            if st == "pending":
                if not force_reschedule:
                    return {"ok": True, "reason": "ALREADY_PENDING", "job_id": int(existing.get("id") or 0)}
                # Reschedule pending job (best-effort).
                await database.execute(
                    """
                    UPDATE reviews_invitation_send_jobs
                    SET send_at = NOW() + (:delay * interval '1 second'),
                        updated_at = NOW()
                    WHERE id = :id AND status = 'pending'
                    """,
                    {"id": int(existing.get("id") or 0), "delay": int(delay)},
                )
                return {"ok": True, "reason": "RESCHEDULED", "job_id": int(existing.get("id") or 0)}
    except Exception:
        pass

    try:
        await database.execute(
            """
            INSERT INTO reviews_invitation_send_jobs
              (merchant_id, order_id, send_at, status, updated_at)
            VALUES
              (:merchant_id, :order_id, NOW() + (:delay * interval '1 second'), 'pending', NOW())
            ON CONFLICT (order_id, status)
            DO UPDATE SET
              send_at = EXCLUDED.send_at,
              updated_at = NOW()
            """,
            {"merchant_id": mid, "order_id": oid, "delay": int(delay)},
        )
        return {"ok": True, "reason": "ENQUEUED"}
    except Exception:
        return {"ok": False, "reason": "DB_ERROR"}

