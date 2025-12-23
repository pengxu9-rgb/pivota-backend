from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol


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
    def __init__(self):
        self._ready = False

    def _try_get_db(self):
        try:
            from db.database import database

            return database
        except Exception:
            return None

    async def _ensure_table(self, db) -> None:
        if self._ready:
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
        self._ready = True

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
            """,
            {"scope": scope, "key": key},
        )
        if not row:
            return None
        return IdempotencyRecord(
            scope=row["scope"],
            key=row["idem_key"],
            created_at=row["created_at"],
            value=dict(row["value_json"]) if row["value_json"] is not None else {},
        )

    async def put(self, *, scope: str, key: str, value: Dict[str, Any]) -> IdempotencyRecord:
        db = self._try_get_db()
        if db is None:
            raise RuntimeError("DATABASE_URL not configured; PostgresIdempotencyStore unavailable")
        await self._ensure_table(db)
        await db.execute(
            """
            INSERT INTO mvp_idempotency_keys (scope, idem_key, value_json)
            VALUES (:scope, :key, CAST(:value_json AS jsonb))
            ON CONFLICT (scope, idem_key) DO NOTHING
            """,
            {"scope": scope, "key": key, "value_json": json.dumps(value, ensure_ascii=False)},
        )
        rec = await self.get(scope=scope, key=key)
        if rec:
            return rec
        return IdempotencyRecord(scope=scope, key=key, created_at=datetime.now(timezone.utc), value=value)

