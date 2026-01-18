#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import Response

from db.database import IS_POSTGRES, database
from routes.reviews_invitation_issuer import (
    SendInvitationEmailFromOrderRequest,
    _internal_key as _invitation_internal_key,
    _invitation_send_delay_seconds,
    _ensure_invitation_send_jobs_table_best_effort,
    send_invitation_email_from_order,
)


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        v = int(raw) if raw else default
    except Exception:
        v = default
    return v


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _reset_stale_processing(*, stale_seconds: int) -> int:
    cutoff = _now() - timedelta(seconds=int(stale_seconds))
    try:
        return await database.execute(
            """
            UPDATE reviews_invitation_send_jobs
            SET status='pending', updated_at=NOW()
            WHERE status='processing' AND last_attempt_at IS NOT NULL AND last_attempt_at < :cutoff
            """,
            {"cutoff": cutoff},
        )
    except Exception:
        return 0


async def _claim_due_jobs(*, limit: int) -> List[Dict[str, Any]]:
    if IS_POSTGRES:
        rows = await database.fetch_all(
            """
            WITH due AS (
              SELECT id
              FROM reviews_invitation_send_jobs
              WHERE status='pending' AND send_at <= NOW()
              ORDER BY send_at ASC
              LIMIT :limit
              FOR UPDATE SKIP LOCKED
            )
            UPDATE reviews_invitation_send_jobs j
            SET status='processing',
                attempts = attempts + 1,
                last_attempt_at = NOW(),
                updated_at = NOW()
            FROM due
            WHERE j.id = due.id
            RETURNING j.*
            """,
            {"limit": int(limit)},
        )
        return [dict(r) for r in rows]

    # Fallback (non-Postgres): best-effort claim without SKIP LOCKED.
    rows = await database.fetch_all(
        """
        SELECT *
        FROM reviews_invitation_send_jobs
        WHERE status='pending' AND send_at <= CURRENT_TIMESTAMP
        ORDER BY send_at ASC
        LIMIT :limit
        """,
        {"limit": int(limit)},
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        updated = await database.execute(
            """
            UPDATE reviews_invitation_send_jobs
            SET status='processing', attempts = attempts + 1, last_attempt_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id AND status='pending'
            """,
            {"id": row["id"]},
        )
        if updated:
            out.append(row)
    return out


async def _mark_sent(*, job_id: int, sendgrid_message_id: Optional[str]) -> None:
    try:
        await database.execute(
            """
            UPDATE reviews_invitation_send_jobs
            SET status='sent',
                sent_at=NOW(),
                updated_at=NOW(),
                sendgrid_message_id=:msg
            WHERE id=:id
            """,
            {"id": int(job_id), "msg": (sendgrid_message_id or "").strip() or None},
        )
    except Exception:
        return


async def _mark_retry_or_error(*, job_id: int, attempts: int, error: str, max_attempts: int) -> None:
    err = (error or "")[:512]
    if attempts >= max_attempts:
        try:
            await database.execute(
                """
                UPDATE reviews_invitation_send_jobs
                SET status='error', last_error=:err, updated_at=NOW()
                WHERE id=:id
                """,
                {"id": int(job_id), "err": err},
            )
        except Exception:
            return
        return

    # Backoff: 60s, 5m, 15m, 30m, 60m...
    backoff = min(3600, int(60 * (attempts**2)))
    try:
        await database.execute(
            """
            UPDATE reviews_invitation_send_jobs
            SET status='pending',
                last_error=:err,
                send_at=NOW() + (:backoff || ' seconds')::interval,
                updated_at=NOW()
            WHERE id=:id
            """,
            {"id": int(job_id), "err": err, "backoff": int(backoff)},
        )
    except Exception:
        return


async def _process_one(job: Dict[str, Any], *, internal_key: str, max_attempts: int) -> Dict[str, Any]:
    job_id = int(job.get("id") or 0)
    merchant_id = str(job.get("merchant_id") or "").strip()
    order_id = str(job.get("order_id") or "").strip()
    attempts = int(job.get("attempts") or 0)

    try:
        resp = await send_invitation_email_from_order(
            body=SendInvitationEmailFromOrderRequest(
                merchant_id=merchant_id,
                order_id=order_id,
                ttl_seconds=7 * 24 * 3600,
            ),
            response=Response(),
            x_internal_key=internal_key,
        )
        sendgrid_message_id = None
        if isinstance(resp, dict):
            sendgrid_message_id = str(resp.get("sendgrid_message_id") or "").strip() or None
        await _mark_sent(job_id=job_id, sendgrid_message_id=sendgrid_message_id)
        return {"id": job_id, "result": "sent"}
    except Exception as e:
        await _mark_retry_or_error(job_id=job_id, attempts=attempts, error=str(e), max_attempts=max_attempts)
        return {"id": job_id, "result": "error"}


async def main() -> int:
    if not (os.getenv("DATABASE_URL") or "").strip():
        print("error=missing_DATABASE_URL")
        return 2

    await database.connect()
    try:
        await _ensure_invitation_send_jobs_table_best_effort()

        internal_key = (_invitation_internal_key() or "").strip()
        if not internal_key:
            print("disabled=1 reason=missing_internal_key")
            return 0

        delay = _invitation_send_delay_seconds()
        worker_enabled = (os.getenv("REVIEWS_INVITATION_WORKER_ENABLED") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if delay <= 0 and not worker_enabled:
            print("disabled=1 reason=delay_disabled")
            return 0

        limit = _env_int("REVIEWS_INVITATION_JOB_BATCH_SIZE", 10)
        max_attempts = _env_int("REVIEWS_INVITATION_JOB_MAX_ATTEMPTS", 5)
        stale_seconds = _env_int("REVIEWS_INVITATION_JOB_STALE_SECONDS", 1800)

        await _reset_stale_processing(stale_seconds=stale_seconds)
        jobs = await _claim_due_jobs(limit=limit)

        processed: List[Dict[str, Any]] = []
        for job in jobs:
            processed.append(await _process_one(job, internal_key=internal_key, max_attempts=max_attempts))

        print(f"ok=1 claimed={len(jobs)} processed={len(processed)}")
        return 0
    finally:
        try:
            await database.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
