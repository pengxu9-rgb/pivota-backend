from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from mvp.constants import EVENT_LEDGER, SCHEMA_VERSION
from mvp.events import emit_best_effort
from mvp.schemas import HashChain, LedgerEvent, LedgerIngest, LedgerRefs, LedgerSource, Money
from services.pcs_hash import chain_hash, sha256_json


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _LedgerChainCursor:
    def __init__(self):
        self._lock = threading.Lock()
        self._prev_by_merchant: Dict[str, str] = {}

    def prev(self, merchant_id: str) -> Optional[str]:
        with self._lock:
            return self._prev_by_merchant.get(merchant_id)

    def advance(self, merchant_id: str, new_hash: str) -> None:
        with self._lock:
            self._prev_by_merchant[merchant_id] = new_hash


_cursor = _LedgerChainCursor()


def build_ledger_event(
    *,
    merchant_id: str,
    event_type: str,
    order_id: Optional[str],
    source: Dict[str, Any],
    amount: Optional[Dict[str, Any]],
    refs: Optional[Dict[str, Any]],
    idempotency_key: Optional[str],
    signature_verified: bool,
    occurred_at: Optional[datetime] = None,
) -> LedgerEvent:
    ts = occurred_at or _utc_now()
    payload = {
        "merchant_id": merchant_id,
        "order_id": order_id,
        "event_type": event_type,
        "source": source,
        "amount": amount,
        "refs": refs,
    }
    payload_sha = sha256_json(payload)
    prev = _cursor.prev(merchant_id)
    idk = idempotency_key or f"ledger_{uuid.uuid4().hex}"
    ch = chain_hash(prev, payload_sha, str(idk), ts.isoformat())
    _cursor.advance(merchant_id, ch)

    money = Money(**amount) if isinstance(amount, dict) and amount.get("value") is not None else None
    return LedgerEvent(
        schema_version=SCHEMA_VERSION,
        event_id=f"led_{uuid.uuid4().hex}",
        merchant_id=merchant_id,
        order_id=order_id,
        event_type=event_type,
        source=LedgerSource(**source),
        amount=money,
        occurred_at=ts,
        ingest=LedgerIngest(received_at=ts, signature_verified=signature_verified, idempotency_key=idempotency_key),
        payload_sha256=payload_sha,
        chain=HashChain(prev_chain_hash=prev, chain_hash=ch),
        refs=LedgerRefs(**(refs or {})),
    )


def emit_ledger_event_best_effort(
    *,
    merchant_id: str,
    event_type: str,
    order_id: Optional[str],
    source: Dict[str, Any],
    amount: Optional[Dict[str, Any]] = None,
    refs: Optional[Dict[str, Any]] = None,
    geo: Optional[Dict[str, Any]] = None,
    surface: str = "unknown",
    adapter: Optional[str] = None,
    risk_tier: str = "unknown",
    idempotency_key: Optional[str] = None,
    signature_verified: bool = False,
) -> None:
    try:
        evt = build_ledger_event(
            merchant_id=merchant_id,
            event_type=event_type,
            order_id=order_id,
            source=source,
            amount=amount,
            refs=refs,
            idempotency_key=idempotency_key,
            signature_verified=signature_verified,
        )
        emit_best_effort(
            event_type=EVENT_LEDGER,
            payload=evt.model_dump(mode="json"),
            merchant_id=merchant_id,
            geo=geo,
            surface=surface,
            adapter=adapter,
            risk_tier=risk_tier,  # type: ignore[arg-type]
            idempotency_key=idempotency_key or evt.event_id,
        )
    except Exception:
        return

