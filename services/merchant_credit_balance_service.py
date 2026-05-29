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
import asyncio
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
CREDIT_TO_USD = Decimal("0.01")
DEFAULT_OVERAGE_CHARGE_CREDITS = 2_000
INTERNAL_COGS_RATIO = Decimal("0.65")
DIRECT_TOPUP_PURPOSE = "direct_credit_topup"
DIRECT_OVERAGE_PURPOSE = "direct_credit_overage"
TOPUP_IDEMPOTENCY_WINDOW_SECONDS = 60


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


class OveragePaymentBlockedError(Exception):
    """Raised when a prior direct overage charge failed for this merchant."""

    def __init__(self, merchant_id: str) -> None:
        self.merchant_id = merchant_id
        self.code = "overage_payment_failed"
        super().__init__(
            f"merchant {merchant_id} is blocked until the failed overage payment is resolved"
        )


class OveragePaymentFailedError(Exception):
    """Raised when a direct overage PaymentIntent cannot be collected."""

    def __init__(
        self,
        merchant_id: str,
        reason: str,
        *,
        payment_intent_id: str = "",
        overage_increment_id: str = "",
    ) -> None:
        self.merchant_id = merchant_id
        self.reason = reason
        self.payment_intent_id = payment_intent_id
        self.overage_increment_id = overage_increment_id
        self.code = "overage_payment_failed"
        super().__init__(
            f"merchant {merchant_id} overage payment failed: {reason}"
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
        "overage_pending_credits": 0,
        "overage_charged_credits": 0,
        "overage_blocked_until_payment": False,
        "overage_last_payment_intent_id": None,
        "overage_last_failed_at": None,
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
    for key in (
        "credits",
        "purchased_credits",
        "allowance_credits",
        "overage_pending_credits",
        "overage_charged_credits",
        "version",
    ):
        out[key] = int(data.get(key) or 0)
    out["overage_blocked_until_payment"] = bool(
        data.get("overage_blocked_until_payment") or False
    )
    out["overage_last_payment_intent_id"] = data.get("overage_last_payment_intent_id")
    out["overage_last_failed_at"] = data.get("overage_last_failed_at")
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


def _is_paid_tier(plan_tier: Any) -> bool:
    return str(plan_tier or "free").strip().lower() != "free"


def _credits_to_amount_cents(credits: int) -> int:
    return int(credits)


def _credits_to_usd_total(credits: int) -> str:
    return f"{(Decimal(int(credits)) * CREDIT_TO_USD):.2f}"


def _internal_cogs_for_customer_credits(credits: int) -> Decimal:
    return (
        Decimal(int(credits)) * CREDIT_TO_USD * INTERNAL_COGS_RATIO
    ).quantize(Decimal("0.0001"))


def _overage_increment_id(
    *,
    merchant_id: str,
    charged_before_credits: int,
    charge_credits: int,
) -> str:
    start = int(charged_before_credits) + 1
    end = int(charged_before_credits) + int(charge_credits)
    return f"direct_overage:{merchant_id}:{start:012d}-{end:012d}"


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


def _topup_idempotency_bucket(now: Optional[datetime] = None) -> int:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return int(instant.timestamp()) // TOPUP_IDEMPOTENCY_WINDOW_SECONDS


def _topup_idempotency_key(*, merchant_id: str, pack_credits: int) -> str:
    return (
        f"topup:{merchant_id}:{int(pack_credits)}:"
        f"{_topup_idempotency_bucket()}"
    )


def _resolve_topup_idempotency_key(
    *,
    merchant_id: str,
    pack_credits: int,
    caller_key: Optional[str],
) -> str:
    explicit_key = str(caller_key).strip() if caller_key and str(caller_key).strip() else ""
    if explicit_key:
        return explicit_key
    return _topup_idempotency_key(
        merchant_id=merchant_id,
        pack_credits=int(pack_credits),
    )


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
                      overage_pending_credits, overage_charged_credits,
                      overage_blocked_until_payment,
                      overage_last_payment_intent_id,
                      overage_last_failed_at,
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
               overage_pending_credits, overage_charged_credits,
               overage_blocked_until_payment,
               overage_last_payment_intent_id, overage_last_failed_at,
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
    payload_extra: Optional[Dict[str, Any]] = None,
) -> bool:
    payload = {
        "operation": operation,
        "category": category,
        "amount_credits": int(amount),
        "purchased_credits": int(purchased_credits),
        "usd_cogs_internal": str(usd_cogs),
        "source_idempotency_key": source_key,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        "post_balance": None,
    }
    if payload_extra:
        payload.update(payload_extra)
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
            "payload": _json_payload(payload),
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
           SET payload = COALESCE(payload, '{}'::jsonb) || CAST(:payload AS JSONB)
         WHERE idempotency_key = :idempotency_key
        """,
        {
            "idempotency_key": operation_key,
            "payload": _json_payload({"post_balance": post_balance}),
        },
    )


async def _record_overage_charge_ledger(
    *,
    conn: Any,
    merchant_id: str,
    charge: Dict[str, Any],
) -> None:
    charge_credits = int(charge["charge_credits"])
    await _claim_operation(
        conn=conn,
        operation_key=(
            "merchant_credit_balance:overage_charge:"
            f"{merchant_id}:{charge['overage_increment_id']}"
        ),
        merchant_id=merchant_id,
        category="overage",
        amount=charge_credits,
        usd_cogs=_internal_cogs_for_customer_credits(charge_credits),
        operation="debit",
        event_type="credit_overage_charge",
        source_key=str(charge["overage_increment_id"]),
        purchased_credits=0,
        payload_extra={
            "payment_intent_id": charge["payment_intent_id"],
            "stripe_payment_intent_id": charge["payment_intent_id"],
            "overage_increment_id": charge["overage_increment_id"],
            "charge_credits": charge_credits,
            "amount_cents": int(charge["amount_cents"]),
            "amount_total": charge["amount_total"],
            "currency": "usd",
            "customer_facing": (
                f"{charge_credits:,} credits overage - "
                f"${charge['amount_total']}"
            ),
        },
    )


async def _set_overage_blocked(
    merchant_id: str,
    *,
    payment_intent_id: str = "",
    conn: Any = None,
) -> None:
    target = conn or database
    await target.execute(
        """
        -- merchant_credit_balance:set_overage_blocked
        UPDATE merchant_credit_balance
           SET overage_blocked_until_payment = TRUE,
               overage_last_payment_intent_id = COALESCE(
                   NULLIF(:payment_intent_id, ''),
                   overage_last_payment_intent_id
               ),
               overage_last_failed_at = NOW(),
               updated_at = NOW(),
               version = version + 1
         WHERE merchant_id = :merchant_id
        """,
        {
            "merchant_id": merchant_id,
            "payment_intent_id": payment_intent_id,
        },
    )


async def _charge_direct_overage_increment(
    merchant_id: str,
    *,
    charged_before_credits: int,
    charge_credits: int,
) -> Dict[str, Any]:
    increment_id = _overage_increment_id(
        merchant_id=merchant_id,
        charged_before_credits=charged_before_credits,
        charge_credits=charge_credits,
    )
    amount_cents = _credits_to_amount_cents(charge_credits)
    try:
        payment_intent = await _create_direct_payment_intent(
            merchant_id=merchant_id,
            purpose=DIRECT_OVERAGE_PURPOSE,
            amount_cents=amount_cents,
            idempotency_key=f"direct_overage_payment_intent:{increment_id}",
            metadata={
                "merchant_id": merchant_id,
                "pivota_purpose": DIRECT_OVERAGE_PURPOSE,
                "overage_increment_id": increment_id,
                "charge_credits": str(int(charge_credits)),
                "amount_cents": str(amount_cents),
            },
            description=(
                f"Pivota direct overage: {charge_credits:,} credits - "
                f"${_credits_to_usd_total(charge_credits)}"
            ),
        )
    except MissingVerifiedPaymentMethodError:
        raise
    except Exception as exc:
        raise OveragePaymentFailedError(
            merchant_id,
            str(exc),
            overage_increment_id=increment_id,
        ) from exc

    payment_intent_id = _stripe_id(payment_intent)
    payment_intent_data = _stripe_object_to_dict(payment_intent)
    status = _billing_as_text(
        payment_intent_data.get("status")
        if isinstance(payment_intent_data, dict)
        else getattr(payment_intent, "status", "")
    )
    if status and status != "succeeded":
        raise OveragePaymentFailedError(
            merchant_id,
            status,
            payment_intent_id=payment_intent_id,
            overage_increment_id=increment_id,
        )
    return {
        "payment_intent_id": payment_intent_id,
        "status": status or "succeeded",
        "overage_increment_id": increment_id,
        "charge_credits": int(charge_credits),
        "amount_cents": amount_cents,
        "amount_total": _credits_to_usd_total(charge_credits),
    }


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

        await ensure_row(merchant_id, conn=tx)
        if operation == "debit":
            await apply_subscription_allowance(merchant_id, conn=tx)

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

        for _attempt in range(_MAX_OPTIMISTIC_RETRIES):
            balance = await _get_balance_with_conn(merchant_id, tx)
            available = int(balance["credits"])
            paid_tier = _is_paid_tier(balance.get("plan_tier"))
            if operation == "debit":
                if balance.get("overage_blocked_until_payment"):
                    raise OveragePaymentBlockedError(merchant_id)
                if available < int(amount) and not paid_tier:
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
                balance_debit = min(available, int(amount))
                overage_shortfall = max(0, int(amount) - available)
                pending_before = int(balance.get("overage_pending_credits") or 0)
                pending_after = pending_before + overage_shortfall
                overage_charge_credits = (
                    (pending_after // DEFAULT_OVERAGE_CHARGE_CREDITS)
                    * DEFAULT_OVERAGE_CHARGE_CREDITS
                    if paid_tier
                    else 0
                )
                overage_charge: Optional[Dict[str, Any]] = None
                if overage_charge_credits:
                    overage_charge = await _charge_direct_overage_increment(
                        merchant_id,
                        charged_before_credits=int(
                            balance.get("overage_charged_credits") or 0
                        ),
                        charge_credits=overage_charge_credits,
                    )
                sql = """
                -- merchant_credit_balance:debit_update
                UPDATE merchant_credit_balance
                   SET credits = credits - :balance_debit,
                       purchased_credits = purchased_credits - :purchased_credits,
                       overage_pending_credits = (
                           overage_pending_credits
                           + :overage_pending_credits
                           - :overage_charge_credits
                       ),
                       overage_charged_credits = (
                           overage_charged_credits + :overage_charge_credits
                       ),
                       overage_blocked_until_payment = CASE
                           WHEN :overage_charge_credits > 0 THEN FALSE
                           ELSE overage_blocked_until_payment
                       END,
                       overage_last_payment_intent_id = COALESCE(
                           :overage_payment_intent_id,
                           overage_last_payment_intent_id
                       ),
                       overage_last_failed_at = CASE
                           WHEN :overage_charge_credits > 0 THEN NULL
                           ELSE overage_last_failed_at
                       END,
                       usd_cogs_internal = usd_cogs_internal + :usd_cogs,
                       updated_at = NOW(),
                       version = version + 1
                 WHERE merchant_id = :merchant_id
                   AND version = :version
                   AND credits >= :balance_debit
                   AND purchased_credits >= :purchased_credits
                   AND (
                       overage_pending_credits + :overage_pending_credits
                   ) >= :overage_charge_credits
                RETURNING credits, allowance_credits, usd_cogs_internal,
                          purchased_credits,
                          overage_pending_credits, overage_charged_credits,
                          overage_blocked_until_payment,
                          overage_last_payment_intent_id,
                          overage_last_failed_at,
                          allowance_period_start,
                          plan_tier, updated_at, version
                """
            else:
                balance_debit = 0
                overage_shortfall = 0
                overage_charge_credits = 0
                overage_charge = None
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
                          purchased_credits,
                          overage_pending_credits, overage_charged_credits,
                          overage_blocked_until_payment,
                          overage_last_payment_intent_id,
                          overage_last_failed_at,
                          allowance_period_start,
                          plan_tier, updated_at, version
                """
            params = {
                "merchant_id": merchant_id,
                "amount": int(amount),
                "purchased_credits": (
                    purchased_debit
                    if operation == "debit"
                    else purchased_credit_amount
                ),
                "usd_cogs": usd_cogs,
                "version": int(balance["version"]),
            }
            if operation == "debit":
                params.update(
                    {
                        "balance_debit": balance_debit,
                        "overage_pending_credits": overage_shortfall,
                        "overage_charge_credits": overage_charge_credits,
                        "overage_payment_intent_id": (
                            overage_charge.get("payment_intent_id")
                            if overage_charge
                            else None
                        ),
                    }
                )
            row = await tx.fetch_one(
                sql,
                params,
            )
            if row is not None:
                post_balance = _balance_from_row(row)
                if operation == "debit":
                    post_balance["purchased_credits_debited"] = purchased_debit
                    post_balance["overage_credits_accrued"] = overage_shortfall
                    if overage_charge:
                        post_balance["overage_charge"] = overage_charge
                        await _record_overage_charge_ledger(
                            conn=tx,
                            merchant_id=merchant_id,
                            charge=overage_charge,
                        )
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
    try:
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
    except OveragePaymentFailedError as exc:
        await _set_overage_blocked(
            merchant_id,
            payment_intent_id=exc.payment_intent_id,
            conn=conn,
        )
        raise


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


def _is_stripe_error(exc: Exception) -> bool:
    cls = exc.__class__
    return (
        cls.__name__.endswith("StripeError")
        or str(getattr(cls, "__module__", "")).split(".", 1)[0] == "stripe"
    )


def _default_payment_method_rejection_reason(
    value: Any,
    *,
    stripe_customer_id: str,
) -> Optional[str]:
    data = _stripe_object_to_dict(value)
    if not isinstance(data, dict):
        return "default_payment_method_not_verified_card"
    payment_method_id = _billing_as_text(data.get("id"))
    if not payment_method_id:
        return "default_payment_method_not_verified_card"
    if _billing_as_text(data.get("type")) != "card":
        return "default_payment_method_not_verified_card"
    customer = data.get("customer")
    if customer and _stripe_id(customer) != stripe_customer_id:
        return "default_payment_method_not_verified_card"
    card = data.get("card") if isinstance(data.get("card"), dict) else {}
    try:
        exp_year = int(card.get("exp_year"))
        exp_month = int(card.get("exp_month"))
    except (TypeError, ValueError):
        return "default_payment_method_not_verified_card"
    today = datetime.now(timezone.utc)
    if exp_year < today.year or (
        exp_year == today.year and exp_month < today.month
    ):
        return "card_expired"
    return None


def _is_verified_default_payment_method(value: Any, *, stripe_customer_id: str) -> bool:
    return _default_payment_method_rejection_reason(
        value,
        stripe_customer_id=stripe_customer_id,
    ) is None


async def require_verified_payment_method(merchant_id: str) -> None:
    """Require a chargeable default Stripe card for paid direct audits."""
    await _verified_default_payment_method_for_direct_merchant(merchant_id)


async def _verified_default_payment_method_for_direct_merchant(
    merchant_id: str,
) -> tuple[str, str]:
    """Return (stripe_customer_id, default_payment_method_id) after validation."""
    stripe_customer_id = await _stripe_customer_id_for_direct_merchant(merchant_id)
    if not stripe_customer_id:
        raise MissingVerifiedPaymentMethodError(merchant_id, "missing_stripe_customer")

    try:
        customer = await asyncio.to_thread(
            stripe_client.v1.customers.retrieve,
            stripe_customer_id,
        )
    except Exception as exc:
        if _is_stripe_error(exc):
            raise MissingVerifiedPaymentMethodError(
                merchant_id, "stripe_unavailable",
            ) from exc
        raise
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
            "no_default_pm",
        )

    payment_method = default_payment_method
    if isinstance(default_payment_method, str):
        try:
            payment_method = await asyncio.to_thread(
                stripe_client.v1.payment_methods.retrieve,
                default_payment_method,
            )
        except Exception as exc:
            if _is_stripe_error(exc):
                raise MissingVerifiedPaymentMethodError(
                    merchant_id, "stripe_unavailable",
                ) from exc
            raise
    rejection_reason = _default_payment_method_rejection_reason(
        payment_method,
        stripe_customer_id=stripe_customer_id,
    )
    if rejection_reason:
        raise MissingVerifiedPaymentMethodError(
            merchant_id,
            rejection_reason,
        )
    payment_method_id = _stripe_id(payment_method)
    if not payment_method_id:
        raise MissingVerifiedPaymentMethodError(
            merchant_id,
            "no_default_pm",
        )
    return stripe_customer_id, payment_method_id


async def _create_direct_payment_intent(
    *,
    merchant_id: str,
    purpose: str,
    amount_cents: int,
    idempotency_key: str,
    metadata: Dict[str, str],
    description: str,
) -> Any:
    stripe_customer_id, payment_method_id = (
        await _verified_default_payment_method_for_direct_merchant(merchant_id)
    )
    payload = {
        "amount": int(amount_cents),
        "currency": "usd",
        "customer": stripe_customer_id,
        "payment_method": payment_method_id,
        "off_session": True,
        "confirm": True,
        "metadata": {
            **{str(k): str(v) for k, v in metadata.items()},
            "merchant_id": merchant_id,
            "pivota_purpose": purpose,
        },
        "description": description,
    }
    return await asyncio.to_thread(
        stripe_client.v1.payment_intents.create,
        payload,
        {"idempotency_key": idempotency_key},
    )


async def create_credit_topup_payment_intent(
    *,
    merchant_id: str,
    pack_credits: int,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create and confirm a manual direct-credit top-up PaymentIntent.

    Credits are only added by the Stripe billing webhook after
    `payment_intent.succeeded`; this function intentionally does not mutate the
    balance.
    """
    credits = int(pack_credits)
    if credits <= 0:
        raise ValueError("pack_credits must be > 0")
    amount_cents = _credits_to_amount_cents(credits)
    caller_key = _resolve_topup_idempotency_key(
        merchant_id=merchant_id,
        pack_credits=credits,
        caller_key=idempotency_key,
    )
    payment_intent = await _create_direct_payment_intent(
        merchant_id=merchant_id,
        purpose=DIRECT_TOPUP_PURPOSE,
        amount_cents=amount_cents,
        idempotency_key=caller_key,
        metadata={
            "merchant_id": merchant_id,
            "pivota_purpose": DIRECT_TOPUP_PURPOSE,
            "idempotency_key": caller_key,
            "pack_credits": str(credits),
            "amount_cents": str(amount_cents),
        },
        description=(
            f"Pivota direct credit top-up: {credits:,} credits - "
            f"${_credits_to_usd_total(credits)}"
        ),
    )
    data = _stripe_object_to_dict(payment_intent)
    status = _billing_as_text(data.get("status") if isinstance(data, dict) else "")
    return {
        "payment_intent_id": _stripe_id(payment_intent),
        "status": status or "created",
        "pack_credits": credits,
        "amount": {
            "currency": "usd",
            "total": _credits_to_usd_total(credits),
        },
    }


async def apply_credit_topup_payment_intent_succeeded(
    payment_intent: Any,
    *,
    conn: Any = None,
) -> Dict[str, Any]:
    data = _stripe_object_to_dict(payment_intent)
    if not isinstance(data, dict):
        return {"ignored": True, "reason": "invalid_payment_intent"}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if _billing_as_text(metadata.get("pivota_purpose")) != DIRECT_TOPUP_PURPOSE:
        return {"ignored": True, "reason": "not_direct_credit_topup"}

    payment_intent_id = _billing_as_text(data.get("id"))
    merchant_id = _billing_as_text(metadata.get("merchant_id"))
    pack_credits_raw = _billing_as_text(metadata.get("pack_credits"))
    ledger_idempotency_key = _billing_as_text(metadata.get("idempotency_key"))
    if not payment_intent_id or not merchant_id or not pack_credits_raw:
        raise ValueError("direct credit top-up PaymentIntent metadata is incomplete")
    return await _apply_credit_topup(
        merchant_id=merchant_id,
        pack_credits=int(pack_credits_raw),
        payment_intent_id=payment_intent_id,
        amount_cents=int(data.get("amount") or _credits_to_amount_cents(int(pack_credits_raw))),
        ledger_idempotency_key=ledger_idempotency_key or payment_intent_id,
        conn=conn,
    )


async def _apply_credit_topup(
    *,
    merchant_id: str,
    pack_credits: int,
    payment_intent_id: str,
    amount_cents: int,
    ledger_idempotency_key: Optional[str] = None,
    conn: Any = None,
) -> Dict[str, Any]:
    credits = int(pack_credits)
    if credits <= 0:
        raise ValueError("pack_credits must be > 0")
    source_key = (
        str(ledger_idempotency_key).strip()
        if ledger_idempotency_key and str(ledger_idempotency_key).strip()
        else payment_intent_id
    )
    operation_key = (
        f"merchant_credit_balance:credit_topup:{merchant_id}:{source_key}"
    )
    async with _transaction(conn) as tx:
        replay = await _fetch_replay(conn=tx, operation_key=operation_key)
        if replay is not None:
            return replay

        claimed = await _claim_operation(
            conn=tx,
            operation_key=operation_key,
            merchant_id=merchant_id,
            category="topup",
            amount=credits,
            usd_cogs=Decimal("0"),
            operation="credit",
            event_type="credit_topup",
            source_key=source_key,
            purchased_credits=credits,
            payload_extra={
                "topup_idempotency_key": source_key,
                "payment_intent_id": payment_intent_id,
                "stripe_payment_intent_id": payment_intent_id,
                "pack_credits": credits,
                "amount_cents": int(amount_cents),
                "amount_total": _credits_to_usd_total(credits),
                "currency": "usd",
            },
        )
        if not claimed:
            replay = await _fetch_replay(conn=tx, operation_key=operation_key)
            if replay is not None:
                return replay
            raise RuntimeError("credit top-up replay row was not readable")

        await ensure_row(merchant_id, conn=tx)
        row = await tx.fetch_one(
            """
            -- merchant_credit_balance:topup_credit_update
            UPDATE merchant_credit_balance
               SET credits = credits + :pack_credits,
                   purchased_credits = purchased_credits + :pack_credits,
                   overage_blocked_until_payment = FALSE,
                   overage_last_payment_intent_id = :payment_intent_id,
                   overage_last_failed_at = NULL,
                   updated_at = NOW(),
                   version = version + 1
             WHERE merchant_id = :merchant_id
            RETURNING credits, allowance_credits, usd_cogs_internal,
                      purchased_credits,
                      overage_pending_credits, overage_charged_credits,
                      overage_blocked_until_payment,
                      overage_last_payment_intent_id,
                      overage_last_failed_at,
                      allowance_period_start,
                      plan_tier, updated_at, version
            """,
            {
                "merchant_id": merchant_id,
                "pack_credits": credits,
                "payment_intent_id": payment_intent_id,
            },
        )
        if row is None:
            raise RuntimeError("credit top-up update did not return a balance row")
        post_balance = _balance_from_row(row)
        post_balance["purchased_credits_credited"] = credits
        await _store_post_balance(
            conn=tx,
            operation_key=operation_key,
            post_balance=post_balance,
        )
        post_balance["replay"] = False
        return post_balance


async def charge_pending_overage_for_merchant(
    merchant_id: str,
    *,
    force: bool = False,
    retry_blocked: bool = False,
    conn: Any = None,
) -> Dict[str, Any]:
    """Charge pending direct overage.

    `force=True` is the daily-sweep mode: collect the current pending amount
    even if it is below the normal $20 threshold. This function is intentionally
    cron-callable; it does not schedule itself.
    """
    try:
        return await _charge_pending_overage_for_merchant(
            merchant_id,
            force=force,
            retry_blocked=retry_blocked,
            conn=conn,
        )
    except OveragePaymentFailedError as exc:
        await _set_overage_blocked(
            merchant_id,
            payment_intent_id=exc.payment_intent_id,
            conn=conn,
        )
        raise


async def _charge_pending_overage_for_merchant(
    merchant_id: str,
    *,
    force: bool,
    retry_blocked: bool,
    conn: Any = None,
) -> Dict[str, Any]:
    async with _transaction(conn) as tx:
        balance = await _get_balance_with_conn(merchant_id, tx)
        if not balance:
            return {"charged": False, "reason": "missing_balance"}
        if balance.get("overage_blocked_until_payment") and not retry_blocked:
            raise OveragePaymentBlockedError(merchant_id)
        pending = int(balance.get("overage_pending_credits") or 0)
        if pending <= 0:
            return {"charged": False, "reason": "no_pending_overage"}
        if force:
            charge_credits = pending
        else:
            charge_credits = (
                pending // DEFAULT_OVERAGE_CHARGE_CREDITS
            ) * DEFAULT_OVERAGE_CHARGE_CREDITS
        if charge_credits <= 0:
            return {"charged": False, "reason": "below_threshold"}

        charge = await _charge_direct_overage_increment(
            merchant_id,
            charged_before_credits=int(balance.get("overage_charged_credits") or 0),
            charge_credits=charge_credits,
        )
        row = await tx.fetch_one(
            """
            -- merchant_credit_balance:pending_overage_charge_update
            UPDATE merchant_credit_balance
               SET overage_pending_credits = overage_pending_credits - :charge_credits,
                   overage_charged_credits = overage_charged_credits + :charge_credits,
                   overage_blocked_until_payment = FALSE,
                   overage_last_payment_intent_id = :payment_intent_id,
                   overage_last_failed_at = NULL,
                   updated_at = NOW(),
                   version = version + 1
             WHERE merchant_id = :merchant_id
               AND overage_pending_credits >= :charge_credits
            RETURNING credits, allowance_credits, usd_cogs_internal,
                      purchased_credits,
                      overage_pending_credits, overage_charged_credits,
                      overage_blocked_until_payment,
                      overage_last_payment_intent_id,
                      overage_last_failed_at,
                      allowance_period_start,
                      plan_tier, updated_at, version
            """,
            {
                "merchant_id": merchant_id,
                "charge_credits": charge_credits,
                "payment_intent_id": charge["payment_intent_id"],
            },
        )
        if row is None:
            raise RuntimeError("pending overage charge update did not converge")
        await _record_overage_charge_ledger(
            conn=tx,
            merchant_id=merchant_id,
            charge=charge,
        )
        return {
            "charged": True,
            "balance": _balance_from_row(row),
            "overage_charge": charge,
        }


async def sweep_pending_overage_charges(
    *,
    limit: int = 100,
    force: bool = True,
    conn: Any = None,
) -> Dict[str, Any]:
    """Cron-callable daily sweep for direct overage balances."""
    target = conn or database
    rows = await target.fetch_all(
        """
        -- merchant_credit_balance:sweep_pending_overage_candidates
        SELECT merchant_id
          FROM merchant_credit_balance
         WHERE plan_tier <> 'free'
           AND overage_pending_credits > 0
           AND overage_blocked_until_payment = FALSE
         ORDER BY updated_at ASC
         LIMIT :limit
        """,
        {"limit": int(limit)},
    )
    charged = 0
    failed = 0
    skipped = 0
    results = []
    for row in rows or []:
        merchant_id = _billing_as_text(_row_to_dict(row).get("merchant_id"))
        if not merchant_id:
            continue
        try:
            result = await charge_pending_overage_for_merchant(
                merchant_id,
                force=force,
                conn=conn,
            )
            results.append({"merchant_id": merchant_id, **result})
            if result.get("charged"):
                charged += 1
            else:
                skipped += 1
        except OveragePaymentFailedError as exc:
            failed += 1
            results.append({
                "merchant_id": merchant_id,
                "charged": False,
                "error": exc.code,
                "reason": exc.reason,
            })
    return {
        "checked": len(rows or []),
        "charged": charged,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }
