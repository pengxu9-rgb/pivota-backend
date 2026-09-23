"""Credits for GMV take already invoiced on a day a later refund reduced.

A refund re-rolls its edge's creation day in gmv_attribution_daily, unless an invoice already
billed that day for the merchant; then the rollup row is left exactly as billed, because the
invoice line (billing_run_items) points at it. The money the merchant was over-billed is owed back
here instead, one row per credit against one invoiced line:

    owed(line) = line.amount_cents - take(line's rollup group, recomputed from its edges now,
                                          at the rollup row's stored take_rate_bp)

A credit is computed automatically (status `pending`), approved by an admin, and only then issued
to Stripe as a credit note on the line's invoice: against the amount due when the invoice is
open, and to the customer's Stripe balance when it is paid. Pivota never refunds cash (the
2026-09-06 no-money rule). See services/gmv_invoice_credits.py.

Created by `metadata.create_all` at startup (main.py imports this module), and by
db/migrations/238_gmv_invoice_credits.sql. Keep the two in step.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.sql import func

from db.database import metadata

#: pending -> approved -> issuing -> issued; pending -> cancelled (by an admin, or superseded when a
#: recompute finds nothing more owed); approved/issuing -> failed (Stripe refused; retryable) ->
#: issuing; failed -> cancelled.
STATUSES = ("pending", "approved", "issuing", "issued", "failed", "cancelled")

gmv_invoice_credits = Table(
    "gmv_invoice_credits",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("billing_run_item_id", BigInteger, nullable=False),
    Column("billing_run_id", BigInteger, nullable=False),
    Column("rollup_id", BigInteger, nullable=False),
    Column("merchant_id", String(100), nullable=False),
    Column("channel_partner_id", BigInteger, nullable=True),
    Column("billed_day", Date, nullable=False),
    Column("stripe_invoice_id", Text, nullable=False),
    Column("currency", String(8), nullable=False, server_default=text("'USD'")),
    # What the credit is worth, and the facts it was computed from.
    Column("amount_cents", BigInteger, nullable=False),
    Column("billed_cents", BigInteger, nullable=False),
    Column("correct_take_cents", BigInteger, nullable=False),
    Column("committed_before_cents", BigInteger, nullable=False),
    Column("take_rate_bp", Integer, nullable=False),
    Column("gross_cents", BigInteger, nullable=False),
    Column("refund_cents", BigInteger, nullable=False),
    Column("status", String(16), nullable=False, server_default=text("'pending'")),
    Column("status_reason", Text, nullable=True),
    Column("approved_by", String(255), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("cancelled_by", String(255), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    Column("issue_attempts", Integer, nullable=False, server_default=text("0")),
    Column("stripe_credit_note_id", Text, nullable=True),
    # How the credit landed at Stripe: 'amount_due' (open invoice) or 'customer_balance' (paid).
    Column("stripe_credit_kind", String(32), nullable=True),
    # Who sent it to Stripe: the approver, or whoever retried a failed issue later.
    Column("issued_by", String(255), nullable=True),
    Column("issued_at", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    # The channel partner's share of this credit (partner settlement v1). NULL until decided:
    # not_applicable | netted | clawed_back | v2_unhandled.
    Column("partner_status", String(32), nullable=True),
    Column("partner_snapshot_id", BigInteger, nullable=True),
    Column("partner_clawback_cents", BigInteger, nullable=True),
    Column("partner_decided_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("amount_cents > 0", name="ck_gmv_invoice_credits_amount_positive"),
    CheckConstraint(
        "status IN ('pending', 'approved', 'issuing', 'issued', 'failed', 'cancelled')",
        name="ck_gmv_invoice_credits_status",
    ),
    CheckConstraint(
        "partner_status IS NULL OR partner_status IN "
        "('not_applicable', 'netted', 'clawed_back', 'v2_unhandled')",
        name="ck_gmv_invoice_credits_partner_status",
    ),
    CheckConstraint(
        "status <> 'issued' OR stripe_credit_note_id IS NOT NULL",
        name="ck_gmv_invoice_credits_issued_has_note",
    ),
)
Index("idx_gmv_invoice_credits_item", gmv_invoice_credits.c.billing_run_item_id)
Index("idx_gmv_invoice_credits_status", gmv_invoice_credits.c.status, gmv_invoice_credits.c.created_at)
Index("idx_gmv_invoice_credits_rollup", gmv_invoice_credits.c.rollup_id)
# At most ONE open (pending) credit per line: a recompute adjusts it instead of stacking another.
Index(
    "uq_gmv_invoice_credits_one_pending_per_item",
    gmv_invoice_credits.c.billing_run_item_id,
    unique=True,
    postgresql_where=text("status = 'pending'"),
    sqlite_where=text("status = 'pending'"),
)
