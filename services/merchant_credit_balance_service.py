"""Merchant credit-balance adapter for SKU audit v3.

The balance source is the dedicated `merchant_credit_balance` table. Credit
and debit operations also write an idempotency/meters row to
`agent_center_usage_events` so replays can return the original post-operation
balance without applying a second mutation.

Credits are one abstract customer-facing balance. Audit/prompt/execution are
ledger categories only; internal USD COGS is never suitable for brand-facing
serialization.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Literal, Optional

from db.database import database

CreditCategory = Literal["audit", "prompt", "execution"]

_VALID_CATEGORIES = {"audit", "prompt", "execution"}
_MAX_OPTIMISTIC_RETRIES = 3


class InsufficientCreditsError(Exception):
    """Raised when a merchant balance cannot cover a debit."""

    def __init__(
        self,
        merchant_id: str,
        category: CreditCategory,
        required: int,
        available: int,
    ) -> None:
        self.merchant_id = merchant_id
        self.kind = category
        self.category = category
        self.required = int(required)
        self.available = int(available)
        super().__init__(
            f"insufficient credits for merchant {merchant_id}: "
            f"category={category} required={required} available={available}"
        )


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _zero_balance() -> Dict[str, Any]:
    return {
        "credits": 0,
        "allowance_credits": 0,
        "plan_tier": "free",
        "updated_at": None,
        "version": 0,
        "usd_cogs_internal": Decimal("0"),
    }


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}


def _balance_from_row(row: Any) -> Dict[str, Any]:
    data = _row_to_dict(row)
    if not data:
        return _zero_balance()
    out = _zero_balance()
    for key in ("credits", "allowance_credits", "version"):
        out[key] = int(data.get(key) or 0)
    out["plan_tier"] = str(data.get("plan_tier") or "free")
    out["updated_at"] = data.get("updated_at")
    out["usd_cogs_internal"] = _decimal(data.get("usd_cogs_internal"))
    return out


def _decode_payload(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _json_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _validate_category_amount(category: str, amount: int) -> CreditCategory:
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"unsupported credit category: {category}")
    if int(amount) < 0:
        raise ValueError("credit amount must be >= 0")
    return category  # type: ignore[return-value]


def _operation_idempotency_key(
    *,
    operation: str,
    merchant_id: str,
    category: str,
    caller_key: str,
) -> str:
    if not caller_key or not str(caller_key).strip():
        raise ValueError("idempotency key is required")
    return (
        f"merchant_credit_balance:{operation}:"
        f"{merchant_id}:{category}:{str(caller_key).strip()}"
    )


@asynccontextmanager
async def _transaction(conn: Any = None):
    if conn is not None:
        yield conn
        return
    async with database.transaction():
        yield database


async def get_balance(merchant_id: str) -> Dict[str, Any]:
    """Return the merchant's internal balance, or zero balance if missing."""
    row = await database.fetch_one(
        """
        -- merchant_credit_balance:get_balance
        SELECT credits, allowance_credits, usd_cogs_internal,
               plan_tier, updated_at, version
          FROM merchant_credit_balance
         WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
    return _balance_from_row(row)


async def _get_balance_with_conn(merchant_id: str, conn: Any) -> Dict[str, Any]:
    row = await conn.fetch_one(
        """
        -- merchant_credit_balance:get_balance
        SELECT credits, allowance_credits, usd_cogs_internal,
               plan_tier, updated_at, version
          FROM merchant_credit_balance
         WHERE merchant_id = :merchant_id
        """,
        {"merchant_id": merchant_id},
    )
    return _balance_from_row(row)


async def ensure_row(merchant_id: str, *, conn: Any = None) -> None:
    """Create a zero-balance row if it does not already exist."""
    target = conn or database
    await target.execute(
        """
        -- merchant_credit_balance:ensure_row
        INSERT INTO merchant_credit_balance (merchant_id)
        VALUES (:merchant_id)
        ON CONFLICT (merchant_id) DO NOTHING
        """,
        {"merchant_id": merchant_id},
    )


async def _fetch_replay(
    *,
    conn: Any,
    operation_key: str,
) -> Optional[Dict[str, Any]]:
    row = await conn.fetch_one(
        """
        -- merchant_credit_balance:fetch_usage_replay
        SELECT payload
          FROM agent_center_usage_events
         WHERE idempotency_key = :idempotency_key
         LIMIT 1
        """,
        {"idempotency_key": operation_key},
    )
    if row is None:
        return None
    payload = _decode_payload(_row_to_dict(row).get("payload"))
    post_balance = payload.get("post_balance")
    if isinstance(post_balance, dict):
        replay = _balance_from_row(post_balance)
    else:
        replay = _zero_balance()
    replay["replay"] = True
    return replay


async def _claim_operation(
    *,
    conn: Any,
    operation_key: str,
    merchant_id: str,
    category: str,
    amount: int,
    usd_cogs: Decimal,
    operation: str,
    event_type: str,
    source_key: str,
) -> bool:
    row = await conn.fetch_one(
        """
        -- merchant_credit_balance:claim_usage_operation
        INSERT INTO agent_center_usage_events
            (id, idempotency_key, merchant_id, store_id, agent_type,
             workflow_type, event_type, provider, billing_mode,
             billing_status, quantity, payload)
        VALUES
            (:id, :idempotency_key, :merchant_id, :store_id, :agent_type,
             :workflow_type, :event_type, :provider, :billing_mode,
             :billing_status, :quantity, CAST(:payload AS JSONB))
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING idempotency_key, payload
        """,
        {
            "id": f"mcb_{uuid.uuid4().hex}",
            "idempotency_key": operation_key,
            "merchant_id": merchant_id,
            "store_id": merchant_id,
            "agent_type": "sku_audit",
            "workflow_type": "merchant_credit_balance",
            "event_type": event_type,
            "provider": "pivota",
            "billing_mode": operation,
            "billing_status": "applied",
            "quantity": int(amount),
            "payload": _json_payload({
                "operation": operation,
                "category": category,
                "amount_credits": int(amount),
                "usd_cogs_internal": str(usd_cogs),
                "source_idempotency_key": source_key,
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "post_balance": None,
            }),
        },
    )
    return row is not None


async def _store_post_balance(
    *,
    conn: Any,
    operation_key: str,
    post_balance: Dict[str, Any],
) -> None:
    await conn.execute(
        """
        -- merchant_credit_balance:store_usage_post_balance
        UPDATE agent_center_usage_events
           SET payload = CAST(:payload AS JSONB)
         WHERE idempotency_key = :idempotency_key
        """,
        {
            "idempotency_key": operation_key,
            "payload": _json_payload({"post_balance": post_balance}),
        },
    )


async def _apply_delta(
    *,
    merchant_id: str,
    category: CreditCategory,
    amount: int,
    usd_cogs: Decimal,
    operation_key: str,
    operation: Literal["debit", "credit"],
    event_type: str,
    source_key: str,
    conn: Any = None,
) -> Dict[str, Any]:
    category = _validate_category_amount(category, amount)
    if usd_cogs < 0:
        raise ValueError("usd_cogs must be >= 0")
    async with _transaction(conn) as tx:
        replay = await _fetch_replay(conn=tx, operation_key=operation_key)
        if replay is not None:
            return replay

        claimed = await _claim_operation(
            conn=tx,
            operation_key=operation_key,
            merchant_id=merchant_id,
            category=category,
            amount=amount,
            usd_cogs=usd_cogs,
            operation=operation,
            event_type=event_type,
            source_key=source_key,
        )
        if not claimed:
            replay = await _fetch_replay(conn=tx, operation_key=operation_key)
            if replay is not None:
                return replay
            raise RuntimeError("credit operation replay row was not readable")

        await ensure_row(merchant_id, conn=tx)
        for _attempt in range(_MAX_OPTIMISTIC_RETRIES):
            balance = await _get_balance_with_conn(merchant_id, tx)
            available = int(balance["credits"])
            if operation == "debit" and available < int(amount):
                raise InsufficientCreditsError(
                    merchant_id, category, int(amount), available,
                )

            if operation == "debit":
                sql = """
                -- merchant_credit_balance:debit_update
                UPDATE merchant_credit_balance
                   SET credits = credits - :amount,
                       usd_cogs_internal = usd_cogs_internal + :usd_cogs,
                       updated_at = NOW(),
                       version = version + 1
                 WHERE merchant_id = :merchant_id
                   AND version = :version
                   AND credits >= :amount
                RETURNING credits, allowance_credits, usd_cogs_internal,
                          plan_tier, updated_at, version
                """
            else:
                sql = """
                -- merchant_credit_balance:credit_update
                UPDATE merchant_credit_balance
                   SET credits = credits + :amount,
                       usd_cogs_internal = GREATEST(
                           usd_cogs_internal - :usd_cogs,
                           0
                       ),
                       updated_at = NOW(),
                       version = version + 1
                 WHERE merchant_id = :merchant_id
                   AND version = :version
                RETURNING credits, allowance_credits, usd_cogs_internal,
                          plan_tier, updated_at, version
                """
            row = await tx.fetch_one(
                sql,
                {
                    "merchant_id": merchant_id,
                    "amount": int(amount),
                    "usd_cogs": usd_cogs,
                    "version": int(balance["version"]),
                },
            )
            if row is not None:
                post_balance = _balance_from_row(row)
                await _store_post_balance(
                    conn=tx,
                    operation_key=operation_key,
                    post_balance=post_balance,
                )
                post_balance["replay"] = False
                return post_balance

        latest = await _get_balance_with_conn(merchant_id, tx)
        if operation == "debit":
            raise InsufficientCreditsError(
                merchant_id, category, int(amount), int(latest["credits"]),
            )
        raise RuntimeError("credit balance optimistic update did not converge")


async def debit(
    merchant_id: str,
    category: CreditCategory,
    amount_credits: int,
    idempotency_key: str,
    *,
    usd_cogs: Any = 0,
    conn: Any = None,
) -> Dict[str, Any]:
    """Atomically debit abstract credits with idempotent replay."""
    operation_key = _operation_idempotency_key(
        operation="debit",
        merchant_id=merchant_id,
        category=category,
        caller_key=idempotency_key,
    )
    return await _apply_delta(
        merchant_id=merchant_id,
        category=category,
        amount=int(amount_credits),
        usd_cogs=_decimal(usd_cogs),
        operation_key=operation_key,
        operation="debit",
        event_type=f"credit_debit_{category}",
        source_key=idempotency_key,
        conn=conn,
    )


async def credit(
    merchant_id: str,
    category: CreditCategory,
    amount_credits: int,
    source_event_id: str,
    *,
    usd_cogs: Any = 0,
    conn: Any = None,
) -> Dict[str, Any]:
    """Add credits for a purchase, grant, or refund with idempotency."""
    operation_key = _operation_idempotency_key(
        operation="credit",
        merchant_id=merchant_id,
        category=category,
        caller_key=source_event_id,
    )
    return await _apply_delta(
        merchant_id=merchant_id,
        category=category,
        amount=int(amount_credits),
        usd_cogs=_decimal(usd_cogs),
        operation_key=operation_key,
        operation="credit",
        event_type=f"credit_grant_{category}",
        source_key=source_event_id,
        conn=conn,
    )
