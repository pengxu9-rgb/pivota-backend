from __future__ import annotations

import json
import os
import asyncio
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol

from pydantic import BaseModel, Field

from mvp.constants import RiskTier, SCHEMA_VERSION, SchemaVersion
from services.pcs_hash import chain_hash, sha256_json


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


class MvpEventEnvelope(BaseModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    event_id: str
    event_type: str
    occurred_at: datetime
    merchant_id: Optional[str] = None
    geo: Optional[Dict[str, Any]] = None
    surface: str = "unknown"
    adapter: Optional[str] = None
    risk_tier: RiskTier = "unknown"
    idempotency_key: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    payload_sha256: str
    prev_chain_hash: Optional[str] = None
    chain_hash: str


class EventSink(Protocol):
    async def append(self, event: MvpEventEnvelope) -> None: ...


class _InMemoryChainCursor:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_by_merchant: Dict[str, str] = {}

    def get_prev(self, merchant_id: Optional[str]) -> Optional[str]:
        if not merchant_id:
            return None
        with self._lock:
            return self._last_by_merchant.get(merchant_id)

    def set_last(self, merchant_id: Optional[str], chain_hash_value: str) -> None:
        if not merchant_id:
            return
        with self._lock:
            self._last_by_merchant[merchant_id] = chain_hash_value


_chain_cursor = _InMemoryChainCursor()


class FileEventSink:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    async def append(self, event: MvpEventEnvelope) -> None:
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


class PostgresEventSink:
    def __init__(self):
        self._ready = False
        self._ensure_lock = asyncio.Lock()

    def _try_get_db(self):
        try:
            from db.database import database

            return database
        except Exception:
            return None

    async def _ensure_table(self, db) -> None:
        async with self._ensure_lock:
            if self._ready:
                return
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mvp_events (
                  id BIGSERIAL PRIMARY KEY,
                  event_id TEXT UNIQUE NOT NULL,
                  schema_version VARCHAR(10) NOT NULL,
                  event_type TEXT NOT NULL,
                  merchant_id VARCHAR(64),
                  occurred_at TIMESTAMPTZ NOT NULL,
                  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  geo_json JSONB,
                  surface TEXT,
                  adapter TEXT,
                  risk_tier TEXT,
                  idempotency_key TEXT,
                  payload_json JSONB NOT NULL,
                  payload_sha256 CHAR(64) NOT NULL,
                  prev_chain_hash CHAR(64),
                  chain_hash CHAR(64) NOT NULL
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_mvp_events_merchant_time ON mvp_events(merchant_id, occurred_at DESC);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_mvp_events_type_time ON mvp_events(event_type, occurred_at DESC);"
            )
            self._ready = True

    async def _get_prev_chain_hash(self, db, merchant_id: str) -> Optional[str]:
        row = await db.fetch_one(
            """
            SELECT chain_hash
            FROM mvp_events
            WHERE merchant_id = :merchant_id
            ORDER BY id DESC
            LIMIT 1
            """,
            {"merchant_id": merchant_id},
        )
        if not row:
            return None
        return row["chain_hash"]

    async def append(self, event: MvpEventEnvelope) -> None:
        db = self._try_get_db()
        if db is None:
            raise RuntimeError("DATABASE_URL not configured; PostgresEventSink unavailable")

        await self._ensure_table(db)

        async with db.transaction():
            prev = None
            if event.merchant_id:
                try:
                    await db.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(:merchant_id))",
                        {"merchant_id": event.merchant_id},
                    )
                except Exception:
                    pass
                prev = await self._get_prev_chain_hash(db, event.merchant_id)

            # Recompute chain based on DB cursor to keep it consistent across processes.
            occurred_iso = event.occurred_at.isoformat()
            idk = event.idempotency_key or event.event_id
            chash = chain_hash(prev, event.payload_sha256, idk, occurred_iso)

            await db.execute(
                """
                INSERT INTO mvp_events
                  (event_id, schema_version, event_type, merchant_id, occurred_at, geo_json,
                   surface, adapter, risk_tier, idempotency_key, payload_json, payload_sha256,
                   prev_chain_hash, chain_hash)
                VALUES
                  (:event_id, :schema_version, :event_type, :merchant_id, :occurred_at, CAST(:geo_json AS jsonb),
                   :surface, :adapter, :risk_tier, :idempotency_key, CAST(:payload_json AS jsonb), :payload_sha256,
                   :prev_chain_hash, :chain_hash)
                ON CONFLICT (event_id) DO NOTHING
                """,
                {
                    "event_id": event.event_id,
                    "schema_version": event.schema_version,
                    "event_type": event.event_type,
                    "merchant_id": event.merchant_id,
                    "occurred_at": event.occurred_at,
                    "geo_json": json.dumps(event.geo) if event.geo is not None else None,
                    "surface": event.surface,
                    "adapter": event.adapter,
                    "risk_tier": event.risk_tier,
                    "idempotency_key": event.idempotency_key,
                    "payload_json": json.dumps(event.payload, ensure_ascii=False),
                    "payload_sha256": event.payload_sha256,
                    "prev_chain_hash": prev,
                    "chain_hash": chash,
                },
            )


def _default_file_sink_path() -> str:
    return os.getenv("MVP_EVENTS_FILE", "mvp_events.jsonl")


_DEFAULT_SINK: Optional[EventSink] = None


def get_default_sink() -> EventSink:
    global _DEFAULT_SINK
    if _DEFAULT_SINK is not None:
        return _DEFAULT_SINK
    prefer_db = os.getenv("MVP_EVENTS_SINK", "db").lower() != "file"
    # SQLite/dev environments cannot support the Postgres DDL used by PostgresEventSink.
    # Fail closed to FileEventSink to avoid noisy background task exceptions.
    if prefer_db:
        try:
            from db.database import IS_POSTGRES

            if not IS_POSTGRES:
                prefer_db = False
        except Exception:
            prefer_db = False
    if prefer_db:
        try:
            _DEFAULT_SINK = PostgresEventSink()
            return _DEFAULT_SINK
        except Exception:
            pass
    _DEFAULT_SINK = FileEventSink(_default_file_sink_path())
    return _DEFAULT_SINK


@dataclass(frozen=True)
class EmitContext:
    merchant_id: Optional[str]
    geo: Optional[Dict[str, Any]]
    surface: str
    adapter: Optional[str]
    risk_tier: RiskTier
    idempotency_key: Optional[str]


def build_envelope(
    *,
    event_type: str,
    payload: Dict[str, Any],
    context: EmitContext,
    occurred_at: Optional[datetime] = None,
    event_id: Optional[str] = None,
) -> MvpEventEnvelope:
    eid = event_id or f"evt_{uuid.uuid4().hex}"
    ts = occurred_at or _utc_now()
    payload_sha = sha256_json(payload)

    prev = _chain_cursor.get_prev(context.merchant_id)
    idk = context.idempotency_key or eid
    chash = chain_hash(prev, payload_sha, idk, ts.isoformat())
    _chain_cursor.set_last(context.merchant_id, chash)

    return MvpEventEnvelope(
        event_id=eid,
        event_type=event_type,
        occurred_at=ts,
        merchant_id=context.merchant_id,
        geo=context.geo,
        surface=context.surface,
        adapter=context.adapter,
        risk_tier=context.risk_tier,
        idempotency_key=context.idempotency_key,
        payload=payload,
        payload_sha256=payload_sha,
        prev_chain_hash=prev,
        chain_hash=chash,
    )


async def emit(
    *,
    sink: Optional[EventSink],
    event_type: str,
    payload: Dict[str, Any],
    context: EmitContext,
) -> None:
    env = build_envelope(event_type=event_type, payload=payload, context=context)
    (sink or get_default_sink())  # ensure default is constructed even if not provided
    await (sink or get_default_sink()).append(env)


def emit_best_effort(
    *,
    event_type: str,
    payload: Dict[str, Any],
    merchant_id: Optional[str],
    geo: Optional[Dict[str, Any]],
    surface: str,
    adapter: Optional[str],
    risk_tier: RiskTier = "unknown",
    idempotency_key: Optional[str] = None,
) -> None:
    try:
        import asyncio

        ctx = EmitContext(
            merchant_id=merchant_id,
            geo=geo,
            surface=surface,
            adapter=adapter,
            risk_tier=risk_tier,
            idempotency_key=idempotency_key,
        )
        sink = get_default_sink()

        coro = emit(sink=sink, event_type=event_type, payload=payload, context=ctx)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            asyncio.run(coro)
    except Exception:
        # Never break the primary business flow due to telemetry.
        return
