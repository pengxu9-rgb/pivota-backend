"""DB accessors for the catalog-onboard queue (migration 158).

Idempotent enqueue (one live row per kind+dedup_key), FOR UPDATE SKIP LOCKED
claim (highest priority first), and completion with bounded retry. Mirrors the
audit/executor worker claim pattern so multiple drainers are safe.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def enqueue(
    *,
    kind: str,
    dedup_key: str,
    payload: Dict[str, Any],
    priority: int = 0,
    source: Optional[str] = None,
    max_attempts: int = 3,
    db: Any = None,
) -> Optional[str]:
    """Insert a pending item; idempotent — returns the new id, or None if an
    equivalent (kind, dedup_key) is already pending/processing (ON CONFLICT)."""
    if not kind or not dedup_key:
        return None
    write_db = db or database
    new_id = f"cobq_{uuid.uuid4().hex}"
    try:
        row = await write_db.fetch_one(
            """
            INSERT INTO catalog_onboard_queue
              (id, kind, dedup_key, payload, priority, source, max_attempts)
            VALUES
              (:id, :kind, :dedup_key, CAST(:payload AS jsonb), :priority, :source, :max_attempts)
            ON CONFLICT (kind, dedup_key) WHERE status IN ('pending','processing')
            DO NOTHING
            RETURNING id
            """,
            {
                "id": new_id,
                "kind": kind,
                "dedup_key": dedup_key,
                "payload": _dumps(payload),
                "priority": int(priority or 0),
                "source": source,
                "max_attempts": int(max_attempts or 3),
            },
        )
        return row["id"] if row else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("enqueue failed for %s/%s: %s", kind, dedup_key, str(exc)[:160])
        return None


async def claim_batch(limit: int = 10, *, db: Any = None) -> List[Dict[str, Any]]:
    """Atomically claim up to `limit` pending items (highest priority first),
    marking them processing + bumping attempts. SKIP LOCKED → drainer-safe."""
    read_db = db or database
    try:
        rows = await read_db.fetch_all(
            """
            UPDATE catalog_onboard_queue
            SET status = 'processing', claimed_at = NOW(),
                attempts = attempts + 1, updated_at = NOW()
            WHERE id IN (
                SELECT id FROM catalog_onboard_queue
                WHERE status = 'pending'
                ORDER BY priority DESC, created_at
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, kind, payload, priority, attempts, max_attempts, source
            """,
            {"limit": max(1, int(limit or 1))},
        )
        return [dict(r) for r in rows or []]
    except Exception as exc:  # noqa: BLE001
        logger.debug("claim_batch failed: %s", str(exc)[:200])
        return []


async def mark_done(item_id: str, *, result: Optional[Dict[str, Any]] = None, db: Any = None) -> None:
    write_db = db or database
    try:
        await write_db.execute(
            "UPDATE catalog_onboard_queue SET status='done', result_jsonb=CAST(:r AS jsonb), "
            "error=NULL, updated_at=NOW() WHERE id=:id",
            {"id": item_id, "r": _dumps(result or {})},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("mark_done failed for %s: %s", item_id, str(exc)[:160])


async def mark_skipped(item_id: str, *, reason: str = "", db: Any = None) -> None:
    write_db = db or database
    try:
        await write_db.execute(
            "UPDATE catalog_onboard_queue SET status='skipped', error=:e, updated_at=NOW() WHERE id=:id",
            {"id": item_id, "e": (reason or "")[:500]},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("mark_skipped failed for %s: %s", item_id, str(exc)[:160])


async def mark_failed(
    item_id: str, *, error: str, attempts: int, max_attempts: int, db: Any = None
) -> None:
    """Re-queue (status='pending') if retry budget remains, else 'failed'."""
    write_db = db or database
    final = int(attempts) >= int(max_attempts)
    try:
        await write_db.execute(
            "UPDATE catalog_onboard_queue SET status=:s, error=:e, claimed_at=NULL, updated_at=NOW() WHERE id=:id",
            {"id": item_id, "s": "failed" if final else "pending", "e": (error or "")[:500]},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("mark_failed failed for %s: %s", item_id, str(exc)[:160])
