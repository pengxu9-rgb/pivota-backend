from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Protocol

logger = logging.getLogger(__name__)

# Records in mvp_idempotency_keys are response snapshots: order_create rows carry
# PSP client_secrets, Checkout Session ids and buyer emails. They are only useful
# for replaying a retried request, so they expire. MVP_IDEMPOTENCY_TTL_DAYS is
# read on every call (no restart needed), clamped to [1, 30]; anything that is
# not an integer falls back to the default.
MVP_IDEMPOTENCY_TTL_DAYS_ENV = "MVP_IDEMPOTENCY_TTL_DAYS"
DEFAULT_IDEMPOTENCY_TTL_DAYS = 7
MIN_IDEMPOTENCY_TTL_DAYS = 1
MAX_IDEMPOTENCY_TTL_DAYS = 30
# Upper bound on expired rows one put() deletes for its scope, so the cleanup
# never turns a request into a bulk purge.
EXPIRED_CLEANUP_BATCH = 100


def idempotency_ttl_days() -> int:
    raw = (os.getenv(MVP_IDEMPOTENCY_TTL_DAYS_ENV) or "").strip()
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_IDEMPOTENCY_TTL_DAYS
    return max(MIN_IDEMPOTENCY_TTL_DAYS, min(MAX_IDEMPOTENCY_TTL_DAYS, days))


def idempotency_cutoff(now: Optional[datetime] = None) -> datetime:
    """Rows created before this instant are expired.

    Computed in Python and bound as a parameter, rather than written as
    `now() - make_interval(...)`, so the SQL stays portable across dialects.
    """
    base = now or datetime.now(timezone.utc)
    return base - timedelta(days=idempotency_ttl_days())


@dataclass(frozen=True)
class IdempotencyRecord:
    scope: str
    key: str
    created_at: datetime
    value: Dict[str, Any]


class IdempotencyStore(Protocol):
    async def get(self, *, scope: str, key: str) -> Optional[IdempotencyRecord]: ...
    async def put(self, *, scope: str, key: str, value: Dict[str, Any]) -> IdempotencyRecord: ...


class InMemoryIdempotencyStore:
    def __init__(self):
        self._store: Dict[str, IdempotencyRecord] = {}

    async def get(self, *, scope: str, key: str) -> Optional[IdempotencyRecord]:
        return self._store.get(f"{scope}:{key}")

    async def put(self, *, scope: str, key: str, value: Dict[str, Any]) -> IdempotencyRecord:
        rec = IdempotencyRecord(scope=scope, key=key, created_at=datetime.now(timezone.utc), value=value)
        self._store[f"{scope}:{key}"] = rec
        return rec


class PostgresIdempotencyStore:
    _table_ensured: bool = False
    _ensure_lock: Optional[asyncio.Lock] = None

    def _try_get_db(self):
        try:
            from db.database import database

            return database
        except Exception:
            return None

    async def _ensure_table(self, db) -> None:
        if PostgresIdempotencyStore._table_ensured:
            return
        if PostgresIdempotencyStore._ensure_lock is None:
            PostgresIdempotencyStore._ensure_lock = asyncio.Lock()
        async with PostgresIdempotencyStore._ensure_lock:
            if PostgresIdempotencyStore._table_ensured:
                return
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mvp_idempotency_keys (
                  id BIGSERIAL PRIMARY KEY,
                  scope TEXT NOT NULL,
                  idem_key TEXT NOT NULL,
                  value_json JSONB NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  UNIQUE (scope, idem_key)
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_mvp_idem_scope_time ON mvp_idempotency_keys(scope, created_at DESC);"
            )
            PostgresIdempotencyStore._table_ensured = True

    async def get(self, *, scope: str, key: str) -> Optional[IdempotencyRecord]:
        db = self._try_get_db()
        if db is None:
            return None
        await self._ensure_table(db)
        row = await db.fetch_one(
            """
            SELECT scope, idem_key, value_json, created_at
            FROM mvp_idempotency_keys
            WHERE scope = :scope AND idem_key = :key
              AND created_at >= :cutoff
            """,
            {"scope": scope, "key": key, "cutoff": idempotency_cutoff()},
        )
        if not row:
            return None
        raw = row["value_json"] if row["value_json"] is not None else None
        value: Dict[str, Any] = {}
        try:
            if raw is None:
                value = {}
            elif isinstance(raw, dict):
                value = raw
            elif isinstance(raw, str):
                parsed = json.loads(raw)
                value = parsed if isinstance(parsed, dict) else {}
            else:
                # Some drivers may return JSONB as a Mapping-like object; fall back to dict().
                value = dict(raw)
        except Exception:
            value = {}
        return IdempotencyRecord(
            scope=row["scope"],
            key=row["idem_key"],
            created_at=row["created_at"],
            value=value,
        )

    async def put(self, *, scope: str, key: str, value: Dict[str, Any]) -> IdempotencyRecord:
        db = self._try_get_db()
        if db is None:
            raise RuntimeError("DATABASE_URL not configured; PostgresIdempotencyStore unavailable")
        await self._ensure_table(db)
        cutoff = idempotency_cutoff()
        # A fresh row with the same (scope, key) wins: DO UPDATE's WHERE is false
        # for it, so nothing changes and the get() below returns the ORIGINAL
        # record (the idempotent replay). An EXPIRED row is overwritten in the
        # same statement, so a stale snapshot can never block a new record.
        await db.execute(
            """
            INSERT INTO mvp_idempotency_keys (scope, idem_key, value_json)
            VALUES (:scope, :key, CAST(:value_json AS jsonb))
            ON CONFLICT (scope, idem_key) DO UPDATE
              SET value_json = EXCLUDED.value_json,
                  created_at = CURRENT_TIMESTAMP
              WHERE mvp_idempotency_keys.created_at < :cutoff
            """,
            {
                "scope": scope,
                "key": key,
                "value_json": json.dumps(value, ensure_ascii=False),
                "cutoff": cutoff,
            },
        )
        await self._delete_expired_best_effort(db, scope=scope, cutoff=cutoff)
        rec = await self.get(scope=scope, key=key)
        if rec:
            return rec
        return IdempotencyRecord(scope=scope, key=key, created_at=datetime.now(timezone.utc), value=value)

    async def _delete_expired_best_effort(self, db, *, scope: str, cutoff: datetime) -> None:
        """Opportunistic, bounded cleanup of this scope's expired rows.

        `DELETE ... LIMIT` is not Postgres, so the bound lives in a subselect on
        the primary key (portable to SQLite too). The subselect walks
        idx_mvp_idem_scope_time (scope, created_at DESC). Concurrent puts may pick
        the same ids; the loser deletes nothing. A failure here never fails the
        put: the record is already written.
        """
        try:
            await db.execute(
                """
                DELETE FROM mvp_idempotency_keys
                WHERE id IN (
                  SELECT id FROM mvp_idempotency_keys
                  WHERE scope = :scope AND created_at < :cutoff
                  ORDER BY created_at
                  LIMIT :batch
                )
                """,
                {"scope": scope, "cutoff": cutoff, "batch": EXPIRED_CLEANUP_BATCH},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "mvp_idempotency expired-row cleanup failed scope=%s error=%s",
                scope,
                type(exc).__name__,
            )
