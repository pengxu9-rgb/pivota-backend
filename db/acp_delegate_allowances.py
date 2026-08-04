"""ACP delegate allowances: the registry backing the delegated-token
(`vt_*`) allowance enforcement in services/acp_checkout_session_service.

Stores NO payment-method material of any kind — no PAN, no CVC, no cryptogram,
not even display last4/brand/IIN. Pivota is not a card vault and never receives
cardholder data (the P1 delegated-token design; the retired pivota-acp service's
`delegate_payment` stored raw PAN+CVC in a JSONB payload, which is exactly what
this schema refuses to be able to do). A schema-guard test asserts no column
here matches number|cvc|pan|cryptogram.

Note: Migration 192 creates this table in production; the registry service also
self-heals the same DDL at runtime (raw DDL first, then this metadata for
SQLite dev/tests — same pattern as db/acp_checkout_sessions.py). Columns are
Text on purpose: the raw DDL is TEXT, and a VARCHAR(n) here could create a
NARROWER type than the migration if the SQLAlchemy path ever won a self-heal
race.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, Table, Text
from sqlalchemy.sql import func

from db.database import metadata


acp_delegate_allowances = Table(
    "acp_delegate_allowances",
    metadata,
    # `vt_<14 hex>` — wire-parity with the retired service's token format.
    Column("token_id", Text, primary_key=True),
    # The acp_checkout_sessions.id this allowance was minted FOR. Presenting the
    # token at any other session is `allowance_session_mismatch`.
    Column("checkout_session_id", Text, nullable=False),
    # Must equal the completing session's merchant — the check the retired
    # service never performed (`allowance_merchant_mismatch`).
    Column("merchant_id", Text, nullable=False),
    # Minor units. Compared against the SERVER-SIDE session total only; equality
    # passes (wire parity: the refusal is `total > max_amount`).
    Column("max_amount", Integer, nullable=False),
    Column("currency", Text, nullable=False),
    # Only 'one_time' is minted or accepted today.
    Column("reason", Text, nullable=False, server_default="one_time"),
    # NOT NULL and actually checked at completion (`allowance_expired`) — the
    # retired service stored this and never read it.
    Column("expires_at", DateTime(timezone=True), nullable=False),
    # Single-use consumption, claimed by a conditional UPDATE ... RETURNING (the
    # same CAS technique as the session claim). Re-binding by the SAME session is
    # idempotent (a retry / stale-resume must not be refused by its own bind);
    # any other session is `allowance_already_used`.
    Column("used", Boolean, nullable=False, server_default="0"),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Column("used_by_session", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


Index(
    "idx_acp_delegate_allowances_session",
    acp_delegate_allowances.c.checkout_session_id,
)
