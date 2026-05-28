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
from routes.billing_routes import (
    _as_text as _billing_as_text,
    _fetch_merchant_billing_row,
    _stripe_object_to_dict,
    stripe_client,
)

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


class MissingVerifiedPaymentMethodError(Exception):
    """Raised when a paid-tier merchant has no chargeable Stripe card."""

    def __init__(self, merchant_id: str, reason: str) -> None:
        self.merchant_id = merchant_id
        self.reason = reason
        self.code = "missing_verified_payment_method"
        super().__init__(
            f"merchant {merchant_id} requires a verified Stripe payment method: {reason}"
        )


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _zero_balance() -> Dict[str, Any]:
    return {
        "credits": 0,
        "purchased_credits": 0,
        "allowance_credits": 0,
        "allowance_period_start": None,
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
    for key in ("credits", "purchased_credits", "allowance_credits", "version"):
        out[key] = int(data.get(key) or 0)
    out["allowance_period_start"] = data.get("allowance_period_start")
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


def _normalize_purchased_credit_amount(
    amount: int,
    purchased_credits: Optional[int],
) -> int:
    purchased = int(amount) if purchased_credits is None else int(purchased_credits)
    if purchased < 0:
        raise ValueError("purchased_credits must be >= 0")
    if purchased > int(amount):
        raise ValueError("purchased_credits cannot exceed credited amount")
    return purchased


def _current_month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


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
    transaction = getattr(database, "transaction", None)
    if transaction is None:
        yield database
        return
    async with database.transaction():
        yield database


async def _active_subscription_allowance(
    merchant_id: str,
    conn: Any,
) -> Optional[Dict[str, Any]]:
    row = await conn.fetch_one(
        """
        -- merchant_credit_balance:active_subscription_allowance
        SELECT sp.name AS plan_tier,
               sp.monthly_credit_allowance
          FROM user_subscriptions us
          JOIN subscription_plans sp
            ON sp.id = us.plan_id
         WHERE us.merchant_id = :merchant_id
           AND us.status IN ('active', 'trialing')
           AND sp.status = 'active'
         ORDER BY us.current_period_start DESC NULLS LAST,
                  us.started_at DESC NULLS LAST,
                  us.created_at DESC NULLS LAST,
                  us.id DESC
         LIMIT 1
        """,
        {"merchant_id": merchant_id},
    )
    return _row_to_dict(row) if row else None


async def apply_subscription_allowance(
    merchant_id: str,
    *,
    conn: Any = None,
) -> Dict[str, Any]:
    """Grant the direct merchant's current calendar-month allowance once.

    Default rollover policy: subscription allowance is use-it-or-lose-it each
    calendar month, while purchased top-up credits persist. `purchased_credits`
    tracks the paid top-up portion so the reset can replace only allowance
    credits without rolling over unused allowance. Founder policy can override
    this later without changing the partner-credit path.
    """
    async with _transaction(conn) as tx:
        subscription = await _active_subscription_allowance(merchant_id, tx)
        if not subscription:
            return await _get_balance_with_conn(merchant_id, tx)

        allowance = int(subscription.get("monthly_credit_allowance") or 0)
        period_start = _current_month_start()
        plan_tier = str(subscription.get("plan_tier") or "free")

        await ensure_row(merchant_id, conn=tx)
        row = await tx.fetch_one(
            """
            -- merchant_credit_balance:apply_subscription_allowance
            UPDATE merchant_credit_balance
               SET credits = purchased_credits + :allowance_credits,
                   allowance_credits = :allowance_credits,
                   allowance_period_start = :allowance_period_start,
                   plan_tier = :plan_tier,
                   updated_at = NOW(),
                   version = version + 1
             WHERE merchant_id = :merchant_id
               AND (
                   allowance_period_start IS NULL
                   OR allowance_period_start < :allowance_period_start
                   OR allowance_period_start >= (
                       :allowance_period_start + INTERVAL '1 month'
                   )
               )
            RETURNING credits, purchased_credits, allowance_credits,
                      allowance_period_start, usd_cogs_internal,
                      plan_tier, updated_at, version
            """,
            {
                "merchant_id": merchant_id,
                "allowance_credits": allowance,
                "allowance_period_start": period_start,
                "plan_tier": plan_tier,
            },
        )
        if row is not None:
            return _balance_from_row(row)
        return await _get_balance_with_conn(merchant_id, tx)


async def get_balance(merchant_id: str) -> Dict[str, Any]:
    """Return the merchant's internal balance, or zero balance if missing."""
    return await apply_subscription_allowance(merchant_id)


async def _get_balance_with_conn(merchant_id: str, conn: Any) -> Dict[str, Any]:
    row = await conn.fetch_one(
        """
        -- merchant_credit_balance:get_balance
        SELECT credits, purchased_credits, allowance_credits,
               allowance_period_start, usd_cogs_internal,
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
    purchased_credits: int,
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
                "purchased_credits": int(purchased_credits),
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
    purchased_credits: Optional[int] = None,
    conn: Any = None,
) -> Dict[str, Any]:
    category = _validate_category_amount(category, amount)
    if usd_cogs < 0:
        raise ValueError("usd_cogs must be >= 0")
    purchased_credit_amount = (
        0 if operation == "debit"
        else _normalize_purchased_credit_amount(int(amount), purchased_credits)
    )
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
            purchased_credits=purchased_credit_amount,
        )
        if not claimed:
            replay = await _fetch_replay(conn=tx, operation_key=operation_key)
            if replay is not None:
                return replay
            raise RuntimeError("credit operation replay row was not readable")

        await ensure_row(merchant_id, conn=tx)
        if operation == "debit":
            await apply_subscription_allowance(merchant_id, conn=tx)
        for _attempt in range(_MAX_OPTIMISTIC_RETRIES):
            balance = await _get_balance_with_conn(merchant_id, tx)
            available = int(balance["credits"])
            if operation == "debit" and available < int(amount):
                raise InsufficientCreditsError(
                    merchant_id, category, int(amount), available,
                )
            purchased_available = int(balance.get("purchased_credits") or 0)
            allowance_available = max(0, available - purchased_available)
            purchased_debit = min(
                purchased_available,
                max(0, int(amount) - allowance_available),
            )

            if operation == "debit":
                sql = """
                -- merchant_credit_balance:debit_update
                UPDATE merchant_credit_balance
                   SET credits = credits - :amount,
                       purchased_credits = purchased_credits - :purchased_credits,
                       usd_cogs_internal = usd_cogs_internal + :usd_cogs,
                       updated_at = NOW(),
                       version = version + 1
                 WHERE merchant_id = :merchant_id
                   AND version = :version
                   AND credits >= :amount
                   AND purchased_credits >= :purchased_credits
                RETURNING credits, allowance_credits, usd_cogs_internal,
                          purchased_credits, allowance_period_start,
                          plan_tier, updated_at, version
                """
            else:
                sql = """
                -- merchant_credit_balance:credit_update
                UPDATE merchant_credit_balance
                   SET credits = credits + :amount,
                       purchased_credits = purchased_credits + :purchased_credits,
                       usd_cogs_internal = GREATEST(
                           usd_cogs_internal - :usd_cogs,
                           0
                       ),
                       updated_at = NOW(),
                       version = version + 1
                 WHERE merchant_id = :merchant_id
                   AND version = :version
                RETURNING credits, allowance_credits, usd_cogs_internal,
                          purchased_credits, allowance_period_start,
                          plan_tier, updated_at, version
                """
            row = await tx.fetch_one(
                sql,
                {
                    "merchant_id": merchant_id,
                    "amount": int(amount),
                    "purchased_credits": (
                        purchased_debit
                        if operation == "debit"
                        else purchased_credit_amount
                    ),
                    "usd_cogs": usd_cogs,
                    "version": int(balance["version"]),
                },
            )
            if row is not None:
                post_balance = _balance_from_row(row)
                if operation == "debit":
                    post_balance["purchased_credits_debited"] = purchased_debit
                else:
                    post_balance["purchased_credits_credited"] = purchased_credit_amount
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
    purchased_credits: Optional[int] = None,
    conn: Any = None,
) -> Dict[str, Any]:
    """Add credits for a purchase, grant, or refund with idempotency.

    By default credited credits are treated as purchased top-up credits so they
    survive calendar allowance reset. Refund callers that know only part of a
    debit consumed top-ups should pass that purchased portion explicitly.
    """
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
        purchased_credits=purchased_credits,
        conn=conn,
    )


async def _stripe_customer_id_for_direct_merchant(merchant_id: str) -> str:
    billing_row = await _fetch_merchant_billing_row(
        database,
        merchant_id=merchant_id,
        contact_email=None,
        stripe_customer_id=None,
    )
    return _billing_as_text((billing_row or {}).get("stripe_customer_id"))


def _stripe_id(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    data = _stripe_object_to_dict(value)
    if isinstance(data, dict):
        return _billing_as_text(data.get("id"))
    return _billing_as_text(getattr(value, "id", None))


def _is_verified_default_payment_method(value: Any, *, stripe_customer_id: str) -> bool:
    data = _stripe_object_to_dict(value)
    if not isinstance(data, dict):
        return False
    payment_method_id = _billing_as_text(data.get("id"))
    if not payment_method_id:
        return False
    if _billing_as_text(data.get("type")) != "card":
        return False
    customer = data.get("customer")
    if customer and _stripe_id(customer) != stripe_customer_id:
        return False
    return True


async def require_verified_payment_method(merchant_id: str) -> None:
    """Require a chargeable default Stripe card for paid direct audits."""
    stripe_customer_id = await _stripe_customer_id_for_direct_merchant(merchant_id)
    if not stripe_customer_id:
        raise MissingVerifiedPaymentMethodError(merchant_id, "missing_stripe_customer")

    import asyncio

    customer = await asyncio.to_thread(
        stripe_client.v1.customers.retrieve,
        stripe_customer_id,
    )
    customer_data = _stripe_object_to_dict(customer)
    invoice_settings = (
        customer_data.get("invoice_settings")
        if isinstance(customer_data, dict)
        else {}
    )
    default_payment_method = (
        invoice_settings.get("default_payment_method")
        if isinstance(invoice_settings, dict)
        else None
    )
    if not default_payment_method:
        raise MissingVerifiedPaymentMethodError(
            merchant_id,
            "missing_default_payment_method",
        )

    payment_method = default_payment_method
    if isinstance(default_payment_method, str):
        payment_method = await asyncio.to_thread(
            stripe_client.v1.payment_methods.retrieve,
            default_payment_method,
        )
    if not _is_verified_default_payment_method(
        payment_method,
        stripe_customer_id=stripe_customer_id,
    ):
        raise MissingVerifiedPaymentMethodError(
            merchant_id,
            "default_payment_method_not_verified_card",
        )
