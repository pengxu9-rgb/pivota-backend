"""
订单数据库表和 CRUD 操作
防御性架构：订单是核心业务数据，只能追加和更新状态，不能删除
"""

import asyncio
import json
import logging

from sqlalchemy import Table, Column, Integer, String, Text, DateTime, JSON, Numeric, Boolean
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional, List, Dict, Any
import secrets
import os
from sqlalchemy import text

from db.database import IS_POSTGRES, metadata, database
from db.schema_guard import (
    _is_unique_violation,
    column_is_not_null,
    column_type_differs,
    guarded_add_columns,
    guarded_ddl,
)

from utils.agent_identity import INTERNAL_TRUSTED_AGENT_PREFIX, caller_identity, is_internal_trusted_identity

logger = logging.getLogger(__name__)


# ============================================================================
# 订单表（核心业务数据）
# ============================================================================

orders = Table(
    "orders",
    metadata,
    Column("order_id", String(50), primary_key=True),  # 订单唯一ID（主键）
    Column("merchant_id", String(50), index=True, nullable=False),
    
    # Legacy fields (for backward compatibility with existing DB)
    Column("store_id", String(50), nullable=True),
    Column("psp_id", String(50), nullable=True),
    # Column("amount", Numeric(10, 2), nullable=True),  # REMOVED - use "total" instead
    
    # 客户信息
    Column("customer_name", String(255), nullable=True),
    Column("customer_email", String(255), nullable=False),
    Column("shipping_address", JSON, nullable=False),  # ShippingAddress JSON
    
    # 订单内容
    Column("items", JSON, nullable=False),  # List[OrderItem] JSON
    
    # 金额（使用 Numeric 精确存储）
    Column("subtotal", Numeric(10, 2), nullable=False),
    Column("discount_total", Numeric(10, 2), default=0),
    Column("shipping_fee", Numeric(10, 2), default=0),
    Column("tax", Numeric(10, 2), default=0),
    Column("total", Numeric(10, 2), nullable=False),
    Column("total_refunded", Numeric(10, 2), default=0),
    Column("currency", String(3), default="USD"),
    
    # 状态机
    Column("status", String(50), default="pending", index=True),  # 订单状态
    Column("payment_status", String(50), default="unpaid", index=True),  # 支付状态
    Column("payment_method", String(50), nullable=True),  # Legacy payment method field
    Column("fulfillment_status", String(50), nullable=True),  # 履约状态
    
    # 支付集成（Stripe）
    Column("payment_intent_id", String(255), nullable=True, unique=True),
    Column("payment_method_id", String(255), nullable=True),
    Column("client_secret", Text, nullable=True),  # PSP client secret / Adyen sessionData
    Column("psp_used", String(50), nullable=True),  # Which PSP was actually used for this order
    
    # 履约集成（Shopify/Wix）
    Column("shopify_order_id", String(255), nullable=True, unique=True),
    Column("tracking_number", String(255), nullable=True),
    Column("carrier", String(100), nullable=True),
    
    # 时间戳
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    Column("paid_at", DateTime(timezone=True), nullable=True),
    Column("shipped_at", DateTime(timezone=True), nullable=True),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    
    # 元数据
    Column("agent_id", String(255), nullable=True, index=True),  # Agent who created this order
    Column("agent_session_id", String(255), nullable=True, index=True),
    Column("metadata", JSON, nullable=True),

    # Buyer Vault linkage (internal-only; never expose global buyer_id to agents)
    Column("buyer_id", String(50), nullable=True, index=True),
    Column("intent_id", String(80), nullable=True, index=True),
    Column("agent_user_ref", String(255), nullable=True, index=True),
    Column("agent_scoped_buyer_ref", String(128), nullable=True, index=True),
    
    # 软删除（防御性设计：订单不能真删除）
    Column("is_deleted", Boolean, default=False, index=True),
)


# ============================================================================
# One order per quote, per agent
# ============================================================================
#
# A quote backs at most one order for the agent that holds it. The row here is that promise, and
# its primary key is what keeps it under concurrency: two creates for one (agent, quote) cannot
# both insert it, on any isolation level, because a unique index is checked against rows other
# transactions have written, not against a snapshot. A "does an order for this quote exist?" read
# before the insert cannot give that: under READ COMMITTED two creates each read "no" and both
# insert.
#
# The row names the ONE order id the quote may use, and `orders.order_id` is a primary key too. So
# whoever writes the order writes it under that id, and at most one such write can succeed. That
# is why claim and order need no shared transaction: a claim whose order was never written (the
# process died between the two statements) is finished by the next create for that quote, under
# the id the claim already names, and never blocks the quote.
#
# A new, empty table: no row written before it existed can violate it, so creating it cannot fail
# on old data. `metadata.create_all` builds it at boot; db/migrations/284 is the record. Until it
# exists `create_order` writes the order without it and says so at WARNING on every such order.
# Rows are never deleted: a cancelled or soft-deleted order still used its quote.

ORDER_QUOTE_CLAIMS_TABLE = "order_quote_claims"

order_quote_claims = Table(
    ORDER_QUOTE_CLAIMS_TABLE,
    metadata,
    # The CALLER the order's agent acts as (utils.agent_identity.caller_identity): the agent id,
    # one shared value for every internal trusted key, and '' for an order created with a quote
    # and no agent (the admin create route).
    Column("agent_id", String(255), primary_key=True),
    Column("quote_id", String(255), primary_key=True),
    Column("order_id", String(50), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

# Module-level constants, identical on both dialects (the SQL prepare gate plans them).
_QUOTE_CLAIM_INSERT_SQL = (
    "INSERT INTO order_quote_claims (agent_id, quote_id, order_id) "
    "VALUES (:agent_id, :quote_id, :order_id) "
    "ON CONFLICT (agent_id, quote_id) DO NOTHING"
)
_QUOTE_CLAIM_SELECT_SQL = (
    "SELECT order_id FROM order_quote_claims WHERE agent_id = :agent_id AND quote_id = :quote_id"
)
# Soft-deleted rows count: the question is whether the id is taken, not whether the order shows.
_QUOTE_CLAIM_ORDER_EXISTS_SQL = "SELECT order_id FROM orders WHERE order_id = :order_id"


class OrderQuoteAlreadyClaimed(Exception):
    """This agent's quote already has its order. Nothing was written by this call."""

    def __init__(self, order_id: str):
        super().__init__(f"an order already exists for this quote: {order_id}")
        self.order_id = order_id


class OrderQuoteClaimUnavailable(Exception):
    """The one-order-per-quote guard could not be read or written, so it is not known whether the
    quote already has an order. No order was written by this call. Retriable."""


def _quote_claim_key(order_data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """(agent, quote) of an order about to be written, or None for an order with no quote.

    Read from the row itself, from the same places the replay lookup reads
    (`metadata.pricing_quote.quote_id`, `agent_id`), so the guard and the lookup cannot disagree
    about which order belongs to which quote.

    The agent is the caller it acts as, by the SAME rule quote ownership uses
    (`caller_identity`): ownership lets every internal trusted key use a quote any of them
    previewed, so they must share its one claim too. Keyed by the raw id, a create under one key
    and a resend under another (a key rotation) each took a claim and each wrote an order.
    """
    md = order_data.get("metadata")
    pricing_quote = md.get("pricing_quote") if isinstance(md, dict) else None
    quote_id = str((pricing_quote.get("quote_id") if isinstance(pricing_quote, dict) else "") or "").strip()
    if not quote_id:
        return None
    return {"agent_id": caller_identity(order_data.get("agent_id")), "quote_id": quote_id}


_SQLITE_NO_CLAIMS_TABLE = f"no such table: {ORDER_QUOTE_CLAIMS_TABLE}"


def _is_missing_quote_claims_table(exc: BaseException) -> bool:
    """True only when the claim INSERT failed because the claims TABLE does not exist.

    PostgreSQL: decided by the error's class and SQLSTATE, never by its text. asyncpg raises
    `UndefinedTableError` with `sqlstate` 42P01 (the `databases` layer passes it through
    unwrapped); psycopg2, the sync driver, carries the same code as `pgcode`. The text is NOT
    read, because "does not exist" is also how PostgreSQL words a missing COLUMN of this table
    (42703), a missing function, a missing schema: none of those is an absent table, and each
    must refuse the order (the caller turns every other error into "cannot tell"). The undefined
    table must also be named as this one (`relation "order_quote_claims" does not exist`): an
    undefined table elsewhere in the statement's reach is not the guard's table missing.

    SQLite has no error codes for this, so there it is the one exact message of an
    `OperationalError`: "no such table: order_quote_claims".
    """
    seen, candidates = set(), [exc]
    while candidates:
        current = candidates.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        undefined_table = (
            getattr(current, "sqlstate", None) == "42P01"
            or getattr(current, "pgcode", None) == "42P01"
            or type(current).__name__ == "UndefinedTableError"
        )
        # The undefined table must be THIS one: a statement can reach another table (a trigger, a
        # rule, a view added later), and an absent table there is not the guard's table missing.
        if undefined_table and f'"{ORDER_QUOTE_CLAIMS_TABLE}"' in str(current):
            return True
        if type(current).__name__ == "OperationalError" and str(current).strip().lower() == _SQLITE_NO_CLAIMS_TABLE:
            return True
        candidates.extend((current.__cause__, current.__context__, getattr(current, "orig", None)))
    return False


async def _claim_quote_for_order(claim_key: Dict[str, str], order_id: str) -> Optional[str]:
    """Claim the quote for `order_id`. Returns the order id the order MUST be written under, or
    None when the claims table does not exist yet (the order is then written unguarded).

    Raises OrderQuoteAlreadyClaimed when the quote's order is already written, and
    OrderQuoteClaimUnavailable when that could not be told. Either way nothing else was written.
    """
    try:
        await database.execute(_QUOTE_CLAIM_INSERT_SQL, {**claim_key, "order_id": order_id})
    except Exception as exc:
        if _is_missing_quote_claims_table(exc):
            logger.warning(
                "[orders] one-order-per-quote guard is OFF: table %s does not exist. Order %s is "
                "written without it (quote %s). Create the table (db/migrations/284) or restart "
                "the service so boot creates it.",
                ORDER_QUOTE_CLAIMS_TABLE,
                order_id,
                claim_key["quote_id"],
            )
            return None
        logger.warning(
            "[orders] one-order-per-quote guard could not claim quote %s: %s; no order written",
            claim_key["quote_id"],
            type(exc).__name__,
        )
        raise OrderQuoteClaimUnavailable(str(exc)) from exc
    try:
        claim = await database.fetch_one(_QUOTE_CLAIM_SELECT_SQL, claim_key)
        claimed_order_id = str((claim["order_id"] if claim else "") or "").strip()
        if not claimed_order_id:
            raise RuntimeError("quote claim row is missing after it was written")
        if claimed_order_id == order_id:
            return order_id
        holder = await database.fetch_one(_QUOTE_CLAIM_ORDER_EXISTS_SQL, {"order_id": claimed_order_id})
    except Exception as exc:
        logger.warning(
            "[orders] one-order-per-quote guard could not be read for quote %s: %s; no order written",
            claim_key["quote_id"],
            type(exc).__name__,
        )
        raise OrderQuoteClaimUnavailable(str(exc)) from exc
    if holder:
        raise OrderQuoteAlreadyClaimed(claimed_order_id)
    # An earlier create claimed the quote and its order row is not there: it died before writing
    # it, or is about to. Write the order under THAT id; the primary key lets one of us through.
    logger.warning(
        "[orders] quote %s was claimed for order %s, which was never written; writing it now",
        claim_key["quote_id"],
        claimed_order_id,
    )
    return claimed_order_id


# ============================================================================
# CRUD 操作
# ============================================================================

_client_secret_storage_ready = False
_client_secret_storage_lock = asyncio.Lock()


async def _ensure_client_secret_storage_allows_long_values() -> None:
    """
    Make `orders.client_secret` safe for long PSP surfaces such as Adyen sessionData.

    Older environments still carry `VARCHAR(500)`, which truncates Adyen sessions
    and makes client-owned confirmation surfaces invalid. We self-heal to `TEXT`
    once and keep the call best-effort.

    Guarded (db/schema_guard.py): bare, the first long secret of every process
    took ACCESS EXCLUSIVE on `orders` for an ALTER COLUMN TYPE with no
    lock_timeout, queued behind any open transaction on the table with every
    checkout queued behind it, although the column has long been TEXT. Now each
    statement runs only while needed, under a 500ms lock_timeout; the process
    retries on a later call until both have succeeded.
    """
    global _client_secret_storage_ready

    if _client_secret_storage_ready:
        return

    async with _client_secret_storage_lock:
        if _client_secret_storage_ready:
            return
        if not IS_POSTGRES:
            # Neither statement exists in SQLite; both always failed there.
            _client_secret_storage_ready = True
            return
        healed = True
        for statement in (
            *guarded_add_columns(
                "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS client_secret TEXT;"
            ),
            guarded_ddl(
                "orders",
                column_type_differs("orders", ("client_secret",), "text"),
                "ALTER TABLE orders ALTER COLUMN client_secret TYPE TEXT;",
            ),
        ):
            try:
                await database.execute(statement)
            except Exception:
                healed = False
        _client_secret_storage_ready = healed


async def _heal_orders(sql: str) -> None:
    """Run one of the error-path heals below guarded (db/schema_guard.py).

    They fire on almost any failed insert or read (the triggers match a bare
    substring such as "total" or "amount"), and bare each one took ACCESS
    EXCLUSIVE on `orders` with no lock_timeout. Guarded, one whose column is
    already there reads the catalog and takes no lock. A failure raises, as the
    bare statement's did.
    """
    for statement in guarded_add_columns(sql):
        await database.execute(statement)

def _stamp_test_traffic(order_data: Dict[str, Any], is_test_merchant_ids: Optional[set] = None) -> None:
    """O-07: stamp a test order's metadata at write (services.test_traffic). Every order create
    reaches the table through create_order, so this is the one place. A caller-supplied stamp is
    removed whatever the flag (the money reads exclude stamped rows); with
    TEST_TRAFFIC_STAMP_ENABLED off nothing is added, and a real order is never touched.
    `is_test_merchant_ids` is create_order's write-time primary-key answer (M-05)."""
    from services import test_traffic

    md = order_data.get("metadata")
    if isinstance(md, dict):
        test_traffic.strip_caller_markers(md)
    reasons = test_traffic.classify_test_traffic(
        merchant_ids=[order_data.get("merchant_id")],
        server_metadata=md if isinstance(md, dict) else None,
        is_test_merchant_ids=is_test_merchant_ids,
    )
    if reasons and test_traffic.stamp_enabled():
        order_data["metadata"] = test_traffic.stamp(md if isinstance(md, dict) else None, reasons)


async def create_order(order_data: Dict[str, Any]) -> str:
    """创建新订单"""
    order_id = f"ORD_{secrets.token_hex(8).upper()}"
    order_data["order_id"] = order_id
    order_data["status"] = "pending"
    order_data["payment_status"] = "unpaid"
    order_data["is_deleted"] = False  # Explicitly set for PostgreSQL
    # M-05: the merchant's is_test, read by primary key now, never from a cache that may be stale
    # (None, with no read, unless both the stamp and the is_test filter flags are on; never raises;
    # a failure or timeout is "not test").
    from services.test_traffic import read_is_test_merchant_ids

    is_test_ids = await read_is_test_merchant_ids([order_data.get("merchant_id")])
    _stamp_test_traffic(order_data, is_test_ids)

    # One order per quote, per agent: claim the quote BEFORE the order is written. See
    # `order_quote_claims` above. With the claim held, a primary-key refusal of the insert means
    # the quote's order is already there (written by an earlier attempt of this create whose
    # answer was lost, or by a concurrent create that finished the same claim).
    quote_claim_held = False
    claim_key = _quote_claim_key(order_data)
    if claim_key is not None:
        claimed_order_id = await _claim_quote_for_order(claim_key, order_id)
        if claimed_order_id is not None:
            quote_claim_held = True
            order_id = claimed_order_id
            order_data["order_id"] = order_id

    query = orders.insert().values(**order_data)
    try:
        await database.execute(query)
        return order_id
    except Exception as e:
        if quote_claim_held and _is_unique_violation(e):
            raise OrderQuoteAlreadyClaimed(order_id) from e
        # Auto-migrate missing columns on production DBs, then retry once
        err = str(e)
        try:
            from sqlalchemy import text
            if "column \"shipping_address\" of relation \"orders\" does not exist" in err or "shipping_address" in err:
                # Add missing columns defensively
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS shipping_address JSONB;"
                )
            if "column \"items\" of relation \"orders\" does not exist" in err or " column \"items\"" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS items JSONB;"
                )
            if "column \"client_secret\" of relation \"orders\" does not exist" in err or "client_secret" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS client_secret TEXT;"
                )
            if "column \"subtotal\" of relation \"orders\" does not exist" in err or "subtotal" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS subtotal NUMERIC(10,2);"
                )
            if "column \"discount_total\" of relation \"orders\" does not exist" in err or "discount_total" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS discount_total NUMERIC(10,2) DEFAULT 0;"
                )
            if "column \"tax\" of relation \"orders\" does not exist" in err or "tax" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS tax NUMERIC(10,2);"
                )
            if "column \"total\" of relation \"orders\" does not exist" in err or "total" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS total NUMERIC(10,2);"
                )
            if "column \"total_refunded\" of relation \"orders\" does not exist" in err or "total_refunded" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS total_refunded NUMERIC(10,2) DEFAULT 0;"
                )
            if "column \"shipping_fee\" of relation \"orders\" does not exist" in err or "shipping_fee" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS shipping_fee NUMERIC(10,2);"
                )
            if "column \"payment_status\" of relation \"orders\" does not exist" in err or "payment_status" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS payment_status VARCHAR(50);"
                )
            if "column \"agent_id\" of relation \"orders\" does not exist" in err or "agent_id" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS agent_id VARCHAR(50);"
                )
            if "column \"agent_session_id\" of relation \"orders\" does not exist" in err or "agent_session_id" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS agent_session_id VARCHAR(100);"
                )
            if "column \"is_deleted\" of relation \"orders\" does not exist" in err or "is_deleted" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;"
                )
            if "column \"metadata\" of relation \"orders\" does not exist" in err or "metadata" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS metadata JSONB;"
                )
            if "column \"buyer_id\" of relation \"orders\" does not exist" in err or "buyer_id" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS buyer_id VARCHAR(50);"
                )
            if "column \"intent_id\" of relation \"orders\" does not exist" in err or "intent_id" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS intent_id VARCHAR(80);"
                )
            if "column \"agent_user_ref\" of relation \"orders\" does not exist" in err or "agent_user_ref" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS agent_user_ref VARCHAR(255);"
                )
            if "column \"agent_scoped_buyer_ref\" of relation \"orders\" does not exist" in err or "agent_scoped_buyer_ref" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS agent_scoped_buyer_ref VARCHAR(128);"
                )
            if "column \"psp_used\" of relation \"orders\" does not exist" in err or "psp_used" in err or "Unconsumed column names: psp_used" in err:
                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS psp_used VARCHAR(50);"
                )
            if "null value in column \"amount\"" in err or "amount" in err:
                # Drop NOT NULL constraint on amount column (we use total instead)
                await database.execute(
                    guarded_ddl(
                        "orders",
                        column_is_not_null("orders", "amount"),
                        "ALTER TABLE orders ALTER COLUMN amount DROP NOT NULL;",
                    )
                )
            # Retry the insert once after migration
            await database.execute(query)
            return order_id
        except Exception as mig_err:
            if quote_claim_held and _is_unique_violation(mig_err):
                raise OrderQuoteAlreadyClaimed(order_id) from mig_err
            # Surface original error if migration fails
            raise mig_err


async def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    """获取订单详情"""
    # PostgreSQL-compatible query
    query = orders.select().where(
        (orders.c.order_id == order_id) & 
        orders.c.is_deleted.is_(False)
    )
    try:
        result = await database.fetch_one(query)
    except Exception as e:
        # Defensive: some prod DBs may lag behind the SQLAlchemy table definition.
        err = str(e)
        if "total_refunded" in err:
            try:
                from sqlalchemy import text

                await _heal_orders(
                    "ALTER TABLE IF EXISTS orders ADD COLUMN IF NOT EXISTS total_refunded NUMERIC(10,2) DEFAULT 0;"
                )
                result = await database.fetch_one(query)
            except Exception:
                raise
        else:
            raise
    return dict(result) if result else None


async def find_replayable_order_for_create(
    *,
    merchant_id: str,
    agent_id: Optional[str],
    idempotency_key: Optional[str] = None,
    quote_id: Optional[str] = None,
    agent_session_id: Optional[str] = None,
    preferred_psp: Optional[str] = None,
    intent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Find the most recent order that is safe to replay for order-create recovery.

    Priority:
    1. metadata.idempotency_key
    2. metadata.pricing_quote.quote_id
    3. agent_session_id

    Every lookup is scoped to the calling agent (`orders.agent_id`). Keys are chosen by the caller,
    and every agent reaches the same merchants, so a key another agent used must never hand this
    caller that agent's order: it simply does not match, and the caller gets a fresh create. With no
    agent there is nothing to scope by, so nothing replays. `intent_id` (a checkout token's session)
    narrows it further to the orders created under that token.

    The QUOTE lookup alone matches "the calling agent" as the caller it acts as (`caller_identity`),
    the rule quote ownership and the quote claim use: an internal trusted key finds an order ANY
    internal trusted key made for that quote, because they are one caller for a quote (the
    service's own credential, across a key rotation), and the claim already makes that order the
    quote's only one. The idempotency-key and session lookups match the raw agent id, as orders
    are owned: an internal key replays only its own orders by key or session. Every other agent
    matches its own id exactly in all three, as before.
    """
    merchant_id = str(merchant_id or "").strip()
    if not merchant_id:
        return None
    agent_id = str(agent_id or "").strip()
    if not agent_id:
        return None
    intent_id = str(intent_id or "").strip() or None
    intent_clause = "\n                  AND metadata ->> 'intent_id' = :intent_id" if intent_id else ""
    # An exact match, or for the shared internal identity a match on its literal prefix (substr,
    # not LIKE: the prefix contains `_`, which LIKE reads as "any character").
    exact_values: Dict[str, Any] = {"agent_id": agent_id}
    exact_clause = "agent_id = :agent_id"
    quote_values, quote_clause = exact_values, exact_clause
    if is_internal_trusted_identity(caller_identity(agent_id)):
        quote_values = {"agent_id": INTERNAL_TRUSTED_AGENT_PREFIX, "agent_prefix_len": len(INTERNAL_TRUSTED_AGENT_PREFIX)}
        quote_clause = "substr(agent_id, 1, :agent_prefix_len) = :agent_id"

    def _normalize_preferred_psp(value: Optional[str]) -> Optional[str]:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized == "stripe_checkout":
            return "stripe"
        return normalized

    def _replay_matches_preferred_psp(row: Dict[str, Any], preferred: Optional[str]) -> bool:
        if not preferred:
            return True
        actual = _normalize_preferred_psp(
            (row or {}).get("psp_used")
            or ((row or {}).get("metadata") or {}).get("preferred_psp")
            or ((row or {}).get("metadata") or {}).get("selected_psp")
        )
        return actual == preferred

    normalized_preferred_psp = _normalize_preferred_psp(preferred_psp)
    candidate_queries = []
    if idempotency_key:
        candidate_queries.append(
            (
                f"""
                SELECT *
                FROM orders
                WHERE merchant_id = :merchant_id
                  AND {exact_clause}
                  AND COALESCE(is_deleted, FALSE) = FALSE
                  AND metadata ->> 'idempotency_key' = :match_value{intent_clause}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(idempotency_key).strip(),
                "idempotency_key",
                exact_values,
            )
        )
    if quote_id:
        candidate_queries.append(
            (
                f"""
                SELECT *
                FROM orders
                WHERE merchant_id = :merchant_id
                  AND {quote_clause}
                  AND COALESCE(is_deleted, FALSE) = FALSE
                  AND metadata -> 'pricing_quote' ->> 'quote_id' = :match_value{intent_clause}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(quote_id).strip(),
                "quote_id",
                quote_values,
            )
        )
    if agent_session_id:
        candidate_queries.append(
            (
                f"""
                SELECT *
                FROM orders
                WHERE merchant_id = :merchant_id
                  AND {exact_clause}
                  AND COALESCE(is_deleted, FALSE) = FALSE
                  AND agent_session_id = :match_value{intent_clause}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                str(agent_session_id).strip(),
                "agent_session_id",
                exact_values,
            )
        )

    for query, match_value, replay_scope, agent_values in candidate_queries:
        if not match_value:
            continue
        values = {"merchant_id": merchant_id, **agent_values, "match_value": match_value}
        if intent_id:
            values["intent_id"] = intent_id
        row = await database.fetch_one(query, values)
        if row:
            row_dict = dict(row)
            if replay_scope != "idempotency_key" and not _replay_matches_preferred_psp(
                row_dict,
                normalized_preferred_psp,
            ):
                continue
            return row_dict
    return None


async def get_orders_by_merchant(
    merchant_id: str, 
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """获取商户的订单列表"""
    query = orders.select().where(
        (orders.c.merchant_id == merchant_id) & 
        orders.c.is_deleted.is_(False)
    )
    
    if status:
        query = query.where(orders.c.status == status)
    
    query = query.order_by(orders.c.created_at.desc()).limit(limit).offset(offset)
    
    results = await database.fetch_all(query)
    return [dict(r) for r in results]


async def get_orders_by_customer(customer_email: str, limit: int = 50) -> List[Dict[str, Any]]:
    """获取客户的订单列表"""
    query = orders.select().where(
        (orders.c.customer_email == customer_email) & 
        orders.c.is_deleted.is_(False)
    ).order_by(orders.c.created_at.desc()).limit(limit)
    
    results = await database.fetch_all(query)
    return [dict(r) for r in results]


def _coerce_metadata_obj(value: Any) -> Dict[str, Any]:
    """`orders.metadata` as a dict. Read through a raw TEXT query, asyncpg hands the JSON column
    back as a `str` (no JSON codec is registered, and must not be: it double-encodes writes); only
    a SQLAlchemy-typed select decodes it. Non-object / unparseable -> {}."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def update_order_status(
    order_id: str,
    status: str, 
    **additional_fields
) -> bool:
    """更新订单状态（防御性：只能前进，不能回退）

    Side-effect (best-effort): when an order transitions into a "completed" or
    shipped/delivered fulfillment state, enqueue a review invitation email job
    for the worker to process.
    """
    before = None
    try:
        before = await database.fetch_one(
            """
            SELECT merchant_id, status, fulfillment_status, payment_status, metadata
            FROM orders
            WHERE order_id = :order_id
            LIMIT 1
            """,
            {"order_id": order_id},
        )
    except Exception:
        before = None

    update_data = {"status": status, "updated_at": datetime.now()}
    update_data.update(additional_fields)
    if isinstance(update_data.get("metadata"), dict):
        existing_metadata = {}
        try:
            existing_metadata = _coerce_metadata_obj(before["metadata"] if before else None)
        except Exception:
            existing_metadata = {}
        # Metadata updates are additive by default. This prevents webhook/aftercare
        # handlers using an older order snapshot from erasing recovery/audit keys
        # such as merchant_order or payment_recovery.
        update_data["metadata"] = {**existing_metadata, **update_data["metadata"]}

    # Defensive: ignore fields not present in the SQLAlchemy table definition.
    # Some environments have divergent schemas; passing unknown keys raises:
    # "Unconsumed column names: <...>"
    valid_cols = set(orders.c.keys())
    update_data = {k: v for (k, v) in update_data.items() if k in valid_cols}
    
    query = orders.update().where(
        orders.c.order_id == order_id
    ).values(**update_data)
    
    result = await database.execute(query)
    ok = result is not None and result > 0
    after = None

    async def _fetch_after_update():
        return await database.fetch_one(
            """
            SELECT merchant_id, status, fulfillment_status, payment_status, shopify_order_id
            FROM orders
            WHERE order_id = :order_id
            LIMIT 1
            """,
            {"order_id": order_id},
        )

    if not ok:
        # Some DB drivers/environments return no reliable rowcount for UPDATE
        # even when the write committed. Verify the intended state before
        # treating the update as failed.
        try:
            after = await _fetch_after_update()
        except Exception:
            after = None
        if after:
            try:
                status_matches = str(after.get("status") or "").strip() == str(status).strip()
                payment_status_matches = (
                    "payment_status" not in update_data
                    or str(after.get("payment_status") or "").strip()
                    == str(update_data.get("payment_status") or "").strip()
                )
                fulfillment_status_matches = (
                    "fulfillment_status" not in update_data
                    or str(after.get("fulfillment_status") or "").strip()
                    == str(update_data.get("fulfillment_status") or "").strip()
                )
                shopify_order_matches = (
                    "shopify_order_id" not in update_data
                    or str(after.get("shopify_order_id") or "").strip()
                    == str(update_data.get("shopify_order_id") or "").strip()
                )
                ok = bool(
                    status_matches
                    and payment_status_matches
                    and fulfillment_status_matches
                    and shopify_order_matches
                )
            except Exception:
                ok = False

    # Best-effort invitation enqueue on completion/shipping transitions.
    if ok:
        if after is None:
            try:
                after = await _fetch_after_update()
            except Exception:
                after = None

        try:
            if before and after:
                before_status = str(before.get("status") or "").strip().lower()
                after_status = str(after.get("status") or "").strip().lower()
                before_ful = str(before.get("fulfillment_status") or "").strip().lower()
                after_ful = str(after.get("fulfillment_status") or "").strip().lower()
                after_paid = str(after.get("payment_status") or "").strip().lower() == "paid"
                merchant_id = str(after.get("merchant_id") or "").strip()

                transitioned_completed = before_status != "completed" and after_status == "completed"
                transitioned_fulfilled = before_ful != after_ful and after_ful in {"shipped", "delivered"}

                if merchant_id and after_paid and (transitioned_completed or transitioned_fulfilled):
                    from services.reviews_invitation_send_jobs_service import (
                        enqueue_invitation_send_job_from_order,
                    )

                    await enqueue_invitation_send_job_from_order(
                        merchant_id=merchant_id,
                        order_id=order_id,
                        force_reschedule=False,
                    )
        except Exception:
            pass

    return ok


async def update_payment_info(
    order_id: str,
    payment_intent_id: str,
    client_secret: str,
    payment_status: str = "processing",
    psp_used: Optional[str] = None
) -> bool:
    """更新支付信息
    
    - payment_intent_id / client_secret / payment_status 按照原有逻辑更新
    - 可选的 psp_used 用于记录实际使用的 PSP 提供方（如 'stripe' 或 'adyen'）
    """
    safe_secret = client_secret
    try:
        if client_secret and len(str(client_secret)) > 480:
            await _ensure_client_secret_storage_allows_long_values()
            safe_secret = str(client_secret)
    except Exception:
        # Preserve the original secret if self-heal fails; callers still need the
        # complete PSP surface for client confirmation flows.
        safe_secret = client_secret

    update_values = {
        "payment_intent_id": payment_intent_id,
        "client_secret": safe_secret,
        "payment_status": payment_status,
        "updated_at": datetime.now(),
    }
    if psp_used:
        update_values["psp_used"] = psp_used
    
    query = orders.update().where(orders.c.order_id == order_id).values(**update_values)
    
    result = await database.execute(query)
    # Handle None result from PostgreSQL
    return result is not None and result > 0


async def update_payment_info_unless_cancelled(
    order_id: str,
    payment_intent_id: str,
    client_secret: str,
    payment_status: str = "processing",
    psp_used: Optional[str] = None,
) -> bool:
    """`update_payment_info`, as ONE conditional UPDATE that refuses a cancelled order (FIX_PLAN C-11,
    CHECKOUT_CANCEL_UPSTREAM_ENABLED; routes/agent_payment_sdk.py create_payment).

    True when THIS call wrote the payment surface. False when the order is cancelled (or gone): nothing is written,
    so a cancel that committed while the PSP object was being created is never overwritten with a live reference
    and "processing". RETURNING, not the rowcount (unreliable under the `databases` driver), reports the outcome;
    the cancel side (services/checkout_cancel_upstream.py) is the same kind of conditional UPDATE, so the two
    serialise on the row.
    """
    safe_secret = client_secret
    try:
        if client_secret and len(str(client_secret)) > 480:
            await _ensure_client_secret_storage_allows_long_values()
            safe_secret = str(client_secret)
    except Exception:
        safe_secret = client_secret

    update_values = {
        "payment_intent_id": payment_intent_id,
        "client_secret": safe_secret,
        "payment_status": payment_status,
        "updated_at": datetime.now(),
    }
    if psp_used:
        update_values["psp_used"] = psp_used

    query = (
        orders.update()
        .where(orders.c.order_id == order_id)
        .where(func.lower(func.coalesce(orders.c.status, "")) != "cancelled")
        .values(**update_values)
        .returning(orders.c.order_id)
    )
    return await database.fetch_one(query) is not None


async def mark_order_paid(order_id: str) -> bool:
    """标记订单已支付.

    ATOMIC, idempotent paid transition. Only flips an order to paid from a
    non-terminal state, and uses RETURNING (not the UPDATE rowcount, which is
    unreliable under the `databases`/asyncpg driver) to report whether THIS call
    performed the transition. Concurrent finalizers (webhook + sync confirm +
    reconcile sweep) therefore serialize: exactly one observes a True return and
    proceeds to one-time side effects (Shopify order, GMV stamp, merchant
    webhook). Returns False when the order was already paid/refunded/cancelled or
    does not exist.
    """
    now = datetime.now()
    row = await database.fetch_one(
        """
        UPDATE orders
        SET status = 'paid',
            payment_status = 'paid',
            paid_at = :now,
            updated_at = :now
        WHERE order_id = :order_id
          AND COALESCE(LOWER(payment_status), '') NOT IN (
              'paid', 'refunded', 'partially_refunded', 'cancelled'
          )
          AND COALESCE(LOWER(status), '') NOT IN (
              'paid', 'refunded', 'partially_refunded', 'cancelled', 'fulfilled'
          )
        RETURNING order_id
        """,
        {"order_id": order_id, "now": now},
    )
    ok = row is not None

    # Best-effort: converge `payments.status` for this order so customer-facing APIs
    # don't show stale "processing" after the PSP webhook confirms payment.
    if ok:
        try:
            row = await database.fetch_one(
                """
                SELECT payment_intent_id
                FROM orders
                WHERE order_id = :order_id
                LIMIT 1
                """,
                {"order_id": order_id},
            )
            payment_intent_id = None
            if row:
                try:
                    payment_intent_id = row["payment_intent_id"]
                except Exception:
                    payment_intent_id = None

            if payment_intent_id:
                await database.execute(
                    """
                    UPDATE payments
                    SET status = 'succeeded'
                    WHERE order_id = :order_id
                      AND payment_intent_id = :payment_intent_id
                      AND status <> 'succeeded'
                    """,
                    {"order_id": order_id, "payment_intent_id": payment_intent_id},
                )
            else:
                await database.execute(
                    """
                    UPDATE payments
                    SET status = 'succeeded'
                    WHERE order_id = :order_id
                      AND status IN ('processing', 'requires_action')
                    """,
                    {"order_id": order_id},
                )
        except Exception:
            pass

    return ok


async def update_fulfillment_info(
    order_id: str,
    shopify_order_id: Optional[str] = None,
    tracking_number: Optional[str] = None,
    carrier: Optional[str] = None,
    fulfillment_status: Optional[str] = None
) -> bool:
    """更新履约信息"""
    update_data: Dict[str, Any] = {"updated_at": datetime.now()}

    if shopify_order_id:
        update_data["shopify_order_id"] = shopify_order_id
    if tracking_number:
        update_data["tracking_number"] = tracking_number
    if carrier:
        update_data["carrier"] = carrier
    if fulfillment_status:
        update_data["fulfillment_status"] = fulfillment_status

    result = await database.execute(
        orders.update().where(orders.c.order_id == order_id).values(**update_data)
    )
    ok = result is not None and result > 0

    # Best-effort: enqueue invitation when fulfillment becomes shipped/delivered.
    if ok and fulfillment_status:
        try:
            row = await database.fetch_one(
                """
                SELECT merchant_id, payment_status, fulfillment_status
                FROM orders
                WHERE order_id = :order_id
                LIMIT 1
                """,
                {"order_id": order_id},
            )
            if row:
                paid = (
                    str((row["payment_status"] if row else "") or "").strip().lower()
                    == "paid"
                )
                ful = str((row["fulfillment_status"] if row else "") or "").strip().lower()
                merchant_id = str((row["merchant_id"] if row else "") or "").strip()
                if paid and merchant_id and ful in {"shipped", "delivered"}:
                    # Imported HERE, not at module scope: db/ -> services/ is a cycle. The name
                    # was simply never imported, so every paid+shipped order raised NameError into
                    # the enclosing except and no invitation was ever enqueued from this path.
                    from services.reviews_invitation_send_jobs_service import (
                        enqueue_invitation_send_job_from_order,
                    )

                    await enqueue_invitation_send_job_from_order(
                        merchant_id=merchant_id,
                        order_id=order_id,
                        force_reschedule=False,
                    )
        except Exception:
            pass

    return ok


async def mark_order_shipped(
    order_id: str,
    tracking_number: str,
    carrier: Optional[str] = None
) -> bool:
    """标记订单已发货"""
    # NOTE: `database.fetch_one()` on SQLAlchemy UPDATE + RETURNING can behave differently
    # across driver/adapter versions. Use `execute()` for the mutation and fetch merchant_id
    # with a follow-up SELECT for consistency.
    updated_at = datetime.now()
    result = await database.execute(
        orders.update()
        .where(orders.c.order_id == order_id)
        .values(
            status="completed",
            fulfillment_status="shipped",
            tracking_number=tracking_number,
            carrier=carrier,
            shipped_at=updated_at,
            updated_at=updated_at,
        )
    )
    ok = result is not None and result > 0
    if not ok:
        return False
    row = await database.fetch_one(
        "SELECT merchant_id, payment_status FROM orders WHERE order_id = :order_id LIMIT 1",
        {"order_id": order_id},
    )

    # Best-effort invitation scheduling: enqueue a job for a worker service to send the email.
    try:
        merchant_id = str((row["merchant_id"] if row else "") or "").strip()
        # PAID, like the two sibling sites. This one never checked, and it did not matter while
        # the enqueue was dead — the missing import above meant every call raised NameError into
        # the `except` below. Making the site live without the check would have let a merchant
        # shipping an UNPAID order queue an invitation job. Two gates downstream still refuse it
        # (`buyer_submit_enabled()` defaults off, and `_order_is_paid` 403s at send time), so the
        # worst case was a junk pending row — but a site that relies on a downstream refusal is
        # not guarded, it is lucky, and its two siblings guard it here.
        paid = str((row["payment_status"] if row else "") or "").strip().lower() == "paid"
        if paid and merchant_id:
            # Same local import as the two sites above — db/ -> services/ is a cycle at module
            # scope. This one was missing too, so the `except Exception: pass` below swallowed a
            # NameError on every call and no invitation was ever enqueued from this path either.
            from services.reviews_invitation_send_jobs_service import (
                enqueue_invitation_send_job_from_order,
            )

            await enqueue_invitation_send_job_from_order(
                merchant_id=merchant_id,
                order_id=order_id,
                force_reschedule=False,
            )
    except Exception:
        # Do not block fulfillment on invitation scheduling failures.
        pass

    return True


# ============================================================================
# 通用更新函数
# ============================================================================

async def update_order(order_id: str, update_data: Dict[str, Any]) -> bool:
    """更新订单数据"""
    if not update_data:
        return False

    update_values: Dict[str, Any] = {}
    for key, value in update_data.items():
        if key in {"order_id", "is_deleted", "created_at"}:
            continue
        if hasattr(orders.c, key):
            update_values[key] = value

    if not update_values:
        return False

    update_values["updated_at"] = datetime.now()
    query = (
        orders.update()
        .where((orders.c.order_id == order_id) & (orders.c.is_deleted.is_(False)))
        .values(**update_values)
        .returning(orders.c.order_id)
    )

    result = await database.fetch_one(query)
    return result is not None

# ============================================================================
# 统计查询
# ============================================================================

async def get_order_stats(merchant_id: str) -> Dict[str, Any]:
    """获取商户订单统计"""
    from sqlalchemy import select, func
    
    # 总订单数
    total_query = select([func.count()]).select_from(orders).where(
        (orders.c.merchant_id == merchant_id) & 
        orders.c.is_deleted.is_(False)
    )
    total_orders = await database.fetch_val(total_query)
    
    # 已支付订单数
    paid_query = select([func.count()]).select_from(orders).where(
        (orders.c.merchant_id == merchant_id) & 
        (orders.c.payment_status == "paid") &
        orders.c.is_deleted.is_(False)
    )
    paid_orders = await database.fetch_val(paid_query)
    
    # 总收入
    revenue_query = select([func.sum(orders.c.total)]).select_from(orders).where(
        (orders.c.merchant_id == merchant_id) & 
        (orders.c.payment_status == "paid") &
        orders.c.is_deleted.is_(False)
    )
    total_revenue = await database.fetch_val(revenue_query) or 0
    
    return {
        "total_orders": total_orders or 0,
        "paid_orders": paid_orders or 0,
        "pending_orders": (total_orders or 0) - (paid_orders or 0),
        "total_revenue": float(total_revenue),
        "currency": "USD"
    }
