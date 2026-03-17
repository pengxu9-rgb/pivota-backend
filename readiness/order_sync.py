from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

from readiness.models import CheckoutSessionRecord, OrderSyncEventRecord


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        out: Dict[str, Any] = {}
        keys = getattr(row, "keys", None)
        if callable(keys):
            for key in keys():
                out[str(key)] = row[key]
        return out


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _json_safe(dump())
    if hasattr(value, "dict") and callable(getattr(value, "dict")):
        return _json_safe(value.dict())
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(_json_safe(value))


class ReadinessJournal(Protocol):
    async def create_checkout_session(
        self,
        *,
        merchant_id: str,
        channel: str,
        variant_id: str,
        quantity: int,
        payment_mode: str,
        session_payload: Dict[str, Any],
        continue_url: Optional[str],
        idempotency_key: Optional[str],
    ) -> CheckoutSessionRecord:
        ...

    async def get_checkout_session(self, checkout_id: str) -> Optional[CheckoutSessionRecord]:
        ...

    async def list_events(self, checkout_id: str) -> List[OrderSyncEventRecord]:
        ...

    async def append_event(self, *, checkout_id: str, event_type: str, event_payload: Dict[str, Any]) -> None:
        ...

    async def update_checkout_session(
        self,
        checkout_id: str,
        *,
        status: Optional[str] = None,
        order_id: Optional[str] = None,
        payment_mode: Optional[str] = None,
        session_payload_patch: Optional[Dict[str, Any]] = None,
    ) -> Optional[CheckoutSessionRecord]:
        ...

    async def advance_order_sync(self, checkout_id: str) -> Dict[str, Any]:
        ...


@dataclass
class InMemoryReadinessJournal(ReadinessJournal):
    sessions: Dict[str, CheckoutSessionRecord] = field(default_factory=dict)
    events: Dict[str, List[OrderSyncEventRecord]] = field(default_factory=dict)
    idempotency_index: Dict[str, str] = field(default_factory=dict)

    async def create_checkout_session(
        self,
        *,
        merchant_id: str,
        channel: str,
        variant_id: str,
        quantity: int,
        payment_mode: str,
        session_payload: Dict[str, Any],
        continue_url: Optional[str],
        idempotency_key: Optional[str],
    ) -> CheckoutSessionRecord:
        if idempotency_key:
            existing_id = self.idempotency_index.get(f"{merchant_id}:{idempotency_key}")
            if existing_id and existing_id in self.sessions:
                return self.sessions[existing_id]

        checkout_id = f"rdchk_{uuid.uuid4().hex[:16]}"
        now = _now_iso()
        record = CheckoutSessionRecord(
            checkout_id=checkout_id,
            merchant_id=merchant_id,
            channel=channel,
            variant_id=variant_id,
            quantity=quantity,
            payment_mode=payment_mode,
            status="checkout_created",
            continue_url=continue_url,
            idempotency_key=idempotency_key,
            session_payload=_json_safe(session_payload),
            created_at=now,
            updated_at=now,
        )
        self.sessions[checkout_id] = record
        self.events.setdefault(checkout_id, []).append(
            OrderSyncEventRecord(
                checkout_id=checkout_id,
                event_type="checkout_created",
                event_payload=_json_safe({
                    "variant_id": variant_id,
                    "quantity": quantity,
                    "payment_mode": payment_mode,
                }),
                created_at=now,
            )
        )
        if idempotency_key:
            self.idempotency_index[f"{merchant_id}:{idempotency_key}"] = checkout_id
        return record

    async def get_checkout_session(self, checkout_id: str) -> Optional[CheckoutSessionRecord]:
        return self.sessions.get(checkout_id)

    async def list_events(self, checkout_id: str) -> List[OrderSyncEventRecord]:
        return list(self.events.get(checkout_id, []))

    async def append_event(self, *, checkout_id: str, event_type: str, event_payload: Dict[str, Any]) -> None:
        event_types = {event.event_type for event in self.events.get(checkout_id, [])}
        if event_type in event_types:
            return
        self.events.setdefault(checkout_id, []).append(
            OrderSyncEventRecord(
                checkout_id=checkout_id,
                event_type=event_type,
                event_payload=event_payload,
                created_at=_now_iso(),
            )
        )

    async def update_checkout_session(
        self,
        checkout_id: str,
        *,
        status: Optional[str] = None,
        order_id: Optional[str] = None,
        payment_mode: Optional[str] = None,
        session_payload_patch: Optional[Dict[str, Any]] = None,
    ) -> Optional[CheckoutSessionRecord]:
        session = self.sessions.get(checkout_id)
        if session is None:
            return None
        if status is not None:
            session.status = status
        if order_id is not None:
            session.order_id = order_id
        if payment_mode is not None:
            session.payment_mode = payment_mode
        if session_payload_patch:
            next_payload = dict(session.session_payload or {})
            next_payload.update(session_payload_patch)
            session.session_payload = _json_safe(next_payload)
        session.updated_at = _now_iso()
        return session

    async def advance_order_sync(self, checkout_id: str) -> Dict[str, Any]:
        session = self.sessions.get(checkout_id)
        if not session:
            raise KeyError(checkout_id)

        existing = {event.event_type for event in self.events.get(checkout_id, [])}
        appended: List[OrderSyncEventRecord] = []

        if "payment_stubbed" not in existing:
            appended.append(
                OrderSyncEventRecord(
                    checkout_id=checkout_id,
                    event_type="payment_stubbed",
                    event_payload={"mode": "stubbed", "captured": False},
                    created_at=_now_iso(),
                )
            )
        if not session.order_id:
            session.order_id = f"rord_{checkout_id[-10:]}"
        if "order_created" not in existing:
            appended.append(
                OrderSyncEventRecord(
                    checkout_id=checkout_id,
                    event_type="order_created",
                    event_payload={"order_id": session.order_id, "mode": "synthetic"},
                    created_at=_now_iso(),
                )
            )
        if "order_forwarded_to_merchant_stub" not in existing:
            appended.append(
                OrderSyncEventRecord(
                    checkout_id=checkout_id,
                    event_type="order_forwarded_to_merchant_stub",
                    event_payload={"merchant_id": session.merchant_id, "stubbed": True},
                    created_at=_now_iso(),
                )
            )
        if "state_synced" not in existing:
            appended.append(
                OrderSyncEventRecord(
                    checkout_id=checkout_id,
                    event_type="state_synced",
                    event_payload={"status": "state_synced", "stubbed": True},
                    created_at=_now_iso(),
                )
            )

        if appended:
            self.events.setdefault(checkout_id, []).extend(appended)
        session.status = "state_synced"
        session.updated_at = _now_iso()
        return {
            "checkout": session,
            "events": await self.list_events(checkout_id),
            "replayed": not bool(appended),
        }


class DatabaseReadinessJournal(ReadinessJournal):
    async def _db(self):
        from db.database import database

        return database

    async def _ensure_tables(self) -> None:
        db = await self._db()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS readiness_checkout_sessions (
              checkout_id TEXT PRIMARY KEY,
              merchant_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              variant_id TEXT NOT NULL,
              quantity INTEGER NOT NULL,
              payment_mode TEXT NOT NULL DEFAULT 'stubbed',
              status TEXT NOT NULL DEFAULT 'checkout_created',
              continue_url TEXT NULL,
              idempotency_key TEXT NULL,
              order_id TEXT NULL,
              session_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_readiness_checkout_idempotency
            ON readiness_checkout_sessions (merchant_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS readiness_order_sync_events (
              id BIGSERIAL PRIMARY KEY,
              checkout_id TEXT NOT NULL REFERENCES readiness_checkout_sessions (checkout_id) ON DELETE CASCADE,
              event_type TEXT NOT NULL,
              event_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              UNIQUE (checkout_id, event_type)
            )
            """
        )

    async def create_checkout_session(
        self,
        *,
        merchant_id: str,
        channel: str,
        variant_id: str,
        quantity: int,
        payment_mode: str,
        session_payload: Dict[str, Any],
        continue_url: Optional[str],
        idempotency_key: Optional[str],
    ) -> CheckoutSessionRecord:
        await self._ensure_tables()
        db = await self._db()
        if idempotency_key:
            row = await db.fetch_one(
                """
                SELECT *
                FROM readiness_checkout_sessions
                WHERE merchant_id = :merchant_id AND idempotency_key = :idempotency_key
                """,
                {"merchant_id": merchant_id, "idempotency_key": idempotency_key},
            )
            if row:
                return self._checkout_from_row(row)

        checkout_id = f"rdchk_{uuid.uuid4().hex[:16]}"
        row = await db.fetch_one(
            """
            INSERT INTO readiness_checkout_sessions (
              checkout_id, merchant_id, channel, variant_id, quantity, payment_mode, status,
              continue_url, idempotency_key, session_payload
            )
            VALUES (
              :checkout_id, :merchant_id, :channel, :variant_id, :quantity, :payment_mode,
              'checkout_created', :continue_url, :idempotency_key, CAST(:session_payload AS JSONB)
            )
            RETURNING *
            """,
            {
                "checkout_id": checkout_id,
                "merchant_id": merchant_id,
                "channel": channel,
                "variant_id": variant_id,
                "quantity": quantity,
                "payment_mode": payment_mode,
                "continue_url": continue_url,
                "idempotency_key": idempotency_key,
                "session_payload": _json_dumps(session_payload),
            },
        )
        await self.append_event(
            checkout_id=checkout_id,
            event_type="checkout_created",
            event_payload={"variant_id": variant_id, "quantity": quantity, "payment_mode": payment_mode},
        )
        return self._checkout_from_row(row)

    async def append_event(self, *, checkout_id: str, event_type: str, event_payload: Dict[str, Any]) -> None:
        db = await self._db()
        await db.execute(
            """
            INSERT INTO readiness_order_sync_events (checkout_id, event_type, event_payload)
            VALUES (:checkout_id, :event_type, CAST(:event_payload AS JSONB))
            ON CONFLICT (checkout_id, event_type) DO NOTHING
            """,
            {
                "checkout_id": checkout_id,
                "event_type": event_type,
                "event_payload": _json_dumps(event_payload),
            },
        )

    async def update_checkout_session(
        self,
        checkout_id: str,
        *,
        status: Optional[str] = None,
        order_id: Optional[str] = None,
        payment_mode: Optional[str] = None,
        session_payload_patch: Optional[Dict[str, Any]] = None,
    ) -> Optional[CheckoutSessionRecord]:
        await self._ensure_tables()
        assignments = []
        params: Dict[str, Any] = {"checkout_id": checkout_id}
        if status is not None:
            assignments.append("status = :status")
            params["status"] = status
        if order_id is not None:
            assignments.append("order_id = :order_id")
            params["order_id"] = order_id
        if payment_mode is not None:
            assignments.append("payment_mode = :payment_mode")
            params["payment_mode"] = payment_mode
        if session_payload_patch:
            checkout = await self.get_checkout_session(checkout_id)
            if checkout is None:
                return None
            merged_payload = dict(checkout.session_payload or {})
            merged_payload.update(session_payload_patch)
            assignments.append("session_payload = CAST(:session_payload AS JSONB)")
            params["session_payload"] = _json_dumps(merged_payload)
        if not assignments:
            return await self.get_checkout_session(checkout_id)
        assignments.append("updated_at = NOW()")
        db = await self._db()
        await db.execute(
            f"""
            UPDATE readiness_checkout_sessions
            SET {", ".join(assignments)}
            WHERE checkout_id = :checkout_id
            """,
            params,
        )
        return await self.get_checkout_session(checkout_id)

    def _checkout_from_row(self, row: Any) -> CheckoutSessionRecord:
        data = _row_to_dict(row)
        data["session_payload"] = _loads(data.get("session_payload")) or {}
        for key in ("created_at", "updated_at"):
            value = data.get(key)
            if isinstance(value, datetime):
                data[key] = value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return CheckoutSessionRecord(**data)

    def _event_from_row(self, row: Any) -> OrderSyncEventRecord:
        data = _row_to_dict(row)
        data["event_payload"] = _loads(data.get("event_payload")) or {}
        value = data.get("created_at")
        if isinstance(value, datetime):
            data["created_at"] = value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return OrderSyncEventRecord(
            checkout_id=str(data.get("checkout_id")),
            event_type=str(data.get("event_type")),
            event_payload=data.get("event_payload") or {},
            created_at=data.get("created_at"),
        )

    async def get_checkout_session(self, checkout_id: str) -> Optional[CheckoutSessionRecord]:
        await self._ensure_tables()
        db = await self._db()
        row = await db.fetch_one(
            """
            SELECT *
            FROM readiness_checkout_sessions
            WHERE checkout_id = :checkout_id
            """,
            {"checkout_id": checkout_id},
        )
        if not row:
            return None
        return self._checkout_from_row(row)

    async def list_events(self, checkout_id: str) -> List[OrderSyncEventRecord]:
        await self._ensure_tables()
        db = await self._db()
        rows = await db.fetch_all(
            """
            SELECT checkout_id, event_type, event_payload, created_at
            FROM readiness_order_sync_events
            WHERE checkout_id = :checkout_id
            ORDER BY created_at ASC
            """,
            {"checkout_id": checkout_id},
        )
        return [self._event_from_row(row) for row in rows]

    async def advance_order_sync(self, checkout_id: str) -> Dict[str, Any]:
        await self._ensure_tables()
        session = await self.get_checkout_session(checkout_id)
        if session is None:
            raise KeyError(checkout_id)

        events = await self.list_events(checkout_id)
        existing = {event.event_type for event in events}
        appended = False

        if "payment_stubbed" not in existing:
            await self.append_event(
                checkout_id=checkout_id,
                event_type="payment_stubbed",
                event_payload={"mode": "stubbed", "captured": False},
            )
            appended = True

        order_id = session.order_id or f"rord_{checkout_id[-10:]}"
        if not session.order_id:
            db = await self._db()
            await db.execute(
                """
                UPDATE readiness_checkout_sessions
                SET order_id = :order_id, status = 'order_created', updated_at = NOW()
                WHERE checkout_id = :checkout_id
                """,
                {"order_id": order_id, "checkout_id": checkout_id},
            )
            appended = True
        if "order_created" not in existing:
            await self.append_event(
                checkout_id=checkout_id,
                event_type="order_created",
                event_payload={"order_id": order_id, "mode": "synthetic"},
            )
            appended = True
        if "order_forwarded_to_merchant_stub" not in existing:
            await self.append_event(
                checkout_id=checkout_id,
                event_type="order_forwarded_to_merchant_stub",
                event_payload={"merchant_id": session.merchant_id, "stubbed": True},
            )
            appended = True
        if "state_synced" not in existing:
            await self.append_event(
                checkout_id=checkout_id,
                event_type="state_synced",
                event_payload={"status": "state_synced", "stubbed": True},
            )
            db = await self._db()
            await db.execute(
                """
                UPDATE readiness_checkout_sessions
                SET status = 'state_synced', updated_at = NOW()
                WHERE checkout_id = :checkout_id
                """,
                {"checkout_id": checkout_id},
            )
            appended = True

        return {
            "checkout": await self.get_checkout_session(checkout_id),
            "events": await self.list_events(checkout_id),
            "replayed": not appended,
        }


_default_journal: ReadinessJournal = DatabaseReadinessJournal()


def get_default_journal() -> ReadinessJournal:
    return _default_journal
